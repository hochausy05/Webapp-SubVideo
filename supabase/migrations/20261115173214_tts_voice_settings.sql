-- Voice choice is project-local. Existing projects start with the M1 preset.
alter table public.projects add column tts_voice_id text not null default 'M1'
    check (tts_voice_id in ('M1','M2','M3','M4','M5','F1','F2','F3','F4','F5'));
