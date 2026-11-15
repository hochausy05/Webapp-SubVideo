insert into storage.buckets (id,name,public,file_size_limit,allowed_mime_types)
values ('autodub-private-tts','autodub-private-tts',false,52428800,array['audio/wav'])
on conflict (id) do nothing;

create table public.tts_runs (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,
  translation_run_id uuid not null,
  job_id uuid not null unique,
  voice_id text not null check (voice_id in ('M1','M2','M3','M4','M5','F1','F2','F3','F4','F5')),
  model text not null, model_revision text not null,
  total_steps integer not null default 8 check (total_steps > 0),
  speed numeric not null default 1.05 check (speed > 0),
  status text not null default 'building' check (status in ('building','ready','failed','cancelled')),
  created_at timestamptz not null default now(), finished_at timestamptz,
  unique (project_id,id), unique (id,translation_run_id),
  foreign key (project_id,translation_run_id) references public.translation_runs(project_id,id) on delete cascade deferrable initially deferred,
  foreign key (project_id,job_id) references public.jobs(project_id,id) on delete cascade deferrable initially deferred
);

create table public.tts_artifacts (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null,
  translation_run_id uuid not null,
  unit_id uuid not null,
  unit_revision integer not null check (unit_revision >= 0),
  text_snapshot text not null check (length(btrim(text_snapshot)) between 1 and 20000),
  storage_path text not null unique,
  status text not null default 'pending' check (status in ('pending','ready')),
  sample_rate integer check (sample_rate > 0),
  samples bigint check (samples > 0),
  duration_us bigint check (duration_us > 0),
  sha256 text check (sha256 is null or sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint check (size_bytes > 44),
  created_at timestamptz not null default now(),
  unique (run_id,unit_id),
  foreign key (run_id,translation_run_id) references public.tts_runs(id,translation_run_id) on delete cascade,
  foreign key (translation_run_id,unit_id) references public.translation_units(run_id,id) on delete cascade,
  check (status <> 'ready' or (sample_rate is not null and samples is not null and duration_us is not null and sha256 is not null and size_bytes is not null))
);

alter table public.projects add column active_tts_run_id uuid;
alter table public.projects add constraint active_tts_run_same_project_fk
  foreign key (id,active_tts_run_id) references public.tts_runs(project_id,id) on delete no action deferrable initially deferred;
create unique index jobs_one_active_tts_per_project_idx on public.jobs(project_id) where kind='tts' and status in ('queued','running');
create index tts_runs_project_idx on public.tts_runs(project_id,created_at desc);
create index tts_artifacts_run_idx on public.tts_artifacts(run_id);
alter table public.tts_runs enable row level security;
alter table public.tts_artifacts enable row level security;
revoke all on public.tts_runs, public.tts_artifacts from public, anon, authenticated;
grant select,insert,update,delete on public.tts_runs, public.tts_artifacts to service_role;

create function public.claim_tts_job(p_worker_id text,p_lease_seconds integer)
returns setof public.jobs language plpgsql security invoker set search_path='' as $$
begin
  perform pg_advisory_xact_lock(hashtextextended('autodub:tts-worker',0));
  if exists(select 1 from public.jobs where kind='tts' and status='running' and lease_expires_at>now()) then return; end if;
  return query select * from public.claim_next_job(p_worker_id,p_lease_seconds,'tts');
end $$;

create function public.finish_tts_job(p_project_id uuid,p_job_id uuid,p_worker_id text,p_status text,p_code text,p_message text)
returns void language plpgsql security invoker set search_path='' as $$
declare j public.jobs; v_status text;
begin
  if p_status not in ('failed','cancelled') then raise exception using errcode='22023',message='Invalid status'; end if;
  select * into j from public.jobs where id=p_job_id and project_id=p_project_id and kind='tts' for update;
  if not found or j.status<>'running' or j.worker_id is distinct from p_worker_id or j.lease_expires_at<=now() then
    raise exception using errcode='PT409',message='TTS lease lost'; end if;
  v_status := case when j.cancel_requested_at is not null then 'cancelled' else p_status end;
  update public.job_attempts set status=v_status,ended_at=now(),failure_code=case when v_status='failed' then coalesce(p_code,'tts_failed') end,
    error_message=case when v_status='failed' then left(coalesce(p_message,'TTS failed'),2000) end
    where job_id=j.id and attempt_number=j.attempt_count and worker_id=p_worker_id and status='running';
  if not found then raise exception using errcode='23514',message='TTS attempt missing'; end if;
  update public.tts_runs set status=v_status,finished_at=now() where job_id=j.id and status='building';
  update public.jobs set status=v_status,worker_id=null,lease_expires_at=null,finished_at=now(),
    cancel_requested_at=case when v_status='cancelled' then coalesce(cancel_requested_at,now()) else cancel_requested_at end,
    error_code=case when v_status='failed' then coalesce(p_code,'tts_failed') end,
    error_message=case when v_status='failed' then left(coalesce(p_message,'TTS failed'),2000) end where id=j.id;
end $$;

create function public.complete_tts_job(p_project_id uuid,p_job_id uuid,p_worker_id text,p_run_id uuid)
returns void language plpgsql security invoker set search_path='' as $$
declare p public.projects; j public.jobs; r public.tts_runs; v_count integer;
begin
  select * into p from public.projects where id=p_project_id and status='active' for update;
  if not found then raise exception using errcode='PT409',message='Project unavailable'; end if;
  select * into j from public.jobs where id=p_job_id and project_id=p_project_id and kind='tts' for update;
  if not found or j.status<>'running' or j.worker_id is distinct from p_worker_id or j.lease_expires_at<=now() or j.cancel_requested_at is not null then
    raise exception using errcode='PT409',message='TTS lease or cancellation invalid'; end if;
  select * into r from public.tts_runs where id=p_run_id and project_id=p_project_id and job_id=p_job_id and status='building' for update;
  if not found or r.voice_id<>p.tts_voice_id or r.translation_run_id is distinct from p.active_translation_run_id
    or not exists(select 1 from public.translation_runs tr where tr.id=r.translation_run_id and tr.transcription_job_id=p.active_transcription_job_id) then
    raise exception using errcode='PT409',message='Voice or translation changed'; end if;
  select count(*) into v_count from public.translation_units where run_id=r.translation_run_id;
  if v_count=0 or (select count(*) from public.tts_artifacts where run_id=r.id and status='ready')<>v_count
    or exists(select 1 from public.translation_units u left join public.tts_artifacts a on a.run_id=r.id and a.unit_id=u.id
      where u.run_id=r.translation_run_id and (a.id is null or a.status<>'ready' or a.unit_revision<>u.revision or a.text_snapshot<>u.final_text)) then
    raise exception using errcode='PT409',message='TTS artifacts missing or translation changed'; end if;
  update public.tts_runs set status='ready',finished_at=now() where id=r.id;
  update public.projects set active_tts_run_id=r.id where id=p.id;
  update public.jobs set status='succeeded',progress=100,progress_message='Đã tạo giọng đọc',worker_id=null,lease_expires_at=null,finished_at=now() where id=j.id;
  update public.job_attempts set status='succeeded',ended_at=now() where job_id=j.id and attempt_number=j.attempt_count and worker_id=p_worker_id and status='running';
  if not found then raise exception using errcode='23514',message='TTS attempt missing'; end if;
end $$;

revoke all on function public.claim_tts_job(text,integer) from public,anon,authenticated;
revoke all on function public.finish_tts_job(uuid,uuid,text,text,text,text) from public,anon,authenticated;
revoke all on function public.complete_tts_job(uuid,uuid,text,uuid) from public,anon,authenticated;
grant execute on function public.claim_tts_job(text,integer),public.finish_tts_job(uuid,uuid,text,text,text,text),public.complete_tts_job(uuid,uuid,text,uuid) to service_role;
