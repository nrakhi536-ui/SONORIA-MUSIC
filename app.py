from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_cors import CORS
from models import db, User, Track, PlayHistory, Like, Playlist, ExternalTrack, UserPlayCount, playlist_tracks, upgrade_schema
from badges import get_user_genre_badge, record_genre_play, GENRE_BADGES
from functools import wraps
from recommend import recommend, dedupe_by_title, weighted_sample, rank_weight
from concurrent.futures import ThreadPoolExecutor
import requests
import feedparser
import os
import uuid
import time
import struct
from collections import Counter
from pathlib import Path
import re as re_module
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-to-something-random"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
# Allow cross-origin calls to the JSON API only (e.g. a frontend served from another local port).
CORS(app, resources={r"/api/*": {"origins": "*"}})

db.init_app(app)
with app.app_context():
    db.create_all()
    upgrade_schema()

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# The database keeps the original role keys; people only ever see these labels.
ROLE_LABELS = {"customer": "User", "artist": "Artist", "admin": "Admin"}
SIGNUP_ROLES = ("customer", "artist")


@app.context_processor
def inject_role_label():
    return {"role_label": lambda role: ROLE_LABELS.get(role, (role or "").title())}


# ---------- iTunes SEARCH API ----------

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
# Home rows backed by iTunes: section name -> search term. "Recommended for You" is personal (/api/recommendations).
HOME_SECTION_TERMS = {"Chill": "Chill", "Top Charts": "Top Hits"}
HOME_ROW_SIZE = {"Chill": 6, "Top Charts": 10}
ITUNES_CACHE_SECONDS = 600


def normalize_itunes_track(item):
    """Maps a raw iTunes API result into the normalized JSON shape the frontend expects."""
    cover = item.get("artworkUrl100") or ""
    return {
        "id": item.get("trackId"),
        "title": item.get("trackName"),
        "artist": item.get("artistName"),
        "album": item.get("collectionName"),
        "cover": cover.replace("/100x100bb.jpg", "/600x600bb.jpg"),
        "stream_url": item.get("previewUrl"),
        "duration": item.get("trackTimeMillis"),
        "genre": item.get("primaryGenreName"),
    }


def query_itunes(term, limit=25):
    """Searches iTunes for songs. Raises requests.RequestException on network or bad-response errors."""
    resp = requests.get(
        ITUNES_SEARCH_URL,
        params={"term": term, "media": "music", "entity": "song", "limit": limit},
        timeout=5,
    )
    resp.raise_for_status()
    try:
        items = resp.json().get("results", [])
    except ValueError as exc:
        raise requests.RequestException("iTunes returned a non-JSON response") from exc
    return [normalize_itunes_track(item) for item in items]


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": [], "error": "Missing query parameter 'q'"}), 400
    try:
        results = query_itunes(query, limit=25)
        return jsonify({"results": results, "error": None})
    except requests.RequestException:
        return jsonify({"results": [], "error": "Could not reach the music search service right now."}), 503


_itunes_cache = {}


def cached_itunes(term, limit=25):
    """query_itunes with a 10-minute cache (home rows, recommendations and autoplay reuse the same searches).

    Returns [] instead of raising, so one slow search never breaks a whole response.
    """
    key = (term.lower(), limit)
    hit = _itunes_cache.get(key)
    if hit and time.monotonic() - hit[0] < ITUNES_CACHE_SECONDS:
        return hit[1]
    try:
        results = query_itunes(term, limit=limit)
    except requests.RequestException:
        return hit[1] if hit else []   # stale beats nothing
    _itunes_cache[key] = (time.monotonic(), results)
    return results


@app.route("/api/songs")
def api_songs():
    """iTunes-backed home rows, each without repeated titles (within or across rows) and freshly shuffled.

    Each row samples from the top 25 results, favouring higher-ranked songs, so it stays relevant but differs
    on every load.
    """
    names = list(HOME_SECTION_TERMS)
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = dict(zip(names, pool.map(lambda n: cached_itunes(HOME_SECTION_TERMS[n]), names)))
    seen, sections = set(), {}
    for name in names:
        items = [t for t in results[name] if t.get("stream_url")]
        shuffled = weighted_sample(items, [rank_weight(i) for i in range(len(items))], len(items))
        sections[name] = dedupe_by_title(shuffled, seen)[:HOME_ROW_SIZE[name]]
    return jsonify({"sections": sections})


