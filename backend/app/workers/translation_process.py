"""One bounded translation subprocess. JSON Lines stdout, diagnostic stderr."""
from __future__ import annotations

import json
import os
import sys
from uuid import UUID

from dotenv import dotenv_values
from app.core.config import BACKEND_ROOT
from app.services.gemini_translation import GeminiTranslator, TranslationError
from app.services.translation_validation import SourceSegment


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main() -> int:
    translator = None
    try:
        data=json.loads(sys.stdin.readline())
        env={**dotenv_values(BACKEND_ROOT / '.env'), **os.environ}
        source=[SourceSegment(id=UUID(s['id']), start_us=s['start_us'], end_us=s['end_us'], text=s['source_text']) for s in data['segments']]
        translator=GeminiTranslator(env.get('GEMINI_API_KEY') or '', data['model'])
        units, usage=translator.translate(source, data['source_language'], lambda stage: emit({'event':'stage','stage':stage}))
        emit({'event':'complete','units':units,'usage':vars(usage)})
        return 0
    except TranslationError as exc:
        emit({'event':'error','code':exc.code,'message':str(exc),'requests':translator.usage.requests if translator else 0})
        return 1
    except Exception as exc:
        print(f'Translation subprocess failure: {type(exc).__name__}', file=sys.stderr, flush=True)
        emit({'event':'error','code':'translation_internal_error','message':'Không thể xử lý bản dịch.'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
