"""Single local TTS Job worker, with private staged artifacts and a bounded child."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from filelock import FileLock, Timeout
from app.core.config import BACKEND_ROOT
from app.services.supabase import Supabase, eq, storage_object_path
from app.services.tts import MODEL, MODEL_REVISION
from app.workers.transcription_worker import LeaseKeeper, TranscriptionWorker

logger = logging.getLogger('autodub.tts.worker')
BUCKET = 'autodub-private-tts'
STOP = threading.Event()


class TTSWorker:
    def __init__(self) -> None:
        self.db = Supabase()
        self.worker_id = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}'

    def run(self) -> None:
        root = BACKEND_ROOT / '.cache' / 'tts'
        root.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(root / 'worker.lock'), timeout=0):
                ready = False
                while not STOP.is_set():
                    rows = self.db.request('POST','/rest/v1/rpc/claim_tts_job',json={'p_worker_id':self.worker_id,'p_lease_seconds':300}).json()
                    if not isinstance(rows, list) or (rows and rows[0].get('kind') != 'tts'):
                        raise ValueError('Invalid TTS claim response')
                    if not ready:
                        logger.info('RUNTIME_READY kind=tts rpc=claim_tts_job supabase=connected')
                        ready = True
                    if rows:
                        logger.info('claimed TTS job=%s attempt=%s', rows[0]['id'], rows[0]['attempt_count'])
                        self.process(rows[0])
                    else:
                        STOP.wait(3)
        except Timeout:
            raise SystemExit('Another TTS Worker is already running') from None

    def process(self, job: dict) -> None:
        keeper = LeaseKeeper(self.db, job, self.worker_id)
        keeper.settings.asr_lease_seconds = 300
        keeper.start()
        status, code, message = 'failed', 'tts_failed', 'Không thể tạo giọng đọc.'
        process: subprocess.Popen | None = None
        root = BACKEND_ROOT / '.cache' / 'tts'
        try:
            keeper.set_stage('Đang kiểm tra bản dịch đã lưu')
            projects = self.db.select('projects', {'select':'status,active_translation_run_id,tts_voice_id,tts_speed', 'id':eq(job['project_id']), 'limit':'1'})
            meta = job['input_metadata']
            speed = float(meta.get('speed', 1.05))
            if not projects or projects[0]['status'] != 'active' or projects[0]['active_translation_run_id'] != meta['translation_run_id'] or projects[0]['tts_voice_id'] != meta['voice_id'] or float(projects[0]['tts_speed']) != speed:
                code, message = 'tts_input_changed', 'Bản dịch hoặc giọng đã thay đổi; hãy tạo Job mới.'
                return
            if self.db.select('jobs', {'select':'id','project_id':eq(job['project_id']),'kind':'eq.translation','status':'in.(queued,running)','limit':'1'}):
                code, message = 'translation_in_progress', 'Đang xử lý bản dịch; hãy tạo lại Job TTS sau khi hoàn tất.'
                return
            units = self.db.select('translation_units', {'select':'id,final_text,revision,position','run_id':eq(meta['translation_run_id']),'order':'position.asc','limit':'501'})
            if not units or len(units) > 500:
                code, message = 'tts_units_invalid', 'Không có câu thoại hoặc vượt giới hạn 500 Unit.'
                return
            if [[unit['id'], unit['revision']] for unit in units] != meta.get('units'):
                code, message = 'tts_input_changed', 'Bản dịch đã đổi từ lúc tạo Job; hãy tạo Job mới.'
                return
            lock = FileLock(str(root / 'inference.lock'))
            while True:
                if STOP.is_set() or keeper.should_cancel() or keeper.lost_event.is_set():
                    status='cancelled'; return
                try:
                    lock.acquire(timeout=1); break
                except Timeout:
                    continue
            try:
                run_id = str(uuid.uuid4())
                self.db.insert('tts_runs', {'id':run_id,'project_id':job['project_id'],'translation_run_id':meta['translation_run_id'],
                    'job_id':job['id'],'voice_id':meta['voice_id'],'speed':speed,'model':MODEL,'model_revision':MODEL_REVISION})
                artifacts = {}
                for unit in units:
                    asset_id = str(uuid.uuid4())
                    path = f"{job['project_id']}/tts/{run_id}/{asset_id}.wav"
                    artifacts[unit['id']] = self.db.insert('tts_artifacts', {'id':asset_id,'run_id':run_id,
                        'translation_run_id':meta['translation_run_id'],'unit_id':unit['id'],'unit_revision':unit['revision'],
                        'text_snapshot':unit['final_text'],'storage_path':path,
                        'raw_storage_path':f"{job['project_id']}/tts/{run_id}/{asset_id}.raw.wav"})
                with tempfile.TemporaryDirectory(prefix='tts-',dir=root) as scratch:
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
                    process = subprocess.Popen([sys.executable,'-m','app.workers.tts_process'],cwd=str(BACKEND_ROOT),
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=flags)
                    assert process.stdin and process.stdout
                    process.stdin.write(json.dumps({'work_dir':scratch,'voice':meta['voice_id'],'speed':speed,'units':units},ensure_ascii=False)+'\n')
                    process.stdin.close()
                    messages: queue.Queue[str | None] = queue.Queue()
                    def read_output() -> None:
                        assert process and process.stdout
                        for line in process.stdout: messages.put(line)
                        messages.put(None)
                    threading.Thread(target=read_output,daemon=True).start()
                    deadline = time.monotonic() + min(1800, 180 + 30 * len(units))
                    result = None
                    while True:
                        if STOP.is_set() or keeper.should_cancel() or keeper.lost_event.is_set(): status='cancelled'; break
                        if time.monotonic() > deadline:
                            code,message='tts_timeout','Tạo giọng đọc vượt thời gian cho phép.'; break
                        try: line=messages.get(timeout=0.5)
                        except queue.Empty: continue
                        if line is None: break
                        event=json.loads(line)
                        if event.get('event')=='stage': keeper.set_stage(event['message'])
                        elif event.get('event')=='progress': keeper.set_stage(f"Đang tạo giọng đọc — {event['count']}/{event['total']} đoạn")
                        elif event.get('event')=='error': code,message=event['code'],event['message']
                        elif event.get('event')=='complete': result=event['items']
                    if process.poll() is None: TranscriptionWorker._terminate_process_tree(process)
                    if status=='cancelled' or result is None: return
                    if len(result) != len(units) or {x['unit_id'] for x in result} != set(artifacts) or any(x.get('speed') != speed for x in result):
                        code,message='tts_result_invalid','Audio chưa bao phủ đầy đủ các Unit.'; return
                    for index, item in enumerate(result):
                        keeper._observe_state()
                        if STOP.is_set() or keeper.should_cancel() or keeper.lost_event.is_set(): status='cancelled'; return
                        path = Path(scratch) / (str(uuid.UUID(item['unit_id'])) + '.wav')
                        if not path.is_file() or path.stat().st_size <= 44 or path.stat().st_size > 52428800:
                            code,message='tts_audio_invalid','Audio được tạo không hợp lệ.'; return
                        digest = hashlib.sha256()
                        with path.open('rb') as stream:
                            for chunk in iter(lambda: stream.read(1024*1024),b''): digest.update(chunk)
                        asset = artifacts[item['unit_id']]
                        keeper.set_stage(f'Đang lưu audio — {index+1}/{len(result)} đoạn')
                        with path.open('rb') as stream:
                            self.db.request('POST',f"/storage/v1/object/{BUCKET}/{storage_object_path(asset['storage_path'])}",
                                content=stream,headers={'Content-Type':'audio/wav','x-upsert':'false'},timeout=120)
                        verify = Path(scratch) / (asset['id'] + '.verify')
                        received, actual = self.db.download_storage_object(BUCKET,asset['storage_path'],verify,path.stat().st_size)
                        verify.unlink(missing_ok=True)
                        if received != path.stat().st_size or actual != digest.hexdigest():
                            code,message='tts_storage_verify_failed','Không xác minh được audio đã lưu.'; return
                        if item['trimmed_samples']:
                            keeper._observe_state()
                            if STOP.is_set() or keeper.should_cancel() or keeper.lost_event.is_set(): status='cancelled'; return
                            raw = path.with_suffix('.raw.wav')
                            if not raw.is_file() or raw.stat().st_size <= 44 or raw.stat().st_size > 52428800:
                                code,message='tts_audio_invalid','Không có audio gốc để đối chiếu.'; return
                            raw_digest = hashlib.sha256()
                            with raw.open('rb') as raw_stream:
                                for chunk in iter(lambda: raw_stream.read(1024*1024),b''): raw_digest.update(chunk)
                            with raw.open('rb') as raw_stream:
                                self.db.request('POST',f"/storage/v1/object/{BUCKET}/{storage_object_path(asset['raw_storage_path'])}",
                                    content=raw_stream,headers={'Content-Type':'audio/wav','x-upsert':'false'},timeout=120)
                            raw_verify = Path(scratch) / (asset['id'] + '.raw.verify')
                            raw_received, raw_actual = self.db.download_storage_object(BUCKET,asset['raw_storage_path'],raw_verify,raw.stat().st_size)
                            raw_verify.unlink(missing_ok=True)
                            if raw_received != raw.stat().st_size or raw_actual != raw_digest.hexdigest():
                                code,message='tts_storage_verify_failed','Không xác minh được audio gốc.'; return
                        self.db.patch('tts_artifacts',{'id':eq(asset['id']),'status':'eq.pending'}, {'status':'ready',
                            'sample_rate':item['sample_rate'],'samples':item['samples'],
                            'duration_us':round(item['samples']*1000000/item['sample_rate']),
                            'size_bytes':received,'sha256':actual,'original_samples':item['original_samples'],
                            'trimmed_samples':item['trimmed_samples'],'processing_metadata':{**item['processing_metadata'], 'speed': item['speed']}})
                    keeper._observe_state()
                    if STOP.is_set() or keeper.should_cancel() or keeper.lost_event.is_set(): status='cancelled'; return
                    self.db.request('POST','/rest/v1/rpc/complete_tts_job',json={'p_project_id':job['project_id'],
                        'p_job_id':job['id'],'p_worker_id':self.worker_id,'p_run_id':run_id},timeout=120)
                    status='succeeded'
                    logger.info('TTS completed job=%s units=%s',job['id'],len(result))
            finally:
                if process and process.poll() is None: TranscriptionWorker._terminate_process_tree(process)
                lock.release()
        except Exception as exc:
            logger.error('TTS job failed job=%s type=%s',job['id'],type(exc).__name__)
            code,message='tts_worker_failed','Không thể xử lý hoặc lưu audio; kết quả cũ được giữ.'
        finally:
            if process and process.poll() is None: TranscriptionWorker._terminate_process_tree(process)
            keeper._observe_state(); keeper.stop()
            if status != 'succeeded' and not keeper.lost_event.is_set():
                if keeper.should_cancel(): status='cancelled'
                try:
                    self.db.request('POST','/rest/v1/rpc/finish_tts_job',json={'p_project_id':job['project_id'],
                        'p_job_id':job['id'],'p_worker_id':self.worker_id,'p_status':status,
                        'p_code':code if status=='failed' else None,'p_message':message if status=='failed' else None})
                except HTTPException: logger.warning('Could not finalize TTS job; lease recovery is required')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    if hasattr(signal,'SIGBREAK'):
        def stop_on_break(_signum: int, _frame: object) -> None: STOP.set()
        signal.signal(signal.SIGBREAK, stop_on_break)
    try: TTSWorker().run()
    except KeyboardInterrupt: logger.info('TTS Worker stopped')
