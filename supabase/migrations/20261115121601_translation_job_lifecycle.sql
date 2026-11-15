-- Privileged translation lifecycle helpers. All functions are SECURITY INVOKER.
create function public.claim_translation_job(p_worker_id text, p_lease_seconds integer)
returns setof public.jobs language plpgsql security invoker set search_path = '' as $$
begin
    perform pg_advisory_xact_lock(hashtextextended('autodub:translation-worker', 0));
    if exists (select 1 from public.jobs where kind = 'translation' and status = 'running' and lease_expires_at > now()) then
        return;
    end if;
    return query select * from public.claim_next_job(p_worker_id, p_lease_seconds, 'translation');
end $$;

create function public.finish_translation_job(p_project_id uuid, p_job_id uuid, p_worker_id text,
    p_status text, p_error_code text default null, p_error_message text default null)
returns public.jobs language plpgsql security invoker set search_path = '' as $$
declare v_job public.jobs; v_result public.jobs; v_code text; v_message text;
begin
    if p_status not in ('failed', 'cancelled') or p_worker_id is null then
        raise exception using errcode='22023', message='Invalid terminal status';
    end if;
    select * into v_job from public.jobs where id=p_job_id and project_id=p_project_id and kind='translation' for update;
    if not found or v_job.status <> 'running' or v_job.worker_id is distinct from p_worker_id or v_job.lease_expires_at <= now() then
        raise exception using errcode='55000', message='Translation lease lost';
    end if;
    v_code := case when p_status='failed' then coalesce(p_error_code,'translation_failed') else null end;
    v_message := case when p_status='failed' then left(coalesce(p_error_message,'Translation failed.'),2000) else null end;
    update public.job_attempts set status=p_status, ended_at=now(), failure_code=v_code, error_message=v_message
      where job_id=p_job_id and attempt_number=v_job.attempt_count and worker_id=p_worker_id and status='running';
    if not found then raise exception using errcode='23514', message='Translation attempt missing'; end if;
    update public.jobs set status=p_status, worker_id=null, lease_expires_at=null, finished_at=now(),
      cancel_requested_at=case when p_status='cancelled' then coalesce(cancel_requested_at,now()) else cancel_requested_at end,
      progress_message=case when p_status='cancelled' then 'Đã hủy dịch' else progress_message end,
      error_code=v_code, error_message=v_message where id=p_job_id returning * into v_result;
    return v_result;
end $$;

create function public.cancel_queued_translation_job(p_project_id uuid, p_job_id uuid)
returns public.jobs language plpgsql security invoker set search_path = '' as $$
declare v_job public.jobs;
begin
    update public.jobs set status='cancelled', cancel_requested_at=now(), finished_at=now(), progress_message='Đã hủy dịch'
      where id=p_job_id and project_id=p_project_id and kind='translation' and status='queued' returning * into v_job;
    return v_job;
end $$;

revoke all on function public.claim_translation_job(text, integer) from public, anon, authenticated;
revoke all on function public.finish_translation_job(uuid, uuid, text, text, text, text) from public, anon, authenticated;
revoke all on function public.cancel_queued_translation_job(uuid, uuid) from public, anon, authenticated;
grant execute on function public.claim_translation_job(text, integer) to service_role;
grant execute on function public.finish_translation_job(uuid, uuid, text, text, text, text) to service_role;
grant execute on function public.cancel_queued_translation_job(uuid, uuid) to service_role;
