#!/usr/bin/env python3
"""Space's Forgotten Moments — episode pipeline.

Stages are resumable: state.json records what finished, so a re-run after a
runner timeout skips straight to the first incomplete stage.

    python main.py                 # produce and upload one episode
    python main.py --dry-run       # everything except the upload
    python main.py --reset         # discard in-progress episode state
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

import yaml

from src import (discover, factcheck, nasa, render, script_gen, shorts as shorts_mod,
                 subtitles, thumbnail, tts)
from src.nasa import Asset
from src.state import State
from src.tts import Word
from src.util import (ROOT, clean_work, config, log, out_dir, read_json, slugify,
                      split_sentences, word_count, work_dir, write_json)

LOGGER = log("main")


# ---------------------------------------------------------------- topics
def load_topics() -> list[dict]:
    with open(ROOT / "config" / "topics.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["topics"]


def save_topics(topics: list[dict]) -> None:
    with open(ROOT / "config" / "topics.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump({"topics": topics}, fh, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100)


def choose(topics: list[dict], used: set[str]) -> tuple[str, list[dict]]:
    """Pick the next episode: a single topic, or a themed set of pieces."""
    available = [t for t in topics if t.get("status") == "pending" and t["id"] not in used]
    if not available:
        raise RuntimeError("Topic queue empty after replenishment attempt.")

    singles = [t for t in available if t["kind"] == "single"]
    pieces = [t for t in available if t["kind"] == "piece"]

    themes: dict[str, list[dict]] = {}
    for p in pieces:
        themes.setdefault(p.get("theme", "Untitled"), []).append(p)
    ready = {k: v for k, v in themes.items() if len(v) >= 3}

    # alternate formats so the channel doesn't look repetitive
    if ready and (not singles or random.random() < 0.45):
        theme = max(ready, key=lambda k: len(ready[k]))
        chosen = ready[theme][:4]
        LOGGER.info("episode type: compilation — %r (%d pieces)", theme, len(chosen))
        return "compilation", chosen

    chosen = [singles[0]] if singles else [available[0]]
    LOGGER.info("episode type: single — %r", chosen[0]["title"])
    return "single", chosen


# ---------------------------------------------------------------- stages
def stage_assets(st: State, selection: list[dict], kind: str) -> dict[str, list[Asset]]:
    media = work_dir("media")
    per_topic: dict[str, list[Asset]] = {}

    want = 45 if kind == "single" else 16
    for topic in selection:
        assets = nasa.gather(topic["query"], want=want)
        verdict = nasa.classify(assets)
        LOGGER.info("%s → %d assets (%s)", topic["id"], len(assets), verdict)

        if verdict == "insufficient":
            LOGGER.warning("%s has too little archive material — skipping", topic["id"])
            continue

        kept = []
        for a in assets:
            if nasa.download(a, media):
                kept.append(a)
            if len(kept) >= want:
                break
        LOGGER.info("%s → %d assets downloaded", topic["id"], len(kept))
        per_topic[topic["id"]] = kept

    if not per_topic:
        raise RuntimeError("No topic yielded usable archive material.")
    return per_topic


def stage_script(selection: list[dict], per_topic: dict[str, list[Asset]],
                 kind: str) -> tuple[str, str, list[dict], str]:
    """Returns (working_title, narration, chapters, combined_source_context)."""
    if kind == "single":
        topic = selection[0]
        assets = per_topic[topic["id"]]
        ctx = nasa.source_context(assets, limit=30)
        narration = script_gen.write_single(topic["title"], topic["angle"], ctx)
        return topic["title"], narration, [], ctx

    theme = selection[0].get("theme", "Forgotten Missions")
    live = [t for t in selection if t["id"] in per_topic]
    pieces = [{
        "id": t["id"],
        "title": t["title"],
        "angle": t["angle"],
        "source_context": nasa.source_context(per_topic[t["id"]], limit=12),
    } for t in live]

    narration, chapters = script_gen.write_compilation(theme, pieces)
    title = f"{len(pieces)} {theme}"
    ctx = "\n\n".join(p["source_context"] for p in pieces)
    return title, narration, chapters, ctx


def run(dry_run: bool = False) -> int:
    cfg = config()
    st = State()

    # the queue refills itself from the archive — it is a cache of discovered
    # subjects, not a hand-maintained list
    if not st.done("topic"):
        try:
            discover.replenish(floor=8, target=12)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("topic discovery failed (%s) — using existing queue", exc)

    topics = load_topics()

    # ------------------------------------------------ topic
    if st.done("topic"):
        kind = st.get("kind")
        ids = st.get("topic_ids", [])
        selection = [t for t in topics if t["id"] in ids]
    else:
        kind, selection = choose(topics, st.used_topics())
        episode_id = f"{dt.date.today():%Y%m%d}-{slugify(selection[0]['title'], 32)}"
        st.begin_episode(episode_id)
        st.mark("topic", kind=kind, topic_ids=[t["id"] for t in selection],
                working_title=selection[0]["title"])

    LOGGER.info("=== episode %s ===", st.data["episode_id"])

    # ------------------------------------------------ assets
    meta_path = work_dir() / "assets.json"
    if st.done("assets") and meta_path.exists():
        raw = read_json(meta_path)
        per_topic = {k: [Asset(**a) for a in v] for k, v in raw.items()}
        LOGGER.info("resumed %d asset groups", len(per_topic))
    else:
        per_topic = stage_assets(st, selection, kind)
        write_json(meta_path, {k: [a.to_dict() for a in v] for k, v in per_topic.items()})
        st.mark("assets", asset_count=sum(len(v) for v in per_topic.values()))

    all_assets: list[Asset] = [a for group in per_topic.values() for a in group]

    # ------------------------------------------------ script
    script_path = work_dir() / "narration.txt"
    ctx_path = work_dir() / "source_context.txt"
    if st.done("script") and script_path.exists():
        narration = script_path.read_text(encoding="utf-8")
        source_ctx = ctx_path.read_text(encoding="utf-8")
        working_title = st.get("working_title")
        chapters = st.get("chapters", [])
    else:
        working_title, narration, chapters, source_ctx = stage_script(selection, per_topic, kind)
        script_path.write_text(narration, encoding="utf-8")
        ctx_path.write_text(source_ctx, encoding="utf-8")
        st.mark("script", working_title=working_title, chapters=chapters,
                words=word_count(narration))
    LOGGER.info("narration: %d words (~%.1f min)",
                word_count(narration), word_count(narration) / cfg["script"]["wpm"])

    # ------------------------------------------------ fact check
    if not st.done("factcheck"):
        result = factcheck.verify(narration, source_ctx)
        if result["verdict"] == "revised":
            narration = result["narration"]
            script_path.write_text(narration, encoding="utf-8")
        st.mark("factcheck", fact_issues=len(result["issues"]))

    # ------------------------------------------------ voiceover
    wav = work_dir("audio") / "narration.wav"
    words_path = work_dir("audio") / "words.json"
    if st.done("voiceover") and wav.exists() and words_path.exists():
        words = [Word(**w) for w in read_json(words_path)]
        vo_dur = st.get("voice_seconds", 0.0)
    else:
        wav, words, vo_dur = tts.synthesize(narration, wav)
        write_json(words_path, [w.__dict__ for w in words])
        st.mark("voiceover", voice_seconds=vo_dur)

    if vo_dur < cfg["video"]["target_min_sec"]:
        raise RuntimeError(f"Episode too short: {vo_dur:.0f}s. Below the 8-minute threshold, refusing to publish.")


    # ------------------------------------------------ subtitles
    ass = work_dir("subs") / "episode.ass"
    if not (st.done("subtitles") and ass.exists()):
        # narration is delayed by the intro card, so captions must be too
        subtitles.build(words, ass, time_offset=-cfg["brand"]["intro_sec"])
        st.mark("subtitles")

    # ------------------------------------------------ visuals
    scenes_dir = work_dir("scenes")
    if st.done("visuals") and st.get("scene_clips"):
        clips = st.get("scene_clips")
    else:
        digest = "\n".join(f"[{a.nasa_id}] {a.title} ({a.media_type})" for a in all_assets)
        n_scenes = max(6, int(vo_dur / 9))
        plan = script_gen.scene_plan(narration, digest, n_scenes)
        from src.visuals import plan_scenes, render_all
        scenes = plan_scenes(narration, words, all_assets, plan)
        clips = render_all(scenes, scenes_dir)
        st.mark("visuals", scene_clips=clips, scene_count=len(clips))

    # ------------------------------------------------ render
    ep_slug = slugify(working_title, 48)
    episode_path = out_dir("episodes") / f"{ep_slug}.mp4"
    if st.done("render") and episode_path.exists():
        LOGGER.info("resumed rendered episode")
        meta = st.get("metadata", {})
    else:
        bookends = work_dir("bookends")
        intro, outro = render.build_bookends(working_title, bookends)
        body = render.concat(clips, work_dir("scenes") / "body.mp4")
        track = render.concat([str(intro), str(body), str(outro)],
                              work_dir("scenes") / "track.mp4")

        meta = script_gen.metadata(working_title, narration, chapters)

        if chapters:
            marks = render.chapter_timestamps(chapters, words)
            lead = cfg["brand"]["intro_sec"]
            lines = ["\nChapters:", "0:00 Introduction"]
            for m in marks:
                total = sum(int(x) * 60 ** i for i, x in enumerate(reversed(m["time"].split(":"))))
                total += int(lead)
                h, rem = divmod(total, 3600)
                mm, ss = divmod(rem, 60)
                stamp = f"{h}:{mm:02d}:{ss:02d}" if h else f"{mm}:{ss:02d}"
                lines.append(f"{stamp} {m['title']}")
            meta["description"] = meta.get("description", "") + "\n" + "\n".join(lines)

        meta["description"] = (
            meta.get("description", "")
            + "\n\nAll imagery and footage in this video is NASA public domain material, "
              "sourced from the NASA Image and Video Library (images.nasa.gov). "
              "Narration is original and written from NASA's own archive records."
        )

        render.finalize(track, wav, ass, episode_path, lead_in=cfg["brand"]["intro_sec"])
        st.mark("render", episode_path=str(episode_path), metadata=meta)

    # ------------------------------------------------ shorts
    if st.done("shorts") and st.get("short_paths"):
        short_paths = [Path(p) for p in st.get("short_paths")]
        short_picks = st.get("short_picks", [])
    else:
        from src.visuals import _sentence_windows
        windows = _sentence_windows(narration, words)
        short_picks = script_gen.pick_shorts(narration, cfg["shorts"]["count"])
        short_paths = shorts_mod.cut(
            episode_path, words, short_picks, windows,
            cfg["brand"]["intro_sec"], out_dir("shorts"), ep_slug,
        )
        st.mark("shorts", short_paths=[str(p) for p in short_paths],
                short_picks=short_picks)

    # ------------------------------------------------ thumbnail
    thumb = out_dir("thumbnails") / f"{ep_slug}.jpg"
    if not (st.done("thumbnail") and thumb.exists()):
        thumbnail.build(meta.get("title", working_title), all_assets, thumb)
        st.mark("thumbnail", thumbnail_path=str(thumb))

    # ------------------------------------------------ upload
    if dry_run:
        LOGGER.info("dry run — skipping upload")
        LOGGER.info("episode:   %s", episode_path)
        LOGGER.info("shorts:    %s", ", ".join(p.name for p in short_paths))
        LOGGER.info("thumbnail: %s", thumb)
        LOGGER.info("title:     %s", meta.get("title"))
        return 0

    from src import upload
    video_id = upload.upload_video(episode_path, meta,
                                   thumb if thumb.exists() else None)
    if not video_id:
        st.fail("upload", "main episode upload returned no id")
        return 1

    for i, sp in enumerate(short_paths):
        pick = short_picks[i] if i < len(short_picks) else {}
        upload.upload_video(sp, shorts_mod.metadata(meta.get("title", working_title), pick, i))

    # retire the topics we just used
    used_ids = set(st.get("topic_ids", []))
    for t in topics:
        if t["id"] in used_ids:
            t["status"] = "used"
    save_topics(topics)

    st.mark("upload", video_id=video_id)
    st.finish_episode(video_id)
    clean_work()
    LOGGER.info("=== done: https://youtu.be/%s ===", video_id)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build everything, upload nothing")
    ap.add_argument("--reset", action="store_true", help="discard in-progress episode state")
    args = ap.parse_args()

    if args.reset:
        st = State()
        st.data["episode_id"] = None
        st.data["stages"] = {}
        st.data["artifacts"] = {}
        st.save()
        clean_work()
        LOGGER.info("state reset")
        return 0

    try:
        return run(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
