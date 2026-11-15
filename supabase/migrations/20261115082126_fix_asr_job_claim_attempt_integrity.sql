-- Fix attempt numbering and make the claim side effects visible to integrity
-- triggers through sequential statements in one atomic function transaction.
create or replace function public.claim_next_job(p_worker_id text, p_lease_seconds integer, p_kind text)
returns setof public.jobs
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_job public.jobs;
    v_claimed public.jobs;
    v_closed_attempt_id uuid;
begin
    if p_worker_id is null or length(btrim(p_worker_id)) not between 1 and 200 then
        raise exception using errcode = '22023', message = 'Worker identifier must contain 1 to 200 characters';
    end if;
    if p_lease_seconds not between 10 and 3600 then
        raise exception using errcode = '22023', message = 'Lease duration must be between 10 and 3600 seconds';
    end if;
    if p_kind not in ('transcription', 'translation', 'tts', 'joint_sync', 'render') then
        raise exception using errcode = '22023', message = 'Unsupported Job kind';
    end if;

    if p_kind = 'transcription' then
        perform pg_advisory_xact_lock(hashtextextended('autodub:single-transcription-worker', 0));
    end if;

    select j.* into v_job
    from public.jobs as j
    join public.projects as p on p.id = j.project_id and p.status = 'active'
    where j.kind = p_kind
      and (
        (j.status = 'queued' and j.available_at <= now() and j.cancel_requested_at is null and j.attempt_count < j.max_attempts)
        or (j.status = 'running' and j.lease_expires_at <= now())
      )
      and (
        p_kind <> 'transcription'
        or not exists (
            select 1 from public.jobs as active
            where active.kind = 'transcription' and active.status = 'running'
              and active.lease_expires_at > now()
        )
      )
    order by case when j.status = 'queued' then j.available_at else j.lease_expires_at end, j.created_at, j.id
    for update of j skip locked
    limit 1;

    if not found then
        return;
    end if;

    if v_job.status = 'running' then
        update public.job_attempts as a
        set status = case when v_job.cancel_requested_at is not null then 'cancelled' else 'failed' end,
            ended_at = now(),
            failure_code = case when v_job.cancel_requested_at is not null then null else 'lease_expired' end,
            error_message = case when v_job.cancel_requested_at is not null then null else 'Worker lease expired before the attempt finished' end
        where a.job_id = v_job.id
          and a.attempt_number = v_job.attempt_count
          and a.worker_id = v_job.worker_id
          and a.status = 'running'
        returning a.id into v_closed_attempt_id;

        if not found then
            raise exception using errcode = '23514', message = 'Expired Job has no matching running Attempt';
        end if;

        if v_job.cancel_requested_at is not null then
            update public.jobs as j
            set status = 'cancelled', worker_id = null, lease_expires_at = null,
                finished_at = now(), error_code = null, error_message = null,
                progress_message = 'Đã hủy nhận diện'
            where j.id = v_job.id;
            return;
        end if;

        if v_job.attempt_count >= v_job.max_attempts then
            update public.jobs as j
            set status = 'failed', worker_id = null, lease_expires_at = null,
                finished_at = now(), error_code = 'attempt_limit_reached',
                error_message = 'The final worker lease expired; no attempts remain'
            where j.id = v_job.id;
            return;
        end if;
    end if;

    -- Keep the selected row locked and increment the counter exactly once.
    update public.jobs as j
    set status = 'running',
        progress = case when v_job.attempt_count > 0 then 0 else j.progress end,
        worker_id = p_worker_id,
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        attempt_count = v_job.attempt_count + 1,
        started_at = coalesce(v_job.started_at, now()),
        finished_at = null,
        error_code = null,
        error_message = null
    where j.id = v_job.id
    returning j.* into v_claimed;

    if not found then
        raise exception using errcode = '40001', message = 'Job changed while being claimed';
    end if;

    -- A separate command after UPDATE lets enforce_attempt_integrity observe
    -- the updated Job and requires Attempt # to equal the stored attempt_count.
    insert into public.job_attempts(job_id, attempt_number, status, worker_id, started_at)
    values (v_claimed.id, v_claimed.attempt_count, 'running', p_worker_id, now());

    return next v_claimed;
    return;
end;
$function$;

revoke all on function public.claim_next_job(text, integer, text) from public, anon, authenticated;
grant execute on function public.claim_next_job(text, integer, text) to service_role;
