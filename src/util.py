"""Shared helpers: config, logging, subprocess, retry."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

ROOT = Path(__file__).resolve().parent.parent

_LOG_FMT = "%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt="%H:%M:%S")


def log(name: str) -> logging.Logger:
    return logging.getLogger(name)


_cfg: dict | None = None


def config() -> dict:
    global _cfg
    if _cfg is None:
        with open(ROOT / "config" / "config.yaml", encoding="utf-8") as fh:
            _cfg = yaml.safe_load(fh)
    return _cfg


def work_dir(*parts: str) -> Path:
    p = ROOT / config()["paths"]["work"]
    for part in parts:
        p = p / part
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir(*parts: str) -> Path:
    p = ROOT / config()["paths"]["output"]
    for part in parts:
        p = p / part
    p.mkdir(parents=True, exist_ok=True)
    return p


def env(key: str, required: bool = True) -> str:
    val = os.environ.get(key, "")
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def run(cmd: Sequence[str], timeout: int = 3600, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, raising with captured stderr on failure."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2500:]
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(map(str, cmd[:6]))}…\n{tail}")
    if not quiet and proc.stderr:
        log("run").debug(proc.stderr[-800:])
    return proc


def ffmpeg(args: Sequence[str], timeout: int = 3600) -> None:
    """FFmpeg with the flags that keep GitHub Actions runners from killing us."""
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    run(base + list(args), timeout=timeout)


def ffprobe_duration(path: str | Path) -> float:
    proc = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=120)
    return float(proc.stdout.strip())


def retry(fn: Callable[[], Any], attempts: int = 4, base_delay: float = 2.0, what: str = "operation") -> Any:
    """Exponential backoff. Free API tiers rate-limit constantly."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i)
            log("retry").warning("%s failed (%s/%s): %s — retrying in %.0fs", what, i + 1, attempts, exc, delay)
            time.sleep(delay)
    raise RuntimeError(f"{what} failed after {attempts} attempts") from last


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:max_len]


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(p)  # atomic


def clean_work() -> None:
    p = ROOT / config()["paths"]["work"]
    if p.exists():
        shutil.rmtree(p)


def split_sentences(text: str) -> list[str]:
    """Conservative sentence splitter — protects common abbreviations."""
    protected = text
    for abbr in ["Dr.", "Mr.", "Mrs.", "St.", "Jr.", "vs.", "U.S.", "No.", "Fig.", "approx."]:
        protected = protected.replace(abbr, abbr.replace(".", "\u0001"))
    parts = re.split(r"(?<=[.!?])\s+", protected)
    out = []
    for part in parts:
        s = part.replace("\u0001", ".").strip()
        if s:
            out.append(s)
    return out


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))
