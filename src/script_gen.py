"""Script generation via Groq (free tier), grounded strictly in NASA metadata."""
from __future__ import annotations

import json
import re

import requests

from .util import config, env, log, retry, split_sentences, word_count

LOGGER = log("script")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM = """You are a documentary scriptwriter for an archive-footage YouTube channel.

ABSOLUTE SOURCING RULE:
The NASA archive metadata provided is your ONLY source. Never introduce a fact,
date, number, name, or quote that does not appear in it. Never paraphrase news
articles, blogs, wikis, or anything from memory. If you are unsure whether a
detail is in the source material, leave it out. A shorter accurate script beats
a longer embellished one.

VOICE:
Measured, factual, quietly compelling. Think a restrained narrator over archive
footage. No hype, no "you won't believe", no conspiracy framing, no rhetorical
questions stacked for drama. Let the material be interesting on its own.

STRUCTURE:
- Open on a concrete image or moment, not a thesis statement.
- Move chronologically unless there is a reason not to.
- Close on what the episode's subject left behind, or what it changed.
- Write in flowing narration. No headings, no bullet points, no stage
  directions, no "[VISUAL:]" markers inside the narration text.
"""


def _chat(messages: list[dict], max_tokens: int | None = None, temperature: float | None = None) -> str:
    """Call Groq, walking the model fallback chain on failure."""
    cfg = config()["script"]
    key = env("GROQ_API_KEY")
    last_err = None

    for model in cfg["models"]:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or cfg["max_tokens"],
            "temperature": cfg["temperature"] if temperature is None else temperature,
        }

        def _call():
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
            if r.status_code == 429:
                raise RuntimeError("rate limited")
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

        try:
            out = retry(_call, attempts=4, base_delay=20.0, what=f"groq:{model}")
            LOGGER.info("script model used: %s", model)
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            LOGGER.warning("model %s unavailable (%s) — falling back", model, exc)

    raise RuntimeError(f"All Groq models failed. Last error: {last_err}")


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    return re.sub(r"\s*```$", "", text).strip()


def write_single(title: str, angle: str, source_context: str) -> str:
    """Full-length narration for a single-topic episode."""
    cfg = config()["script"]
    prompt = f"""Write the narration for one documentary episode.

TITLE: {title}
ANGLE: {angle}

TARGET LENGTH: {cfg['words_min']}-{cfg['words_max']} words. This is a hard
requirement — the episode must run 8 to 10 minutes at a slow narration pace.

NASA ARCHIVE SOURCE MATERIAL (your only permitted source):
{source_context}

Return ONLY the narration text. No title, no preamble, no closing note."""

    text = _chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ])
    return _clean_narration(text)


def write_compilation(theme: str, pieces: list[dict]) -> tuple[str, list[dict]]:
    """Themed compilation. Returns (full narration, chapter list with word offsets)."""
    cfg = config()["script"]
    per_piece = (cfg["words_min"] + cfg["words_max"]) // 2 // max(1, len(pieces))

    segments: list[dict] = []
    parts: list[str] = []

    intro = _chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"Write a 70-90 word cold open for a compilation episode titled "
            f"\"{theme}\". It covers these {len(pieces)} subjects: "
            + "; ".join(p["title"] for p in pieces)
            + ". Establish the common thread. Do not describe any subject in "
              "detail yet. Return only the narration."
        )},
    ], max_tokens=3000)
    parts.append(_clean_narration(intro))

    for i, piece in enumerate(pieces):
        transition = "" if i == 0 else (
            "Begin with a one-sentence transition from the previous subject "
            f"(\"{pieces[i-1]['title']}\") before moving on. "
        )
        body = _chat([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"""{transition}Write a {per_piece}-word segment.

SUBJECT: {piece['title']}
ANGLE: {piece['angle']}

NASA ARCHIVE SOURCE MATERIAL (your only permitted source):
{piece['source_context']}

Return only the narration text."""},
        ], max_tokens=5000)
        body = _clean_narration(body)
        segments.append({"title": piece["title"], "topic_id": piece["id"], "text": body})
        parts.append(body)

    outro = _chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"Write a 50-70 word close for the compilation \"{theme}\". Tie the "
            "subjects together and end on what they collectively represent. "
            "Do not ask viewers to like or subscribe. Return only the narration."
        )},
    ], max_tokens=3000)
    parts.append(_clean_narration(outro))

    full = "\n\n".join(parts)

    # chapter offsets, in words, so render.py can convert to timestamps
    chapters = []
    cursor = word_count(parts[0])
    for seg in segments:
        chapters.append({"title": seg["title"], "word_offset": cursor})
        cursor += word_count(seg["text"])

    return full, chapters


