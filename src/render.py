"""Final assembly: intro → scenes → outro, with captions burned in."""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .util import ROOT, config, ffmpeg, ffprobe_duration, log, work_dir

LOGGER = log("render")


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _brand_card(text: str, subtitle: str, dest: Path, W: int, H: int) -> Path:
    """Navy/silver title card reusing the channel's planet motif."""
    b = config()["brand"]
    navy, silver_hi, steel = _hex(b["navy"]), _hex(b["silver_hi"]), _hex(b["steel"])

    img = Image.new("RGB", (W, H), navy)
    d = ImageDraw.Draw(img)

    import random
    rnd = random.Random(4)
    for _ in range(int(W * H / 9000)):
        x, y = rnd.uniform(0, W), rnd.uniform(0, H)
        r = rnd.uniform(0.6, 2.0)
        a = rnd.randint(40, 170)
        d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(int(c * a / 255 + navy[i] * (1 - a / 255))
                                                          for i, c in enumerate(silver_hi)))

    # soft planet limb, lower right
    glow = Image.new("RGB", (W, H), navy)
    gd = ImageDraw.Draw(glow)
    cx, cy, r = W * 0.86, H * 0.62, H * 0.30
    gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(120, 128, 145))
    glow = glow.filter(ImageFilter.GaussianBlur(H * 0.02))
    img = Image.blend(img, glow, 0.5)
    d = ImageDraw.Draw(img)

    def fit(t: str, path: str, start: int, max_w: int) -> ImageFont.FreeTypeFont:
        s = start
        while s > 20:
            f = ImageFont.truetype(str(ROOT / path), s)
            if d.textlength(t, font=f) <= max_w:
                return f
            s -= 2
        return ImageFont.truetype(str(ROOT / path), 20)

    max_w = int(W * 0.62)
    f_title = fit(text, b["font_title"], int(H * 0.10), max_w)
    f_sub = ImageFont.truetype(str(ROOT / b["font_body"]), int(H * 0.032))

    x0 = int(W * 0.09)
    d.text((x0, int(H * 0.42)), text, font=f_title, fill=silver_hi)
    if subtitle:
        d.text((x0 + 2, int(H * 0.42) + f_title.size + int(H * 0.03)),
               subtitle, font=f_sub, fill=steel)

    d.line([(x0, int(H * 0.38)), (x0 + int(W * 0.10), int(H * 0.38))], fill=steel, width=3)

    img.save(dest, "PNG")
    return dest


def _card_clip(png: Path, seconds: float, dest: Path, fade: float = 0.6) -> Path:
    v = config()["video"]
    ffmpeg([
        "-loop", "1", "-i", str(png),
        "-t", f"{seconds:.2f}",
        "-vf", (f"scale={v['width']}:{v['height']},fps={v['fps']},"
                f"fade=t=in:st=0:d={fade},"
                f"fade=t=out:st={max(0, seconds - fade):.2f}:d={fade},"
                f"format=yuv420p"),
        "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
        str(dest),
    ], timeout=300)
    return dest


def build_bookends(title: str, dest: Path) -> tuple[Path, Path]:
    b, v = config()["brand"], config()["video"]
    W, H = v["width"], v["height"]

    intro_png = _brand_card(title, "Space's Forgotten Moments", dest / "intro.png", W, H)
    outro_png = _brand_card("Space's Forgotten Moments",
                            "All footage: NASA public domain archive",
                            dest / "outro.png", W, H)

    intro = _card_clip(intro_png, b["intro_sec"], dest / "intro.mp4")
    outro = _card_clip(outro_png, b["outro_sec"], dest / "outro.mp4")
    return intro, outro


def concat(clips: list[str], dest: Path) -> Path:
    """Stream-copy concat — no re-encode, so this stays fast and cheap."""
    listing = dest.parent / f"{dest.stem}_concat.txt"
    listing.write_text(
        "\n".join(f"file '{Path(c).resolve()}'" for c in clips) + "\n",
        encoding="utf-8",
    )
    ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest)],
           timeout=1800)
    return dest


def finalize(video_track: Path, voice_wav: Path, ass_path: Path, dest: Path,
             bed: Path | None = None, lead_in: float = 0.0) -> Path:
    """Mux visuals + narration (+ optional atmosphere bed) and burn captions."""
    v = config()["video"]
    fonts_dir = (ROOT / "assets" / "fonts").resolve()
    ass = str(ass_path).replace("\\", "/").replace(":", r"\:")

    # captions are timed to the narration, which starts after the intro card
    delay_ms = int(lead_in * 1000)

    # measure the visual track so audio padding is bounded — an unbounded apad
    # buffers forever and gets the runner OOM-killed
    video_dur = ffprobe_duration(video_track)
    pad_to = int(video_dur * 48000) + 48000

    inputs = ["-i", str(video_track), "-i", str(voice_wav)]
    if bed and Path(bed).exists():
        inputs += ["-i", str(bed)]
        audio_filter = (
            f"[1:a]adelay={delay_ms}|{delay_ms},aresample=48000[vo];"
            f"[2:a]volume=0.11,aresample=48000[bed];"
            f"[vo][bed]amix=inputs=2:duration=longest:dropout_transition=3,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,"
            f"apad=whole_len={pad_to}[aout]"
        )
    else:
        audio_filter = (
            f"[1:a]adelay={delay_ms}|{delay_ms},aresample=48000,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,"
            f"apad=whole_len={pad_to}[aout]"
        )

    video_filter = f"[0:v]ass='{ass}':fontsdir='{fonts_dir}'[vout]"

    ffmpeg(inputs + [
        "-filter_complex", f"{video_filter};{audio_filter}",
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", v["audio_bitrate"],
        "-movflags", "+faststart",
        "-shortest",
        str(dest),
    ], timeout=5400)

    LOGGER.info("final video: %s (%.1f min)", dest.name, ffprobe_duration(dest) / 60)
    return dest


def chapter_timestamps(chapters: list[dict], words: list) -> list[dict]:
    """Convert compilation chapter word-offsets into HH:MM:SS marks."""
    out = []
    for ch in chapters:
        idx = min(ch["word_offset"], len(words) - 1)
        t = words[idx].start if words else 0.0
        h, rem = divmod(int(t), 3600)
        m, s = divmod(rem, 60)
        stamp = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        out.append({"title": ch["title"], "time": stamp})
    return out