@app.route("/api/recommendations")
def api_recommendations():
    """Personal picks for the home row and for autoplay.

    Query: limit (<=30), recent (comma genres, newest first), seed (id of the playing track), seed_genre,
    seed_title, exclude (comma ids), exclude_title (repeatable). Signed-in listeners also get their stored
    plays and likes counted. Returns {tracks, genres, reason}.
    """
    args = request.args
    limit = max(1, min(30, args.get("limit", default=12, type=int)))
    recent = [g.strip() for g in args.get("recent", "").split(",") if g.strip()]
    exclude = [i.strip() for i in args.get("exclude", "").split(",") if i.strip()]
    seed = None
    if args.get("seed"):
        seed_track = playable_track(args["seed"]) if args["seed"].startswith("local-") else None
        seed = seed_track.to_dict() if seed_track else {
            "id": args["seed"], "genre": args.get("seed_genre"), "title": args.get("seed_title")}
    tracks, genres = recommend(
        user_id=current_user.id if current_user.is_authenticated else None,
        recent_genres=recent, seed=seed, exclude_ids=exclude, exclude_titles=args.getlist("exclude_title"),
        limit=limit, itunes_search=cached_itunes)
    if seed:
        reason = f"More like “{seed.get('title') or 'this song'}”"
    elif genres:
        reason = "Because you listen to " + " & ".join(genres[:2])
    else:
        reason = "Popular on Sonoria right now"
    return jsonify({"tracks": tracks, "genres": genres, "reason": reason})


# Local catalogue sections (Track.section) and the headings the SPA shows for them, in page order.
LOCAL_SECTIONS = {
    "trending": "Trending Now",
    "viral": "Viral on Reels & Shorts",
    "artist": "Artists",
}


@app.route("/api/home_sections")
def api_home_sections():
    """Locally hosted tracks (full MP3 + cover + Canvas loop), grouped by home-page section.

    Each request returns every section in a fresh random order, so the home feed reshuffles on refresh.
    """
    tracks = (Track.query.filter(Track.approved.is_(True), Track.section.in_(LOCAL_SECTIONS))
              .order_by(db.func.random()).all())
    grouped = {key: [] for key in LOCAL_SECTIONS}
    for track in tracks:
        grouped[track.section].append(track.to_dict())
    return jsonify({
        "sections": [{"key": key, "title": title, "tracks": grouped[key]} for key, title in LOCAL_SECTIONS.items()],
    })


# ---------- PLAYLISTS + GENRE BADGE API ----------

def api_login_required(view):
    """Like login_required, but answers API calls with 401 JSON instead of redirecting to /login."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Log in to use playlists and badges."}), 401
        return view(*args, **kwargs)
    return wrapper


PLAYLIST_NAME_MAX = 100
CATALOGUE_ONLY = "Only Sonoria catalogue tracks can be added to playlists."


def parse_local_track_id(value):
    """Accepts a catalogue track id as the SPA sends it ("local-3") or as a plain int; None if invalid."""
    if isinstance(value, str) and value.startswith("local-"):
        value = value[len("local-"):]
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def playable_track(value):
    """The approved, streamable catalogue Track for an id from the client, or None."""
    track_id = parse_local_track_id(value)
    track = db.session.get(Track, track_id) if track_id is not None else None
    return track if track and track.approved and track.stream_url else None


def own_playlist(playlist_id):
    """The current user's playlist, or None (someone else's playlist is treated as missing)."""
    playlist = db.session.get(Playlist, playlist_id)
    return playlist if playlist and playlist.user_id == current_user.id else None


def add_to_playlist(playlist, track):
    """Adds track unless it is already there; returns True when it was added."""
    if track in playlist.tracks:
        return False
    db.session.execute(playlist_tracks.insert().values(playlist_id=playlist.id, track_id=track.id))
    db.session.expire(playlist, ["tracks"])
    return True


@app.route("/api/playlists")
@api_login_required
def api_list_playlists():
    """The user's playlists, newest first. ?track_id=local-3 also flags which ones already hold that track."""
    playlists = (Playlist.query.filter_by(user_id=current_user.id)
                 .order_by(Playlist.created_at.desc(), Playlist.id.desc()).all())
    check_id = parse_local_track_id(request.args.get("track_id"))
    result = []
    for playlist in playlists:
        item = playlist.to_dict()
        if check_id is not None:
            item["contains_track"] = any(t.id == check_id for t in playlist.tracks)
        result.append(item)
    return jsonify({"playlists": result})


