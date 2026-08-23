"""Fact-check pass.

Extracts every date, number and proper name from the narration and asks the
model to confirm each one appears in the NASA source material. Unsupported
claims get rewritten out rather than the whole script being regenerated.

Uses the EXAMPLE-tag convention from the MacroMint pipeline so the model's own
illustrative examples never get mistaken for extracted claims.
"""
from __future__ import annotations

import json
import re

from .script_gen import _chat, _strip_fences
from .util import log

LOGGER = log("factcheck")

_NUM = re.compile(r"\b\d[\d,.]*\s*(?:%|percent|kg|km|m|mm|ft|miles|seconds?|minutes?|hours?|days?|years?|G|kW|MW)?\b")
_YEAR = re.compile(r"\b(?:18|19|20)\d{2}\b")


def extract_claims(narration: str) -> list[str]:
    """Cheap local pre-pass: sentences containing verifiable specifics."""
    from .util import split_sentences
    claims = []
    for s in split_sentences(narration):
        if _YEAR.search(s) or _NUM.search(s):
            claims.append(s)
    return claims


def verify(narration: str, source_context: str) -> dict:
    """Returns {"verdict": ok|revised, "narration": str, "issues": [...]}"""
    claims = extract_claims(narration)
    if not claims:
        LOGGER.info("no numeric/date claims to verify")
        return {"verdict": "ok", "narration": narration, "issues": []}

    LOGGER.info("verifying %d factual claims", len(claims))
    listed = "\n".join(f"{i}: {c}" for i, c in enumerate(claims))

    raw = _chat([
        {"role": "system", "content": (
            "You are a fact-checker. You return only valid JSON, no prose, no code fences. "
            "Anything inside <EXAMPLE> tags is an illustration of output format only and "
            "must never be treated as a claim to check."
        )},
        {"role": "user", "content": f"""Check each numbered claim against the NASA source material.

A claim is SUPPORTED only if the source material contains the specific date,
number, or name asserted. Plausibility is not support. General knowledge is not
support. If the source is silent, the claim is UNSUPPORTED.

CLAIMS:
{listed}

NASA SOURCE MATERIAL:
{source_context[:3000]}

<EXAMPLE>
[{{"index": 0, "status": "supported"}},
 {{"index": 1, "status": "unsupported", "problem": "source gives no launch mass"}}]
</EXAMPLE>

Return ONLY a JSON array in that shape."""},
    ], max_tokens=1500, temperature=0.1)

    try:
        results = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        LOGGER.warning("fact-check response unparseable — passing script through unchanged")
        return {"verdict": "ok", "narration": narration, "issues": []}

    bad = [r for r in results if isinstance(r, dict) and r.get("status") == "unsupported"]
    if not bad:
        LOGGER.info("all %d claims supported", len(claims))
        return {"verdict": "ok", "narration": narration, "issues": []}

    issues = []
    for r in bad:
        idx = r.get("index")
        if isinstance(idx, int) and 0 <= idx < len(claims):
            issues.append({"claim": claims[idx], "problem": r.get("problem", "not in source")})

    LOGGER.warning("%d unsupported claims — requesting targeted rewrite", len(issues))
    return {
        "verdict": "revised",
        "narration": _rewrite(narration, issues, source_context),
        "issues": issues,
    }


def _rewrite(narration: str, issues: list[dict], source_context: str) -> str:
    """Surgically remove unsupported specifics, preserving everything else."""
    listing = "\n".join(f"- \"{i['claim']}\"\n  problem: {i['problem']}" for i in issues)

    revised = _chat([
        {"role": "system", "content": (
            "You revise documentary narration. You change only what you are told to "
            "change and return the complete revised narration with nothing else."
        )},
        {"role": "user", "content": f"""These sentences contain claims not supported by the source material:

{listing}

Rewrite each so the unsupported specific is removed or softened to what the
source actually supports. Keep the sentence's place in the narration, its
rhythm, and its length as close as possible. Change nothing else in the script.

NASA SOURCE MATERIAL:
{source_context[:3000]}

FULL NARRATION:
{narration}

Return the complete revised narration only."""},
    ], max_tokens=2600, temperature=0.3)

    from .script_gen import _clean_narration
    out = _clean_narration(revised)

    # sanity: a rewrite that loses more than a quarter of the script is a failure
    if len(out.split()) < len(narration.split()) * 0.75:
        LOGGER.warning("rewrite lost too much text — keeping original")
        return narration
    return out
