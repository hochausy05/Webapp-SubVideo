"""Bounded worker child: load Supertonic once and synthesize a list sequentially."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

from app.core.config import get_settings
from app.services.tts import VoiceEngine


def emit(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.readline())
        work_dir = Path(payload['work_dir']).resolve()
        if not work_dir.is_dir():
            raise ValueError('Worker scratch directory missing')
        emit({'event':'stage','message':'Đang tải và khởi tạo model Supertonic 3'})
        engine = VoiceEngine(get_settings().tts_model_dir)
        results = []
        for index, unit in enumerate(payload['units']):
            unit_id = str(UUID(unit['id']))
            path = work_dir / (unit_id + '.wav')
            metadata = engine.generate(unit['final_text'], payload['voice'], path, payload['speed'])
            results.append({'unit_id':unit_id, 'path':str(path), **metadata})
            emit({'event':'progress','count':index+1,'total':len(payload['units'])})
        emit({'event':'complete','items':results})
        return 0
    except Exception as exc:
        message = 'Văn bản chứa ký hiệu hoặc thẻ cần xem lại trước khi tạo giọng đọc.' if isinstance(exc, ValueError) and 'review' in str(exc) else f'Tạo giọng đọc thất bại ({type(exc).__name__}).'
        emit({'event':'error','code':'tts_input_review' if isinstance(exc, ValueError) and 'review' in str(exc) else 'tts_inference_failed','message':message})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