@app.route("/api/playlists", methods=["POST"])
@api_login_required
def api_create_playlist():
    """Creates {"name": ...}; an optional "track_id" is added straight away (create-and-add from the modal)."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Give the playlist a name."}), 400
    if len(name) > PLAYLIST_NAME_MAX:
        return jsonify({"error": f"Playlist names can be at most {PLAYLIST_NAME_MAX} characters."}), 400

    track = None
    if data.get("track_id") is not None:
        track = playable_track(data["track_id"])
        if not track:
            return jsonify({"error": CATALOGUE_ONLY}), 400

    playlist = Playlist(name=name, user_id=current_user.id)
    db.session.add(playlist)
    db.session.flush()
    if track:
        add_to_playlist(playlist, track)
    db.session.commit()
    return jsonify({"playlist": playlist.to_dict(), "added": bool(track)}), 201


@app.route("/api/playlists/<int:playlist_id>")
@api_login_required
def api_get_playlist(playlist_id):
    playlist = own_playlist(playlist_id)
    if not playlist:
        return jsonify({"error": "Playlist not found."}), 404
    return jsonify({**playlist.to_dict(), "tracks": [t.to_dict() for t in playlist.tracks]})


@app.route("/api/playlists/<int:playlist_id>/tracks", methods=["POST"])
@api_login_required
def api_add_playlist_track(playlist_id):
    """Adds {"track_id": "local-3"}; 201 when added, 200 with added=false when it was already there."""
    playlist = own_playlist(playlist_id)
    if not playlist:
        return jsonify({"error": "Playlist not found."}), 404
    track = playable_track((request.get_json(silent=True) or {}).get("track_id"))
    if not track:
        return jsonify({"error": CATALOGUE_ONLY}), 400

    added = add_to_playlist(playlist, track)
    db.session.commit()
    return jsonify({"playlist": playlist.to_dict(), "added": added}), 201 if added else 200


@app.route("/api/track/play", methods=["POST"])
def api_track_play():
    """Records a play: {"track_id": "local-3"} for catalogue tracks, or {"track_id": <iTunes id>, "genre": ...}.

    Catalogue plays bump the track's play count for everyone; signed-in users also get the play added
    to their genre stats and receive their (possibly new) badge. Guests get {"recorded": false}.
    """
    data = request.get_json(silent=True) or {}
    raw_id = data.get("track_id")
    if raw_id is None:
        return jsonify({"error": "Missing track_id."}), 400

    track = None
    if isinstance(raw_id, str) and raw_id.startswith("local-"):
        track = playable_track(raw_id)
        if not track:
            return jsonify({"error": "Track not found."}), 404
        track.play_count = (track.play_count or 0) + 1

    recorded = current_user.is_authenticated
    if recorded:
        genre = track.genre if track else str(data.get("genre") or "")[:100]
        record_genre_play(current_user.id, genre)
        if track:
            db.session.add(PlayHistory(user_id=current_user.id, track_id=track.id))
    db.session.commit()
    return jsonify({"recorded": recorded, "badge": get_user_genre_badge(current_user.id) if recorded else None})


@app.route("/api/user/badge")
@api_login_required
def api_user_badge():
    """{"badge": {"badge", "genre", "plays", "total_plays"}} or {"badge": null} before the first play."""
    return jsonify({"badge": get_user_genre_badge(current_user.id)})


# ---------- LIKES (signed-in accounts; guests keep theirs in localStorage) ----------

ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"


def external_track_for(itunes_id):
    """The cached ExternalTrack for an iTunes song, looked up on iTunes the first time (never trusted from the client)."""
    external_id = f"itunes:{itunes_id}"
    cached = db.session.get(ExternalTrack, external_id)
    if cached:
        return cached
    resp = requests.get(ITUNES_LOOKUP_URL, params={"id": itunes_id, "entity": "song"}, timeout=5)
    resp.raise_for_status()
    songs = [r for r in resp.json().get("results", []) if r.get("kind") == "song"]
    if not songs:
        return None
    data = normalize_itunes_track(songs[0])
    cached = ExternalTrack(external_id=external_id, title=data["title"] or "Untitled", artist=data["artist"],
                           album=data["album"], cover=data["cover"], stream_url=data["stream_url"],
                           duration_ms=data["duration"], genre=data["genre"])
    db.session.add(cached)
    return cached


def like_filter(raw_id):
    """Like.query filter kwargs for a client track id ("local-3" or an iTunes id), or None if it's malformed."""
    if isinstance(raw_id, str) and raw_id.startswith("local-"):
        track_id = parse_local_track_id(raw_id)
        return {"track_id": track_id} if track_id is not None else None
    itunes_id = str(raw_id)
    return {"external_id": f"itunes:{itunes_id}"} if itunes_id.isdigit() else None


def liked_track_dict(like):
    if like.track:
        return like.track.to_dict()
    return like.external_track.to_dict() if like.external_track else None


@app.route("/api/likes")
@api_login_required
def api_list_likes():
    """The signed-in account's liked songs, most recent first, in the normalized track shape."""
    likes = Like.query.filter_by(user_id=current_user.id).order_by(Like.liked_at.desc(), Like.id.desc()).all()
    return jsonify({"tracks": [t for t in map(liked_track_dict, likes) if t]})


@app.route("/api/likes", methods=["POST"])
@api_login_required
def api_add_like():
    """Likes {"track_id": "local-3"} or {"track_id": <iTunes id>}; idempotent."""
    raw_id = (request.get_json(silent=True) or {}).get("track_id")
    where = like_filter(raw_id)
    if not where:
        return jsonify({"error": "Unknown track."}), 400
    existing = Like.query.filter_by(user_id=current_user.id, **where).first()
    if existing:
        return jsonify({"track": liked_track_dict(existing), "liked": True})

    if "track_id" in where:
        track = playable_track(raw_id)
        if not track:
            return jsonify({"error": "Track not found."}), 404
        like = Like(user_id=current_user.id, track_id=track.id)
    else:
        try:
            external = external_track_for(str(raw_id))
        except (requests.RequestException, ValueError):
            return jsonify({"error": "Could not reach the music service to save this like."}), 503
        if not external:
            return jsonify({"error": "Track not found."}), 404
        like = Like(user_id=current_user.id, external_id=external.external_id)
    db.session.add(like)
    db.session.commit()
    return jsonify({"track": liked_track_dict(like), "liked": True}), 201


