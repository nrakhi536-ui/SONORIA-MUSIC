from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_cors import CORS
from models import db, User, Track, PlayHistory, Like, upgrade_schema
from concurrent.futures import ThreadPoolExecutor
import requests
import feedparser
import os
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


# ---------- iTunes SEARCH API ----------

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
HOME_SECTION_TERMS = ["Chill", "Top Hits", "Synthwave"]


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


@app.route("/api/songs")
def api_songs():
    """Fallback route to populate home sections with a few popular-term searches."""
    def fetch(term):
        try:
            return query_itunes(term, limit=10)
        except requests.RequestException:
            return []

    # Run the section searches concurrently so the home page waits on one round-trip, not three.
    with ThreadPoolExecutor(max_workers=len(HOME_SECTION_TERMS)) as pool:
        sections = dict(zip(HOME_SECTION_TERMS, pool.map(fetch, HOME_SECTION_TERMS)))
    return jsonify({"sections": sections})


# Local catalogue sections (Track.section) and the headings the SPA shows for them, in page order.
LOCAL_SECTIONS = {
    "trending": "Trending Now",
    "viral": "Viral on Reels & Shorts",
    "artist": "Artists",
}


@app.route("/api/home_sections")
def api_home_sections():
    """Locally hosted tracks (full MP3 + cover + Canvas loop), grouped by home-page section."""
    tracks = (Track.query.filter(Track.approved.is_(True), Track.section.in_(LOCAL_SECTIONS))
              .order_by(Track.play_count.desc(), Track.id).all())
    grouped = {key: [] for key in LOCAL_SECTIONS}
    for track in tracks:
        grouped[track.section].append(track.to_dict())
    return jsonify({
        "sections": [{"key": key, "title": title, "tracks": grouped[key]} for key, title in LOCAL_SECTIONS.items()],
    })


LRCLIB_URL = "https://lrclib.net/api"
LRC_TIMESTAMP = re_module.compile(r"^\s*(\[\d+:\d+(?:\.\d+)?\])+\s*")


def lyrics_payload(record):
    """Turns an LRCLIB record into {lines, instrumental}, preferring plain text over synced LRC."""
    text = record.get("plainLyrics") or record.get("syncedLyrics") or ""
    lines = [LRC_TIMESTAMP.sub("", line).rstrip() for line in text.splitlines()]
    return {"lines": lines, "instrumental": bool(record.get("instrumental"))}


