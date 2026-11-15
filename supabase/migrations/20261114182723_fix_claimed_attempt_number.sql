-- A newly claimed Job's first run is attempt_count + 1 (attempt counts start at 0).
create or replace function public.claim_next_job(p_worker_id text, p_lease_seconds integer default 120)
returns setof public.jobs
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if p_worker_id is null or length(btrim(p_worker_id)) not between 1 and 200 then
        raise exception using errcode = '22023', message = 'Worker identifier must contain 1 to 200 characters';
    end if;
    if p_lease_seconds not between 10 and 3600 then
        raise exception using errcode = '22023', message = 'Lease duration must be between 10 and 3600 seconds';
    end if;

    return query
    with candidate as materialized (
        select j.id, j.status, j.attempt_count, j.max_attempts, j.cancel_requested_at
        from public.jobs as j
        join public.projects as p on p.id = j.project_id and p.status = 'active'
        where (
            (j.status = 'queued' and j.available_at <= now() and j.cancel_requested_at is null and j.attempt_count < j.max_attempts)
            or (j.status = 'running' and j.lease_expires_at <= now())
        )
        order by case when j.status = 'queued' then j.available_at else j.lease_expires_at end, j.created_at, j.id
        for update of j skip locked
        limit 1
    ),
    closed_attempt as (
        update public.job_attempts as a
        set status = case when c.cancel_requested_at is not null then 'cancelled' else 'failed' end,
            ended_at = now(),
            failure_code = case when c.cancel_requested_at is not null then null else 'lease_expired' end,
            error_message = case when c.cancel_requested_at is not null then null else 'Worker lease expired before the attempt finished' end
        from candidate as c
        where c.status = 'running' and a.job_id = c.id
          and a.attempt_number = c.attempt_count and a.status = 'running'
        returning a.id
    ),
    terminal_job as (
        update public.jobs as j
        set status = case when c.cancel_requested_at is not null then 'cancelled' else 'failed' end,
            worker_id = null, lease_expires_at = null, finished_at = now(),
            error_code = case when c.cancel_requested_at is not null then null else 'attempt_limit_reached' end,
            error_message = case when c.cancel_requested_at is not null then null else 'The final worker lease expired; no attempts remain' end
        from candidate as c
        where j.id = c.id and c.status = 'running'
          and (c.cancel_requested_at is not null or c.attempt_count >= c.max_attempts)
        returning j.id
    ),
    claimed as (
        update public.jobs as j
        set status = 'running',
            progress = case when j.attempt_count > 0 then 0 else j.progress end,
            worker_id = p_worker_id,
            lease_expires_at = now() + make_interval(secs => p_lease_seconds),
            attempt_count = j.attempt_count + 1,
            started_at = coalesce(j.started_at, now()),
            finished_at = null, error_code = null, error_message = null
        from candidate as c
        where j.id = c.id and c.cancel_requested_at is null and c.attempt_count < c.max_attempts
        returning j.*
    ),
    new_attempt as (
        insert into public.job_attempts (job_id, attempt_number, status, worker_id, started_at)
        select c.id, c.attempt_count + 1, 'running', p_worker_id, now()
        from claimed as c
        returning job_id
    )
    select c.* from claimed as c join new_attempt as a on a.job_id = c.id;
end;
$function$;

revoke all on function public.claim_next_job(text, integer) from public, anon, authenticated;
grant execute on function public.claim_next_job(text, integer) to service_role;
