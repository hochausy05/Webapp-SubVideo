"""Bulk-first Gemini translation. The standalone caller is responsible for process locking."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from app.services.translation_prompts import GLOBAL_POLICY, PASS1, PASS2, PROMPT_VERSION
from app.services.translation_validation import Pass1Response, Pass2Response, SourceSegment, validate_pass1, validate_pass2

logger = logging.getLogger("autodub.translation")


class TranslationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Usage:
    requests: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class GeminiTranslator:
    def __init__(self, api_key: str, model: str, *, max_network_retries: int = 1):
        if not api_key or not model:
            raise TranslationError("gemini_not_configured", "Gemini key or model is not configured.")
        self.model = model
        self.max_network_retries = min(max(max_network_retries, 0), 1)
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta", timeout=120000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.usage = Usage()

    def _call(self, pass_name: str, instruction: str, payload: dict[str, Any], schema: type[Pass1Response] | type[Pass2Response]):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > 150_000 or len(payload.get("segments", payload.get("units", []))) > 500:
            raise TranslationError("context_too_large", "Transcript exceeds the current safe bulk budget; chunking is not yet supported.")
        for attempt in range(self.max_network_retries + 1):
            started = time.monotonic()
            self.usage.requests += 1
            try:
                response = self.client.models.generate_content(
                    model=self.model, contents=body,
                    config=types.GenerateContentConfig(system_instruction=GLOBAL_POLICY + "\n\n" + instruction,
                        response_mime_type="application/json", response_json_schema=schema.model_json_schema(),
                        temperature=0.2, max_output_tokens=16384),
                )
                usage = response.usage_metadata
                if usage:
                    self.usage.input_tokens += usage.prompt_token_count or 0
                    self.usage.output_tokens += usage.candidates_token_count or 0
                if not response.text:
                    raise TranslationError("gemini_empty_response", "Gemini returned an empty response or refused the request.")
                try:
                    parsed = schema.model_validate_json(response.text)
                except ValidationError as exc:
                    raise TranslationError("gemini_invalid_response", "Gemini returned invalid structured output.") from exc
                logger.info("model=%s pass=%s request=%s elapsed=%.2f validation=schema_ok", self.model, pass_name, self.usage.requests, time.monotonic() - started)
                return parsed
            except errors.APIError as exc:
                status = exc.code
                diagnostic = " ".join(str(getattr(exc, "message", "")).split())
                diagnostic = re.sub(r"https?://\S+", "[URL redacted]", diagnostic)
                diagnostic = re.sub(r"(?i)(api[_ -]?key|token|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", diagnostic)
                logger.warning("Gemini API failure pass=%s status=%s provider_status=%s detail=%s", pass_name, status, getattr(exc, "status", None), diagnostic[:400])
                if status == 429:
                    raise TranslationError("gemini_quota", "Gemini quota or rate limit reached; retry manually later.") from exc
                if status == 402:
                    raise TranslationError("gemini_billing", "Gemini billing or prepaid balance requires attention.") from exc
                if status is not None and 500 <= status <= 599 and attempt < self.max_network_retries:
                    self.usage.retries += 1
                    time.sleep(2)
                    continue
                detail = str(getattr(exc, "message", ""))
                if "schema" in detail.lower():
                    raise TranslationError("gemini_schema_rejected", f"Gemini rejected the structured output schema (HTTP {status}).") from exc
                raise TranslationError("gemini_provider_error", f"Gemini request failed (HTTP {status}).") from exc
            except (TimeoutError, ConnectionError) as exc:
                if attempt < self.max_network_retries:
                    self.usage.retries += 1
                    time.sleep(2)
                    continue
                raise TranslationError("gemini_network_error", "Gemini network request failed.") from exc
        raise AssertionError("unreachable")

    def translate(self, source: list[SourceSegment], source_language: str = "zh", progress: Callable[[str], None] = lambda _stage: None) -> tuple[list[dict[str, Any]], Usage]:
        payload = {"source_language": source_language, "target_language": "vi", "segments": [s.model_dump(mode="json") for s in source]}
        progress("translating_pass_1")
        pass1 = self._call("pass1", PASS1, payload, Pass1Response)
        progress("validating_pass_1")
        try:
            units = validate_pass1(source, pass1)
        except ValueError as exc:
            raise TranslationError("pass1_coverage_invalid", "Pass 1 did not cover the source transcript exactly once.") from exc
        payload2 = {"source_language": source_language, "target_language": "vi", "context": payload["segments"], "units": [
            {"unit_id": str(u.id), "source_segment_ids": [str(x) for x in u.source_segment_ids], "source_text": u.source_text,
             "translated_text": u.translated_text, "available_duration_ms": (u.end_us - u.start_us) // 1000} for u in units]}
        progress("optimizing_pass_2")
        pass2 = self._call("pass2", PASS2, payload2, Pass2Response)
        progress("validating_pass_2")
        try:
            optimized = validate_pass2(units, pass2)
        except ValueError as exc:
            raise TranslationError("pass2_coverage_invalid", "Pass 2 did not preserve the validated unit IDs.") from exc
        translated = []
        for unit, result in zip(units, optimized):
            warnings = list(filter(None, [unit.review_reason, (result.review_reason or "Model requested review") if result.needs_review else ""]))
            if re.search(r"[\u3400-\u9fff]", result.optimized_text):
                warnings.append("Chinese characters remain in Vietnamese output")
            source_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", unit.source_text))
            output_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", result.optimized_text))
            if source_numbers and source_numbers != output_numbers:
                warnings.append("Numeric detail requires review")
            if len(result.optimized_text) > max(160, len(unit.translated_text) * 2):
                warnings.append("Unusually long optimized text")
            translated.append({**unit.model_dump(mode="json"), "optimized_text": result.optimized_text, "final_text": result.optimized_text,
                "needs_review": bool(warnings), "review_reason": "; ".join(warnings), "prompt_version": PROMPT_VERSION})
        return translated, self.usage
