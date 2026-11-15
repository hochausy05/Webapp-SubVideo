-- Add immutable input provenance and bounded result provenance for real ASR runs.
alter table public.jobs
    add column input_metadata jsonb not null default '{}'::jsonb
        check (jsonb_typeof(input_metadata) = 'object' and pg_column_size(input_metadata) <= 65536),
    add column result_metadata jsonb not null default '{}'::jsonb
        check (jsonb_typeof(result_metadata) = 'object' and pg_column_size(result_metadata) <= 65536);

create unique index jobs_one_active_transcription_per_project_idx
    on public.jobs (project_id)
    where kind = 'transcription' and status in ('queued', 'running');

create or replace function public.enforce_job_status_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    project_status text;
begin
    select p.status into project_status
    from public.projects as p
    where p.id = new.project_id
    for update;
    if not found then
        raise exception using errcode = '23503', message = 'Job Project does not exist';
    end if;

    if project_status <> 'active' then
        if tg_op <> 'UPDATE'
           or new.status <> 'cancelled'
           or old.status not in ('queued', 'running')
           or new.cancel_requested_at is null then
            raise exception using errcode = '55000', message = 'Project is deleting; new Jobs and Job writes are blocked';
        end if;
    end if;

    if tg_op = 'UPDATE' then
        if old.project_id <> new.project_id or old.kind <> new.kind
           or old.idempotency_key <> new.idempotency_key
           or old.input_fingerprint is distinct from new.input_fingerprint
           or old.input_metadata is distinct from new.input_metadata
           or old.max_attempts <> new.max_attempts or old.created_at <> new.created_at then
            raise exception using errcode = '23514', message = 'Job identity and inputs are immutable';
        end if;
        if old.result_metadata is distinct from new.result_metadata
           and not (old.kind = 'transcription' and old.status = 'running'
                    and new.status = 'succeeded' and new.result_validated_at is not null) then
            raise exception using errcode = '23514', message = 'Job result metadata can only be written with a validated transcription';
        end if;
        if old.status in ('succeeded', 'failed', 'cancelled') and new.status <> old.status then
            raise exception using errcode = '23514', message = 'Terminal Job status cannot be changed';
        end if;
        if old.status = 'queued' and new.status not in ('queued', 'running', 'cancelled') then
            raise exception using errcode = '23514', message = 'Invalid queued Job transition';
        end if;
        if old.status = 'running' and new.status not in ('running', 'succeeded', 'failed', 'cancelled') then
            raise exception using errcode = '23514', message = 'Invalid running Job transition';
        end if;
        if new.attempt_count < old.attempt_count or new.attempt_count > old.attempt_count + 1 then
            raise exception using errcode = '23514', message = 'Job attempt count may only increase by one';
        end if;
        if new.attempt_count = old.attempt_count and new.progress < old.progress then
            raise exception using errcode = '23514', message = 'Job progress cannot move backwards within an attempt';
        end if;
        if new.attempt_count = old.attempt_count + 1 and new.status <> 'running' then
            raise exception using errcode = '23514', message = 'A new Job attempt must be claimed as running';
        end if;
    end if;
    return new;
end;
$function$;

-- Keep the established SKIP LOCKED claim transaction, add a required Job kind,
-- and serialize transcription claims so separate worker processes still run one ASR job at a time.
drop function public.claim_next_job(text, integer);

