"""Standalone Supertonic 3 CPU synthesis; no Job or storage side effects."""
from __future__ import annotations

import time
import re
import unicodedata
import hashlib
from pathlib import Path

import numpy as np
import soundfile as sf
from supertonic import TTS
from supertonic.config import get_model_revision


VOICES = frozenset(f'{gender}{number}' for gender in ('M', 'F') for number in range(1, 6))
MODEL = 'supertonic-3'
MODEL_REVISION = '724fb5abbf5502583fb520898d45929e62f02c0b'
SPEEDS = (1.05, 1.15, 1.25, 1.35, 1.45)
NORMALIZATION_VERSION = 'whitespace-v1'

def normalize_tts_text(text: str) -> str:
    """Collapse whitespace only; preserve all words, accents and punctuation."""
    return ' '.join(text.split())


class VoiceEngine:
    def __init__(self, model_dir: Path) -> None:
        if get_model_revision(MODEL) != MODEL_REVISION:
            raise RuntimeError('The installed SDK model revision differs from the pinned revision.')
        self.model_dir = model_dir
        self.engine = TTS(model=MODEL, model_dir=model_dir, auto_download=True, intra_op_num_threads=4, inter_op_num_threads=1)
        self.styles: dict[str, object] = {}

    def generate(self, text: str, voice_id: str, destination: Path, speed: float = 1.05) -> dict:
        if voice_id not in VOICES:
            raise ValueError('Unknown voice preset')
        if speed not in SPEEDS:
            raise ValueError('Unsupported speech speed')
        text_for_tts = normalize_tts_text(text)
        if not text_for_tts or len(text) > 20000:
            raise ValueError('Text must contain between 1 and 20000 characters')
        if re.search(r'<\s*/?\s*[A-Za-z][A-Za-z0-9_ -]*\s*>', text):
            raise ValueError('Markup or expression tags require review before speech synthesis')
        if any(char in text for char in ('@', '_', '/', '\\', '|', '#', '→', '←')) or 'e.g.,' in text or 'i.e.,' in text or any(unicodedata.category(char) == 'So' for char in text):
            raise ValueError('Symbols or abbreviations require review before speech synthesis')
        if voice_id not in self.styles:
            self.styles[voice_id] = self.engine.get_voice_style(voice_id)
        start = time.monotonic()
        wave, _ = self.engine.synthesize(text=text_for_tts, voice_style=self.styles[voice_id], lang='vi', speed=speed)
        samples = np.asarray(wave).reshape(-1)
        if samples.size == 0 or not np.all(np.isfinite(samples)) or float(np.max(np.abs(samples))) < 1e-5:
            raise ValueError('Synthesized audio is empty, non-finite, or silent')
        if float(np.max(np.abs(samples))) > 1.0:
            raise ValueError('Synthesized audio would clip in PCM format')
        destination.parent.mkdir(parents=True, exist_ok=True)
        original_samples = samples.size
        # Only trim an exceptionally quiet tail: -80 dBFS, over 0.8 seconds,
        # with 0.4 seconds retained after the last sample above threshold.
        # Preserve the untrimmed PCM file whenever this conservative rule fires.
        audible = np.flatnonzero(np.abs(samples) > 0.0001)
        trim = 0
        if audible.size:
            last = int(audible[-1]) + 1
            if original_samples - last > int(self.engine.sample_rate * 0.8):
                end = min(original_samples, last + int(self.engine.sample_rate * 0.4))
                trim = original_samples - end
                if trim:
                    sf.write(str(destination.with_suffix('.raw.wav')), samples, self.engine.sample_rate, subtype='PCM_16', format='WAV')
                    samples = samples[:end]
        sf.write(str(destination), samples, self.engine.sample_rate, subtype='PCM_16', format='WAV')
        info = sf.info(str(destination))
        if info.frames <= 0 or info.samplerate != self.engine.sample_rate or info.channels != 1:
            raise ValueError('Generated WAV failed validation')
        return {'voice': voice_id, 'model': MODEL, 'model_revision': MODEL_REVISION, 'lang': 'vi',
                'total_steps': 8, 'speed': speed,
                'sample_rate': info.samplerate, 'samples': info.frames, 'duration_seconds': info.frames / info.samplerate,
                'original_samples': original_samples, 'trimmed_samples': trim,
                'processing_metadata': {'tail_threshold_dbfs': -80, 'min_tail_seconds': 0.8, 'retained_tail_seconds': 0.4,
                                        'normalization_version': NORMALIZATION_VERSION, 'text_for_tts': text_for_tts,
                                        'tts_input_sha256': hashlib.sha256(text_for_tts.encode('utf-8')).hexdigest()},
                'inference_seconds': round(time.monotonic() - start, 3), 'peak': float(np.max(np.abs(samples)))}
