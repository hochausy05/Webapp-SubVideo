-- Phase 02 foundation for Projects, private source media, durable Jobs, attempts, and source Segments.
-- The five application tables are public-schema objects so the trusted FastAPI service can use the
-- Data API. Only service_role receives table/function grants; anon/authenticated receive none.

create table public.projects (
    id uuid primary key default gen_random_uuid(),
    name text not null check (length(btrim(name)) between 1 and 200),
    description text check (description is null or length(description) <= 20000),
    status text not null default 'active' check (status in ('active', 'deleting')),
    source_language text not null default 'und'
        check (source_language ~ '^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$'),
    target_language text not null default 'vi'
        check (target_language ~ '^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$'),
    source_video_asset_id uuid,
    active_transcription_job_id uuid,
    deletion_requested_at timestamptz,
    deletion_state text not null default 'none'
        check (deletion_state in ('none', 'pending', 'failed')),
    deletion_error_code text,
    deletion_error_message text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (
        (status = 'active' and deletion_requested_at is null and deletion_state = 'none')
        or
        (status = 'deleting' and deletion_requested_at is not null and deletion_state in ('pending', 'failed'))
    ),
    check (
        (deletion_state = 'failed' and deletion_error_code is not null)
        or
        (deletion_state <> 'failed' and deletion_error_code is null and deletion_error_message is null)
    )
);

create table public.media_assets (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references public.projects(id) on delete cascade deferrable initially deferred,
    kind text not null default 'source_video' check (kind = 'source_video'),
    status text not null default 'pending'
        check (status in ('pending', 'ready', 'failed', 'deletion_pending', 'deletion_failed')),
    storage_bucket text not null default 'autodub-private-media'
        check (storage_bucket = 'autodub-private-media'),
    storage_path text not null unique check (
        storage_path ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/source-video/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(mp4|mov|mkv|webm)$'
    ),
    original_filename text not null check (
        length(original_filename) between 1 and 255
        and position('/' in original_filename) = 0
        and position(chr(92) in original_filename) = 0
    ),
    mime_type text not null check (mime_type in ('video/mp4', 'video/quicktime', 'video/x-matroska', 'video/webm')),
    size_bytes bigint not null check (size_bytes between 1 and 536870912),
    duration_us bigint check (duration_us is null or duration_us between 1 and 7200000000),
    sha256 char(64) check (sha256 is null or sha256 ~ '^[0-9a-f]{64}$'),
    expected_sha256 char(64) check (expected_sha256 is null or expected_sha256 ~ '^[0-9a-f]{64}$'),
    technical_metadata jsonb not null default '{}'::jsonb
        check (jsonb_typeof(technical_metadata) = 'object' and pg_column_size(technical_metadata) <= 65536),
    provenance text not null default 'user_upload' check (provenance = 'user_upload'),
    upload_idempotency_key text check (upload_idempotency_key is null or length(upload_idempotency_key) between 1 and 200),
    upload_offset_bytes bigint not null default 0 check (upload_offset_bytes >= 0 and upload_offset_bytes <= size_bytes),
    upload_session_url text,
    upload_expires_at timestamptz,
    write_lease_expires_at timestamptz,
    failure_code text,
    failure_message text,
    measured_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (project_id, id),
    unique (project_id, upload_idempotency_key),
    check (
        status <> 'ready'
        or (duration_us is not null and sha256 is not null and measured_at is not null and technical_metadata <> '{}'::jsonb)
    ),
    check (
        (status in ('failed', 'deletion_failed') and failure_code is not null)
        or
        (status not in ('failed', 'deletion_failed') and failure_code is null and failure_message is null)
    )
);