create function public.claim_next_job(p_worker_id text, p_lease_seconds integer, p_kind text)
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
    if p_kind not in ('transcription', 'translation', 'tts', 'joint_sync', 'render') then
        raise exception using errcode = '22023', message = 'Unsupported Job kind';
    end if;
    if p_kind = 'transcription' then
        perform pg_advisory_xact_lock(hashtextextended('autodub:single-transcription-worker', 0));
    end if;

    return query
    with candidate as materialized (
        select j.id, j.status, j.attempt_count, j.max_attempts, j.cancel_requested_at
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

create function public.finish_transcription_job(
    p_project_id uuid,
    p_job_id uuid,
    p_worker_id text,
    p_status text,
    p_error_code text default null,
    p_error_message text default null
)
returns public.jobs
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_job public.jobs;
    v_result public.jobs;
    v_error_code text;
    v_error_message text;
begin
    if p_status not in ('failed', 'cancelled') then
        raise exception using errcode = '22023', message = 'Transcription terminal status must be failed or cancelled';
    end if;

    select j.* into v_job from public.jobs as j
    where j.id = p_job_id and j.project_id = p_project_id and j.kind = 'transcription'
    for update;
    if not found or v_job.status <> 'running' or v_job.worker_id <> p_worker_id
       or v_job.lease_expires_at <= now() then
        raise exception using errcode = '55000', message = 'Worker no longer owns the active transcription lease';
    end if;

    v_error_code := case when p_status = 'failed' then coalesce(p_error_code, 'asr_failed') else null end;
    v_error_message := case when p_status = 'failed' then left(coalesce(p_error_message, 'Transcription failed.'), 2000) else null end;

    update public.job_attempts as a
    set status = p_status, ended_at = now(), failure_code = v_error_code, error_message = v_error_message
    where a.job_id = p_job_id and a.attempt_number = v_job.attempt_count
      and a.worker_id = p_worker_id and a.status = 'running';
    if not found then
        raise exception using errcode = '23514', message = 'Current transcription attempt is missing';
    end if;

    update public.jobs as j
    set status = p_status, progress_message = case when p_status = 'cancelled' then 'Đã hủy nhận diện' else j.progress_message end,
        worker_id = null, lease_expires_at = null, cancel_requested_at = case when p_status = 'cancelled' then coalesce(j.cancel_requested_at, now()) else j.cancel_requested_at end,
        finished_at = now(), error_code = v_error_code, error_message = v_error_message
    where j.id = p_job_id returning j.* into v_result;
    return v_result;
end;
$function$;

create function public.complete_transcription_job(
    p_project_id uuid,
    p_job_id uuid,
    p_worker_id text,
    p_source_asset_id uuid,
    p_segments jsonb,
    p_result_metadata jsonb
)
returns public.jobs
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_job public.jobs;
    v_project public.projects;
    v_result public.jobs;
    v_segment_count integer;
    v_segments_valid boolean;
    v_source_duration_us bigint;
    v_tolerance_us bigint;
begin
    if jsonb_typeof(p_segments) <> 'array' or jsonb_array_length(p_segments) < 1 then
        raise exception using errcode = '23514', message = 'A valid transcription must contain at least one source Segment';
    end if;
    if jsonb_array_length(p_segments) > 100000
       or jsonb_typeof(p_result_metadata) <> 'object' or pg_column_size(p_result_metadata) > 65536 then
        raise exception using errcode = '23514', message = 'Transcription result exceeds the supported size';
    end if;

    select j.* into v_job from public.jobs as j
    where j.id = p_job_id and j.project_id = p_project_id and j.kind = 'transcription'
    for update;
    if not found or v_job.status <> 'running' or v_job.worker_id <> p_worker_id
       or v_job.lease_expires_at <= now() or v_job.cancel_requested_at is not null then
        raise exception using errcode = '55000', message = 'Worker no longer owns the uncancelled transcription lease';
    end if;
    if v_job.input_metadata ->> 'source_asset_id' <> p_source_asset_id::text then
        raise exception using errcode = '23514', message = 'Transcription source Asset does not match its immutable Job input';
    end if;
    v_source_duration_us := (v_job.input_metadata ->> 'source_duration_us')::bigint;
    v_tolerance_us := coalesce((v_job.input_metadata ->> 'timeline_tolerance_us')::bigint, 250000);

    select p.* into v_project from public.projects as p where p.id = p_project_id for update;
    if not found or v_project.status <> 'active' or v_project.source_video_asset_id <> p_source_asset_id then
        raise exception using errcode = '55000', message = 'Project or source Asset changed before transcription could be activated';
    end if;

    select count(*), coalesce(bool_and(
        position = expected_position
        and start_us is not null and end_us is not null
        and start_us >= 0 and end_us > start_us
        and end_us <= v_source_duration_us + v_tolerance_us
        and source_text is not null and length(btrim(source_text)) between 1 and 20000
        and (speaker_label is null or length(speaker_label) between 1 and 200)
        and (previous_start_us is null or start_us >= previous_start_us)
    ), false)
    into v_segment_count, v_segments_valid
    from (
        select x.*,
            row_number() over (order by x.position) - 1 as expected_position,
            lag(x.start_us) over (order by x.position) as previous_start_us
        from jsonb_to_recordset(p_segments) as x(
            position integer, start_us bigint, end_us bigint, source_text text, speaker_label text
        )
    ) as checked;

    if v_segment_count <> jsonb_array_length(p_segments) or not v_segments_valid then
        raise exception using errcode = '23514', message = 'Transcription Segment order, text, or timestamps are invalid';
    end if;

    insert into public.segments(project_id, transcription_job_id, position, start_us, end_us, source_text, speaker_label)
    select p_project_id, p_job_id, x.position, x.start_us, x.end_us, x.source_text, x.speaker_label
    from jsonb_to_recordset(p_segments) as x(
        position integer, start_us bigint, end_us bigint, source_text text, speaker_label text
    )
    order by x.position;

    update public.jobs as j
    set status = 'succeeded', progress = 100, progress_message = 'Nhận diện hoàn tất',
        worker_id = null, lease_expires_at = null, finished_at = now(), error_code = null,
        error_message = null, result_validated_at = now(), result_metadata = p_result_metadata
    where j.id = p_job_id returning j.* into v_result;

    update public.job_attempts as a
    set status = 'succeeded', ended_at = now(), failure_code = null, error_message = null
    where a.job_id = p_job_id and a.attempt_number = v_job.attempt_count
      and a.worker_id = p_worker_id and a.status = 'running';
    if not found then
        raise exception using errcode = '23514', message = 'Current transcription attempt is missing';
    end if;

    update public.projects as p set active_transcription_job_id = p_job_id
    where p.id = p_project_id;
    return v_result;
end;
$function$;

create function public.cancel_queued_transcription_job(p_project_id uuid, p_job_id uuid)
returns public.jobs
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_job public.jobs;
begin
    update public.jobs as j
    set status = 'cancelled', cancel_requested_at = now(), finished_at = now(),
        progress_message = 'Đã hủy nhận diện', error_code = null, error_message = null
    where j.id = p_job_id and j.project_id = p_project_id and j.kind = 'transcription'
      and j.status = 'queued'
    returning j.* into v_job;
    return v_job;
end;
$function$;

revoke all on function public.claim_next_job(text, integer, text) from public, anon, authenticated;
revoke all on function public.finish_transcription_job(uuid, uuid, text, text, text, text) from public, anon, authenticated;
revoke all on function public.complete_transcription_job(uuid, uuid, text, uuid, jsonb, jsonb) from public, anon, authenticated;
revoke all on function public.cancel_queued_transcription_job(uuid, uuid) from public, anon, authenticated;
grant execute on function public.claim_next_job(text, integer, text) to service_role;
grant execute on function public.finish_transcription_job(uuid, uuid, text, text, text, text) to service_role;
grant execute on function public.complete_transcription_job(uuid, uuid, text, uuid, jsonb, jsonb) to service_role;
grant execute on function public.cancel_queued_transcription_job(uuid, uuid) to service_role;
