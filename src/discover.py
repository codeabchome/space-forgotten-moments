"""Automated topic discovery.

The topic queue is not a hand-written list. This module sweeps the NASA archive
across mission families, centers and eras, clusters what it finds, classifies
each cluster by how much material exists, and has the LLM name the episode and
write its angle. Output is appended to config/topics.yaml.

`main.py` calls `replenish()` whenever the pending queue drops below a floor,
so the channel does not run out of subjects.
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter

import yaml

from . import nasa
from .nasa import Asset
from .script_gen import _chat, _strip_fences
from .util import ROOT, config, log, slugify

LOGGER = log("discover")

# Broad sweeps across the archive. These are search surfaces, not episode
# topics — the specific subject emerges from what actually comes back.
SEED_QUERIES = [
    # programs and mission families
    "Mercury program", "Gemini program", "Apollo program", "Skylab",
    "Apollo Soyuz Test Project", "Space Shuttle program", "Spacelab",
    "Viking program Mars", "Mariner program", "Pioneer program",
    "Voyager program", "Ranger program", "Surveyor program", "Lunar Orbiter",
    "Explorer satellite", "Vanguard satellite", "Nimbus satellite",
    "Landsat", "Seasat", "Magellan Venus", "Galileo Jupiter",
    "Ulysses spacecraft", "Clementine", "Mars Pathfinder", "Deep Space 1",
    "Genesis spacecraft", "Stardust comet", "Deep Impact comet",
    "Phoenix Mars lander", "Dawn spacecraft", "Kepler mission",
    # aeronautics and testing
    "X-15 research aircraft", "X-1 supersonic", "lifting body research",
    "wind tunnel test", "flight research center Dryden", "helicopter research",
    "supersonic transport research", "vertical takeoff aircraft",
    "hypersonic research vehicle", "parawing test", "parachute test",
    "drop test facility", "structural test article", "vibration test",
    "rocket engine test stand", "static fire test", "thermal vacuum chamber",
    "centrifuge test", "zero gravity aircraft", "high altitude balloon",
    # facilities and infrastructure
    "Langley Research Center", "Glenn Research Center", "Ames Research Center",
    "Marshall Space Flight Center", "Stennis Space Center",
    "Jet Propulsion Laboratory", "Kennedy Space Center construction",
    "mission control center", "Deep Space Network", "vehicle assembly building",
    "launch pad construction", "tracking station",
    # people and operations
    "astronaut training", "spacesuit development", "flight simulator",
    "recovery ship splashdown", "quarantine facility", "mission planning",
    # science and instruments
    "sounding rocket", "cosmic ray experiment", "solar observatory",
    "radio astronomy", "earth resources survey", "weather satellite",
    "biosatellite", "plant growth experiment space", "materials processing space",
]

STOPWORDS = {
    "nasa", "space", "center", "flight", "research", "photo", "image", "view",
    "test", "the", "and", "for", "with", "from", "this", "that", "during",
    "shows", "showing", "taken", "photograph", "picture", "left", "right",
    "front", "back", "top", "bottom", "one", "two", "three", "new", "old",
}


def _topics_path():
    return ROOT / "config" / "topics.yaml"


def load_queue() -> list[dict]:
    with open(_topics_path(), encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("topics", []) or []


def save_queue(topics: list[dict]) -> None:
    with open(_topics_path(), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"topics": topics}, fh, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100)


def pending_count(topics: list[dict] | None = None) -> int:
    topics = topics if topics is not None else load_queue()
    return sum(1 for t in topics if t.get("status") == "pending")


def _digest(assets: list[Asset], limit: int = 22) -> str:
    lines = []
    for a in assets[:limit]:
        desc = " ".join(a.description.split())[:300]
        lines.append(f"- [{a.nasa_id}] {a.title} ({a.date_created[:4]}, {a.center})\n  {desc}")
    return "\n".join(lines)


def _year_span(assets: list[Asset]) -> tuple[str, str]:
    years = sorted({a.date_created[:4] for a in assets if a.date_created[:4].isdigit()})
    return (years[0], years[-1]) if years else ("", "")


def _refine_queries(seed: str, assets: list[Asset], n: int = 3) -> list[str]:
    """Derive narrower follow-up queries from what the sweep actually returned."""
    tokens: Counter[str] = Counter()
    for a in assets:
        for w in re.findall(r"\b[A-Za-z][\w-]{3,}\b", f"{a.title} {' '.join(a.keywords)}"):
            lw = w.lower()
            if lw not in STOPWORDS and not lw.isdigit():
                tokens[lw] += 1
    common = [w for w, c in tokens.most_common(14) if c >= 3]
    random.shuffle(common)
    return [seed] + [f"{seed} {w}" for w in common[:n]]


def _propose(seed: str, assets: list[Asset], kind: str) -> dict | None:
    """Ask the model to name the episode, grounded only in the returned metadata."""
    y0, y1 = _year_span(assets)
    span = f"{y0}–{y1}" if y0 and y1 and y0 != y1 else (y0 or "unknown")

    role = (
        "This cluster has enough material for a full 8-10 minute episode."
        if kind == "single" else
        "This cluster has only a few minutes of material. It will be one segment "
        "inside a themed compilation episode, so also propose the theme it belongs to."
    )

    theme_field = '' if kind == "single" else '\n  "theme": "3-5 word theme this segment belongs to, e.g. Machines That Flew Once",'

    raw = _chat([
        {"role": "system", "content": (
            "You return only valid JSON, no prose, no code fences. You propose "
            "documentary episode subjects strictly from supplied NASA archive "
            "metadata. Never invent a mission, date, or fact that is not in the "
            "material given to you."
        )},
        {"role": "user", "content": f"""Propose one documentary subject from this NASA archive cluster.