create table public.jobs (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references public.projects(id) on delete cascade deferrable initially deferred,
    kind text not null check (kind in ('transcription', 'translation', 'tts', 'joint_sync', 'render')),
    status text not null default 'queued' check (status in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    progress integer not null default 0 check (progress between 0 and 100),
    progress_message text check (progress_message is null or length(progress_message) <= 500),
    input_fingerprint char(64) check (input_fingerprint is null or input_fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_key text not null check (length(idempotency_key) between 1 and 200),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    max_attempts integer not null default 3 check (max_attempts between 1 and 10),
    available_at timestamptz not null default now(),
    worker_id text check (worker_id is null or length(worker_id) between 1 and 200),
    lease_expires_at timestamptz,
    cancel_requested_at timestamptz,
    error_code text,
    error_message text check (error_message is null or length(error_message) <= 2000),
    result_validated_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz,
    unique (project_id, id),
    check (
        (status = 'running' and worker_id is not null and lease_expires_at is not null and started_at is not null and finished_at is null)
        or
        (status <> 'running' and worker_id is null and lease_expires_at is null)
    ),
    check ((status in ('succeeded', 'failed', 'cancelled')) = (finished_at is not null)),
    check ((status = 'failed') = (error_code is not null)),
    check (error_code is not null or error_message is null),
    check (result_validated_at is null or (kind = 'transcription' and status = 'succeeded')),
    check (kind <> 'transcription' or status <> 'succeeded' or result_validated_at is not null)
);

create table public.job_attempts (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade deferrable initially deferred,
    attempt_number integer not null check (attempt_number >= 1),
    status text not null check (status in ('running', 'succeeded', 'failed', 'cancelled')),
    worker_id text not null check (length(worker_id) between 1 and 200),
    started_at timestamptz not null default now(),
    ended_at timestamptz,
    failure_code text,
    error_message text check (error_message is null or length(error_message) <= 2000),
    created_at timestamptz not null default now(),
    unique (job_id, attempt_number),
    check ((status = 'running') = (ended_at is null)),
    check (status not in ('succeeded', 'failed', 'cancelled') or ended_at is not null),
    check ((status = 'failed') = (failure_code is not null)),
    check (failure_code is not null or error_message is null)
);

create table public.segments (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null,
    transcription_job_id uuid not null,
    position integer not null check (position >= 0),
    start_us bigint not null check (start_us >= 0),
    end_us bigint not null check (end_us > start_us),
    source_text text not null check (length(btrim(source_text)) between 1 and 20000),
    speaker_label text check (speaker_label is null or length(speaker_label) between 1 and 200),
    created_at timestamptz not null default now(),
    unique (project_id, transcription_job_id, position),
    foreign key (project_id, transcription_job_id)
        references public.jobs(project_id, id) on delete cascade deferrable initially deferred
);

alter table public.projects
    add constraint projects_source_video_same_project_fk
        foreign key (id, source_video_asset_id)
        references public.media_assets(project_id, id)
        on delete no action deferrable initially deferred,
    add constraint projects_active_transcription_same_project_fk
        foreign key (id, active_transcription_job_id)
        references public.jobs(project_id, id)
        on delete no action deferrable initially deferred;

create index projects_listing_idx on public.projects (status, updated_at desc, id desc);
create index media_assets_project_created_idx on public.media_assets (project_id, created_at desc, id desc);
create index jobs_project_created_idx on public.jobs (project_id, created_at desc, id desc);
create index jobs_queue_schedule_idx on public.jobs (available_at, created_at, id)
    where status = 'queued' and cancel_requested_at is null;
create index jobs_running_lease_idx on public.jobs (lease_expires_at, id) where status = 'running';
create unique index jobs_one_active_idempotency_key_idx
    on public.jobs (project_id, kind, idempotency_key)
    where status in ('queued', 'running');
create index job_attempts_job_number_idx on public.job_attempts (job_id, attempt_number desc);
create index segments_project_transcription_position_idx
    on public.segments (project_id, transcription_job_id, position);

create function public.set_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    new.updated_at := now();
    return new;
end;
$function$;

create function public.enforce_project_lifecycle_and_references()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    asset_status text;
    asset_kind text;
    job_kind text;
    job_status text;
    job_validated_at timestamptz;
begin
    if tg_op = 'UPDATE' then
        if old.status = 'deleting' and new.status <> 'deleting' then
            raise exception using errcode = '23514', message = 'Project deletion cannot be cancelled';
        end if;
        if old.status = 'active' and new.status = 'deleting' then
            new.deletion_requested_at := coalesce(new.deletion_requested_at, now());
            new.deletion_state := 'pending';
            new.deletion_error_code := null;
            new.deletion_error_message := null;
        elsif new.status = 'deleting' and new.deletion_state = 'pending' then
            new.deletion_error_code := null;
            new.deletion_error_message := null;
        end if;
    end if;

    if new.source_video_asset_id is not null
       and (tg_op = 'INSERT' or new.source_video_asset_id is distinct from old.source_video_asset_id) then
        select a.status, a.kind into asset_status, asset_kind
        from public.media_assets as a
        where a.id = new.source_video_asset_id and a.project_id = new.id;
        if not found or asset_status <> 'ready' or asset_kind <> 'source_video' then
            raise exception using errcode = '23514', message = 'Selected source video must be a ready asset in the same Project';
        end if;
    end if;

    if new.active_transcription_job_id is not null
       and (tg_op = 'INSERT' or new.active_transcription_job_id is distinct from old.active_transcription_job_id) then
        select j.kind, j.status, j.result_validated_at into job_kind, job_status, job_validated_at
        from public.jobs as j
        where j.id = new.active_transcription_job_id and j.project_id = new.id;
        if not found or job_kind <> 'transcription' or job_status <> 'succeeded' or job_validated_at is null then
            raise exception using errcode = '23514', message = 'Active transcription must reference a validated successful run in the same Project';
        end if;
    end if;

    return new;
end;
$function$;

create function public.enforce_asset_status_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if tg_op = 'UPDATE' then
        if old.id <> new.id or old.project_id <> new.project_id or old.kind <> new.kind
           or old.storage_bucket <> new.storage_bucket or old.storage_path <> new.storage_path
           or old.original_filename <> new.original_filename or old.mime_type <> new.mime_type
           or old.size_bytes <> new.size_bytes or old.expected_sha256 is distinct from new.expected_sha256
           or old.upload_idempotency_key is distinct from new.upload_idempotency_key then
            raise exception using errcode = '23514', message = 'Media asset identity and original upload metadata are immutable';
        end if;
        if old.status <> new.status and not (
            (old.status = 'pending' and new.status in ('ready', 'failed', 'deletion_pending'))
            or (old.status = 'ready' and new.status in ('deletion_pending', 'deletion_failed'))
            or (old.status = 'failed' and new.status in ('deletion_pending', 'deletion_failed'))
            or (old.status = 'deletion_pending' and new.status = 'deletion_failed')
            or (old.status = 'deletion_failed' and new.status = 'deletion_pending')
        ) then
            raise exception using errcode = '23514', message = 'Invalid media asset status transition';
        end if;
    end if;
    return new;
end;
$function$;

create function public.enforce_job_status_transition()
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
           or old.max_attempts <> new.max_attempts or old.created_at <> new.created_at then
            raise exception using errcode = '23514', message = 'Job identity and inputs are immutable';
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

create function public.enforce_attempt_integrity()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    parent_attempt_count integer;
    parent_worker_id text;
    parent_status text;
begin
    if tg_op = 'INSERT' then
        select j.attempt_count, j.worker_id, j.status
        into parent_attempt_count, parent_worker_id, parent_status
        from public.jobs as j where j.id = new.job_id;
        if not found or parent_status <> 'running'
           or new.attempt_number <> parent_attempt_count or new.worker_id <> parent_worker_id
           or new.status <> 'running' then
            raise exception using errcode = '23514', message = 'Attempt must match the currently claimed Job run';
        end if;
    else
        if old.job_id <> new.job_id or old.attempt_number <> new.attempt_number
           or old.worker_id <> new.worker_id or old.started_at <> new.started_at then
            raise exception using errcode = '23514', message = 'Job attempt identity is immutable';
        end if;
        if old.status <> 'running' and new.status <> old.status then
            raise exception using errcode = '23514', message = 'Terminal Job attempt status cannot be changed';
        end if;
        if old.status = 'running' and new.status not in ('running', 'succeeded', 'failed', 'cancelled') then
            raise exception using errcode = '23514', message = 'Invalid Job attempt transition';
        end if;
    end if;
    return new;
end;
$function$;

create function public.enforce_segment_integrity()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    project_status text;
    job_kind text;
    job_status text;
begin
    if tg_op = 'UPDATE' then
        raise exception using errcode = '23514', message = 'Source Segments are immutable';
    end if;
    if tg_op = 'DELETE' then
        if pg_trigger_depth() > 1 then
            return old;
        end if;
        raise exception using errcode = '23514', message = 'Source Segments can only be removed with their Project';
    end if;

    select p.status into project_status from public.projects as p where p.id = new.project_id for update;
    if not found or project_status <> 'active' then
        raise exception using errcode = '55000', message = 'Project is deleting; Segment writes are blocked';
    end if;
    select j.kind, j.status into job_kind, job_status
    from public.jobs as j where j.id = new.transcription_job_id and j.project_id = new.project_id;
    if not found or job_kind <> 'transcription' or job_status <> 'running' then
        raise exception using errcode = '23514', message = 'Segment must belong to a running transcription Job in the same Project';
    end if;
    return new;
end;
$function$;

create function public.activate_source_video(p_project_id uuid, p_asset_id uuid)
returns public.projects
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    result public.projects;
begin
    update public.projects as p
    set source_video_asset_id = p_asset_id
    where p.id = p_project_id and p.status = 'active'
      and exists (
          select 1 from public.media_assets as a
          where a.id = p_asset_id and a.project_id = p.id
            and a.kind = 'source_video' and a.status = 'ready'
      )
    returning p.* into result;
    if not found then
        raise exception using errcode = '23514', message = 'Ready source video not found for active Project';
    end if;
    return result;
end;
$function$;

create function public.activate_transcription_job(p_project_id uuid, p_job_id uuid)
returns public.projects
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    result public.projects;
begin
    update public.projects as p
    set active_transcription_job_id = p_job_id
    where p.id = p_project_id and p.status = 'active'
      and exists (
          select 1 from public.jobs as j
          where j.id = p_job_id and j.project_id = p.id
            and j.kind = 'transcription' and j.status = 'succeeded'
            and j.result_validated_at is not null
      )
    returning p.* into result;
    if not found then
        raise exception using errcode = '23514', message = 'Validated successful transcription not found for active Project';
    end if;
    return result;
end;
$function$;

create function public.claim_next_job(p_worker_id text, p_lease_seconds integer default 120)
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
            or
            (j.status = 'running' and j.lease_expires_at <= now())
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
            worker_id = null,
            lease_expires_at = null,
            finished_at = now(),
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
            finished_at = null,
            error_code = null,
            error_message = null
        from candidate as c
        where j.id = c.id and c.cancel_requested_at is null and c.attempt_count < c.max_attempts
        returning j.*
    ),
    new_attempt as (
        insert into public.job_attempts (job_id, attempt_number, status, worker_id, started_at)
        select c.id, c.attempt_count, 'running', p_worker_id, now()
        from claimed as c
        returning job_id
    )
    select c.* from claimed as c join new_attempt as a on a.job_id = c.id;
end;
$function$;

create trigger projects_lifecycle_and_references_before_write
before insert or update on public.projects
for each row execute function public.enforce_project_lifecycle_and_references();
create trigger projects_updated_at_before_update
before update on public.projects
for each row execute function public.set_updated_at();

create trigger media_assets_status_before_write
before insert or update on public.media_assets
for each row execute function public.enforce_asset_status_transition();
create trigger media_assets_updated_at_before_update
before update on public.media_assets
for each row execute function public.set_updated_at();

create trigger jobs_status_before_write
before insert or update on public.jobs
for each row execute function public.enforce_job_status_transition();
create trigger jobs_updated_at_before_update
before update on public.jobs
for each row execute function public.set_updated_at();
create trigger job_attempts_integrity_before_write
before insert or update on public.job_attempts
for each row execute function public.enforce_attempt_integrity();
create trigger segments_integrity_before_write
before insert or update or delete on public.segments
for each row execute function public.enforce_segment_integrity();

alter table public.projects enable row level security;
alter table public.media_assets enable row level security;
alter table public.jobs enable row level security;
alter table public.job_attempts enable row level security;
alter table public.segments enable row level security;

revoke all on table public.projects, public.media_assets, public.jobs, public.job_attempts, public.segments
    from public, anon, authenticated;
grant select, insert, update, delete on table public.projects, public.media_assets, public.jobs, public.job_attempts, public.segments
    to service_role;

revoke all on function public.set_updated_at() from public, anon, authenticated;
revoke all on function public.enforce_project_lifecycle_and_references() from public, anon, authenticated;
revoke all on function public.enforce_asset_status_transition() from public, anon, authenticated;
revoke all on function public.enforce_job_status_transition() from public, anon, authenticated;
revoke all on function public.enforce_attempt_integrity() from public, anon, authenticated;
revoke all on function public.enforce_segment_integrity() from public, anon, authenticated;
revoke all on function public.activate_source_video(uuid, uuid) from public, anon, authenticated;
revoke all on function public.activate_transcription_job(uuid, uuid) from public, anon, authenticated;
revoke all on function public.claim_next_job(text, integer) from public, anon, authenticated;
revoke all on function public.rls_auto_enable() from public, anon, authenticated;
grant execute on function public.activate_source_video(uuid, uuid) to service_role;
grant execute on function public.activate_transcription_job(uuid, uuid) to service_role;
grant execute on function public.claim_next_job(text, integer) to service_role;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'autodub-private-media',
    'autodub-private-media',
    false,
    536870912,
    array['video/mp4', 'video/quicktime', 'video/x-matroska', 'video/webm']::text[]
);
