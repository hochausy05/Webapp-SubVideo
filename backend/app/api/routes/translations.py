from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.services.supabase import Supabase, SupabaseRequestError, eq
from app.services.translation_edits import BulkTranslationEdit
from app.services.translation_prompts import PROMPT_VERSION
from app.core.config import BACKEND_ROOT
from dotenv import dotenv_values

router = APIRouter()


def project_or_404(db: Supabase, project_id: uuid.UUID) -> dict:
    rows = db.select('projects', {'select': 'id,status,active_transcription_job_id,active_translation_run_id,source_language', 'id': eq(str(project_id)), 'limit': '1'})
    if not rows:
        raise HTTPException(404, detail={'code': 'project_not_found', 'message': 'Không tìm thấy Project.'})
    return rows[0]


@router.post('/projects/{project_id}/translations')
def start_translation(project_id: uuid.UUID) -> dict:
    db = Supabase()
    project = project_or_404(db, project_id)
    transcript = project.get('active_transcription_job_id')
    if project['status'] != 'active' or not transcript:
        raise HTTPException(409, detail={'code': 'transcript_unavailable', 'message': 'Cần transcript nguồn đang hoạt động trước khi dịch.'})
    existing = db.select('jobs', {'select':'*', 'project_id':eq(str(project_id)), 'kind':'eq.translation', 'status':'in.(queued,running)', 'limit':'1'})
    if existing:
        return {'job':existing[0], 'reused':True}
    previous = project.get('active_translation_run_id')
    if previous and db.select('translation_units', {'select':'id', 'run_id':eq(previous), 'revision':'gt.0', 'limit':'1'}):
        raise HTTPException(409, detail={'code':'edited_translation_protected', 'message':'Bản dịch đã chỉnh sửa được bảo vệ; chưa hỗ trợ thay thế tự động.'})
    segments = db.select('segments', {'select':'id', 'project_id':eq(str(project_id)), 'transcription_job_id':eq(transcript), 'limit':'1'})
    if not segments:
        raise HTTPException(409, detail={'code':'transcript_empty', 'message':'Transcript chưa có Segment để dịch.'})
    env = dotenv_values(BACKEND_ROOT / '.env')
    model = env.get('GEMINI_MODEL') or 'gemini-2.5-flash'
    language = project.get('source_language') or 'und'
    metadata = {'transcription_job_id':transcript, 'active_translation_run_id':previous, 'source_language':'zh' if language == 'und' else language, 'model':model, 'prompt_version':PROMPT_VERSION}
    fingerprint = hashlib.sha256((transcript + ':' + model + ':' + PROMPT_VERSION).encode()).hexdigest()
    row = {'project_id':str(project_id), 'kind':'translation', 'status':'queued', 'progress':0,
           'progress_message':'Đang chờ Worker dịch', 'input_fingerprint':fingerprint,
           'idempotency_key':'translation:' + fingerprint, 'input_metadata':metadata, 'max_attempts':1}
    try:
        return {'job':db.insert('jobs', row), 'reused':False}
    except HTTPException as exc:
        if exc.status_code == 409:
            existing = db.select('jobs', {'select':'*', 'project_id':eq(str(project_id)), 'kind':'eq.translation', 'status':'in.(queued,running)', 'limit':'1'})
            if existing: return {'job':existing[0], 'reused':True}
        raise


@router.get('/projects/{project_id}/translations')
def get_translation(project_id: uuid.UUID) -> dict:
    db=Supabase(); project=project_or_404(db, project_id)
    jobs=db.select('jobs', {'select':'*','project_id':eq(str(project_id)),'kind':'eq.translation','order':'created_at.desc,id.desc','limit':'1'})
    run_id=project.get('active_translation_run_id')
    if not run_id: return {'run':None,'units':[],'latest_job':jobs[0] if jobs else None,'stale':False}
    runs=db.select('translation_runs', {'select':'*','project_id':eq(str(project_id)),'id':eq(run_id),'limit':'1'})
    if not runs: raise HTTPException(500,detail={'code':'translation_missing','message':'Không tìm thấy dữ liệu bản dịch.'})
    units=db.select('translation_units', {'select':'*','run_id':eq(run_id),'order':'position.asc','limit':'500'})
    mappings=db.select('translation_unit_segments', {'select':'unit_id,segment_id,position','run_id':eq(run_id),'order':'unit_id.asc,position.asc','limit':'5000'})
    segments=db.select('segments', {'select':'id,source_text','project_id':eq(str(project_id)),'transcription_job_id':eq(runs[0]['transcription_job_id']),'order':'position.asc','limit':'1000'})
    text_by_id={s['id']:s['source_text'] for s in segments}
    for unit in units:
        ids=[m['segment_id'] for m in mappings if m['unit_id']==unit['id']]
        unit['source_segment_ids']=ids
        unit['source_text']=' '.join(text_by_id.get(i,'') for i in ids)
    return {'run':runs[0],'units':units,'latest_job':jobs[0] if jobs else None,'stale':runs[0]['transcription_job_id'] != project.get('active_transcription_job_id')}


@router.post('/projects/{project_id}/translations/{job_id}/cancel')
def cancel_translation(project_id: uuid.UUID, job_id: uuid.UUID) -> dict:
    db=Supabase(); project_or_404(db,project_id)
    rows=db.select('jobs',{'select':'*','id':eq(str(job_id)),'project_id':eq(str(project_id)),'kind':'eq.translation','limit':'1'})
    if not rows: raise HTTPException(404,detail={'code':'translation_job_not_found','message':'Không tìm thấy Job dịch.'})
    job=rows[0]
    if job['status']=='queued':
        result=db.request('POST','/rest/v1/rpc/cancel_queued_translation_job',json={'p_project_id':str(project_id),'p_job_id':str(job_id)}).json()
        if not result or not result.get('id'):
            rows=db.select('jobs',{'select':'*','id':eq(str(job_id)),'project_id':eq(str(project_id)),'kind':'eq.translation','limit':'1'})
            job=rows[0] if rows else job
    if job['status']=='running':
        db.patch('jobs',{'id':eq(str(job_id)),'project_id':eq(str(project_id)),'status':'eq.running','cancel_requested_at':'is.null'}, {'cancel_requested_at':datetime.now(timezone.utc).isoformat(),'progress_message':'Đang hủy dịch'})
    return {'ok':True}


@router.patch('/projects/{project_id}/translations')
def edit_translation(project_id: uuid.UUID, payload: BulkTranslationEdit) -> dict:
    db=Supabase(); project_or_404(db,project_id)
    try:
        count=db.request('POST','/rest/v1/rpc/bulk_edit_translation',json={'p_project_id':str(project_id),'p_run_id':str(payload.run_id),'p_edits':[x.model_dump(mode='json') for x in payload.edits]}).json()
    except SupabaseRequestError as exc:
        if exc.provider_code in {'40001','23514','PT409'} or exc.provider_http_status == 409:
            raise HTTPException(409,detail={'code':'translation_conflict','message':'Bản dịch đã thay đổi hoặc transcript nguồn không còn active. Nội dung chưa lưu vẫn được giữ.'}) from exc
        raise
    return {'updated':count}
