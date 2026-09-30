# Sonoria

A Flask music streaming app with a single-page web player styled with the "Violet Dusk" design system.
It streams 30-second song previews from the iTunes Search API and shows lyrics from LRCLIB.

## Features
- **Web player (`/`)**: Home, Explore, Library and Search views; mood filters; a persistent player
  with seek, volume, shuffle, repeat, a queue panel, a lyrics panel and a video/visualizer mode.
  Every track has a **⋯** menu (also on right-click and in the player) with **Add to queue**: queued songs
  play after the current one, before the rest of the list, without interrupting playback.
  When the list and your queue run out, **Autoplay** keeps going with songs like the last one (toggle in the
  queue panel). **Recommended for You** is personal: it's built from your recent plays, long-term genres and
  likes, and re-weights itself as you listen. Top Charts never repeats a title and reshuffles on each load.
- **Canvas**: in Video mode each song's looping ~20 s canvas video fills the stage. Songs without one (or
  whose video fails) get an animated cover instead.
- **Likes**: guests' likes stay in the browser (`localStorage["guest_liked_songs"]`); signed-in users and
  artists get their own likes on the server. Guest likes are never merged into an account.
- **JSON API**: iTunes search, home-page sections, lyrics, likes, playlists and badges (see below).
- **Accounts and roles**: signup, login and logout for users, artists and admins.
- **Artist tools**: a dashboard with live stream counts, a performance chart, track management and
  drag-and-drop uploads that **publish immediately** (no approval step), with optional cover art
  (JPG/PNG/WebP, 5 MB) and a canvas video (MP4, about 20 s, 15 MB); a public artist page.
- **Admin tools**: dashboard, and review of any older unapproved uploads.

## Requirements
- Python 3.9 or newer
- Internet access. Search, streaming and lyrics come from `itunes.apple.com` and `lrclib.net`.

## Setup
```bash
python -m venv venv

# Activate the virtual environment
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

## Running the app
```bash
python app.py
```

The first run creates the SQLite database at `instance/app.db`. Then open:

| URL | What it is |
| --- | --- |
| http://127.0.0.1:5000/ | The single-page web player (also at `/spa`) |
| http://127.0.0.1:5000/signup | Create an account |
| http://127.0.0.1:5000/login | Log in |
| http://127.0.0.1:5000/classic | The older server-rendered home page |

You can open a view directly with a link like `/#library` or `/#explore`.

### JSON API
| Endpoint | Returns |
| --- | --- |
| `GET /api/search?q=<term>` | `{results: [track], error}`: up to 25 iTunes songs |
| `GET /api/songs` | `{sections: {"Chill": [...], "Top Charts": [...]}}` for the home page: no title appears twice, and each load samples the top results in a new order |
| `GET /api/recommendations` | `{tracks, genres, reason}`. Query: `limit`, `recent` (comma genres, newest first), `seed` (+ `seed_genre`, `seed_title`) for autoplay, `exclude` (comma ids), `exclude_title` (repeatable). Signed-in listeners' stored plays and likes also count |
| `GET /api/lyrics?artist=<a>&title=<t>[&album=<al>&duration=<sec>]` or `?track_id=local-3` | `{lines: [str], synced: [{time, text}] \| null, instrumental, source, error}`; 404 if no lyrics are found. Catalogue tracks use their `.lrc` file first (`source: "local"`); otherwise LRCLIB `/api/get`, then a search. `synced` is only set when the recording is within 4 s of `duration` |
| `GET /api/likes` | `{tracks: [track]}`: the signed-in account's liked songs, newest first |
| `POST /api/likes` | Body `{track_id}` (`"local-3"` or an iTunes id; iTunes details are looked up server-side) |
| `DELETE /api/likes/<track_id>` | Removes a like |
| `GET /api/home_sections` | `{sections: [{key, title, tracks: [track]}]}` for the local catalogue: Trending Now, Viral on Reels & Shorts, Artists. Tracks come back in a new random order on every call |
| `GET /api/playlists[?track_id=local-3]` | `{playlists: [{id, name, track_count, created_at, contains_track?}]}` for the signed-in user |
| `POST /api/playlists` | Body `{name, track_id?}`: creates a playlist, optionally adding a track straight away |
| `GET /api/playlists/<id>` | `{id, name, track_count, created_at, tracks: [track]}` |
| `POST /api/playlists/<id>/tracks` | Body `{track_id}`: 201 when added, 200 with `added: false` if it was already there |
| `POST /api/track/play` | Body `{track_id, genre?}`: records a play and returns `{recorded, badge}` |
| `GET /api/user/badge` | `{badge: {badge, genre, plays, total_plays}}`, or `{badge: null}` before the first play |

Playlist, like and badge endpoints need a signed-in user and return 401 JSON otherwise. `POST /api/track/play` also
works for guests: it bumps catalogue play counts and returns `{recorded: false}`. Only Sonoria catalogue
tracks (`local-*` ids) can go in playlists. Listener badges come from the user's most played genre, as mapped in
[badges.py](badges.py).

