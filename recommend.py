"""Personalised recommendations: taste profile from listening history and likes, then scored, deduplicated,
randomised picks from the Sonoria catalogue plus genre-matched iTunes songs.

Used by /api/recommendations for the home "Recommended for You" row and for continuous autoplay.
"""
import math
import random
import re
from collections import defaultdict

from badges import GENRE_BADGES, normalize_genre
from models import ExternalTrack, Like, PlayHistory, Track, UserPlayCount

# How much each signal counts toward the taste profile
WEIGHT_LONG_TERM = 1.0    # share of all plays per genre (UserPlayCount)
WEIGHT_RECENT = 1.6       # recent plays, newest counting most
WEIGHT_LIKED = 0.8        # genres of liked songs
WEIGHT_SEED = 1.4         # the song that's playing, for autoplay ("more like this")
RECENT_DECAY = 0.8        # each older play counts 20% less
RECENT_WINDOW = 25
SHARPNESS = 2.0           # >1 favours the best matches more strongly when sampling; 1 would be flatter

# iTunes search terms that return good songs for each badge genre
GENRE_SEARCH_TERMS = {
    "Romantic": "romantic love songs", "Sad": "sad songs", "Classical": "classical music",
    "Retro": "retro hits", "Rock": "rock hits", "Devotional": "devotional songs", "Party": "party hits",
    "Folk": "folk songs", "Pop": "pop hits", "Indie": "indie", "EDM": "edm hits", "Bollywood": "bollywood hits",
    "Punjabi": "punjabi hits", "Lofi": "lofi", "HipHop": "hip hop hits",
}
COLD_START_TERM = "top hits"
ITUNES_GENRES_PER_REQUEST = 3


