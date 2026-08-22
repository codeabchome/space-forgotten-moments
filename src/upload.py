"""YouTube Data API v3 upload.

Auth uses a long-lived refresh token stored as a repo secret, so the workflow
never needs an interactive consent screen. Generate it once locally with
`python -m src.upload --authorize`.
"""
from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as build_service
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .util import config, env, log, retry

LOGGER = log("upload")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]


def _client():
    creds = Credentials(
        token=None,
        refresh_token=env("YT_REFRESH_TOKEN"),
        client_id=env("YT_CLIENT_ID"),
        client_secret=env("YT_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build_service("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_video(path: Path, meta: dict, thumbnail: Path | None = None,
                 privacy: str | None = None) -> str | None:
    """Upload one video. Returns the video ID, or None on failure."""
    cfg = config()["upload"]
    yt = _client()

    body = {
        "snippet": {
            "title": meta["title"][:95],
            "description": meta.get("description", "")[:4900],
            "tags": meta.get("tags", [])[:30],
            "categoryId": cfg["category_id"],
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy or cfg["privacy"],
            "selfDeclaredMadeForKids": cfg["made_for_kids"],
            "license": "youtube",
            "embeddable": True,
        },
    }

    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True,
                            mimetype="video/mp4")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                LOGGER.info("  upload %d%%", int(status.progress() * 100))
        except HttpError as exc:
            if exc.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                LOGGER.warning("transient upload error %s — retry %d", exc.resp.status, retries)
                continue
            LOGGER.error("upload failed: %s", exc)
            return None

    video_id = response.get("id")
    LOGGER.info("published: https://youtu.be/%s", video_id)

    if thumbnail and Path(thumbnail).exists():
        try:
            retry(
                lambda: yt.thumbnails().set(
                    videoId=video_id, media_body=MediaFileUpload(str(thumbnail))
                ).execute(),
                attempts=3,
                what="thumbnail set",
            )
            LOGGER.info("thumbnail attached")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("thumbnail upload failed: %s", exc)

    return video_id


def authorize() -> None:
    """Interactive one-time consent. Run locally, then store the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = Path("client_secrets.json")
    if not secrets.exists():
        raise SystemExit(
            "Place your OAuth client_secrets.json (Desktop app type) next to this repo first.\n"
            "Google Cloud Console → APIs & Services → Credentials → Create OAuth client ID."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\nStore these as GitHub repository secrets:\n")
    print(f"  YT_CLIENT_ID      = {creds.client_id}")
    print(f"  YT_CLIENT_SECRET  = {creds.client_secret}")
    print(f"  YT_REFRESH_TOKEN  = {creds.refresh_token}\n")


if __name__ == "__main__":
    import sys
    if "--authorize" in sys.argv:
        authorize()