@app.route("/api/likes/<track_id>", methods=["DELETE"])
@api_login_required
def api_remove_like(track_id):
    where = like_filter(track_id)
    if not where:
        return jsonify({"error": "Unknown track."}), 400
    Like.query.filter_by(user_id=current_user.id, **where).delete()
    db.session.commit()
    return jsonify({"liked": False})


LRCLIB_URL = "https://lrclib.net/api"
LRC_TIMESTAMP = re_module.compile(r"^\s*(\[\d+:\d+(?:\.\d+)?\])+\s*")
LRC_TAG = re_module.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
SYNC_DURATION_TOLERANCE = 4  # seconds; a longer or shorter recording would put every line at the wrong time
LRC_ID_TAG = re_module.compile(r"^\s*\[[a-z]+:.*\]\s*$", re_module.IGNORECASE)  # [ar: ...], [ti: ...] headers
# Timed lyrics for catalogue tracks: <slug>.lrc, where slug comes from the audio filename.
# Files you add here win over LRCLIB; synced LRCLIB matches are saved here too, so they work offline.
LYRICS_DIR = Path(app.instance_path) / "lyrics"


def track_slug(track):
    stem = Path(track.audio_file or track.title or "").stem
    return re_module.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def local_lyrics_path(track):
    return LYRICS_DIR / f"{track_slug(track)}.lrc"


def read_local_lyrics(track):
    """{lines, synced, instrumental, source} from the track's .lrc file, or None if there isn't one."""
    path = local_lyrics_path(track)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    synced = parse_synced_lyrics(text)
    if synced:
        lines = [line["text"] for line in synced]
    else:  # an untimed file still shows as plain lyrics
        lines = [line.rstrip() for line in text.splitlines() if not LRC_ID_TAG.match(line)]
    return {"lines": lines, "synced": synced or None, "instrumental": False, "source": "local"}


def save_local_lyrics(track, record):
    """Caches an LRCLIB record's timed lyrics as the track's .lrc file."""
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    minutes, seconds = divmod(int(record.get("duration") or 0), 60)
    header = [f"[ar: {track.artist.username}]", f"[ti: {track.title}]", f"[length: {minutes:02d}:{seconds:02d}]",
              f"[re: LRCLIB #{record.get('id', '')}]", ""]
    local_lyrics_path(track).write_text("\n".join(header) + record["syncedLyrics"].strip() + "\n", encoding="utf-8")


def parse_synced_lyrics(lrc):
    """LRC text -> [{time (s), text}] sorted by time. A line may carry several timestamps; empty text marks a break."""
    synced = []
    for raw in lrc.splitlines():
        stamps = LRC_TAG.findall(raw)
        if not stamps:
            continue
        text = LRC_TAG.sub("", raw).strip()
        for minutes, seconds in stamps:
            synced.append({"time": round(int(minutes) * 60 + float(seconds), 2), "text": text})
    return sorted(synced, key=lambda line: line["time"])


def lyrics_payload(record, duration=None):
    """Turns an LRCLIB record into {lines, synced, instrumental}.

    lines is plain text (for display without timing); synced is [{time, text}] when LRCLIB has
    timestamps and, if the caller gave a duration, the record is the same length recording.
    """
    text = record.get("plainLyrics") or record.get("syncedLyrics") or ""
    lines = [LRC_TIMESTAMP.sub("", line).rstrip() for line in text.splitlines()]
    synced = parse_synced_lyrics(record.get("syncedLyrics") or "") or None
    record_duration = record.get("duration")
    if synced and duration and record_duration and abs(record_duration - duration) > SYNC_DURATION_TOLERANCE:
        synced = None
    return {"lines": lines, "synced": synced, "instrumental": bool(record.get("instrumental"))}


def best_lyrics_match(results, duration=None):
    """Prefers records with synced lyrics, then the one whose length is closest to the recording."""
    def score(record):
        has_synced = bool(record.get("syncedLyrics"))
        gap = abs((record.get("duration") or 0) - duration) if duration else 0
        return (not (has_synced and gap <= SYNC_DURATION_TOLERANCE), gap)
    return min(results, key=score)


