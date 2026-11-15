import unittest
from uuid import uuid4
from fastapi import HTTPException
from app.api.routes.tts import _translation_for_tts


class Database:
    def __init__(self):
        self.tables = {
            'jobs': [],
            'translation_runs': [{'transcription_job_id':'asr', 'target_language':'vi'}],
            'translation_units': [{'id':'u', 'position':0, 'start_us':0, 'end_us':1000000, 'revision':0, 'final_text':'Xin chào'}],
            'segments': [{'id':'s', 'start_us':0, 'end_us':1000000}],
            'translation_unit_segments': [{'unit_id':'u', 'segment_id':'s', 'position':0}],
        }
    def select(self, table, _params): return self.tables[table]


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.project = {'status':'active','active_transcription_job_id':'asr','active_translation_run_id':'run'}
        self.id = uuid4()

    def test_valid_saved_translation(self):
        self.assertEqual(_translation_for_tts(self.db, self.id, self.project)[0], 'run')

    def test_missing_translation_and_running_translation_rejected(self):
        self.project['active_translation_run_id'] = None
        with self.assertRaises(HTTPException) as err: _translation_for_tts(self.db, self.id, self.project)
        self.assertEqual(err.exception.detail['code'], 'translation_not_ready')
        self.db.tables['jobs'] = [{'id':'job'}]
        with self.assertRaises(HTTPException) as err: _translation_for_tts(self.db, self.id, self.project)
        self.assertEqual(err.exception.detail['code'], 'translation_in_progress')

    def test_missing_mapping_and_blank_text_rejected(self):
        self.db.tables['translation_unit_segments'] = []
        with self.assertRaises(HTTPException) as err: _translation_for_tts(self.db, self.id, self.project)
        self.assertEqual(err.exception.detail['code'], 'translation_invalid')
        self.db.tables['translation_unit_segments'] = [{'unit_id':'u','segment_id':'s','position':0}]
        self.db.tables['translation_units'][0]['final_text'] = '  '
        with self.assertRaises(HTTPException): _translation_for_tts(self.db, self.id, self.project)