SWEEP TERM: {seed}
ASSETS FOUND: {len(assets)} ({span})
{role}

ARCHIVE METADATA:
{_digest(assets)}

The subject must be genuinely supported by this metadata — something the
footage actually shows. Prefer the overlooked and the specific over the famous
and the general. If this cluster is too generic to make one coherent episode
about, return {{"skip": true}}.

Return ONLY JSON:
{{
  "title": "episode title, under 60 chars, no clickbait punctuation",{theme_field}
  "angle": "one sentence stating what the episode is actually about",
  "query": ["2-4 images.nasa.gov search terms to gather footage later"]
}}"""},
    ], temperature=0.7, max_tokens=700)

    try:
        prop = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        LOGGER.warning("proposal for %r unparseable", seed)
        return None

    if not isinstance(prop, dict) or prop.get("skip") or not prop.get("title"):
        return None
    if not prop.get("query"):
        prop["query"] = [seed]
    return prop


def _is_duplicate(prop: dict, existing: list[dict]) -> bool:
    """Reject near-identical titles and fully overlapping query sets."""
    new_slug = slugify(prop["title"], 40)
    new_words = set(re.findall(r"\w+", prop["title"].lower())) - STOPWORDS

    for t in existing:
        if slugify(t.get("title", ""), 40) == new_slug:
            return True
        old_words = set(re.findall(r"\w+", t.get("title", "").lower())) - STOPWORDS
        if new_words and old_words:
            overlap = len(new_words & old_words) / len(new_words | old_words)
            if overlap > 0.6:
                return True
    return False


def discover(target: int = 12, max_sweeps: int = 30) -> list[dict]:
    """Sweep the archive and return newly proposed topic entries."""
    cfg = config()["nasa"]
    existing = load_queue()
    used_seeds = {t.get("seed") for t in existing if t.get("seed")}

    pool = [s for s in SEED_QUERIES if s not in used_seeds] or list(SEED_QUERIES)
    random.shuffle(pool)

    found: list[dict] = []
    sweeps = 0

    for seed in pool:
        if len(found) >= target or sweeps >= max_sweeps:
            break
        sweeps += 1

        try:
            assets = nasa.search(seed, page_limit=2)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("sweep %r failed: %s", seed, exc)
            continue

        assets = [a for a in assets if a.nasa_id and a.preview]
        verdict = nasa.classify(assets)
        if verdict == "insufficient":
            LOGGER.info("sweep %-36s → %3d assets — too thin, skipping", seed, len(assets))
            continue

        prop = _propose(seed, assets, verdict)
        if not prop:
            LOGGER.info("sweep %-36s → %3d assets — no coherent subject", seed, len(assets))
            continue

        if _is_duplicate(prop, existing + found):
            LOGGER.info("sweep %-36s → duplicate of an existing topic", seed)
            continue

        entry = {
            "id": f"auto-{slugify(prop['title'], 24)}",
            "kind": "single" if verdict == "single" else "piece",
            "title": prop["title"][:70],
            "angle": prop["angle"],
            "query": _refine_queries(seed, assets)[:4] if verdict == "single" else prop["query"][:4],
            "seed": seed,
            "assets_seen": len(assets),
            "status": "pending",
        }
        if entry["kind"] == "piece":
            entry["theme"] = prop.get("theme") or "Forgotten Hardware"

        found.append(entry)
        LOGGER.info("sweep %-36s → %3d assets — %s [%s]",
                    seed, len(assets), entry["title"], entry["kind"])

    LOGGER.info("discovered %d new topics across %d sweeps", len(found), sweeps)
    return found


def replenish(floor: int = 8, target: int = 12) -> int:
    """Top the queue back up if it has run low. Returns how many were added."""
    topics = load_queue()
    pending = pending_count(topics)
    if pending >= floor:
        LOGGER.info("queue healthy: %d pending", pending)
        return 0

    LOGGER.info("queue low (%d pending) — sweeping the archive", pending)
    new = discover(target=target)
    if not new:
        LOGGER.warning("discovery found nothing new")
        return 0

    save_queue(topics + new)
    LOGGER.info("queue: %d → %d pending", pending, pending + len(new))
    return len(new)


def stats() -> dict:
    topics = load_queue()
    themes: Counter[str] = Counter()
    for t in topics:
        if t.get("kind") == "piece" and t.get("status") == "pending":
            themes[t.get("theme", "?")] += 1

    return {
        "total": len(topics),
        "pending": pending_count(topics),
        "used": sum(1 for t in topics if t.get("status") == "used"),
        "singles_pending": sum(1 for t in topics
                               if t.get("kind") == "single" and t.get("status") == "pending"),
        "pieces_pending": sum(1 for t in topics
                              if t.get("kind") == "piece" and t.get("status") == "pending"),
        "themes_ready": sum(1 for _, c in themes.items() if c >= 3),
        "themes": dict(themes),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="NASA archive topic discovery")
    ap.add_argument("--sweep", type=int, metavar="N", help="discover N new topics and save")
    ap.add_argument("--stats", action="store_true", help="show queue health")
    ap.add_argument("--dry", action="store_true", help="discover but do not write")
    args = ap.parse_args()

    if args.stats or not (args.sweep or args.dry):
        s = stats()
        print(f"\n  total topics     {s['total']}")
        print(f"  pending          {s['pending']}  ({s['singles_pending']} singles, "
              f"{s['pieces_pending']} pieces)")
        print(f"  used             {s['used']}")
        print(f"  themes ready     {s['themes_ready']}")
        for name, count in sorted(s["themes"].items(), key=lambda kv: -kv[1]):
            print(f"    {count:2d}  {name}")
        print()

    if args.sweep or args.dry:
        n = args.sweep or 10
        new = discover(target=n)
        for t in new:
            print(f"  [{t['kind']:6s}] {t['title']}")
        if not args.dry and new:
            save_queue(load_queue() + new)
            print(f"\nsaved {len(new)} topics")