@app.route("/api/lyrics")
def api_lyrics():
    """Lyrics by artist + title (+ optional album / duration in seconds, or track_id=local-3).

    Catalogue tracks use their .lrc file when there is one. Otherwise LRCLIB's /api/get is tried first,
    then a search. Returns {lines, synced, instrumental, source, error}; synced is null when no timed
    lyrics match the recording, so the client falls back to plain lines.
    """
    raw_track_id = request.args.get("track_id", "")
    track = playable_track(raw_track_id) if raw_track_id.startswith("local-") else None
    if track:
        local = read_local_lyrics(track)
        if local:
            return jsonify({**local, "error": None})
        artist, title = track.artist.username, track.title
        album, duration = "", round(track.duration_ms / 1000) if track.duration_ms else None
    else:
        artist = request.args.get("artist", "").strip()
        title = request.args.get("title", "").strip()
        album, duration_arg = request.args.get("album", "").strip(), request.args.get("duration", "")
        duration = int(duration_arg) if duration_arg.isdigit() else None
    if not artist or not title:
        return jsonify({"lines": [], "synced": None, "error": "Missing 'artist' or 'title' parameter"}), 400

    params = {"artist_name": artist, "track_name": title}
    headers = {"User-Agent": "Sonoria/1.0 (music streaming demo)"}
    try:
        record = None
        resp = requests.get(f"{LRCLIB_URL}/get", headers=headers, timeout=5,
                            params={**params, **({"album_name": album} if album else {}),
                                    **({"duration": duration} if duration else {})})
        if resp.ok:
            record = resp.json()
        else:
            resp = requests.get(f"{LRCLIB_URL}/search", params=params, headers=headers, timeout=5)
            resp.raise_for_status()
            results = [r for r in resp.json() if r.get("plainLyrics") or r.get("syncedLyrics") or r.get("instrumental")]
            record = best_lyrics_match(results, duration) if results else None
    except (requests.RequestException, ValueError):
        return jsonify({"lines": [], "synced": None, "error": "Could not reach the lyrics service right now."}), 503

    if not record:
        return jsonify({"lines": [], "synced": None, "error": "No lyrics found for this track."}), 404
    payload = lyrics_payload(record, duration)
    if track and payload["synced"]:
        save_local_lyrics(track, record)
    return jsonify({**payload, "source": "lrclib", "error": None})


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------- AUTH ----------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        role = request.form.get("role", "customer")

        if role == "admin":
            return "Admin accounts cannot be created through signup", 403
        if role not in SIGNUP_ROLES:
            return render_template("signup.html", error="Choose User or Artist.")

        if User.query.filter_by(username=username).first():
            return render_template("signup.html", error="Username already taken")

        user = User(username=username, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("login"))

    return render_template("signup.html", error=None)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("home"))
        return render_template("login.html", error="Invalid username or password")

    return render_template("login.html", error=None)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------- HOME ----------

@app.route("/spa")
@app.route("/")  # registered first (decorators apply bottom-up), so url_for("home") builds "/"
def home():
    """The JS-driven single-page app (Violet Dusk redesign); /spa is kept as an alias for old links."""
    badge = get_user_genre_badge(current_user.id) if current_user.is_authenticated else None
    return render_template("index.html", badge=badge)


@app.route("/classic")
def classic_home():
    """The original server-rendered home page."""
    recently_played = []
    if current_user.is_authenticated:
        history = (PlayHistory.query
                   .filter_by(user_id=current_user.id)
                   .order_by(PlayHistory.played_at.desc())
                   .limit(10).all())
        seen = set()
        for hrow in history:
            if hrow.track_id not in seen:
                recently_played.append(hrow.track)
                seen.add(hrow.track_id)

    trending = (Track.query.filter_by(approved=True)
                .order_by(Track.play_count.desc()).limit(10).all())

    viral_shorts = (Track.query.filter_by(approved=True)
                     .order_by(Track.id.desc()).limit(10).all())

    singer_ids = (db.session.query(Track.artist_id)
                  .filter_by(approved=True).distinct().all())
    singers = [db.session.get(User, sid[0]) for sid in singer_ids]

    liked_ids = set()
    if current_user.is_authenticated:
        liked_ids = {l.track_id for l in Like.query.filter_by(user_id=current_user.id).all()}

    return render_template(
        "home.html",
        recently_played=recently_played,
        trending=trending,
        viral_shorts=viral_shorts,
        singers=singers,
        liked_ids=liked_ids,
    )


@app.route("/play/<int:track_id>")
def play_track(track_id):
    track = db.session.get(Track, track_id)
    if not track:
        return "Track not found", 404

    track.play_count += 1
    if current_user.is_authenticated:
        db.session.add(PlayHistory(user_id=current_user.id, track_id=track.id))
        record_genre_play(current_user.id, track.genre)
    db.session.commit()

    return redirect(url_for("classic_home"))


def artist_stats(tracks):
    """Totals shared by the public artist page and the artist dashboard."""
    total = sum(t.play_count or 0 for t in tracks)
    top = max(tracks, key=lambda t: t.play_count or 0) if tracks else None
    genres = Counter(t.genre or "Other" for t in tracks).most_common()
    return {
        "total_streams": total,
        "track_count": len(tracks),
        "top_track": top.title if top and top.play_count else None,
        "avg_streams": round(total / len(tracks), 1) if tracks else 0,
        "genres": genres,
    }


@app.route("/artist/<int:artist_id>")
def artist_page(artist_id):
    artist = db.session.get(User, artist_id)
    if not artist or artist.role != "artist":
        return "Artist not found", 404
    tracks = (Track.query.filter_by(artist_id=artist_id, approved=True)
              .order_by(Track.play_count.desc(), Track.id).all())
    playable = [t.to_dict() for t in tracks if t.stream_url]
    return render_template("artist_page.html", artist=artist, tracks=tracks, playable=playable,
                           stats=artist_stats(tracks))


# ---------- LIBRARY / LIKED SONGS ----------

@app.route("/library")
@login_required
def library():
    liked = (Like.query.filter_by(user_id=current_user.id)
             .order_by(Like.liked_at.desc()).all())
    liked_tracks = [l.track for l in liked if l.track]  # this page only lists catalogue tracks
    return render_template("library.html", liked_tracks=liked_tracks)


