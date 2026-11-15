"""Private local TTS preview endpoint."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict
from starlette.background import BackgroundTask

from app.core.config import BACKEND_ROOT, get_settings
from app.services.tts import MODEL, MODEL_REVISION, VOICES, SPEEDS
from app.services.supabase import Supabase, eq, storage_object_path
from app.workers.transcription_worker import TranscriptionWorker

router = APIRouter()
SAMPLE_TEXT = 'Hôm nay, chúng ta cùng khám phá câu chuyện này.'


class VoiceSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    voice_id: str

class SpeedSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    speed: float

def validate_speed(speed: float) -> None:
    if speed not in SPEEDS:
        raise HTTPException(422, detail={'code':'invalid_speed', 'message':'Tốc độ đọc không hợp lệ.'})

@router.patch('/projects/{project_id}/tts/speed')
def select_speed(project_id: uuid.UUID, body: SpeedSelection) -> dict:
    validate_speed(body.speed)
    row = Supabase().patch('projects', {'id':eq(str(project_id)), 'status':'eq.active'}, {'tts_speed':body.speed})
    if not row:
        raise HTTPException(404, detail={'code':'project_not_found', 'message':'Không tìm thấy Project đang hoạt động.'})
    return {'speed':float(row['tts_speed'])}


@router.patch('/projects/{project_id}/tts/voice')
def select_voice(project_id: uuid.UUID, body: VoiceSelection) -> dict:
    if body.voice_id not in VOICES:
        raise HTTPException(422, detail={'code':'invalid_voice', 'message':'Giọng đọc không hợp lệ.'})
    row = Supabase().patch('projects', {'id':eq(str(project_id)), 'status':'eq.active'}, {'tts_voice_id':body.voice_id})
    if not row:
        raise HTTPException(404, detail={'code':'project_not_found', 'message':'Không tìm thấy Project đang hoạt động.'})
    return {'voice_id':row['tts_voice_id']}


def _project(db: Supabase, project_id: uuid.UUID) -> dict:
    rows = db.select('projects', {'select':'id,status,tts_voice_id,tts_speed,active_tts_run_id,active_translation_run_id,active_transcription_job_id','id':eq(str(project_id)),'limit':'1'})
    if not rows:
        raise HTTPException(404, detail={'code':'project_not_found','message':'Không tìm thấy Project.'})
    return rows[0]


def _translation_for_tts(db: Supabase, project_id: uuid.UUID, project: dict) -> tuple[str, list[dict]]:
    if project['status'] != 'active' or not project.get('active_transcription_job_id'):
        raise HTTPException(409, detail={'code':'translation_not_ready','message':'Cần nhận diện lời thoại trước khi tạo giọng đọc.'})
    if db.select('jobs', {'select':'id','project_id':eq(str(project_id)),'kind':'eq.translation','status':'in.(queued,running)','limit':'1'}):
        raise HTTPException(409, detail={'code':'translation_in_progress','message':'Đang xử lý bản dịch. Hãy chờ hoàn tất trước khi tạo giọng đọc.'})
    run_id = project.get('active_translation_run_id')
    if not run_id:
        raise HTTPException(409, detail={'code':'translation_not_ready','message':'Hãy dịch video sang tiếng Việt trước khi tạo giọng đọc.'})
    runs = db.select('translation_runs', {'select':'transcription_job_id,target_language','id':eq(run_id),'project_id':eq(str(project_id)),'limit':'1'})
    if not runs or runs[0]['transcription_job_id'] != project['active_transcription_job_id'] or runs[0]['target_language'] != 'vi':
        raise HTTPException(409, detail={'code':'translation_revision_conflict','message':'Bản dịch không còn khớp transcript nguồn.'})
    units = db.select('translation_units', {'select':'id,position,start_us,end_us,revision,final_text','run_id':eq(run_id),'order':'position.asc','limit':'501'})
    source = db.select('segments', {'select':'id,start_us,end_us','project_id':eq(str(project_id)),'transcription_job_id':eq(project['active_transcription_job_id']),'order':'position.asc','limit':'501'})
    mappings = db.select('translation_unit_segments', {'select':'unit_id,segment_id,position','run_id':eq(run_id),'order':'unit_id.asc,position.asc','limit':'1501'})
    if not units or not source or len(units)>500 or len(source)>500 or len(mappings)>1500 or any(not isinstance(u['final_text'],str) or not u['final_text'].strip() for u in units):
        raise HTTPException(409,detail={'code':'translation_invalid','message':'Bản dịch hoặc transcript rỗng, không hợp lệ hoặc vượt giới hạn.'})
    by_unit: dict[str,list[str]] = {}
    for mapping in mappings:
        by_unit.setdefault(mapping['unit_id'], []).append(mapping['segment_id'])
    offset = 0
    for position, unit in enumerate(units):
        ids = by_unit.pop(unit['id'], [])
        group = source[offset:offset+len(ids)]
        if (unit['position'] != position or not 1 <= len(ids) <= 3 or [s['id'] for s in group] != ids
                or unit['start_us'] != group[0]['start_us'] or unit['end_us'] != group[-1]['end_us']
                or (len(ids) > 1 and unit['end_us'] - unit['start_us'] > 4_000_000)):
            raise HTTPException(409,detail={'code':'translation_invalid','message':'Nhóm bản dịch không hợp lệ. Hãy tạo lại bản dịch theo quy tắc mới.'})
        offset += len(ids)
    if offset != len(source) or by_unit:
        raise HTTPException(409,detail={'code':'translation_invalid','message':'Bản dịch thiếu hoặc lặp Segment nguồn; hãy tạo lại bản dịch.'})
    return run_id, units


@router.get('/projects/{project_id}/tts/readiness')
def tts_readiness(project_id: uuid.UUID) -> dict:
    db=Supabase(); project=_project(db,project_id)
    try:
        _translation_for_tts(db, project_id, project)
        if db.select('jobs', {'select':'id','project_id':eq(str(project_id)),'kind':'eq.tts','status':'in.(queued,running)','limit':'1'}):
            return {'ready':False,'code':'tts_job_already_active','message':'Job tạo giọng đọc đang chờ hoặc đang chạy.'}
    except HTTPException as exc:
        if exc.status_code != 409: raise
        return {'ready':False, **exc.detail}
    return {'ready':True,'code':None,'message':None}


@router.post('/projects/{project_id}/tts')
def start_tts(project_id: uuid.UUID) -> dict:
    db = Supabase(); project = _project(db,project_id)
    run_id, units = _translation_for_tts(db, project_id, project)
    active = db.select('jobs',{'select':'*','project_id':eq(str(project_id)),'kind':'eq.tts','status':'in.(queued,running)','limit':'1'})
    snapshot = {'translation_run_id':run_id,'voice_id':project['tts_voice_id'],'speed':float(project['tts_speed']),'units':[(u['id'],u['revision']) for u in units]}
    fingerprint = hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()
    if active:
        raise HTTPException(409,detail={'code':'tts_job_already_active','message':'Job tạo giọng đọc đang chờ hoặc đang chạy.'})
    row = {'project_id':str(project_id),'kind':'tts','status':'queued','progress_message':'Đang chờ Worker TTS',
        'input_fingerprint':fingerprint,'idempotency_key':'tts:'+fingerprint,'input_metadata':snapshot,'max_attempts':1}
    try: return {'job':db.insert('jobs',row),'reused':False}
    except HTTPException as exc:
        if exc.status_code == 409:
            active=db.select('jobs',{'select':'*','project_id':eq(str(project_id)),'kind':'eq.tts','status':'in.(queued,running)','limit':'1'})
            if active: raise HTTPException(409, detail={'code':'tts_job_already_active','message':'Job tạo giọng đọc đang chờ hoặc đang chạy.'}) from exc
        raise


@router.get('/projects/{project_id}/tts')
def tts_state(project_id: uuid.UUID) -> dict:
    db=Supabase(); project=_project(db,project_id)
    jobs=db.select('jobs',{'select':'*','project_id':eq(str(project_id)),'kind':'eq.tts','order':'created_at.desc,id.desc','limit':'1'})
    run_id=project.get('active_tts_run_id')
    if not run_id: return {'run':None,'items':[],'latest_job':jobs[0] if jobs else None,'stale':False}
    runs=db.select('tts_runs',{'select':'id,translation_run_id,voice_id,model,model_revision,total_steps,speed,status,finished_at','id':eq(run_id),'project_id':eq(str(project_id)),'limit':'1'})
    if not runs: raise HTTPException(500,detail={'code':'tts_run_missing','message':'Không tìm thấy dữ liệu audio.'})
    run=runs[0]
    source_run=db.select('translation_runs',{'select':'transcription_job_id','id':eq(run['translation_run_id']),'project_id':eq(str(project_id)),'limit':'1'})
    units=db.select('translation_units',{'select':'id,position,revision,final_text','run_id':eq(run['translation_run_id']),'order':'position.asc','limit':'500'})
    assets=db.select('tts_artifacts',{'select':'id,unit_id,unit_revision,text_snapshot,sample_rate,samples,duration_us,original_samples,trimmed_samples,status','run_id':eq(run_id),'limit':'500'})
    by_unit={x['unit_id']:x for x in assets}
    stale_run=run['voice_id'] != project['tts_voice_id'] or run['translation_run_id'] != project['active_translation_run_id'] or run['model'] != MODEL or run['model_revision'] != MODEL_REVISION or run['total_steps'] != 8 or float(run['speed']) != float(project['tts_speed']) or not source_run or source_run[0]['transcription_job_id'] != project['active_transcription_job_id']
    items=[]
    for unit in units:
        asset=by_unit.get(unit['id'])
        if not asset: continue
        stale=stale_run or asset['unit_revision'] != unit['revision'] or asset['text_snapshot'] != unit['final_text']
        items.append({'id':asset['id'],'unit_id':unit['id'],'position':unit['position'],'final_text':unit['final_text'],
            'duration_us':asset['duration_us'],'sample_rate':asset['sample_rate'],'status':asset['status'],'stale':stale,
            'original_samples':asset['original_samples'],'trimmed_samples':asset['trimmed_samples']})
    return {'run':run,'items':items,'latest_job':jobs[0] if jobs else None,'stale':stale_run or len(items) != len(units) or any(i['stale'] or i['status'] != 'ready' for i in items)}


@router.get('/projects/{project_id}/tts/{artifact_id}/playback')
def tts_playback(project_id: uuid.UUID, artifact_id: uuid.UUID, raw: bool = False) -> dict:
    db=Supabase(); project=_project(db,project_id)
    rows=db.select('tts_artifacts',{'select':'id,run_id,unit_id,unit_revision,text_snapshot,status,storage_path,raw_storage_path,trimmed_samples', 'id':eq(str(artifact_id)), 'limit':'1'})
    if not rows: raise HTTPException(404,detail={'code':'tts_audio_not_found','message':'Không tìm thấy audio.'})
    asset=rows[0]
    runs=db.select('tts_runs',{'select':'id,voice_id,translation_run_id,model,model_revision,total_steps,speed,status','id':eq(asset['run_id']),'project_id':eq(str(project_id)),'limit':'1'})
    if not runs or project.get('active_tts_run_id') != asset['run_id'] or asset['status']!='ready':
        raise HTTPException(404,detail={'code':'tts_audio_not_found','message':'Không tìm thấy audio đang hoạt động.'})
    run=runs[0]
    source_run=db.select('translation_runs',{'select':'transcription_job_id','id':eq(run['translation_run_id']),'project_id':eq(str(project_id)),'limit':'1'})
    units=db.select('translation_units',{'select':'revision,final_text','id':eq(asset['unit_id']),'run_id':eq(run['translation_run_id']),'limit':'1'})
    if run['voice_id'] != project['tts_voice_id'] or run['translation_run_id'] != project['active_translation_run_id'] or run['model'] != MODEL or run['model_revision'] != MODEL_REVISION or run['total_steps'] != 8 or float(run['speed']) != float(project['tts_speed']) or not source_run or source_run[0]['transcription_job_id'] != project['active_transcription_job_id'] or not units or units[0]['revision'] != asset['unit_revision'] or units[0]['final_text'] != asset['text_snapshot']:
        raise HTTPException(409,detail={'code':'tts_audio_stale','message':'Giọng hoặc bản dịch đã đổi; hãy tạo audio mới.'})
    selected_path = asset['raw_storage_path'] if raw and asset['trimmed_samples'] else asset['storage_path']
    if not selected_path: raise HTTPException(502,detail={'code':'tts_raw_unavailable','message':'Không tìm thấy audio gốc.'})
    signed=db.request('POST',f"/storage/v1/object/sign/autodub-private-tts/{storage_object_path(selected_path)}",json={'expiresIn':300}).json()
    url=signed.get('signedURL') or signed.get('signedUrl')
    if not url: raise HTTPException(502,detail={'code':'tts_playback_unavailable','message':'Không thể phát audio.'})
    if not url.startswith('https://'):
        url=db.root + (url if url.startswith('/storage/v1/') else '/storage/v1' + (url if url.startswith('/') else '/'+url))
    return {'playback_url':url,'expires_in':300}


@router.post('/projects/{project_id}/tts/{job_id}/cancel')
def cancel_tts(project_id: uuid.UUID, job_id: uuid.UUID) -> dict:
    db=Supabase(); _project(db,project_id)
    rows=db.select('jobs',{'select':'id,status','id':eq(str(job_id)),'project_id':eq(str(project_id)),'kind':'eq.tts','limit':'1'})
    if not rows: raise HTTPException(404,detail={'code':'tts_job_not_found','message':'Không tìm thấy Job TTS.'})
    now=datetime.now(timezone.utc).isoformat()
    if rows[0]['status']=='queued':
        updated=db.patch('jobs',{'id':eq(str(job_id)),'project_id':eq(str(project_id)),'status':'eq.queued'},
            {'status':'cancelled','cancel_requested_at':now,'finished_at':now,'progress_message':'Đã hủy tạo giọng đọc'})
        if updated: return {'ok':True}
    db.patch('jobs',{'id':eq(str(job_id)),'project_id':eq(str(project_id)),'status':'eq.running','cancel_requested_at':'is.null'},
        {'cancel_requested_at':now,'progress_message':'Đang hủy tạo giọng đọc'})
    return {'ok':True}


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


@router.post('/tts/preview/{voice_id}')
def preview_voice(voice_id: str, body: SpeedSelection) -> FileResponse:
    if voice_id not in VOICES:
        raise HTTPException(422, detail={'code':'invalid_voice', 'message':'Giọng đọc không hợp lệ.'})
    validate_speed(body.speed)
    root = BACKEND_ROOT / '.cache' / 'tts' / 'previews'
    root.mkdir(parents=True, exist_ok=True)
    # A private generated directory prevents a client from specifying any path.
    directory = root / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    output = directory / f'{voice_id}.wav'
    raw_output = directory / f'{voice_id}.raw.wav'
    lock = FileLock(str(root.parent / 'inference.lock'), timeout=0)
    process: subprocess.Popen | None = None
    try:
        with lock:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            process = subprocess.Popen([sys.executable, '-m', 'app.services.tts_cli', '--text', SAMPLE_TEXT,
                '--voices', voice_id, '--speed', str(body.speed), '--output-dir', str(directory), '--model-dir', str(get_settings().tts_model_dir)],
                cwd=str(BACKEND_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            try:
                exit_code = process.wait(timeout=180)
            except subprocess.TimeoutExpired as exc:
                TranscriptionWorker._terminate_process_tree(process)
                raise HTTPException(504, detail={'code':'tts_preview_timeout', 'message':'Nghe thử quá thời gian cho phép; hãy thử lại.'}) from exc
            if exit_code != 0 or not output.is_file() or output.stat().st_size <= 44:
                raise HTTPException(502, detail={'code':'tts_preview_failed', 'message':'Không tạo được audio nghe thử. Kiểm tra model Supertonic 3.'})
    except Timeout as exc:
        try: directory.rmdir()
        except OSError: pass
        raise HTTPException(409, detail={'code':'tts_busy', 'message':'Đang tạo audio; hãy đợi rồi thử lại.'}) from exc
    except BaseException:
        if process and process.poll() is None:
            TranscriptionWorker._terminate_process_tree(process)
        output.unlink(missing_ok=True)
        raw_output.unlink(missing_ok=True)
        try: directory.rmdir()
        except OSError: pass
        raise

    def cleanup() -> None:
        _remove_file(output)
        _remove_file(raw_output)
        try: directory.rmdir()
        except OSError: pass
    return FileResponse(output, media_type='audio/wav', filename=f'{voice_id}.wav', background=BackgroundTask(cleanup))
