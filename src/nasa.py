"""images.nasa.gov client.

No API key required. Everything returned is NASA public-domain material,
which is the whole sourcing rule for this channel: NASA raw data only,
never a news site's paraphrase.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import requests

from .util import config, log, retry, slugify

LOGGER = log("nasa")
UA = {"User-Agent": "SpaceForgottenMoments/1.0 (archive documentary pipeline)"}
TIMEOUT = 45


@dataclass
class Asset:
    nasa_id: str
    title: str
    description: str
    media_type: str          # image | video
    date_created: str
    center: str
    keywords: list[str] = field(default_factory=list)
    preview: str = ""        # thumbnail url
    href: str = ""           # collection.json url
    local: str = ""          # filled in after download

    def to_dict(self) -> dict:
        return asdict(self)


def _get(url: str, params: dict | None = None) -> dict:
    def _call():
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    return retry(_call, what=f"GET {url.split('/')[-1]}")


def search(query: str, media_types: list[str] | None = None, page_limit: int = 3) -> list[Asset]:
    """Search the NASA library. Returns flattened Asset records."""
    cfg = config()["nasa"]
    media_types = media_types or cfg["media_types"]
    assets: list[Asset] = []

    for page in range(1, page_limit + 1):
        data = _get(f"{cfg['api_base']}/search", {
            "q": query,
            "media_type": ",".join(media_types),
            "page": page,
        })
        items = data.get("collection", {}).get("items", [])
        if not items:
            break

        for item in items:
            meta = (item.get("data") or [{}])[0]
            links = item.get("links") or []
            preview = next((l.get("href", "") for l in links if l.get("rel") == "preview"), "")
            assets.append(Asset(
                nasa_id=meta.get("nasa_id", ""),
                title=(meta.get("title") or "").strip(),
                description=(meta.get("description") or "").strip(),
                media_type=meta.get("media_type", ""),
                date_created=meta.get("date_created", ""),
                center=meta.get("center", ""),
                keywords=meta.get("keywords") or [],
                preview=preview,
                href=item.get("href", ""),
            ))
        time.sleep(0.4)  # be polite to a free public API

    LOGGER.info("search %r → %d assets", query, len(assets))
    return assets


def gather(queries: list[str], want: int = 40) -> list[Asset]:
    """Run queries in order, deduped, until we have enough material."""
    seen: set[str] = set()
    out: list[Asset] = []
    for q in queries:
        for a in search(q):
            if a.nasa_id and a.nasa_id not in seen and a.preview:
                seen.add(a.nasa_id)
                out.append(a)
        if len(out) >= want:
            break
    return out[:want]


def classify(assets: list[Asset]) -> str:
    """single | piece | insufficient — how much episode a topic can carry."""
    cfg = config()["nasa"]
    n = len(assets)
    if n >= cfg["single_topic_min_assets"]:
        return "single"
    if n >= cfg["compilation_min_assets"]:
        return "piece"
    return "insufficient"


def best_media_url(asset: Asset) -> str:
    """Resolve collection.json to the highest-quality usable file."""
    if not asset.href:
        return asset.preview

    try:
        files = _get(asset.href)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("collection fetch failed for %s: %s", asset.nasa_id, exc)
        return asset.preview

    if not isinstance(files, list):
        return asset.preview

    if asset.media_type == "video":
        # prefer mid-size mp4 — 4K originals blow up runner disk and encode time
        for tag in ("~mobile.mp4", "~small.mp4", "~medium.mp4", "orig.mp4", ".mp4"):
            hit = next((f for f in files if isinstance(f, str) and f.endswith(tag)), None)
            if hit:
                return hit
        return ""

    # images: orig is usually a large TIF/JPG; ~large is the sweet spot
    for tag in ("~large.jpg", "~medium.jpg", "~orig.jpg", "~orig.png"):
        hit = next((f for f in files if isinstance(f, str) and f.endswith(tag)), None)
        if hit:
            return hit
    return asset.preview


def download(asset: Asset, dest_dir: Path) -> str | None:
    """Fetch an asset to disk. Returns local path or None."""
    url = best_media_url(asset)
    if not url:
        return None

    ext = ".mp4" if asset.media_type == "video" else ".jpg"
    path = dest_dir / f"{slugify(asset.nasa_id, 50)}{ext}"
    if path.exists() and path.stat().st_size > 8192:
        asset.local = str(path)
        return str(path)

    def _fetch():
        with requests.get(url, headers=UA, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            with open(path, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    fh.write(chunk)

    try:
        retry(_fetch, attempts=3, what=f"download {asset.nasa_id}")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("download failed %s: %s", asset.nasa_id, exc)
        return None

    if path.stat().st_size < 8192:
        path.unlink(missing_ok=True)
        return None

    asset.local = str(path)
    return str(path)


def source_context(assets: list[Asset], limit: int = 30) -> str:
    """Compact factual digest fed to the LLM as its ONLY source material."""
    lines = []
    for a in assets[:limit]:
        desc = " ".join(a.description.split())[:420]
        lines.append(
            f"- [{a.nasa_id}] {a.title}\n"
            f"  date: {a.date_created[:10]} | center: {a.center} | type: {a.media_type}\n"
            f"  description: {desc}"
        )
    return "\n".join(lines)
