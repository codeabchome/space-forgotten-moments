"""Resumable pipeline state.

Every stage records completion in state.json so a re-run after a runner
timeout picks up where it stopped instead of burning API quota again.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .util import ROOT, config, log, read_json, write_json

LOGGER = log("state")

STAGES = [
    "topic",
    "assets",
    "script",
    "factcheck",
    "voiceover",
    "subtitles",
    "visuals",
    "render",
    "shorts",
    "thumbnail",
    "upload",
]


class State:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or (ROOT / config()["paths"]["state"]))
        self.data: dict[str, Any] = read_json(self.path, default=None) or self._blank()

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {
            "episode_id": None,
            "started_at": None,
            "stages": {},
            "artifacts": {},
            "used_topics": [],
            "published": [],
        }

    # ---------------------------------------------------------------- episode
    def begin_episode(self, episode_id: str) -> None:
        if self.data.get("episode_id") == episode_id:
            LOGGER.info("Resuming episode %s (stages done: %s)",
                        episode_id, ", ".join(self.completed()) or "none")
            return
        LOGGER.info("Starting new episode %s", episode_id)
        self.data["episode_id"] = episode_id
        self.data["started_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.data["stages"] = {}
        self.data["artifacts"] = {}
        self.save()

    def finish_episode(self, video_id: str | None) -> None:
        self.data["published"].append({
            "episode_id": self.data["episode_id"],
            "video_id": video_id,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        for tid in self.data["artifacts"].get("topic_ids", []):
            if tid not in self.data["used_topics"]:
                self.data["used_topics"].append(tid)
        self.data["episode_id"] = None
        self.data["stages"] = {}
        self.data["artifacts"] = {}
        self.save()

    # ---------------------------------------------------------------- stages
    def done(self, stage: str) -> bool:
        return self.data["stages"].get(stage, {}).get("status") == "done"

    def completed(self) -> list[str]:
        return [s for s in STAGES if self.done(s)]

    def mark(self, stage: str, **artifacts: Any) -> None:
        self.data["stages"][stage] = {
            "status": "done",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.data["artifacts"].update(artifacts)
        self.save()
        LOGGER.info("✓ stage complete: %s", stage)

    def fail(self, stage: str, error: str) -> None:
        self.data["stages"][stage] = {
            "status": "failed",
            "error": error[:500],
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.save()

    # ---------------------------------------------------------------- data
    def get(self, key: str, default: Any = None) -> Any:
        return self.data["artifacts"].get(key, default)

    def used_topics(self) -> set[str]:
        return set(self.data.get("used_topics", []))

    def save(self) -> None:
        write_json(self.path, self.data)
