from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.services.translation_validation import (
    Pass1Response, Pass1Unit, Pass2Response, Pass2Unit, SourceSegment,
    group_source_segments, validate_pass1, validate_pass2, validate_persisted_result,
)
from app.services.gemini_translation import GeminiTranslator, TranslationError, Usage


def segment(start: int, end: int, text: str) -> SourceSegment:
    return SourceSegment(id=uuid4(), start_us=start, end_us=end, text=text)


def response(groups: list[list[SourceSegment]]) -> Pass1Response:
    return Pass1Response(units=[Pass1Unit(source_segment_ids=[str(s.id) for s in group], translated_text='Bản dịch', needs_review=False, review_reason='') for group in groups])


class GroupingTests(unittest.TestCase):
    def test_seven_consecutive_segments_have_exact_order_and_bounded_groups(self) -> None:
        source = [segment(i * 1_000_000, (i + 1) * 1_000_000, 'tiếp，') for i in range(7)]
        groups = group_source_segments(source)
        self.assertEqual([len(group) for group in groups], [3, 3, 1])
        self.assertEqual([s.id for group in groups for s in group], [s.id for s in source])
        units = validate_pass1(source, response(groups))
        self.assertTrue(all(u.start_us == g[0].start_us and u.end_us == g[-1].end_us for u, g in zip(units, groups)))

    def test_duration_pause_actions_and_single_long_segment(self) -> None:
        source = [segment(0, 2_100_000, 'một，'), segment(2_100_000, 4_200_000, 'hai，'),
                  segment(5_200_001, 6_000_000, 'hành động.'), segment(6_000_000, 11_000_000, 'dài')]
        groups = group_source_segments(source)
        self.assertEqual([len(group) for group in groups], [1, 1, 1, 1])
        self.assertEqual(groups[-1][0].end_us - groups[-1][0].start_us, 5_000_000)
        together = group_source_segments([segment(0, 500_000, 'cùng câu，'), segment(500_000, 1_000_000, 'phần còn lại')])
        self.assertEqual(len(together[0]), 2)

    def test_invalid_provider_group_or_changed_boundaries_cannot_publish(self) -> None:
        source = [segment(i * 1_000_000, (i + 1) * 1_000_000, 'câu.') for i in range(7)]
        with self.assertRaises(ValidationError):
            Pass1Unit(source_segment_ids=[str(s.id) for s in source], translated_text='x', needs_review=False, review_reason='')
        bad = Pass1Response(units=[Pass1Unit(source_segment_ids=[str(s.id) for s in source[:2]], translated_text='x', needs_review=False, review_reason='')] + response([[s] for s in source[2:]]).units)
        with self.assertRaises(ValueError): validate_pass1(source, bad)
        units = validate_pass1(source, response(group_source_segments(source)))
        result = [{**u.model_dump(mode='json'), 'optimized_text':'dịch', 'final_text':'dịch'} for u in units]
        validate_persisted_result(source, result)
        result[0]['end_us'] += 1
        with self.assertRaises(ValueError): validate_persisted_result(source, result)

    def test_pass2_cannot_drop_reorder_or_modify_mapping(self) -> None:
        source = [segment(0, 1_000_000, 'a.'), segment(1_000_000, 2_000_000, 'b.')]
        units = validate_pass1(source, response(group_source_segments(source)))
        with self.assertRaises(ValueError):
            validate_pass2(units, Pass2Response(units=[Pass2Unit(unit_id=str(units[1].id), optimized_text='x', needs_review=False, review_reason='')]))
        with self.assertRaises(ValidationError):
            Pass2Unit(unit_id=str(units[0].id), optimized_text='x', needs_review=False, review_reason='', source_segment_ids=[])

    def test_two_pass_pipeline_uses_full_context_and_frozen_groups(self) -> None:
        source = [segment(i * 1_000_000, (i + 1) * 1_000_000, 'liên tiếp，') for i in range(7)]
        class FakeTranslator(GeminiTranslator):
            def __init__(self) -> None: self.usage = Usage(); self.passes: list[str] = []
            def _call(self, pass_name: str, _instruction: str, payload: dict, _schema: object):
                self.passes.append(pass_name)
                if pass_name == 'pass1':
                    self.assert_context = len(payload['segments'])
                    return Pass1Response(units=[Pass1Unit(source_segment_ids=g['source_segment_ids'], translated_text='dịch', needs_review=False, review_reason='') for g in payload['groups']])
                self.assert_context2 = len(payload['context'])
                return Pass2Response(units=[Pass2Unit(unit_id=u['unit_id'], optimized_text='dịch', needs_review=False, review_reason='') for u in payload['units']])
        translator = FakeTranslator()
        result, _ = translator.translate(source)
        validate_persisted_result(source, result)
        self.assertEqual((translator.assert_context, translator.assert_context2), (7, 7))
        self.assertEqual(translator.passes, ['pass1', 'pass2'])
        self.assertEqual([len(u['source_segment_ids']) for u in result], [3, 3, 1])

    def test_changed_group_stops_before_pass2(self) -> None:
        source = [segment(i * 1_000_000, (i + 1) * 1_000_000, 'câu.') for i in range(3)]
        class BadTranslator(GeminiTranslator):
            def __init__(self) -> None: self.usage = Usage(); self.calls = 0
            def _call(self, *_args: object):
                self.calls += 1
                return response([source])
        translator = BadTranslator()
        with self.assertRaises(TranslationError) as caught: translator.translate(source)
        self.assertEqual(caught.exception.code, 'pass1_grouping_invalid')
        self.assertEqual(translator.calls, 1)


if __name__ == '__main__':
    unittest.main()
