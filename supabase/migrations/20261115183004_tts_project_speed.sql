-- Existing TTS implementation synthesized at 1.05; preserve every existing Project.
alter table public.projects add column tts_speed numeric not null default 1.05;
alter table public.projects alter column tts_speed set default 1.25;
alter table public.projects add constraint projects_tts_speed_check
  check (tts_speed in (1.05, 1.15, 1.25, 1.35, 1.45));

-- Publication must also check the snapshot under the same Project row lock.
create or replace function public.complete_tts_job(p_project_id uuid,p_job_id uuid,p_worker_id text,p_run_id uuid)
returns void language plpgsql security invoker set search_path='' as $$
declare p public.projects; j public.jobs; r public.tts_runs; v_count integer;
begin
  select * into p from public.projects where id=p_project_id and status='active' for update;
  if not found then raise exception using errcode='PT409',message='Project unavailable'; end if;
  select * into j from public.jobs where id=p_job_id and project_id=p_project_id and kind='tts' for update;
  if not found or j.status<>'running' or j.worker_id is distinct from p_worker_id or j.lease_expires_at<=now() or j.cancel_requested_at is not null then
    raise exception using errcode='PT409',message='TTS lease or cancellation invalid'; end if;
  select * into r from public.tts_runs where id=p_run_id and project_id=p_project_id and job_id=p_job_id and status='building' for update;
  if not found or r.voice_id<>p.tts_voice_id or r.speed<>p.tts_speed or r.translation_run_id is distinct from p.active_translation_run_id
    or not exists(select 1 from public.translation_runs tr where tr.id=r.translation_run_id and tr.transcription_job_id=p.active_transcription_job_id) then
    raise exception using errcode='PT409',message='Voice, speed or translation changed'; end if;
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
