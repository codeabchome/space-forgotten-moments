"""ASS karaoke captions built from the Kokoro word timeline."""
from __future__ import annotations

from pathlib import Path

from .tts import Word
from .util import config, log

LOGGER = log("subs")

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Doc,{font},{size},{primary},{highlight},{outline},&H64000000,0,0,0,0,100,100,0,0,1,{ow},1,2,90,90,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _group(words: list[Word], max_chars: int) -> list[list[Word]]:
    """Pack words into caption lines that break on punctuation where possible."""
    lines: list[list[Word]] = []
    current: list[Word] = []
    length = 0

    for w in words:
        add = len(w.text) + (1 if current else 0)
        gap_break = current and (w.start - current[-1].end) > 0.45

        if current and (length + add > max_chars or gap_break):
            lines.append(current)
            current, length = [], 0
            add = len(w.text)

        current.append(w)
        length += add

    if current:
        lines.append(current)
    return lines


def build(words: list[Word], out_path: Path, vertical: bool = False,
          time_offset: float = 0.0) -> Path:
    """Write an .ass file with per-word karaoke highlighting.

    time_offset is SUBTRACTED from every timestamp. Pass a positive value when
    re-timing a clip cut out of the middle of the episode (Shorts); pass a
    negative value to push captions later, e.g. -intro_sec so they start after
    the branded intro card rather than on top of it.
    """
    cfg = config()
    sub = cfg["subtitles"]
    vid = cfg["shorts"] if vertical else cfg["video"]
    max_chars = 24 if vertical else sub["max_chars_per_line"]
    size = int(sub["font_size"] * (1.35 if vertical else 1.0))
    margin_v = int(vid["height"] * (0.34 if vertical else 0.11))

    header = _HEADER.format(
        w=vid["width"], h=vid["height"],
        font=sub["font_name"], size=size,
        primary=sub["primary"], highlight=sub["highlight"],
        outline=sub["outline"], ow=sub["outline_width"], mv=margin_v,
    )

    lines = _group(words, max_chars)
    events = []

    for line in lines:
        start = line[0].start - time_offset
        end = line[-1].end - time_offset
        if end <= 0:
            continue
        start = max(0.0, start)

        # \k durations are in centiseconds; each word lights up as it is spoken
        payload = ""
        for w in line:
            cs = max(1, int(round((w.end - w.start) * 100)))
            payload += f"{{\\k{cs}}}{w.text} "

        events.append(
            f"Dialogue: 0,{_ts(start)},{_ts(end + 0.12)},Doc,,0,0,0,,{payload.strip()}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    LOGGER.info("captions: %d lines → %s", len(events), out_path.name)
    return out_path


def slice_words(words: list[Word], start_sec: float, end_sec: float) -> list[Word]:
    """Words falling inside a time window — used when cutting Shorts."""
    return [w for w in words if w.start >= start_sec - 0.01 and w.end <= end_sec + 0.35]
