"""Strict contracts for translation responses; no provider or persistence side effects."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceSegment(StrictModel):
    id: UUID
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    text: str = Field(min_length=1)


class Pass1Unit(StrictModel):
    source_segment_ids: list[str] = Field(min_length=1, max_length=3)
    translated_text: str = Field(min_length=1)
    needs_review: bool
    review_reason: str

    @field_validator("translated_text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("translation cannot be blank")
        return value.strip()


class Pass1Response(StrictModel):
    units: list[Pass1Unit] = Field(min_length=1)


class Pass2Unit(StrictModel):
    unit_id: str = Field(min_length=1)
    optimized_text: str = Field(min_length=1)
    needs_review: bool
    review_reason: str

    @field_validator("optimized_text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        text = value.strip()
        if not text or re.fullmatch(r"(?:n/?a|none|null|error|lỗi|\.{2,}|\[.*?\])", text, re.IGNORECASE):
            raise ValueError("optimized translation is blank or a placeholder")
        return text


class Pass2Response(StrictModel):
    units: list[Pass2Unit] = Field(min_length=1)


class TranslationUnit(StrictModel):
    id: UUID
    source_segment_ids: list[UUID]
    start_us: int
    end_us: int
    source_text: str
    translated_text: str
    needs_review: bool
    review_reason: str


MAX_SEGMENTS_PER_UNIT = 3
SOFT_MAX_DURATION_US = 4_000_000
MAX_PAUSE_US = 750_000  # Provisional boundary heuristic, not a semantic guarantee.


def group_source_segments(source: list[SourceSegment]) -> list[list[SourceSegment]]:
    if not source or len({item.id for item in source}) != len(source):
        raise ValueError('source transcript is empty or has duplicate IDs')
    if any(s.end_us <= s.start_us for s in source) or any(a.start_us > b.start_us for a, b in zip(source, source[1:])):
        raise ValueError('source transcript has invalid or unordered timestamps')
    groups: list[list[SourceSegment]] = []
    for segment in source:
        if groups:
            group = groups[-1]
            previous = group[-1]
            # Only join with explicit continuation punctuation; bare ASR fragments
            # are ambiguous and remain separate rather than guessing meaning.
            continuation = bool(re.search(r'[,，、\-—…]$', previous.text.rstrip()))
            if (continuation and len(group) < MAX_SEGMENTS_PER_UNIT
                    and segment.start_us - previous.end_us <= MAX_PAUSE_US
                    and segment.end_us >= max(s.end_us for s in group)
                    and segment.end_us - group[0].start_us <= SOFT_MAX_DURATION_US):
                group.append(segment)
                continue
        groups.append([segment])
    return groups


def validate_pass1(source: list[SourceSegment], response: Pass1Response, expected_groups: list[list[SourceSegment]] | None = None) -> list[TranslationUnit]:
    if not source or len({item.id for item in source}) != len(source):
        raise ValueError("source transcript is empty or has duplicate IDs")
    if any(s.end_us <= s.start_us for s in source) or any(a.start_us > b.start_us for a, b in zip(source, source[1:])):
        raise ValueError("source transcript has invalid or unordered timestamps")
    expected = [str(item.id) for item in source]
    actual = [identifier for unit in response.units for identifier in unit.source_segment_ids]
    if actual != expected:
        raise ValueError("pass 1 must partition every source ID exactly once in order")
    groups = expected_groups if expected_groups is not None else group_source_segments(source)
    if [[str(s.id) for s in group] for group in groups] != [unit.source_segment_ids for unit in response.units]:
        raise ValueError('pass 1 changed the frozen source groups')
    result: list[TranslationUnit] = []
    offset = 0
    for unit in response.units:
        group = source[offset:offset + len(unit.source_segment_ids)]
        offset += len(group)
        warnings = [unit.review_reason.strip()] if unit.needs_review and unit.review_reason.strip() else []
        if unit.needs_review and not warnings:
            warnings.append("Model requested review")
        if len(group) > MAX_SEGMENTS_PER_UNIT or (len(group) > 1 and group[-1].end_us - group[0].start_us > SOFT_MAX_DURATION_US):
            raise ValueError('source group exceeds the segment or duration limit')
        result.append(TranslationUnit(id=uuid4(), source_segment_ids=[s.id for s in group], start_us=group[0].start_us, end_us=group[-1].end_us, source_text=" ".join(s.text for s in group), translated_text=unit.translated_text, needs_review=bool(warnings), review_reason="; ".join(warnings)))
    return result


def validate_pass2(units: list[TranslationUnit], response: Pass2Response) -> list[Pass2Unit]:
    expected = [str(unit.id) for unit in units]
    actual = [unit.unit_id for unit in response.units]
    if not expected or actual != expected:
        raise ValueError("pass 2 must return each frozen unit ID exactly once in order")
    return response.units


def validate_persisted_result(source: list[SourceSegment], result: list[dict]) -> None:
    """Recheck child-process output immediately before the persistence RPC."""
    groups = group_source_segments(source)
    if len(result) != len(groups):
        raise ValueError('translation unit count differs from frozen groups')
    seen: set[str] = set()
    for unit, group in zip(result, groups):
        uid = str(UUID(unit['id']))
        if uid in seen:
            raise ValueError('duplicate translation unit ID')
        seen.add(uid)
        if (unit['source_segment_ids'] != [str(s.id) for s in group]
                or unit['start_us'] != group[0].start_us or unit['end_us'] != group[-1].end_us
                or not isinstance(unit['translated_text'], str) or not unit['translated_text'].strip()
                or not isinstance(unit['optimized_text'], str) or not unit['optimized_text'].strip()
                or unit.get('final_text') != unit['optimized_text']):
            raise ValueError('translation output changed frozen groups or timestamps')
