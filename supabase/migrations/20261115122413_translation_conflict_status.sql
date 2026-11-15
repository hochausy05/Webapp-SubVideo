-- Return application conflicts without triggering serialization retries.
create or replace function public.bulk_edit_translation(p_project_id uuid, p_run_id uuid, p_edits jsonb)
returns integer language plpgsql security invoker set search_path = '' as $$
declare
    v_edit jsonb;
    v_unit public.translation_units;
    v_count integer := 0;
    v_seen uuid[] := '{}';
    v_id uuid;
    v_revision integer;
    v_text text;
begin
    if p_edits is null or jsonb_typeof(p_edits) <> 'array' or jsonb_array_length(p_edits) not between 1 and 500 then
        raise exception using errcode = '22023', message = 'Invalid edit batch';
    end if;
    perform 1 from public.projects p join public.translation_runs r
      on r.project_id = p.id and r.id = p.active_translation_run_id
      where p.id = p_project_id and p.status = 'active' and r.id = p_run_id
        and r.transcription_job_id = p.active_transcription_job_id
      for update of p;
    if not found then
        raise exception using errcode = 'PT409', message = 'Translation or source transcript is no longer active';
    end if;
    for v_edit in select value from jsonb_array_elements(p_edits) order by value->>'unit_id' loop
        v_id := (v_edit->>'unit_id')::uuid;
        v_revision := (v_edit->>'revision')::integer;
        v_text := v_edit->>'final_text';
        if v_id is null or v_revision is null or v_revision < 0 or v_text is null
           or length(btrim(v_text)) not between 1 and 20000 or v_id = any(v_seen) then
            raise exception using errcode = '22023', message = 'Invalid or duplicate edit';
        end if;
        v_seen := array_append(v_seen, v_id);
        select * into v_unit from public.translation_units u
          where u.run_id = p_run_id and u.id = v_id for update;
        if not found or v_unit.revision <> v_revision then
            raise exception using errcode = 'PT409', message = 'Translation unit version conflict';
        end if;
    end loop;
    for v_edit in select value from jsonb_array_elements(p_edits) loop
        update public.translation_units set final_text = v_edit->>'final_text', revision = revision + 1
          where run_id = p_run_id and id = (v_edit->>'unit_id')::uuid;
        v_count := v_count + 1;
    end loop;
    return v_count;
end $$;
revoke all on function public.bulk_edit_translation(uuid, uuid, jsonb) from public, anon, authenticated;
grant execute on function public.bulk_edit_translation(uuid, uuid, jsonb) to service_role;

-- Persisting and publishing a run is one transaction, guarded by a current lease,
-- the exact active transcript and the active translation revision observed at start.
create or replace function public.complete_translation_job(p_project_id uuid, p_job_id uuid, p_worker_id text, p_units jsonb)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare
    v_project public.projects;
    v_job public.jobs;
    v_previous uuid;
    v_run uuid := gen_random_uuid();
    v_unit jsonb;
    v_id uuid;
    v_ids uuid[];
    v_expected uuid[];
    v_actual uuid[] := '{}';
    v_first public.segments;
    v_last public.segments;
    v_unit_position integer := 0;
    v_segment_position integer;
    v_segment_id uuid;
