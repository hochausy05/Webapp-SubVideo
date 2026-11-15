-- Cover the referencing column order of the two same-Project composite FKs.
drop index if exists public.projects_source_video_reference_idx;
drop index if exists public.projects_transcription_reference_idx;
create index projects_source_video_reference_fk_idx
    on public.projects (id, source_video_asset_id) where source_video_asset_id is not null;
create index projects_transcription_reference_fk_idx
    on public.projects (id, active_transcription_job_id) where active_transcription_job_id is not null;
