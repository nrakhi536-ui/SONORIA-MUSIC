"""Saves timed (LRC) lyrics for every catalogue track to instance/lyrics/<slug>.lrc.

The API serves these files first, so synced lyrics work offline and without LRCLIB rate limits.
Sources, in order: LRCLIB /api/get, a search by artist + title, then a search by title alone.
Only lyrics whose recording is within 4 s of the MP3 are saved, so the timings line up.
A file that already exists is left alone (use --force to refetch), which also means you can
drop in your own .lrc file for any song LRCLIB doesn't have.

Usage:  python fetch_lyrics.py [--force]
"""
import argparse
import time

import requests

from app import (LRCLIB_URL, SYNC_DURATION_TOLERANCE, app, local_lyrics_path, save_local_lyrics)
from models import Track

HEADERS = {"User-Agent": "Sonoria/1.0 (music streaming demo)"}
RETRIES = 4


def lrclib(endpoint, params):
    """GET an LRCLIB endpoint, backing off on 429/503. Returns parsed JSON, or None on 404."""
    for attempt in range(RETRIES):
        resp = requests.get(f"{LRCLIB_URL}/{endpoint}", params=params, headers=HEADERS, timeout=15)
        if resp.status_code in (429, 503):
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    raise requests.RequestException(f"LRCLIB kept refusing {endpoint} (rate limited)")


def find_synced(track):
    """The LRCLIB record with synced lyrics closest in length to the track's MP3, or None."""
    duration = (track.duration_ms or 0) / 1000
    artist = track.artist.username

    def fits(record):
        return bool(record and record.get("syncedLyrics")) and (
            not duration or abs((record.get("duration") or 0) - duration) <= SYNC_DURATION_TOLERANCE)

    exact = lrclib("get", {"artist_name": artist, "track_name": track.title, "duration": round(duration)})
    if fits(exact):
        return exact
    for params in ({"artist_name": artist, "track_name": track.title}, {"track_name": track.title}):
        candidates = [r for r in (lrclib("search", params) or []) if fits(r)]
        if candidates:
            return min(candidates, key=lambda r: abs((r.get("duration") or 0) - duration))
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="refetch lyrics that are already saved")
    args = parser.parse_args()

    with app.app_context():
        tracks = Track.query.filter(Track.section.isnot(None)).order_by(Track.title).all()
        saved = 0
        for track in tracks:
            path = local_lyrics_path(track)
            if path.exists() and not args.force:
                status = "already saved"
                saved += 1
            else:
                try:
                    record = find_synced(track)
                except requests.RequestException as exc:
                    status = f"skipped ({exc})"
                else:
                    if record:
                        save_local_lyrics(track, record)
                        status = f"saved ({record['duration']:.0f}s recording)"
                        saved += 1
                    else:
                        status = "no synced lyrics that match this recording"
            print(f"{track.title:<24} {track.artist.username:<30} {status}")
        print(f"\n{saved}/{len(tracks)} catalogue tracks have synced lyrics in {path.parent}")


if __name__ == "__main__":
    main()
