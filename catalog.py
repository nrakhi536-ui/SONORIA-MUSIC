"""The Sonoria catalogue in the database: 15 songs with their artists, covers and canvas videos.

static/media/catalog.json (written by generate_media.py and committed with the media) is the source of truth.
The app calls ensure_catalog() on startup, so a fresh database - e.g. a new deploy on Render - is filled
automatically. Needs only Flask-SQLAlchemy: none of generate_media.py's rendering libraries.
"""
import json
import logging
import os
import secrets
import time
from pathlib import Path

from models import db, Track, User

ROOT = Path(__file__).resolve().parent
CATALOG_FILE = ROOT / "static" / "media" / "catalog.json"
UNKNOWN_ARTIST = "Unknown Artist"
LOCK_WAIT_SECONDS = 30
LOCK_STALE_SECONDS = 120

log = logging.getLogger("sonoria.catalog")


def exact_case_exists(stored_path, root=ROOT):
    """True when a site path like /static/media/audio/Baby Doll.mp3 names a file with exactly this spelling and case.

    Windows and macOS ignore case, so Path.exists() alone would pass /static/Media/... or baby doll.MP3,
    which then 404 on a case-sensitive Linux host such as Render.
    """
    current = Path(root)
    for part in stored_path.lstrip("/").split("/"):
        if not current.is_dir() or part not in os.listdir(current):
            return False
        current = current / part
    return current.is_file()


def load_catalog(path=CATALOG_FILE):
    """The catalogue entries, or [] if the file is missing or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))["tracks"]
    except (OSError, ValueError, KeyError) as exc:
        log.warning("Catalogue file %s not loaded: %s", path, exc)
        return []


def sync_catalog(entries, root=ROOT):
    """Upserts one approved Track per entry (keyed by audio filename), each owned by an artist User.

    Play counts, likes and playlist links are kept. A song whose audio file is missing is skipped;
    a missing cover or canvas is stored as NULL so the player falls back to its animated cover.
    Returns {"created", "updated", "skipped", "missing_media"}.
    """
    report = {"created": 0, "updated": 0, "skipped": [], "missing_media": []}
    for info in entries:
        if not exact_case_exists(info["stream_url"], root):
            report["skipped"].append(info["stream_url"])
            continue
        media = {}
        for field, key in (("cover_art", "cover"), ("canvas_video", "video_url")):
            path = info.get(key)
            if path and not exact_case_exists(path, root):
                report["missing_media"].append(path)
                path = None
            media[field] = path

        artist_name = info.get("artist") or UNKNOWN_ARTIST
        artist = User.query.filter_by(username=artist_name).first()
        if not artist:
            artist = User(username=artist_name, role="artist")
            artist.set_password(secrets.token_urlsafe(24))  # catalogue account; nobody logs in as it
            db.session.add(artist)
            db.session.flush()

        track = Track.query.filter(Track.section.isnot(None), Track.audio_file == info["file"]).first()
        if track:
            report["updated"] += 1
        else:
            track = Track(play_count=0)
            db.session.add(track)
            report["created"] += 1
        track.title = info["title"]
        track.artist_id = artist.id
        track.album = info.get("album")
        track.genre = info.get("genre") or "Other"
        track.section = info["section"]
        track.stream_url = info["stream_url"]
        track.cover_art = media["cover_art"]
        track.canvas_video = media["canvas_video"]
        track.audio_file = info["file"]
        track.duration_ms = info.get("duration_ms")
        track.approved = True
    db.session.commit()
    return report


class _SeedLock:
    """Cross-platform lock file (atomic create), so only one process at a time sets up the database."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self.path.unlink(missing_ok=True)  # left behind by a crashed process
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Timed out waiting for {self.path}")
                time.sleep(0.2)

    def __exit__(self, *exc):
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


def startup_lock(app):
    """Hold while creating tables and seeding: gunicorn may boot several workers at the same moment."""
    os.makedirs(app.instance_path, exist_ok=True)
    return _SeedLock(Path(app.instance_path) / ".startup.lock")


def ensure_catalog(app):
    """Makes the database match catalog.json (adding any missing songs, fixing changed paths).

    Call inside startup_lock(app) and an app context.
    """
    entries = load_catalog()
    if not entries:
        return None
    report = sync_catalog(entries)
    if report["created"]:
        log.warning("Seeded %d catalogue tracks into the database.", report["created"])
    for path in report["skipped"]:
        log.warning("Catalogue audio missing on disk (song skipped): %s", path)
    for path in report["missing_media"]:
        log.warning("Catalogue media missing on disk (using the animated fallback): %s", path)
    return report


def write_catalog(entries, path=CATALOG_FILE):
    """Saves the catalogue for ensure_catalog(); called by generate_media.py after it resolves every file."""
    fields = ("title", "artist", "album", "genre", "section", "file", "duration_ms", "stream_url", "cover", "video_url")
    data = {"tracks": [{k: e.get(k) for k in fields} for e in entries]}
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
