"""Kokoro-82M voiceover (Apache 2.0, CPU-only ONNX).

Word timestamps
---------------
kokoro-onnx does not emit word-level timings. Rather than depend on a TTS
engine's timestamp field — which is exactly what broke the edge-tts pipeline —
we synthesise ONE SENTENCE AT A TIME. Each sentence's duration is then measured
from its own audio buffer, which is exact, and words are distributed inside that
window by a syllable-weighted model.

Error therefore cannot accumulate across the episode: every sentence boundary is
a hard resync point. Worst case a word is a few tens of milliseconds off inside
its own sentence, which is invisible in karaoke captions.
"""
from __future__ import annotations

import re
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .util import config, log, split_sentences

LOGGER = log("tts")

_VOWELS = re.compile(r"[aeiouy]+", re.IGNORECASE)
_WORD = re.compile(r"\b[\w'-]+\b")


@dataclass
class Word:
    text: str
    start: float
    end: float


def _syllables(word: str) -> int:
    """Rough English syllable count — good enough for intra-sentence weighting."""
    w = word.lower().strip("'-")
    if not w:
        return 1
    groups = _VOWELS.findall(w)
    n = len(groups)
    if w.endswith("e") and not w.endswith(("le", "ee", "ye")) and n > 1:
        n -= 1
    return max(1, n)


def _weight(word: str) -> float:
    """Time cost of a word: syllables plus a small fixed articulation cost."""
    base = _syllables(word) + 0.45
    if word.endswith((",", ";", ":")):
        base += 0.55          # comma pause
    if word.endswith((".", "!", "?")):
        base += 0.9           # sentence-final pause
    if re.search(r"\d", word):
        base += 1.2           # digits are read out long ("1973" = four syllables)
    return base


def _distribute(sentence: str, t0: float, duration: float) -> list[Word]:
    """Lay a sentence's words across its measured audio window."""
    tokens = sentence.split()
    if not tokens:
        return []

    weights = [_weight(t) for t in tokens]
    total = sum(weights) or 1.0

    words: list[Word] = []
    cursor = t0
    for tok, w in zip(tokens, weights):
        span = duration * (w / total)
        clean = _WORD.findall(tok)
        label = clean[0] if clean else tok
        words.append(Word(text=label, start=round(cursor, 3), end=round(cursor + span, 3)))
        cursor += span
    return words


def _load_engine():
    from kokoro_onnx import Kokoro
    cfg = config()["tts"]
    model, voices = Path(cfg["model_path"]), Path(cfg["voices_path"])
    if not model.exists() or not voices.exists():
        raise RuntimeError(
            f"Kokoro model files missing ({model}, {voices}). "
            "Run `python -m src.tts --fetch` or let the workflow's model cache step handle it."
        )
    return Kokoro(str(model), str(voices))


def synthesize(narration: str, out_wav: Path) -> tuple[Path, list[Word], float]:
    """Render the full narration. Returns (wav path, word timeline, duration)."""
    cfg = config()["tts"]
    kokoro = _load_engine()
    sentences = split_sentences(narration)
    LOGGER.info("synthesising %d sentences with voice=%s speed=%.2f",
                len(sentences), cfg["voice"], cfg["speed"])

    chunks: list[np.ndarray] = []
    timeline: list[Word] = []
    cursor = 0.0
    sample_rate = cfg["sample_rate"]

    # a short breath between sentences keeps documentary pacing from running together
    gap = np.zeros(int(sample_rate * 0.22), dtype=np.float32)

    for i, sentence in enumerate(sentences):
        samples, sr = kokoro.create(
            sentence,
            voice=cfg["voice"],
            speed=cfg["speed"],
            lang=cfg["lang"],
        )
        sample_rate = sr
        samples = np.asarray(samples, dtype=np.float32)
        duration = len(samples) / sr

        timeline.extend(_distribute(sentence, cursor, duration))
        cursor += duration

        chunks.append(samples)
        if i < len(sentences) - 1:
            chunks.append(gap)
            cursor += len(gap) / sr

        if (i + 1) % 25 == 0:
            LOGGER.info("  … %d/%d sentences (%.1fs)", i + 1, len(sentences), cursor)

    audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = (audio / peak) * 0.89          # normalise, leave headroom for the bed

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())

    total = len(audio) / sample_rate
    LOGGER.info("voiceover: %.1fs (%.1f min), %d words timed", total, total / 60, len(timeline))

    if not timeline:
        raise RuntimeError("Voiceover produced no word timestamps — refusing to continue.")

    return out_wav, timeline, total


def fetch_models() -> None:
    """One-time download of Kokoro weights (cached by the workflow)."""
    import requests
    cfg = config()["tts"]
    for url, dest in ((cfg["model_url"], cfg["model_path"]), (cfg["voices_url"], cfg["voices_path"])):
        p = Path(dest)
        if p.exists() and p.stat().st_size > 1 << 20:
            LOGGER.info("already present: %s", p)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("downloading %s → %s", url, p)
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(p, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        LOGGER.info("  %.0f MB", p.stat().st_size / (1 << 20))


if __name__ == "__main__":
    import sys
    if "--fetch" in sys.argv:
        fetch_models()
