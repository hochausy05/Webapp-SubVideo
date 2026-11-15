"""Validation of an atomic translation bulk-edit request."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TranslationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: UUID
    revision: int = Field(ge=0)
    final_text: str = Field(min_length=1, max_length=20000)

    @field_validator("final_text")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("final_text must contain visible text")
        return value


class BulkTranslationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    edits: list[TranslationEdit] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_distinct_units(self) -> "BulkTranslationEdit":
        if len({edit.unit_id for edit in self.edits}) != len(self.edits):
            raise ValueError("each translation unit may appear only once")
        return self