Tracks store their media in `cover_art` and `canvas_video`; the API still returns them as `cover` and
`video_url`. Each `track` has this shape: `{id, title, artist, album, cover (600×600), stream_url (30s preview), duration (ms)}`.
Local catalogue tracks also have `video_url` (~20 s Canvas loop), `section` and `local: true`, their `id` looks like
`"local-3"`, and `stream_url` is the full-length MP3.
CORS is enabled for `/api/*` only.

### Local catalogue (full songs, covers and Canvas loops)
Put MP3s in `static/media/audio/`, then run:
```bash
pip install -r requirements-dev.txt
python generate_media.py          # add --force to re-render existing covers and videos
python generate_media.py --force --seed 7   # a different set of palettes, fonts and visualizers
```
Your own media always wins: put a cover at `static/media/covers/<slug>.jpg|jpeg|png|webp` and a canvas video at
`static/media/video/<slug>.mp4` (the slug comes from the MP3 name, e.g. `perfect`). Anything missing is generated:
a 600×600 cover and a 20-second looping MP4. Generated files are listed in `static/media/.generated.json`, and
`--force` only re-renders those, so it never overwrites your artwork. Each track gets its own palette, title font and cover pattern (drawn from the song's
loudness), plus one of four audio-reactive video visualizers: ring, bars, wave or pulse. The same `--seed` always
gives the same looks. It also adds or updates the track in the database.
Titles, artists and home-page sections come from the `CATALOG` table at the top of `generate_media.py`. Edit it
and re-run to change them. In the SPA, switch the player to **Video** to see a track's Canvas loop.

### Synced lyrics for the catalogue
```bash
python fetch_lyrics.py            # add --force to refetch
```
Saves timed LRC lyrics from LRCLIB to `instance/lyrics/<slug>.lrc` for every catalogue song whose recording
matches (within 4 s), so synced lyrics work offline. The slug comes from the MP3 name, e.g. `perfect.lrc` or
`challa-jab-tak-hai-jaan.lrc`. For a song LRCLIB doesn't have, drop your own `.lrc` file (lines like
`[00:12.50] lyric text`) in that folder; local files always win. `instance/` isn't committed to git.

### Deploying (e.g. Render)
The database isn't committed (`instance/` is git-ignored), so on startup the app creates it and fills in the
15 catalogue songs from `static/media/catalog.json`, which `generate_media.py` writes. Commit that file together
with the media, and re-run `generate_media.py` after changing the catalogue. Every path in it matches the committed
files exactly, including capitalization, which matters on Linux hosts. Startup is safe with several gunicorn workers.

- Start command: `gunicorn app:app`
- Set `SECRET_KEY` to a long random string.
- Render's disk is wiped on each deploy, so accounts, likes, playlists and uploads reset. To keep them, attach a
  persistent disk and set `SONORIA_DATABASE_URI=sqlite:////var/data/app.db` (uploads would also need to live on
  that disk or in external storage).

### Make yourself an admin
Open `instance/app.db` in DB Browser for SQLite. In the Execute SQL tab, run:
```sql
UPDATE user SET role='admin' WHERE username='yourusername';
```

## Automated player tests
`tests/player_test.py` is an end-to-end test that uses [Playwright](https://playwright.dev/python/)
to drive a real browser through the web player. It runs 49 checks:
- playback of iTunes previews and the time display
- seeking by click, drag and keyboard
- volume and mute
- play/pause, next/previous and auto-advance
- shuffle and repeat
- liking a track
- the queue and lyrics panels
- Audio/Video mode
- playing indicators in every view
- error toasts, including auto-skipping a broken preview
- the mobile layout

### 1. Install the test dependencies
With the virtual environment activated:
```bash
pip install -r requirements-dev.txt
```

The test uses a browser that is already installed, Microsoft Edge by default, so nothing else needs
downloading. To use Chrome instead, pass `--browser chrome`. To use Playwright's own Chromium, install it
first with `python -m playwright install chromium` and pass `--browser chromium`. Playwright's Chromium
may not play the AAC audio that iTunes previews use, so Edge or Chrome is recommended.

### 2. Run the tests
```bash
python tests/player_test.py
```

You don't need to start the app first. The test starts Sonoria on a free local port, runs every check,
and shuts it down again. A run takes about a minute and needs internet access.

Useful options:

| Option | Effect |
| --- | --- |
| `--browser msedge\|chrome\|chromium` | Which browser to use. Defaults to `msedge`, or the `SONORIA_BROWSER` environment variable if set. |
| `--headed` | Show the browser window while the tests run |
| `--url http://127.0.0.1:5000` | Test a server that is already running instead of starting one |
| `--screenshots <folder>` | Where to save screenshots. Defaults to `tests/screenshots/`, which git ignores. |

### Reading the results
Each check prints `PASS` or `FAIL`, then a summary like `49/49 passed`. The script exits with code `0` when
everything passes and `1` otherwise, so you can use it in CI.

Screenshots of the queue panel, lyrics panel, video mode, error toasts and mobile layout are saved for a visual review.

The output ends with a list of "expected network failures". These are intentional and don't mean a
check failed:
- LRCLIB returning 404 for instrumental tracks
- a deliberately broken preview URL used to test the error toast

Because the tests use live iTunes and LRCLIB data, a check can occasionally fail if either service is slow
or down. Re-run before assuming the app is broken.
