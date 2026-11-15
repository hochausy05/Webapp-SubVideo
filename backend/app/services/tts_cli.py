"""Standalone Supertonic test CLI, independent of FastAPI and Supabase."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.core.config import get_settings
from app.services.tts import VoiceEngine, VOICES, SPEEDS


def main() -> None:
    parser = argparse.ArgumentParser(description='Synthesize Vietnamese speech using Supertonic 3 on CPU')
    parser.add_argument('--text', required=True)
    parser.add_argument('--voices', nargs='+', choices=sorted(VOICES), default=['M1'])
    parser.add_argument('--speed', type=float, choices=SPEEDS, default=1.05)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, default=get_settings().tts_model_dir)
    args = parser.parse_args()
    print('Loading Supertonic 3; the first run may download the pinned model.', flush=True)
    start = time.monotonic()
    engine = VoiceEngine(args.model_dir)
    print(json.dumps({'stage': 'model_loaded', 'seconds': round(time.monotonic() - start, 3), 'cache': str(args.model_dir)}), flush=True)
    for voice in args.voices:
        path = args.output_dir / f'{voice}.wav'
        metadata = engine.generate(args.text, voice, path, args.speed)
        print(json.dumps({'stage': 'synthesized', 'path': str(path), **metadata}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