def title_key(title):
    """Normalised title for de-duplication: "Perfect (Remastered) - Live" and "perfect" match."""
    text = (title or "").lower()
    text = re.sub(r"\s*[\(\[].*?[\)\]]", "", text)   # (feat. X), [Remix], (Live)...
    text = re.split(r"\s+-\s+", text)[0]              # "Song - Radio Edit"
    text = re.sub(r"\bfeat\.?.*$|\bft\.?.*$", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def weighted_sample(items, weights, k, rng=random):
    """k items without replacement, more likely the heavier they are (Efraimidis-Spirakis)."""
    keyed = [(rng.random() ** (1.0 / max(w, 1e-6)), i) for i, w in enumerate(weights)]
    keyed.sort(reverse=True)
    return [items[i] for _, i in keyed[:k]]


def dedupe_by_title(tracks, seen=None):
    """Keeps the first track for each normalised title (and skips titles already in `seen`)."""
    seen = set() if seen is None else seen
    out = []
    for track in tracks:
        key = title_key(track.get("title"))
        if key and key not in seen:
            seen.add(key)
            out.append(track)
    return out


def rank_weight(rank):
    """Relevance of a search result by its position: #1 counts most, but lower ranks still get a chance."""
    return 1.0 / (1 + rank) ** 0.6


def _normalise(weights):
    total = sum(weights.values())
    return {g: w / total for g, w in weights.items() if w > 0} if total else {}


def taste_profile(user_id=None, recent_genres=(), seed_genre=None):
    """{genre: weight} summing to 1, from long-term plays, recent plays, likes and the playing song.

    recent_genres comes from the client (newest first) so guests and iTunes plays count too;
    signed-in users also get their stored history. Returns (profile, favourite_artist_ids).
    """
    long_term, recent, liked = defaultdict(float), defaultdict(float), defaultdict(float)
    artists = defaultdict(float)
    recent_list = [normalize_genre(g) for g in recent_genres][:RECENT_WINDOW]

    if user_id:
        for row in UserPlayCount.query.filter_by(user_id=user_id):
            long_term[row.genre] += row.play_count
        history = (PlayHistory.query.filter(PlayHistory.user_id == user_id, PlayHistory.track_id.isnot(None))
                   .order_by(PlayHistory.played_at.desc(), PlayHistory.id.desc()).limit(RECENT_WINDOW).all())
        if not recent_list:
            recent_list = [normalize_genre(h.track.genre) for h in history if h.track]
        for i, h in enumerate(history):
            if h.track:
                artists[h.track.artist_id] += RECENT_DECAY ** i
        for like in Like.query.filter_by(user_id=user_id):
            genre = like.track.genre if like.track else (like.external_track.genre if like.external_track else None)
            liked[normalize_genre(genre)] += 1
            if like.track:
                artists[like.track.artist_id] += 0.5

    for i, genre in enumerate(recent_list):
        recent[genre] += RECENT_DECAY ** i

    profile = defaultdict(float)
    for source, weight in ((long_term, WEIGHT_LONG_TERM), (recent, WEIGHT_RECENT), (liked, WEIGHT_LIKED)):
        for genre, share in _normalise(source).items():
            profile[genre] += weight * share
    if seed_genre:
        profile[normalize_genre(seed_genre)] += WEIGHT_SEED
    profile.pop("Other", None)   # "Other" says nothing about taste
    return _normalise(profile), _normalise(artists)


def recommend(user_id=None, recent_genres=(), seed=None, exclude_ids=(), exclude_titles=(), limit=12,
              itunes_search=None, rng=random):
    """Scored, randomised, title-deduplicated picks. Returns (tracks, top_genres).

    seed: the playing track dict (for autoplay). exclude_ids / exclude_titles: already played or queued,
    so another version of a song you just heard doesn't come straight back.
    itunes_search(term) -> [track dicts] adds genre-matched iTunes songs (skipped if None).
    """
    seed = seed or {}
    profile, artist_affinity = taste_profile(user_id, recent_genres, seed.get("genre"))
    excluded = {str(i) for i in exclude_ids} | ({str(seed["id"])} if seed.get("id") is not None else set())
    top_genres = sorted(profile, key=profile.get, reverse=True)

    # Sonoria catalogue + published uploads
    local = [t for t in Track.query.filter(Track.approved.is_(True), Track.stream_url.isnot(None))
             if f"local-{t.id}" not in excluded]
    max_plays = max((t.play_count or 0 for t in local), default=0)
    candidates, weights = [], []
    for t in local:
        genre = normalize_genre(t.genre)
        score = (2.0 * profile.get(genre, 0.0)
                 + 0.6 * artist_affinity.get(t.artist_id, 0.0)
                 + (0.25 * math.log1p(t.play_count or 0) / math.log1p(max_plays) if max_plays else 0)
                 + (0.35 if seed.get("artist_id") and t.artist_id == seed.get("artist_id") else 0)
                 + 0.08)  # everything keeps a small chance, so the feed never goes stale
        candidates.append(t.to_dict())
        weights.append(score)

    # iTunes songs for the listener's top genres (or what's popular, for a brand-new listener)
    if itunes_search:
        terms = [(GENRE_SEARCH_TERMS[g], profile[g]) for g in top_genres if g in GENRE_SEARCH_TERMS][:ITUNES_GENRES_PER_REQUEST]
        for term, genre_weight in terms or [(COLD_START_TERM, 0.5)]:
            for rank, item in enumerate(itunes_search(term)):
                if item.get("stream_url") and str(item.get("id")) not in excluded:
                    # a search for "romantic love songs" also returns jazz and new age; its own genre must agree
                    own_genre = profile.get(normalize_genre(item.get("genre")), 0.0)
                    candidates.append(item)
                    weights.append(genre_weight * rank_weight(rank) + 1.2 * own_genre + 0.04)

    weights = [w ** SHARPNESS for w in weights]
    seen = {title_key(t) for t in exclude_titles} | {title_key(seed.get("title"))}
    seen.discard("")
    picks = dedupe_by_title(weighted_sample(candidates, weights, len(candidates), rng), seen)[:limit]
    return picks, [g for g in top_genres if g in GENRE_BADGES][:3]