def _clean_narration(text: str) -> str:
    """Strip anything that would get read aloud but shouldn't be."""
    text = _strip_fences(text)
    text = re.sub(r"^\s*(?:#+\s*|\*\*.*?\*\*\s*$)", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(?:VISUAL|SFX|MUSIC|B-ROLL|ARCHIVE)[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def scene_plan(narration: str, asset_digest: str, n_scenes: int) -> list[dict]:
    """Map narration sentences onto archive assets.

    Returns [{sentence_start, sentence_end, nasa_id, reason}, ...]
    """
    sentences = split_sentences(narration)
    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(sentences))

    prompt = f"""Match narration to archive footage.

NARRATION (numbered sentences):
{numbered}

AVAILABLE NASA ASSETS:
{asset_digest}

Divide the narration into exactly {n_scenes} consecutive scenes covering every
sentence with no gaps and no overlaps. Assign each scene the single most
visually appropriate asset. An asset may repeat only if unavoidable.

Return ONLY a JSON array, no other text:
[{{"start": 0, "end": 4, "nasa_id": "...", "reason": "brief"}}, ...]
"""
    raw = _chat([
        {"role": "system", "content": "You return only valid JSON. No prose, no code fences."},
        {"role": "user", "content": prompt},
    ], temperature=0.3)

    try:
        plan = json.loads(_strip_fences(raw))
        if isinstance(plan, list) and plan:
            return plan
    except json.JSONDecodeError:
        LOGGER.warning("scene plan JSON invalid — falling back to even split")
    return []


def pick_shorts(narration: str, n: int = 3) -> list[dict]:
    """Flag the most striking self-contained moments for vertical clips."""
    sentences = split_sentences(narration)
    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(sentences))

    raw = _chat([
        {"role": "system", "content": "You return only valid JSON. No prose, no code fences."},
        {"role": "user", "content": f"""From this narration, pick the {n} most compelling
self-contained moments to cut as vertical short clips.

Each must stand alone without the surrounding episode, run roughly 25-50 seconds
when read aloud (about 60-120 words), and end on a satisfying beat rather than
mid-thought.

NARRATION:
{numbered}

Return ONLY a JSON array:
[{{"start": 3, "end": 9, "hook": "punchy 4-7 word on-screen title"}}, ...]"""},
    ], temperature=0.5)

    try:
        picks = json.loads(_strip_fences(raw))
        return picks if isinstance(picks, list) else []
    except json.JSONDecodeError:
        LOGGER.warning("shorts JSON invalid — using fallback segmentation")
        step = max(1, len(sentences) // (n + 1))
        return [{"start": i * step, "end": min(len(sentences) - 1, i * step + 5),
                 "hook": "Archive"} for i in range(1, n + 1)]


def metadata(title: str, narration: str, chapters: list[dict] | None = None) -> dict:
    """YouTube title / description / tags."""
    raw = _chat([
        {"role": "system", "content": "You return only valid JSON. No prose, no code fences."},
        {"role": "user", "content": f"""Write YouTube metadata for this episode.

WORKING TITLE: {title}
NARRATION (opening):
{narration[:2200]}

Return ONLY JSON:
{{
  "title": "under 70 chars, specific and factual, no clickbait punctuation, no ALL CAPS",
  "description": "2-3 short paragraphs summarising the episode, then a line stating all footage is NASA public domain material",
  "tags": ["12-16 lowercase search terms"]
}}"""},
    ], temperature=0.6)

    try:
        meta = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        meta = {"title": title, "description": "", "tags": []}

    meta.setdefault("title", title)
    meta["title"] = meta["title"][:95]
    meta.setdefault("tags", [])
    meta.setdefault("description", "")
    return meta