@app.route("/like/<int:track_id>")
@login_required
def like_track(track_id):
    track = db.session.get(Track, track_id)
    if not track:
        return "Track not found", 404

    existing = Like.query.filter_by(user_id=current_user.id, track_id=track_id).first()
    if not existing:
        db.session.add(Like(user_id=current_user.id, track_id=track_id))
        db.session.commit()

    return redirect(request.referrer or url_for("home"))


@app.route("/unlike/<int:track_id>")
@login_required
def unlike_track(track_id):
    existing = Like.query.filter_by(user_id=current_user.id, track_id=track_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()

    return redirect(request.referrer or url_for("library"))


# ---------- SEARCH ----------

@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    results = []
    error = None

    if query:
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "media": "music", "limit": 15},
                timeout=5
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except requests.RequestException:
            error = "Could not reach the music search service right now. Try again shortly."

    return render_template("search.html", results=results, query=query, error=error)


@app.route("/search-podcasts")
def search_podcasts():
    query = request.args.get("q", "").strip()
    episodes = []
    podcast_title = None
    error = None

    if query:
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "media": "podcast", "limit": 1},
                timeout=5,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])

            if results:
                podcast_title = results[0].get("collectionName")
                feed_url = results[0].get("feedUrl")

                if feed_url:
                    feed = feedparser.parse(feed_url)
                    for entry in feed.entries[:10]:
                        audio_url = None
                        for link in entry.get("links", []):
                            if link.get("type", "").startswith("audio"):
                                audio_url = link.get("href")
                                break
                        episodes.append({
                            "title": entry.get("title", "Untitled episode"),
                            "audio_url": audio_url,
                        })
        except requests.RequestException:
            error = "Could not reach the podcast search service right now. Try again shortly."

    return render_template(
        "search_podcasts.html",
        episodes=episodes,
        podcast_title=podcast_title,
        query=query,
        error=error,
    )


# ---------- ARTIST ----------

