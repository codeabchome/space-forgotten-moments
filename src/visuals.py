"""Scene construction: narration → NASA assets → Ken Burns clips."""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from .nasa import Asset
from .tts import Word
from .util import config, ffmpeg, ffprobe_duration, log, split_sentences

LOGGER = log("visuals")


@dataclass
class Scene:
    index: int
    start: float
    end: float
    asset: Asset
    clip: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


def _sentence_windows(narration: str, words: list[Word]) -> list[tuple[float, float]]:
    """Map each sentence onto its (start, end) in the voiceover timeline."""
    sentences = split_sentences(narration)
    windows: list[tuple[float, float]] = []
    cursor = 0

    for sentence in sentences:
        n = len(sentence.split())
        chunk = words[cursor:cursor + n]
        if not chunk:
            break
        windows.append((chunk[0].start, chunk[-1].end))
        cursor += n

    return windows


def plan_scenes(narration: str, words: list[Word], assets: list[Asset],
                llm_plan: list[dict] | None = None) -> list[Scene]:
    """Build the scene list, honouring the LLM's asset matching where valid."""
    cfg = config()["visuals"]
    windows = _sentence_windows(narration, words)
    if not windows:
        raise RuntimeError("Could not align narration sentences to the word timeline.")

    by_id = {a.nasa_id: a for a in assets if a.local}
    usable = [a for a in assets if a.local]
    if not usable:
        raise RuntimeError("No archive assets downloaded — cannot build visuals.")

    total = windows[-1][1]
    scenes: list[Scene] = []

    if llm_plan:
        for i, entry in enumerate(llm_plan):
            try:
                s_i, e_i = int(entry["start"]), int(entry["end"])
            except (KeyError, TypeError, ValueError):
                continue
            s_i = max(0, min(s_i, len(windows) - 1))
            e_i = max(s_i, min(e_i, len(windows) - 1))
            asset = by_id.get(entry.get("nasa_id", ""), usable[i % len(usable)])
            scenes.append(Scene(i, windows[s_i][0], windows[e_i][1], asset))

    if not scenes:
        LOGGER.info("no usable LLM scene plan — splitting evenly across assets")
        target = max(cfg["min_scene_sec"], min(cfg["max_scene_sec"], 9.0))
        n = max(1, int(total / target))
        per = max(1, len(windows) // n)
        for i in range(0, len(windows), per):
            block = windows[i:i + per]
            scenes.append(Scene(len(scenes), block[0][0], block[-1][1],
                                usable[len(scenes) % len(usable)]))

    # enforce contiguity and duration bounds
    scenes.sort(key=lambda s: s.start)
    fixed: list[Scene] = []
    for i, sc in enumerate(scenes):
        sc.index = i
        sc.start = fixed[-1].end if fixed else 0.0
        if i == len(scenes) - 1:
            sc.end = total
        if sc.duration < cfg["min_scene_sec"] and fixed:
            fixed[-1].end = sc.end       # absorb runt scenes into the previous one
            continue
        fixed.append(sc)

    LOGGER.info("planned %d scenes over %.1fs (avg %.1fs)",
                len(fixed), total, total / max(1, len(fixed)))
    return fixed


def render_scene(scene: Scene, dest: Path, seed: int = 0) -> str:
    """Ken Burns for stills, trimmed loop for video assets."""
    cfg = config()
    v, vis = cfg["video"], cfg["visuals"]
    W, H, FPS = v["width"], v["height"], v["fps"]
    dur = max(0.6, scene.duration)
    out = dest / f"scene_{scene.index:04d}.mp4"

    if out.exists() and out.stat().st_size > 4096:
        return str(out)

    src = scene.asset.local

    if scene.asset.media_type == "video":
        try:
            src_dur = ffprobe_duration(src)
        except Exception:  # noqa: BLE001
            src_dur = dur
        # loop the clip if it is shorter than the narration window needs
        loops = max(0, int(dur / max(0.5, src_dur)) + 1)
        ffmpeg([
            "-stream_loop", str(loops), "-i", src,
            "-t", f"{dur:.3f}",
            "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},fps={FPS},format=yuv420p"),
            "-an",
            "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
            str(out),
        ], timeout=900)
        scene.clip = str(out)
        return str(out)

    # ---- still image: slow push with a drifting anchor ----
    rnd = random.Random(seed + scene.index)
    frames = max(2, int(dur * FPS))
    zoom_to = vis["ken_burns_zoom"]
    zoom_in = rnd.random() < 0.72

    if zoom_in:
        z = f"min(1+({zoom_to}-1)*on/{frames},{zoom_to})"
    else:
        z = f"max({zoom_to}-({zoom_to}-1)*on/{frames},1)"

    # drift the crop centre so consecutive scenes don't feel mechanical
    dx, dy = rnd.choice([(0.5, 0.5), (0.38, 0.44), (0.62, 0.46), (0.5, 0.38), (0.46, 0.6)])
    x = f"iw*{dx}-(iw/zoom/2)"
    y = f"ih*{dy}-(ih/zoom/2)"

    # oversample before zoompan, otherwise the push shimmers on fine detail
    ffmpeg([
        "-loop", "1", "-i", src,
        "-t", f"{dur:.3f}",
        "-vf", (
            f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,"
            f"crop={W*2}:{H*2},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
            f"format=yuv420p"
        ),
        "-an",
        "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
        str(out),
    ], timeout=900)

    scene.clip = str(out)
    return str(out)


def render_all(scenes: list[Scene], dest: Path) -> list[str]:
    clips = []
    for sc in scenes:
        try:
            clips.append(render_scene(sc, dest))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("scene %d failed (%s) — substituting neighbour", sc.index, exc)
            if clips:
                clips.append(clips[-1])
        if (sc.index + 1) % 10 == 0:
            LOGGER.info("  … %d/%d scenes rendered", sc.index + 1, len(scenes))
    if not clips:
        raise RuntimeError("Every scene failed to render.")
    return clips
