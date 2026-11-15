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
    source_segment_ids: list[str] = Field(min_length=1)
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


def validate_pass1(source: list[SourceSegment], response: Pass1Response) -> list[TranslationUnit]:
    if not source or len({item.id for item in source}) != len(source):
        raise ValueError("source transcript is empty or has duplicate IDs")
    if any(s.end_us <= s.start_us for s in source) or any(a.start_us > b.start_us for a, b in zip(source, source[1:])):
        raise ValueError("source transcript has invalid or unordered timestamps")
    expected = [str(item.id) for item in source]
    actual = [identifier for unit in response.units for identifier in unit.source_segment_ids]
    if actual != expected:
        raise ValueError("pass 1 must partition every source ID exactly once in order")
    result: list[TranslationUnit] = []
    offset = 0
    for unit in response.units:
        group = source[offset:offset + len(unit.source_segment_ids)]
        offset += len(group)
        warnings = [unit.review_reason.strip()] if unit.needs_review and unit.review_reason.strip() else []
        if unit.needs_review and not warnings:
            warnings.append("Model requested review")
        if len(group) > 10 or group[-1].end_us - group[0].start_us > 30_000_000:
            warnings.append("Unusually large source group")
        result.append(TranslationUnit(id=uuid4(), source_segment_ids=[s.id for s in group], start_us=group[0].start_us, end_us=group[-1].end_us, source_text=" ".join(s.text for s in group), translated_text=unit.translated_text, needs_review=bool(warnings), review_reason="; ".join(warnings)))
    return result


def validate_pass2(units: list[TranslationUnit], response: Pass2Response) -> list[Pass2Unit]:
    expected = [str(unit.id) for unit in units]
    actual = [unit.unit_id for unit in response.units]
    if not expected or actual != expected:
        raise ValueError("pass 2 must return each frozen unit ID exactly once in order")
    return response.units
