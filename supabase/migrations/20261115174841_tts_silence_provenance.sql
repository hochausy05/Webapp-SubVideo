alter table public.tts_artifacts
  add column raw_storage_path text unique,
  add column original_samples bigint check (original_samples is null or original_samples > 0),
  add column trimmed_samples bigint not null default 0 check (trimmed_samples >= 0),
  add column processing_metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(processing_metadata)='object' and pg_column_size(processing_metadata)<=8192);