@app.route("/api/lyrics")
def api_lyrics():
    """Looks up lyrics on LRCLIB by artist + title (+ optional album / duration in seconds)."""
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    if not artist or not title:
        return jsonify({"lines": [], "error": "Missing 'artist' or 'title' parameter"}), 400

    params = {"artist_name": artist, "track_name": title}
    headers = {"User-Agent": "Sonoria/1.0 (music streaming demo)"}
    try:
        # Exact match first (needs album + duration), then fall back to fuzzy search.
        album, duration = request.args.get("album", "").strip(), request.args.get("duration", "")
        if album and duration.isdigit():
            resp = requests.get(f"{LRCLIB_URL}/get", headers=headers, timeout=5,
                                params={**params, "album_name": album, "duration": duration})
            if resp.ok:
                return jsonify({**lyrics_payload(resp.json()), "error": None})

        resp = requests.get(f"{LRCLIB_URL}/search", params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        results = [r for r in resp.json() if r.get("plainLyrics") or r.get("syncedLyrics") or r.get("instrumental")]
    except (requests.RequestException, ValueError):
        return jsonify({"lines": [], "error": "Could not reach the lyrics service right now."}), 503

    if not results:
        return jsonify({"lines": [], "error": "No lyrics found for this track."}), 404
    return jsonify({**lyrics_payload(results[0]), "error": None})


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


# ---------- CUSTOMER: HOME ----------

@app.route("/")
@app.route("/spa")
def home():
    """The JS-driven single-page app (Violet Dusk redesign); /spa is kept as an alias for old links."""
    return render_template("index.html")


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
    db.session.commit()

    return redirect(url_for("home"))


@app.route("/artist/<int:artist_id>")
def artist_page(artist_id):
    artist = db.session.get(User, artist_id)
    if not artist or artist.role != "artist":
        return "Artist not found", 404
    tracks = Track.query.filter_by(artist_id=artist_id, approved=True).all()
    return render_template("artist_page.html", artist=artist, tracks=tracks)


# ---------- LIBRARY / LIKED SONGS ----------

@app.route("/library")
@login_required
def library():
    liked = (Like.query.filter_by(user_id=current_user.id)
             .order_by(Like.liked_at.desc()).all())
    liked_tracks = [l.track for l in liked]
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
ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/artist/dashboard")
@login_required
def artist_dashboard():
    if current_user.role != "artist":
        return "Not authorized", 403
    my_tracks = Track.query.filter_by(artist_id=current_user.id).order_by(Track.id.desc()).all()
    return render_template("artist_dashboard.html", tracks=my_tracks)


@app.route("/artist/upload", methods=["GET", "POST"])
@login_required
def upload_track():
    if current_user.role != "artist":
        return "Not authorized", 403

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        genre = request.form.get("genre", "Other")
        file = request.files.get("audio_file")

        if not title:
            return render_template("upload.html", error="Title is required.")
        if not file or file.filename == "":
            return render_template("upload.html", error="Please choose an audio file.")
        if not allowed_file(file.filename):
            return render_template("upload.html", error="Only mp3, wav, or m4a files are allowed.")

        filename = secure_filename(file.filename)
        file.save(os.path.join(UPLOAD_FOLDER, filename))

        track = Track(title=title, artist_id=current_user.id, audio_file=filename, genre=genre)
        db.session.add(track)
        db.session.commit()
        return redirect(url_for("artist_dashboard"))

    return render_template("upload.html", error=None)


@app.route("/artist/edit/<int:track_id>", methods=["GET", "POST"])
@login_required
def edit_track(track_id):
    if current_user.role != "artist":
        return "Not authorized", 403
    track = db.session.get(Track, track_id)
    if not track:
        return "Track not found", 404
    if track.artist_id != current_user.id:
        return "Not authorized - this isn't your track", 403

    if request.method == "POST":
        new_title = request.form.get("title", "").strip()
        if not new_title:
            return render_template("edit_track.html", track=track, error="Title is required.")
        track.title = new_title
        track.approved = False
        db.session.commit()
        return redirect(url_for("artist_dashboard"))

    return render_template("edit_track.html", track=track, error=None)


@app.route("/artist/delete/<int:track_id>")
@login_required
def delete_track(track_id):
    if current_user.role != "artist":
        return "Not authorized", 403
    track = db.session.get(Track, track_id)
    if not track:
        return "Track not found", 404
    if track.artist_id != current_user.id:
        return "Not authorized - this isn't your track", 403
    db.session.delete(track)
    db.session.commit()
    return redirect(url_for("artist_dashboard"))


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

GENRE_TAG_MAP = {
    "Romantic": "Heartbroken Aashiq",
    "HipHop": "Street Poet",
    "EDM": "Bass Chaser",
    "Devotional": "Sukoon Seeker",
    "Retro": "Purani Yaadon Ka Deewana",
    "Rock": "Chaos Bringer",
    "Other": "Genre Explorer",
}


def compute_user_tag(user_id):
    from sqlalchemy import func
    result = (
        db.session.query(Track.genre, func.count(PlayHistory.id).label("play_count"))
        .join(PlayHistory, PlayHistory.track_id == Track.id)
        .filter(PlayHistory.user_id == user_id)
        .group_by(Track.genre)
        .order_by(func.count(PlayHistory.id).desc())
        .first()
    )
    if not result:
        return None, None
    top_genre = result[0]
    return top_genre, GENRE_TAG_MAP.get(top_genre, "Genre Explorer")


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
    return render_template("profile.html", genre=genre, tag=tag, streak=streak)


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
