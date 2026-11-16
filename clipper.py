#!/usr/bin/env python3
"""
Hämtar populära Twitch-klipp, gör om dem till 9:16 Shorts (max 59 s)
och laddar (valfritt) upp dem till YouTube.

Miljövariabler:
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
  STREAMERS            kommaseparerad lista med Twitch-inloggningsnamn (de du har tillstånd från)
  MAX_CLIPS            antal klipp per körning (standard 3)
  UPLOAD               "true" för att ladda upp till YouTube (standard "false")
  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN   (endast vid UPLOAD=true)
  YT_PRIVACY           private | unlisted | public (standard private)

Krav: pip install requests google-api-python-client google-auth yt-dlp
      samt ffmpeg installerat.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

OUT = Path("out")
STATE = Path("processed.json")
MAX_SECONDS = 59


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------- Twitch ----------
def twitch_token() -> str:
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": os.environ["TWITCH_CLIENT_ID"],
            "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def twitch_get(path: str, token: str, params: dict) -> list:
    r = requests.get(
        f"https://api.twitch.tv/helix/{path}",
        headers={
            "Client-Id": os.environ["TWITCH_CLIENT_ID"],
            "Authorization": f"Bearer {token}",
        },
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


def top_clips(token: str, login: str, hours: int = 24, limit: int = 10) -> list:
    users = twitch_get("users", token, {"login": login})
    if not users:
        print(f"Hittade inte streamern {login}", file=sys.stderr)
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips = twitch_get(
        "clips", token,
        {"broadcaster_id": users[0]["id"], "started_at": since, "first": limit},
    )
    return sorted(clips, key=lambda c: c["view_count"], reverse=True)


# ---------- Nedladdning + konvertering ----------
def download(clip: dict) -> Path:
    OUT.mkdir(exist_ok=True)
    raw = OUT / f"{clip['id']}_raw.mp4"
    subprocess.run(["yt-dlp", "-o", str(raw), "--no-playlist", clip["url"]], check=True)
    return raw


def to_short(raw: Path, clip: dict) -> Path:
    final = OUT / f"{clip['id']}.mp4"
    # Suddig bakgrund (9:16) med originalklippet centrerat ovanpå
    vf = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bgb];"
        "[fg]scale=1080:-2[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(raw), "-t", str(MAX_SECONDS),
            "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(final),
        ],
        check=True,
    )
    raw.unlink(missing_ok=True)
    return final


# ---------- YouTube ----------
def youtube_upload(path: Path, clip: dict) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)

    streamer = clip["broadcaster_name"]
    title = f"{clip['title']} - {streamer} #Shorts"[:100]
    description = (
        f"Klipp från {streamer}: https://twitch.tv/{streamer}\n"
        f"Originalklipp: {clip['url']}\n\n#Shorts #{streamer}"
    )
    body = {
        "snippet": {"title": title, "description": description, "categoryId": "20"},
        "status": {
            "privacyStatus": os.environ.get("YT_PRIVACY", "private"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(path), chunksize=-1, resumable=True, mimetype="video/mp4")
    resp = yt.videos().insert(part="snippet,status", body=body, media_body=media).execute()
    return resp["id"]


# ---------- Huvudflöde ----------
def main() -> None:
    streamers = [s.strip() for s in os.environ["STREAMERS"].split(",") if s.strip()]
    max_clips = int(os.environ.get("MAX_CLIPS", "3"))
    do_upload = os.environ.get("UPLOAD", "false").lower() == "true"

    state = load_state()
    token = twitch_token()

    candidates = []
    for login in streamers:
        candidates += [c for c in top_clips(token, login) if c["id"] not in state]
    candidates.sort(key=lambda c: c["view_count"], reverse=True)

    for clip in candidates[:max_clips]:
        print(f"Bearbetar: {clip['broadcaster_name']} - {clip['title']} ({clip['view_count']} visningar)")
        try:
            short = to_short(download(clip), clip)
            entry = {"title": clip["title"], "streamer": clip["broadcaster_name"], "file": short.name}
            if do_upload:
                entry["youtube_id"] = youtube_upload(short, clip)
                print(f"  Uppladdad: https://youtube.com/shorts/{entry['youtube_id']}")
            state[clip["id"]] = entry
        except Exception as e:  # fortsätt med nästa klipp
            print(f"  Misslyckades: {e}", file=sys.stderr)

    save_state(state)


if __name__ == "__main__":
    main()
