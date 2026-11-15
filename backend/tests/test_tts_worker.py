from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.workers.tts_worker import TTSWorker


class Flag:
    def is_set(self) -> bool: return False


class Keeper:
    def __init__(self, *_args: object) -> None:
        self.settings = type('Settings', (), {'asr_lease_seconds': 120})()
        self.lost_event = Flag()
    def start(self) -> None: pass
    def stop(self) -> None: pass
    def set_stage(self, _message: str) -> None: pass
    def _observe_state(self) -> None: pass
    def should_cancel(self) -> bool: return False


class FakeDatabase:
    def __init__(self, unit_id: str) -> None:
        self.unit_id = unit_id
        self.finished: dict | None = None
        self.artifacts: list[dict] = []
    def select(self, table: str, _params: dict) -> list[dict]:
        if table == 'projects': return [{'status':'active','active_translation_run_id':'run-fixture','tts_voice_id':'M1','tts_speed':1.05}]
        if table == 'jobs': return []
        if table == 'translation_units': return [{'id':self.unit_id,'final_text':'Ví dụ','revision':0,'position':0}]
        raise AssertionError(table)
    def insert(self, table: str, row: dict) -> dict:
        if table == 'tts_artifacts': self.artifacts.append(row)
        return row
    def request(self, _method: str, path: str, **kwargs: object) -> object:
        if path.startswith('/storage/v1/object/'):
            raise HTTPException(502, detail={'code':'storage_failed'})
        if path.endswith('/finish_tts_job'):
            self.finished = kwargs['json']  # type: ignore[assignment]
            return object()
        raise AssertionError(path)


class ChildInput(io.StringIO):
    def __init__(self, process: 'FakeProcess') -> None:
        super().__init__(); self.process = process
    def close(self) -> None:
        payload = json.loads(self.getvalue())
        unit = payload['units'][0]
        path = Path(payload['work_dir']) / (unit['id'] + '.wav')
        path.write_bytes(b'R' * 100)
        self.process.stdout = io.StringIO(json.dumps({'event':'complete','items':[{'unit_id':unit['id'], 'sample_rate':44100,'samples':100,'speed':payload['speed']}]}) + '\n')
        super().close()


class FakeProcess:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.stdout = io.StringIO('')
        self.stdin = ChildInput(self)
    def poll(self) -> int: return 0


class TTSWorkerTests(unittest.TestCase):
    def test_cancel_before_inference_does_not_create_artifact(self) -> None:
        class CancelledKeeper(Keeper):
            def should_cancel(self) -> bool: return True
        unit_id = str(uuid.uuid4())
        db = FakeDatabase(unit_id)
        worker = TTSWorker.__new__(TTSWorker)
        worker.db = db  # type: ignore[assignment]
        worker.worker_id = 'worker-fixture'
        job = {'id':str(uuid.uuid4()),'project_id':str(uuid.uuid4()),'input_metadata':{
            'translation_run_id':'run-fixture','voice_id':'M1','units':[[unit_id,0]]}}
        with tempfile.TemporaryDirectory() as root, patch('app.workers.tts_worker.BACKEND_ROOT', Path(root)), \
             patch('app.workers.tts_worker.LeaseKeeper', CancelledKeeper):
            (Path(root) / '.cache' / 'tts').mkdir(parents=True)
            worker.process(job)
        self.assertEqual(db.artifacts, [])
        self.assertEqual(db.finished['p_status'], 'cancelled')  # type: ignore[index]

    def test_storage_failure_keeps_manifest_and_marks_job_failed(self) -> None:
        unit_id = str(uuid.uuid4())
        db = FakeDatabase(unit_id)
        worker = TTSWorker.__new__(TTSWorker)
        worker.db = db  # type: ignore[assignment]
        worker.worker_id = 'worker-fixture'
        job = {'id':str(uuid.uuid4()),'project_id':str(uuid.uuid4()),'input_metadata':{
            'translation_run_id':'run-fixture','voice_id':'M1','units':[[unit_id,0]]}}
        with tempfile.TemporaryDirectory() as root, patch('app.workers.tts_worker.BACKEND_ROOT', Path(root)), \
             patch('app.workers.tts_worker.LeaseKeeper', Keeper), \
             patch('app.workers.tts_worker.subprocess.Popen', FakeProcess):
            (Path(root) / '.cache' / 'tts').mkdir(parents=True)
            worker.process(job)
        self.assertEqual(len(db.artifacts), 1)
        self.assertEqual(db.artifacts[0]['text_snapshot'], 'Ví dụ')
        self.assertEqual(db.finished['p_status'], 'failed')  # type: ignore[index]


if __name__ == '__main__':
    unittest.main()