UPLOAD_FOLDER = os.path.join("static", "uploads")
COVER_FOLDER = os.path.join(UPLOAD_FOLDER, "covers")    # artists' cover art
CANVAS_FOLDER = os.path.join(UPLOAD_FOLDER, "canvas")   # artists' canvas videos
ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a"}
UPLOAD_GENRES = list(GENRE_BADGES) + ["Other"]
MB = 1024 * 1024
MAX_AUDIO_BYTES, MAX_COVER_BYTES, MAX_CANVAS_BYTES = 20 * MB, 5 * MB, 15 * MB
MAX_CANVAS_SECONDS = 22  # canvas loops are ~20 s; a little slack for encoders that round up
for folder in (UPLOAD_FOLDER, COVER_FOLDER, CANVAS_FOLDER):
    os.makedirs(folder, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = MAX_AUDIO_BYTES + MAX_COVER_BYTES + MAX_CANVAS_BYTES + MB


def file_size(file):
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    return size


def image_extension(head):
    """jpg/png/webp from the file's magic bytes, or None (the extension a browser sends can lie)."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def mp4_duration(data):
    """Seconds from an MP4's movie header (mvhd box), or None if it isn't a readable MP4."""
    if data[4:8] != b"ftyp":
        return None
    at = data.find(b"mvhd")
    if at < 0 or at + 32 > len(data):
        return None
    version = data[at + 4]
    if version == 1:
        timescale, duration = struct.unpack(">IQ", data[at + 24:at + 36])
    else:
        timescale, duration = struct.unpack(">II", data[at + 16:at + 24])
    return duration / timescale if timescale else None


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def wants_json():
    return request.accept_mimetypes.best == "application/json"


def artist_only():
    """None if the current user may use the artist tools, else the error response."""
    if current_user.role != "artist":
        return (jsonify({"error": "Artists only."}), 403) if wants_json() else ("Not authorized", 403)
    return None


def own_track_or_error(track_id):
    """(track, None) for the current artist's own track, else (None, error response)."""
    track = db.session.get(Track, track_id)
    if not track:
        return None, ((jsonify({"error": "Track not found."}), 404) if wants_json() else ("Track not found", 404))
    if track.artist_id != current_user.id:
        return None, ((jsonify({"error": "That isn't your track."}), 403) if wants_json()
                      else ("Not authorized - this isn't your track", 403))
    return track, None


@app.errorhandler(413)
def upload_too_large(_error):
    message = "That file is over the 20 MB upload limit."
    return (jsonify({"error": message}), 413) if wants_json() else (message, 413)


@app.route("/artist/dashboard")
@login_required
def artist_dashboard():
    denied = artist_only()
    if denied:
        return denied
    my_tracks = Track.query.filter_by(artist_id=current_user.id).order_by(Track.id.desc()).all()
    return render_template("artist_dashboard.html", tracks=my_tracks, stats=artist_stats(my_tracks),
                           genres=UPLOAD_GENRES, track_data=[t.to_dict() for t in my_tracks if t.stream_url],
                           active_tab=request.args.get("tab", "overview"))


@app.route("/artist/upload", methods=["GET", "POST"])
@login_required
def upload_track():
    """Publishes an upload straight away: no admin approval. Answers JSON to fetch/XHR, redirects forms."""
    denied = artist_only()
    if denied:
        return denied
    if request.method == "GET":
        return redirect(url_for("artist_dashboard", tab="upload"))

    def fail(message):
        if wants_json():
            return jsonify({"error": message}), 400
        return redirect(url_for("artist_dashboard", tab="upload", error=message))

    title = request.form.get("title", "").strip()
    genre = request.form.get("genre", "Other")
    file = request.files.get("audio_file")
    if not title:
        return fail("Title is required.")
    if len(title) > 200:
        return fail("Titles can be at most 200 characters.")
    if genre not in UPLOAD_GENRES:
        genre = "Other"
    if not file or file.filename == "":
        return fail("Please choose an audio file.")
    if not allowed_file(file.filename):
        return fail("Only mp3, wav, or m4a files are allowed.")
    if file_size(file) > MAX_AUDIO_BYTES:
        return fail("The audio file is over the 20 MB limit.")

    # Optional cover art and canvas video, checked by content rather than by name
    cover, cover_ext = request.files.get("cover_art"), None
    if cover and cover.filename:
        if file_size(cover) > MAX_COVER_BYTES:
            return fail("Cover art must be 5 MB or smaller.")
        cover_ext = image_extension(cover.stream.read(16))
        cover.stream.seek(0)
        if not cover_ext:
            return fail("Cover art must be a JPG, PNG or WebP image.")
    canvas, canvas_data = request.files.get("canvas_video"), None
    if canvas and canvas.filename:
        if file_size(canvas) > MAX_CANVAS_BYTES:
            return fail("The canvas video must be 15 MB or smaller.")
        canvas_data = canvas.stream.read()
        seconds = mp4_duration(canvas_data)
        if seconds is None:
            return fail("The canvas video must be an MP4 file.")
        if seconds > MAX_CANVAS_SECONDS:
            return fail(f"The canvas video is {seconds:.0f} s long; keep it to about 20 seconds.")

    # Unique names so two artists uploading "song.mp3" never overwrite each other
    token = uuid.uuid4().hex[:10]
    extension = file.filename.rsplit(".", 1)[1].lower()
    safe = secure_filename(file.filename) or f"track.{extension}"
    filename = f"{token}_{safe}"
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    cover_path = canvas_path = None
    if cover_ext:
        cover.save(os.path.join(COVER_FOLDER, f"{token}.{cover_ext}"))
        cover_path = f"/static/uploads/covers/{token}.{cover_ext}"
    if canvas_data:
        Path(CANVAS_FOLDER, f"{token}.mp4").write_bytes(canvas_data)
        canvas_path = f"/static/uploads/canvas/{token}.mp4"

    track = Track(title=title, artist_id=current_user.id, audio_file=filename, genre=genre,
                  approved=True, play_count=0, stream_url=f"/static/uploads/{filename}",
                  cover_art=cover_path, canvas_video=canvas_path)
    db.session.add(track)
    db.session.commit()
    if wants_json():
        return jsonify({"track": track.to_dict()}), 201
    return redirect(url_for("artist_dashboard", tab="tracks"))


@app.route("/artist/edit/<int:track_id>", methods=["GET", "POST"])
@login_required
def edit_track(track_id):
    denied = artist_only()
    if denied:
        return denied
    track, error = own_track_or_error(track_id)
    if error:
        return error

    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        new_title = str(data.get("title", "")).strip()
        genre = data.get("genre", track.genre)
        if not new_title or len(new_title) > 200:
            message = "Title is required." if not new_title else "Titles can be at most 200 characters."
            if wants_json():
                return jsonify({"error": message}), 400
            return render_template("edit_track.html", track=track, genres=UPLOAD_GENRES, error=message)
        track.title = new_title
        track.genre = genre if genre in UPLOAD_GENRES else track.genre
        db.session.commit()  # stays published: edits don't need approval
        if wants_json():
            return jsonify({"track": track.to_dict()})
        return redirect(url_for("artist_dashboard", tab="tracks"))

    return render_template("edit_track.html", track=track, genres=UPLOAD_GENRES, error=None)


@app.route("/artist/delete/<int:track_id>", methods=["POST"])
@login_required
def delete_track(track_id):
    denied = artist_only()
    if denied:
        return denied
    track, error = own_track_or_error(track_id)
    if error:
        return error

    # Likes and play history point at the track; playlist links go with it via the relationship
    Like.query.filter_by(track_id=track.id).delete()
    PlayHistory.query.filter_by(track_id=track.id).delete()
    audio_file = track.audio_file or ""
    in_uploads = bool(audio_file) and track.stream_url == f"/static/uploads/{audio_file}"
    extras = [path for path in (track.cover_art, track.canvas_video)
              if path and path.startswith(("/static/uploads/covers/", "/static/uploads/canvas/"))]
    db.session.delete(track)
    db.session.commit()
    upload = Path(UPLOAD_FOLDER) / audio_file
    if in_uploads and upload.is_file() and not Track.query.filter_by(audio_file=audio_file).count():
        upload.unlink()
    for path in extras:  # the artist's own cover / canvas files belong to this track alone
        Path(path.lstrip("/")).unlink(missing_ok=True)
    if wants_json():
        return jsonify({"deleted": track_id})
    return redirect(url_for("artist_dashboard", tab="tracks"))


# ---------- ADMIN ----------

def is_admin(user):
    return user.is_authenticated and user.role == "admin"


@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    if not is_admin(current_user):
        return "Not authorized", 403
    total_users = User.query.count()
    total_tracks = Track.query.count()
    pending_count = Track.query.filter_by(approved=False).count()
    return render_template("admin_dashboard.html", total_users=total_users, total_tracks=total_tracks, pending_count=pending_count)


@app.route("/admin/approve")
@login_required
def admin_approve():
    if not is_admin(current_user):
        return "Not authorized", 403
    pending_tracks = Track.query.filter_by(approved=False).all()
    return render_template("admin_approve.html", tracks=pending_tracks)


@app.route("/admin/approve/<int:track_id>")
@login_required
def approve_track(track_id):
    if not is_admin(current_user):
        return "Not authorized", 403
    track = db.session.get(Track, track_id)
    if not track:
        return "Track not found", 404
    track.approved = True
    db.session.commit()
    return redirect(url_for("admin_approve"))


@app.route("/admin/reject/<int:track_id>")
@login_required
def reject_track(track_id):
    if not is_admin(current_user):
        return "Not authorized", 403
    track = db.session.get(Track, track_id)
    if not track:
        return "Track not found", 404
    db.session.delete(track)
    db.session.commit()
    return redirect(url_for("admin_approve"))


# ---------- ENGAGEMENT: TAG + STREAK ----------

def compute_user_tag(user_id):
    """(top genre, badge title) for the profile pages; (None, None) before the first play."""
    badge = get_user_genre_badge(user_id)
    return (badge["genre"], badge["badge"]) if badge else (None, None)


def func_date(column):
    from sqlalchemy import func
    return func.date(column)


def compute_listening_streak(user_id):
    from datetime import date, timedelta, datetime as dt
    play_dates = (
        db.session.query(func_date(PlayHistory.played_at))
        .filter(PlayHistory.user_id == user_id)
        .distinct()
        .all()
    )
    played_days = set()
    for row in play_dates:
        if row[0]:
            played_days.add(dt.strptime(row[0], "%Y-%m-%d").date())
    if not played_days:
        return 0
    streak = 0
    check_day = date.today()
    if check_day not in played_days:
        check_day -= timedelta(days=1)
    while check_day in played_days:
        streak += 1
        check_day -= timedelta(days=1)
    return streak


@app.route("/profile")
@login_required
def profile():
    genre, tag = compute_user_tag(current_user.id)
    streak = compute_listening_streak(current_user.id)
    genre_counts = (UserPlayCount.query.filter_by(user_id=current_user.id)
                    .order_by(UserPlayCount.play_count.desc()).all())
    recent, seen = [], set()
    for row in (PlayHistory.query.filter(PlayHistory.user_id == current_user.id, PlayHistory.track_id.isnot(None))
                .order_by(PlayHistory.played_at.desc(), PlayHistory.id.desc()).limit(40)):
        if row.track and row.track_id not in seen and row.track.stream_url:
            recent.append(row.track.to_dict())
            seen.add(row.track_id)
        if len(recent) == 6:
            break
    stats = {
        "total_plays": sum(r.play_count for r in genre_counts),
        "playlists": Playlist.query.filter_by(user_id=current_user.id).count(),
        "likes": Like.query.filter_by(user_id=current_user.id).count(),
    }
    return render_template("profile.html", genre=genre, tag=tag, streak=streak, stats=stats,
                           genre_counts=[(r.genre, r.play_count) for r in genre_counts], recent=recent)


@app.route("/tag/<username>")
def public_tag(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        return "User not found", 404
    genre, tag = compute_user_tag(user.id)
    return render_template("public_tag.html", username=username, genre=genre, tag=tag)


# ---------- LYRICS ----------

@app.route("/lyrics/<int:track_id>")
def track_lyrics(track_id):
    track = db.session.get(Track, track_id)
    if not track:
        return "Track not found", 404

    lines = []
    error = None
    try:
        resp = requests.get(
            "https://lrclib.net/api/search",
            params={"artist_name": track.artist.username, "track_name": track.title},
            timeout=5,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            lrc_text = results[0].get("syncedLyrics", "") or ""
            for match in re_module.finditer(r"\[(\d+):(\d+\.\d+)\](.*)", lrc_text):
                minutes, seconds, text = match.groups()
                timestamp = int(minutes) * 60 + float(seconds)
                if text.strip():
                    lines.append({"time": timestamp, "text": text.strip()})
    except requests.RequestException:
        error = "Could not reach the lyrics service right now."

    return render_template("track_lyrics.html", track=track, lines=lines, error=error)


@app.route("/dashboard")
@login_required
def dashboard():
    return f"Logged in as {current_user.username} (role: {current_user.role})"


if __name__ == "__main__":
    app.run(debug=True)
