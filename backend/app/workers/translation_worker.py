"""Translation Job worker using the existing durable queue and a bounded subprocess."""
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from filelock import FileLock, Timeout

from app.core.config import BACKEND_ROOT
from app.services.supabase import Supabase, eq
from app.services.translation_validation import SourceSegment, validate_persisted_result
from app.services.translation_prompts import PROMPT_VERSION
from app.workers.transcription_worker import LeaseKeeper, TranscriptionWorker

logger=logging.getLogger('autodub.translation.worker')
STAGES={'translating_pass_1':'Đang dịch Pass 1','validating_pass_1':'Đang kiểm tra Pass 1',
        'optimizing_pass_2':'Đang tối ưu Pass 2','validating_pass_2':'Đang kiểm tra Pass 2'}


class TranslationWorker:
    def __init__(self) -> None:
        self.db=Supabase()
        self.worker_id=f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}'

    def run(self) -> None:
        lock_path=BACKEND_ROOT / '.cache' / 'gemini-translation.lock'
        lock_path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with FileLock(str(lock_path),timeout=0):
                logger.info('Translation Worker starting; run only one Worker per deployment')
                readiness_reported=False
                while True:
                    rows=self.db.request('POST','/rest/v1/rpc/claim_translation_job',json={'p_worker_id':self.worker_id,'p_lease_seconds':300}).json()
                    if not isinstance(rows,list) or (rows and (not isinstance(rows[0],dict) or rows[0].get('kind') != 'translation')):
                        raise ValueError('Invalid translation claim response')
                    if not readiness_reported:
                        logger.info('RUNTIME_READY kind=translation rpc=claim_translation_job supabase=connected')
                        readiness_reported=True
                    if not rows:
                        time.sleep(3); continue
                    logger.info('claimed translation job=%s attempt=%s',rows[0]['id'],rows[0]['attempt_count'])
                    self.process(rows[0])
        except Timeout:
            raise SystemExit('Another local Gemini translation process is running') from None

    def process(self, job: dict) -> None:
        keeper=LeaseKeeper(self.db,job,self.worker_id)
        keeper.settings.asr_lease_seconds=300
        keeper.start()
        status='failed'; code='translation_failed'; message='Không thể hoàn tất bản dịch.'
        process=None
        try:
            keeper.set_stage('Đang tải transcript nguồn')
            if job['input_metadata'].get('prompt_version') != PROMPT_VERSION:
                code='translation_version_changed'; message='Cấu hình dịch đã thay đổi; hãy tạo Job mới.'; return
            project=self.db.select('projects',{'select':'status,active_transcription_job_id','id':eq(job['project_id']),'limit':'1'})
            source=job['input_metadata']['transcription_job_id']
            if not project or project[0]['status'] != 'active' or project[0]['active_transcription_job_id'] != source:
                code='source_changed'; message='Transcript nguồn đã thay đổi; hãy tạo Job mới.'; return
            segments=self.db.select('segments',{'select':'id,start_us,end_us,source_text','project_id':eq(job['project_id']), 'transcription_job_id':eq(source),'order':'position.asc','limit':'501'})
            if not segments or len(segments)>500:
                code='transcript_unavailable'; message='Transcript trống hoặc vượt giới hạn dịch hiện tại.'; return
            keeper._observe_state()
            if keeper.should_cancel() or keeper.lost_event.is_set():
                status='cancelled'; return
            options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {}
            process=subprocess.Popen([sys.executable,'-m','app.workers.translation_process'],cwd=str(BACKEND_ROOT),
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',**options)
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps({'segments':segments,'model':job['input_metadata']['model'],'source_language':job['input_metadata']['source_language']},ensure_ascii=False)+'\n')
            process.stdin.close()
            messages: queue.Queue[str | None]=queue.Queue()
            def read_output() -> None:
                assert process is not None and process.stdout is not None
                for line in process.stdout: messages.put(line)
                messages.put(None)
            threading.Thread(target=read_output,daemon=True).start()
            started=time.monotonic(); result=None
            while True:
                if keeper.should_cancel() or keeper.lost_event.is_set():
                    status='cancelled'; break
                if time.monotonic()-started > 300:
                    code='translation_timeout'; message='Dịch vượt thời gian cho phép; hãy thử lại.'; break
                try: line=messages.get(timeout=0.5)
                except queue.Empty: continue
                if line is None: break
                event=json.loads(line)
                if event.get('event')=='stage': keeper.set_stage(STAGES.get(event.get('stage'),'Đang xử lý bản dịch'))
                elif event.get('event')=='error':
                    code=event.get('code','translation_failed')
                    message={'gemini_quota':'Gemini đang giới hạn quota hoặc tốc độ; hãy thử lại sau.',
                             'gemini_billing':'Thanh toán hoặc số dư Gemini cần được kiểm tra.'}.get(code,event.get('message',message))
                elif event.get('event')=='complete': result=event
            if process.poll() is None: TranscriptionWorker._terminate_process_tree(process)
            if status=='cancelled' or result is None: return
            try:
                validated_source = [SourceSegment(id=uuid.UUID(s['id']), start_us=s['start_us'], end_us=s['end_us'], text=s['source_text']) for s in segments]
                validate_persisted_result(validated_source, result['units'])
            except (KeyError, TypeError, ValueError):
                code='translation_grouping_invalid'; message='Kết quả có nhóm Segment hoặc timestamp không hợp lệ; bản dịch cũ được giữ.'; return
            keeper._observe_state()
            if keeper.should_cancel() or keeper.lost_event.is_set(): status='cancelled'; return
            keeper.set_stage('Đang lưu bản dịch')
            self.db.request('POST','/rest/v1/rpc/complete_translation_job',json={'p_project_id':job['project_id'],'p_job_id':job['id'],'p_worker_id':self.worker_id,'p_units':result['units']},timeout=120)
            logger.info('Translation completed job=%s requests=%s',job['id'],result['usage'].get('requests'))
            status='succeeded'
        except Exception as exc:
            logger.error('Translation failed job=%s error_type=%s',job['id'],type(exc).__name__)
            code='translation_persist_failed' if isinstance(exc,HTTPException) else 'translation_worker_error'
            message='Không thể lưu hoặc xử lý bản dịch; bản cũ được giữ.'
        finally:
            if process and process.poll() is None: TranscriptionWorker._terminate_process_tree(process)
            keeper._observe_state(); keeper.stop()
            if status != 'succeeded' and not keeper.lost_event.is_set():
                if keeper.should_cancel(): status='cancelled'
                try:
                    self.db.request('POST','/rest/v1/rpc/finish_translation_job',json={'p_project_id':job['project_id'],'p_job_id':job['id'],'p_worker_id':self.worker_id,'p_status':status,'p_error_code':code if status=='failed' else None,'p_error_message':message if status=='failed' else None})
                except HTTPException: logger.warning('Could not finish translation job; lease recovery is required')


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    if hasattr(signal, 'SIGBREAK'):
        def stop_on_break(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, stop_on_break)
    try:
        TranslationWorker().run()
    except KeyboardInterrupt:
        logger.info('Translation Worker stopped')
