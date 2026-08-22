"""Vertical Shorts cut from the finished episode."""
from __future__ import annotations

from pathlib import Path

from .subtitles import build as build_ass, slice_words
from .tts import Word
from .util import ROOT, config, ffmpeg, log, slugify

LOGGER = log("shorts")


def _window(picks: dict, words: list[Word], n_sentences: int,
            sentence_windows: list[tuple[float, float]]) -> tuple[float, float] | None:
    try:
        s_i, e_i = int(picks["start"]), int(picks["end"])
    except (KeyError, TypeError, ValueError):
        return None
    s_i = max(0, min(s_i, n_sentences - 1))
    e_i = max(s_i, min(e_i, n_sentences - 1))
    return sentence_windows[s_i][0], sentence_windows[e_i][1]


def cut(episode: Path, words: list[Word], picks: list[dict],
        sentence_windows: list[tuple[float, float]], lead_in: float,
        dest: Path, title_slug: str) -> list[Path]:
    """Produce vertical clips with re-timed captions."""
    cfg = config()
    sh, v = cfg["shorts"], cfg["video"]
    W, H = sh["width"], sh["height"]
    made: list[Path] = []

    for i, pick in enumerate(picks[: sh["count"]]):
        win = _window(pick, words, len(sentence_windows), sentence_windows)
        if not win:
            continue
        start, end = win
        dur = end - start

        if dur < sh["min_sec"]:
            end = min(start + sh["min_sec"], sentence_windows[-1][1])
            dur = end - start
        if dur > sh["max_sec"]:
            end = start + sh["max_sec"]
            dur = sh["max_sec"]
        if dur < 8:
            continue

        # episode timeline includes the intro card
        cut_start = start + lead_in

        clip_words = slice_words(words, start, end)
        if not clip_words:
            continue
        ass = build_ass(
            clip_words,
            dest / f"short_{i+1}.ass",
            vertical=True,
            time_offset=start,
        )

        out = dest / f"{title_slug}_short_{i+1}.mp4"
        fonts_dir = (ROOT / "assets" / "fonts").resolve()
        ass_esc = str(ass).replace("\\", "/").replace(":", r"\:")

        ffmpeg([
            "-ss", f"{cut_start:.3f}", "-i", str(episode),
            "-t", f"{dur:.3f}",
            "-vf", (
                f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},"
                f"ass='{ass_esc}':fontsdir='{fonts_dir}',"
                f"format=yuv420p"
            ),
            "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            str(out),
        ], timeout=1200)

        made.append(out)
        LOGGER.info("short %d: %.1fs — %s", i + 1, dur, pick.get("hook", ""))

    return made


def metadata(episode_title: str, pick: dict, index: int) -> dict:
    hook = (pick.get("hook") or "Archive").strip().rstrip(".")
    return {
        "title": f"{hook} #Shorts"[:95],
        "description": (
            f"From the episode: {episode_title}\n\n"
            "Footage: NASA public domain archive.\n"
            "#Shorts #NASA #SpaceHistory"
        ),
        "tags": ["nasa", "space history", "shorts", "archive footage",
                 "space documentary", "spaceflight"],
    }
