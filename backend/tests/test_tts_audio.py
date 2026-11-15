from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from app.services.tts import VoiceEngine, normalize_tts_text


class FakeEngine:
    sample_rate = 44100
    def __init__(self, wave: np.ndarray) -> None: self.wave = wave; self.last_speed = None
    def get_voice_style(self, _voice: str) -> object: return object()
    def synthesize(self, **kwargs: object) -> tuple[np.ndarray, np.ndarray]:
        self.last_speed = kwargs['speed']
        return self.wave, np.array([0.0])


class AudioSafetyTests(unittest.TestCase):
    def make_engine(self, samples: np.ndarray) -> VoiceEngine:
        engine = VoiceEngine.__new__(VoiceEngine)
        engine.engine = FakeEngine(samples)  # type: ignore[assignment]
        engine.styles = {}
        return engine

    def test_long_silent_tail_retains_margin_and_raw_audio(self) -> None:
        rate = 44100
        source = np.concatenate([np.full(rate, 0.2), np.zeros(rate * 2)])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'audio.wav'
            info = self.make_engine(source).generate('Ví dụ', 'M1', out)
            raw, _ = sf.read(out.with_suffix('.raw.wav'))
            result, _ = sf.read(out)
        self.assertEqual(info['original_samples'], len(source))
        self.assertEqual(len(raw), len(source))
        self.assertEqual(len(result), rate + int(rate * 0.4))
        self.assertEqual(info['trimmed_samples'], len(raw) - len(result))
        self.assertTrue(np.allclose(raw[:rate], result[:rate]))

    def test_short_tail_is_left_untouched(self) -> None:
        rate = 44100
        source = np.concatenate([np.full(rate, 0.2), np.zeros(rate // 2)])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'audio.wav'
            info = self.make_engine(source).generate('Ví dụ', 'M1', out)
            self.assertFalse(out.with_suffix('.raw.wav').exists())
        self.assertEqual(info['trimmed_samples'], 0)
        self.assertEqual(info['samples'], len(source))

    def test_empty_silent_and_invalid_voice_are_rejected(self) -> None:
        engine = self.make_engine(np.zeros(44100))
        for text, voice in [('', 'M1'), ('Ví dụ', 'invalid'), ('Ví dụ', 'M1')]:
            with self.assertRaises(ValueError): engine.generate(text, voice, Path('unused.wav'))

    def test_speed_is_forwarded_to_synthesis_and_invalid_speed_rejected(self) -> None:
        engine = self.make_engine(np.full(44100, 0.2))
        with tempfile.TemporaryDirectory() as directory:
            result = engine.generate('Ví dụ', 'M1', Path(directory) / 'speech.wav', 1.35)
            self.assertEqual(engine.engine.last_speed, 1.35)
            self.assertEqual(result['speed'], 1.35)
            with self.assertRaises(ValueError):
                engine.generate('Ví dụ', 'M1', Path(directory) / 'invalid.wav', 1.2)

    def test_normalization_keeps_words_accents_and_punctuation(self) -> None:
        for text, expected in [('  Tôi  là sủi cảo.  ', 'Tôi là sủi cảo.'), ('Bạn ổn chứ?', 'Bạn ổn chứ?'), ('Ồ, tuyệt!', 'Ồ, tuyệt!'), ('Không dấu', 'Không dấu')]:
            self.assertEqual(normalize_tts_text(text), expected)


if __name__ == '__main__':
    unittest.main()
