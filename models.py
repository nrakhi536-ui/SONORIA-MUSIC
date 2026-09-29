from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import quote
from datetime import datetime, timezone

db = SQLAlchemy()


def utc_now():
    """Naive UTC with microseconds; SQLite's CURRENT_TIMESTAMP only has whole seconds, too coarse for ordering."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def media_url(path):
    """Percent-encodes a stored site path ("/static/media/audio/Baby Doll.mp3") into a URL; None stays None."""
    return quote(path) if path else path


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
    cover_art = db.Column(db.String(500))       # cover image path
    stream_url = db.Column(db.String(500), index=True)
    canvas_video = db.Column(db.String(500))    # looping canvas MP4 (~20 s); NULL -> animated cover fallback
    duration_ms = db.Column(db.Integer)
    section = db.Column(db.String(20), index=True)  # "trending" | "viral" | "artist"; NULL for plain uploads

    artist = db.relationship("User", backref="tracks")

    def to_dict(self):
        """Same shape as the normalized /api/search results, plus the local-only media fields.

        The id is prefixed so local tracks never collide with iTunes ids in the frontend's liked/queue state.
        Media paths are stored exactly as the files are named on disk and percent-encoded here into URLs.
        """
        return {
            "id": f"local-{self.id}",
            "title": self.title,
            "artist": self.artist.username,
            "artist_id": self.artist_id,
            "album": self.album,
            "cover": media_url(self.cover_art),
            "stream_url": media_url(self.stream_url),
            "video_url": media_url(self.canvas_video),
            "duration": self.duration_ms,
            "genre": self.genre,
            "play_count": self.play_count or 0,
            "section": self.section,
            "local": True,
        }

    def __repr__(self):
        return f"<Track {self.title} by {self.artist.username}>"


# Many-to-many: which catalogue tracks are in which playlist, in the order they were added.
playlist_tracks = db.Table(
    "playlist_tracks",
    db.Column("playlist_id", db.Integer, db.ForeignKey("playlist.id", ondelete="CASCADE"), primary_key=True),
    db.Column("track_id", db.Integer, db.ForeignKey("track.id", ondelete="CASCADE"), primary_key=True),
    db.Column("added_at", db.DateTime, default=utc_now, server_default=db.func.now()),
)


class Playlist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    user = db.relationship("User", backref="playlists")
    tracks = db.relationship("Track", secondary=playlist_tracks, order_by=playlist_tracks.c.added_at,
                             backref="playlists")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "track_count": len(self.tracks),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserPlayCount(db.Model):
    """How many times a user has played each genre - the input to their genre badge.

    genre is a canonical badge genre from badges.py ("Romantic", "HipHop", ...) or "Other".
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    genre = db.Column(db.String(50), nullable=False)
    play_count = db.Column(db.Integer, nullable=False, default=0)
    last_played_at = db.Column(db.DateTime, default=utc_now, server_default=db.func.now())  # breaks ties toward the recent genre

    __table_args__ = (db.UniqueConstraint("user_id", "genre", name="unique_user_genre"),)


# Columns added to Track after the first release; SQLite needs them ALTERed onto existing databases.
# Columns that were renamed in the model: table -> {old name: new name}. Renamed in place so data is kept.
RENAMED_COLUMNS = {"track": {"cover": "cover_art", "video_url": "canvas_video"}}


def rename_columns():
    inspector = db.inspect(db.engine)
    tables = set(inspector.get_table_names())
    for table, renames in RENAMED_COLUMNS.items():
        if table not in tables:
            continue
        have = {col["name"] for col in inspector.get_columns(table)}
        with db.engine.begin() as conn:
            for old, new in renames.items():
                if old in have and new not in have:
                    conn.execute(db.text(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"'))


def upgrade_schema():
    """Adds columns that models gained after their table was created (db.create_all() never alters tables).

    Only nullable columns can be added this way; that covers every column added so far
    (Track's media fields, and external_id on Like / PlayHistory). Renamed columns are handled first.
    """
    rename_columns()
    inspector = db.inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    statements = []
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all() just made it with every column
        have = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            if not column.nullable:
                raise RuntimeError(f"Can't add NOT NULL column {table.name}.{column.name} to an existing table")
            ddl = column.type.compile(dialect=db.engine.dialect)
            statements.append(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
    if statements:
        with db.engine.begin() as conn:
            for statement in statements:
                conn.execute(db.text(statement))
    rebuild_relaxed_tables()


def rebuild_relaxed_tables():
    """Recreates tables where a column is NOT NULL in the database but nullable in the model.

    SQLite can't drop a NOT NULL constraint in place. like.track_id and play_history.track_id were
    created NOT NULL before likes/plays of iTunes songs (external_id) existed, which made those rows
    impossible to insert. The table is renamed, recreated from the model and the rows copied back.
    """
    inspector = db.inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        columns = {col["name"]: col for col in inspector.get_columns(table.name)}
        stale = [c.name for c in table.columns if c.nullable and c.name in columns and not columns[c.name]["nullable"]]
        if not stale:
            continue
        shared = ", ".join(f'"{name}"' for name in columns if name in table.columns)
        old_name = f"{table.name}__old"
        old_indexes = [index["name"] for index in inspector.get_indexes(table.name)]
        with db.engine.begin() as conn:
            conn.execute(db.text(f'ALTER TABLE "{table.name}" RENAME TO "{old_name}"'))
            for name in old_indexes:  # they move with the renamed table and would clash with the new ones
                conn.execute(db.text(f'DROP INDEX IF EXISTS "{name}"'))
            table.create(conn)
            conn.execute(db.text(f'INSERT INTO "{table.name}" ({shared}) SELECT {shared} FROM "{old_name}"'))
            conn.execute(db.text(f'DROP TABLE "{old_name}"'))


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
    """A signed-in user (listener or artist) liking a track - powers Liked Songs.

    Guests' likes never come here; they stay in the browser's localStorage.
    """
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
