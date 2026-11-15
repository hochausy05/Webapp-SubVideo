-- Cover the referenced composite keys used by Projects and serialize media
-- creation/finalization with Project deletion.
create index projects_source_video_reference_idx
    on public.projects (source_video_asset_id, id) where source_video_asset_id is not null;
create index projects_transcription_reference_idx
    on public.projects (active_transcription_job_id, id) where active_transcription_job_id is not null;

create or replace function public.enforce_asset_status_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    project_status text;
begin
    select p.status into project_status
    from public.projects as p where p.id = new.project_id for update;
    if not found then
        raise exception using errcode = '23503', message = 'Media Project does not exist';
    end if;
    if project_status <> 'active' and new.status not in ('failed', 'deletion_pending', 'deletion_failed') then
        raise exception using errcode = '55000', message = 'Project is deleting; media writes are blocked';
    end if;

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
