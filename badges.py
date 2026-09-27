"""Genre badges: turns a user's per-genre play counts into a fun listener title."""
from models import db, UserPlayCount

# Canonical genre -> badge title. These genre names are what UserPlayCount stores.
GENRE_BADGES = {
    "Romantic": "Blind in love",
    "Sad": "Heartbroken Aashiq",
    "Classical": "Sur Sadhak",
    "Retro": "Nostalgic Deewana",
    "Rock": "Chaos Headbanger",
    "Devotional": "Lowkey Bhakt",
    "Party": "Unhinged Party Animal",
    "Folk": "Desi Deewana",
    "Pop": "Certified Popstar",
    "Indie": "Indie Kid",
    "EDM": "Beat Junkie",
    "Bollywood": "Desi CEO",
    "Punjabi": "Punjab Da Puttar",
    "Lofi": "Lowkey Daydreamer",
    "HipHop": "Beat Dealer",
}
EXPLORER_BADGE = "Indecisive Explorer"
OTHER_GENRE = "Other"

# "Listens to everything": at least this many genres, with none taking this big a share of plays.
EXPLORER_MIN_GENRES = 4
EXPLORER_MAX_TOP_SHARE = 0.30

# Keywords that map raw genre labels (catalogue genres, iTunes primaryGenreName) onto canonical genres.
# Checked in order, so the more specific labels come first: "Indian Pop" is Bollywood, not Pop;
# "Classical" is not Retro's "classic"; "Dance/Electronic" is EDM before "dance" means Party.
GENRE_KEYWORDS = [
    ("Sad", ("sad", "heartbreak")),
    ("Romantic", ("romantic", "romance", "love")),
    ("Classical", ("classical", "carnatic", "hindustani", "ghazal", "opera")),
    ("Retro", ("retro", "oldies", "classic", "golden")),
    ("Rock", ("rock", "metal", "punk", "grunge")),
    ("Devotional", ("devotional", "spiritual", "bhajan", "gospel", "christian", "sufi")),
    ("EDM", ("edm", "electronic", "electronica", "house", "techno", "trance", "dubstep")),
    ("Party", ("party", "dance", "disco")),
    ("Punjabi", ("punjabi", "bhangra")),
    ("Bollywood", ("bollywood", "indian pop", "hindi", "filmi")),
    ("Folk", ("folk", "country", "haryanvi", "bhojpuri", "regional")),
    ("HipHop", ("hip-hop", "hip hop", "hiphop", "rap", "trap")),
    ("Lofi", ("lofi", "lo-fi", "chill", "ambient")),
    ("Indie", ("indie", "alternative", "singer/songwriter")),
    ("Pop", ("pop", "r&b")),
]


def normalize_genre(raw):
    """Maps a raw genre label to a canonical GENRE_BADGES key, or "Other" when nothing matches."""
    label = (raw or "").strip().lower()
    if not label:
        return OTHER_GENRE
    for genre in GENRE_BADGES:
        if label == genre.lower():
            return genre
    for genre, keywords in GENRE_KEYWORDS:
        if any(word in label for word in keywords):
            return genre
    return OTHER_GENRE


def record_genre_play(user_id, raw_genre):
    """Adds one play of raw_genre to the user's counts. The caller commits."""
    genre = normalize_genre(raw_genre)
    row = UserPlayCount.query.filter_by(user_id=user_id, genre=genre).first()
    if row:
        row.play_count += 1
        row.last_played_at = db.func.now()
    else:
        db.session.add(UserPlayCount(user_id=user_id, genre=genre, play_count=1))


def get_user_genre_badge(user_id):
    """The user's badge from their top played genre.

    Returns {"badge", "genre", "plays", "total_plays"}, or None before their first play.
    Ties go to the genre played most recently. Plays that match no badge genre ("Other") count
    toward the total but never pick the badge; a user with only those gets Indecisive Explorer.
    """
    rows = UserPlayCount.query.filter_by(user_id=user_id).all()
    total = sum(r.play_count for r in rows)
    if not total:
        return None

    ranked = sorted((r for r in rows if r.genre in GENRE_BADGES and r.play_count > 0),
                    key=lambda r: (r.play_count, r.last_played_at), reverse=True)
    if not ranked:
        return {"badge": EXPLORER_BADGE, "genre": None, "plays": 0, "total_plays": total}

    top = ranked[0]
    if len(ranked) >= EXPLORER_MIN_GENRES and top.play_count / total < EXPLORER_MAX_TOP_SHARE:
        return {"badge": EXPLORER_BADGE, "genre": None, "plays": top.play_count, "total_plays": total}
    return {"badge": GENRE_BADGES[top.genre], "genre": top.genre, "plays": top.play_count, "total_plays": total}