begin
    select * into v_project from public.projects where id = p_project_id and status = 'active' for update;
    if not found then raise exception using errcode = 'PT409', message = 'Project unavailable'; end if;
    select * into v_job from public.jobs where id = p_job_id and project_id = p_project_id for update;
    if not found or v_job.kind <> 'translation' or v_job.status <> 'running'
       or p_worker_id is null or v_job.worker_id is distinct from p_worker_id or v_job.lease_expires_at <= now()
       or v_job.cancel_requested_at is not null then
        raise exception using errcode = 'PT409', message = 'Translation job lease or cancellation invalid';
    end if;
    if v_project.active_transcription_job_id is distinct from (v_job.input_metadata->>'transcription_job_id')::uuid then
        raise exception using errcode = 'PT409', message = 'Source transcript changed';
    end if;
    v_previous := (v_job.input_metadata->>'active_translation_run_id')::uuid;
    if v_project.active_translation_run_id is distinct from v_previous then
        raise exception using errcode = 'PT409', message = 'Active translation changed';
    end if;
    if v_previous is not null and exists (
      select 1 from public.translation_units where run_id = v_previous and revision > 0
    ) then
        raise exception using errcode = 'PT409', message = 'Edited translation must not be replaced automatically';
    end if;
    if p_units is null or jsonb_typeof(p_units) <> 'array' or jsonb_array_length(p_units) not between 1 and 500 then
        raise exception using errcode = '22023', message = 'Invalid translation result';
    end if;
    select array_agg(s.id order by s.position) into v_expected from public.segments s
      where s.project_id = p_project_id and s.transcription_job_id = v_project.active_transcription_job_id;
    if coalesce(array_length(v_expected, 1), 0) = 0 then
        raise exception using errcode = '23514', message = 'Source transcript is empty';
    end if;
    insert into public.translation_runs(id, project_id, transcription_job_id, translation_job_id,
        source_language, target_language, model, prompt_version)
    values (v_run, p_project_id, v_project.active_transcription_job_id, p_job_id,
        v_job.input_metadata->>'source_language', 'vi', v_job.input_metadata->>'model', v_job.input_metadata->>'prompt_version');
    for v_unit in select value from jsonb_array_elements(p_units) loop
        v_id := (v_unit->>'id')::uuid;
        select array_agg(value::uuid order by ordinality) into v_ids
          from jsonb_array_elements_text(v_unit->'source_segment_ids') with ordinality;
        if v_id is null or coalesce(array_length(v_ids, 1), 0) = 0 then
            raise exception using errcode = '23514', message = 'Translation unit missing source segments';
        end if;
        v_actual := v_actual || v_ids;
        select * into v_first from public.segments where id = v_ids[1] and project_id = p_project_id and transcription_job_id = v_project.active_transcription_job_id;
        if not found then raise exception using errcode = '23514', message = 'Invalid first segment'; end if;
        select * into v_last from public.segments where id = v_ids[array_length(v_ids, 1)] and project_id = p_project_id and transcription_job_id = v_project.active_transcription_job_id;
        if not found then raise exception using errcode = '23514', message = 'Invalid last segment'; end if;
        insert into public.translation_units(id, run_id, position, start_us, end_us, pass1_text, pass2_text, final_text, needs_review, review_reason)
        values (v_id, v_run, v_unit_position, v_first.start_us, v_last.end_us,
            v_unit->>'translated_text', v_unit->>'optimized_text', v_unit->>'optimized_text',
            coalesce((v_unit->>'needs_review')::boolean, false), coalesce(v_unit->>'review_reason', ''));
        v_segment_position := 0;
        foreach v_segment_id in array v_ids loop
            insert into public.translation_unit_segments(run_id, unit_id, project_id, transcription_job_id, segment_id, position)
            values (v_run, v_id, p_project_id, v_project.active_transcription_job_id, v_segment_id, v_segment_position);
            v_segment_position := v_segment_position + 1;
        end loop;
        v_unit_position := v_unit_position + 1;
    end loop;
    if v_actual is distinct from v_expected then
        raise exception using errcode = '23514', message = 'Translation does not partition source transcript in order';
    end if;
    update public.projects set active_translation_run_id = v_run where id = p_project_id;
    update public.jobs set status = 'succeeded', progress = 100, progress_message = 'Dịch hoàn tất',
      worker_id = null, lease_expires_at = null, finished_at = now() where id = p_job_id;
    update public.job_attempts set status = 'succeeded', ended_at = now(), failure_code = null, error_message = null
      where job_id = p_job_id and attempt_number = v_job.attempt_count and worker_id = p_worker_id and status = 'running';
    if not found then raise exception using errcode = '23514', message = 'Translation attempt missing'; end if;
    return v_run;
end $$;
revoke all on function public.complete_translation_job(uuid, uuid, text, jsonb) from public, anon, authenticated;
grant execute on function public.complete_translation_job(uuid, uuid, text, jsonb) to service_role;
