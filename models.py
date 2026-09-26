from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="customer")

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class Track(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    artist_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    is_podcast = db.Column(db.Boolean, default=False)
    approved = db.Column(db.Boolean, default=False)
    play_count = db.Column(db.Integer, default=0)
    audio_file = db.Column(db.String(300))
    genre = db.Column(db.String(50), default="Other")
    # Local catalogue media (see generate_media.py); URLs are site-relative, e.g. /static/media/...
    album = db.Column(db.String(300))
    cover = db.Column(db.String(500))
    stream_url = db.Column(db.String(500), index=True)
    video_url = db.Column(db.String(500))
    duration_ms = db.Column(db.Integer)
    section = db.Column(db.String(20), index=True)  # "trending" | "viral" | "artist"; NULL for plain uploads

    artist = db.relationship("User", backref="tracks")

    def to_dict(self):
        """Same shape as the normalized /api/search results, plus the local-only media fields.

        The id is prefixed so local tracks never collide with iTunes ids in the frontend's liked/queue state.
        """
        return {
            "id": f"local-{self.id}",
            "title": self.title,
            "artist": self.artist.username,
            "album": self.album,
            "cover": self.cover,
            "stream_url": self.stream_url,
            "video_url": self.video_url,
            "duration": self.duration_ms,
            "genre": self.genre,
            "section": self.section,
            "local": True,
        }

    def __repr__(self):
        return f"<Track {self.title} by {self.artist.username}>"


# Columns added to Track after the first release; SQLite needs them ALTERed onto existing databases.
TRACK_UPGRADE_COLUMNS = {
    "album": "VARCHAR(300)",
    "cover": "VARCHAR(500)",
    "stream_url": "VARCHAR(500)",
    "video_url": "VARCHAR(500)",
    "duration_ms": "INTEGER",
    "section": "VARCHAR(20)",
}


def upgrade_schema():
    """Adds any missing Track columns in place (db.create_all() never alters existing tables)."""
    existing = {col["name"] for col in db.inspect(db.engine).get_columns("track")}
    missing = {name: ddl for name, ddl in TRACK_UPGRADE_COLUMNS.items() if name not in existing}
    if not missing:
        return
    with db.engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(db.text(f"ALTER TABLE track ADD COLUMN {name} {ddl}"))


class ExternalTrack(db.Model):
    """Cached metadata for a song hosted by an outside catalogue (currently iTunes).

    external_id is namespaced by source, e.g. "itunes:697195462", so other catalogues can be added later.
    Metadata comes from the iTunes Lookup API on the server, never from the client.
    """
    external_id = db.Column(db.String(64), primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    artist = db.Column(db.String(300))
    album = db.Column(db.String(300))
    cover = db.Column(db.String(500))
    stream_url = db.Column(db.String(500))
    duration_ms = db.Column(db.Integer)
    genre = db.Column(db.String(100))
    fetched_at = db.Column(db.DateTime, server_default=db.func.now())

    @property
    def source_id(self):
        """The id within its source catalogue, as the frontend knows it (an int for iTunes)."""
        raw = self.external_id.split(":", 1)[1]
        return int(raw) if raw.isdigit() else raw

    def to_dict(self):
        """Same shape as the normalized /api/search results."""
        return {
            "id": self.source_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "cover": self.cover,
            "stream_url": self.stream_url,
            "duration": self.duration_ms,
            "genre": self.genre,
        }

    def __repr__(self):
        return f"<ExternalTrack {self.external_id} {self.title}>"


# Like and PlayHistory point at exactly one of: a local uploaded Track (track_id)
# or an external catalogue song (external_id).
ONE_TRACK_REFERENCE = "(track_id IS NULL) <> (external_id IS NULL)"


class PlayHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    track_id = db.Column(db.Integer, db.ForeignKey("track.id"), nullable=True)
    external_id = db.Column(db.String(64), db.ForeignKey("external_track.external_id"), nullable=True, index=True)
    played_at = db.Column(db.DateTime, server_default=db.func.now())

    user = db.relationship("User")
    track = db.relationship("Track")
    external_track = db.relationship("ExternalTrack")

    __table_args__ = (
        db.CheckConstraint(ONE_TRACK_REFERENCE, name="play_history_one_track"),
    )


class Like(db.Model):
    """A customer 'liking' a track - powers the Library / Liked Songs page."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    track_id = db.Column(db.Integer, db.ForeignKey("track.id"), nullable=True)
    external_id = db.Column(db.String(64), db.ForeignKey("external_track.external_id"), nullable=True)
    liked_at = db.Column(db.DateTime, server_default=db.func.now())

    user = db.relationship("User")
    track = db.relationship("Track")
    external_track = db.relationship("ExternalTrack")

    __table_args__ = (
        db.UniqueConstraint("user_id", "track_id", name="unique_like"),
        db.UniqueConstraint("user_id", "external_id", name="unique_external_like"),
        db.CheckConstraint(ONE_TRACK_REFERENCE, name="like_one_track"),
    )
