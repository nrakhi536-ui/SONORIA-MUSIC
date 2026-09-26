# Sonoria

A Flask music streaming app with a single-page web player styled with the "Violet Dusk" design system.
It streams 30-second song previews from the iTunes Search API and shows lyrics from LRCLIB.

## Features
- **Web player (`/spa`)**: Home, Explore, Library and Search views; mood filters; a persistent player
  with seek, volume, shuffle, repeat, a queue panel, a lyrics panel and a video/visualizer mode.
- **JSON API**: iTunes search, home-page sections and lyrics lookup (see below).
- **Accounts and roles**: signup, login and logout for customers, artists and admins.
- **Artist tools**: upload tracks, edit tracks and view an artist dashboard.
- **Admin tools**: dashboard, and approve or reject uploaded tracks.

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
| http://127.0.0.1:5000/spa | The single-page web player |
| http://127.0.0.1:5000/signup | Create an account |
| http://127.0.0.1:5000/login | Log in |
| http://127.0.0.1:5000/ | The older server-rendered home page |

You can open a view directly with a link like `/spa#library` or `/spa#explore`.

### JSON API
| Endpoint | Returns |
| --- | --- |
| `GET /api/search?q=<term>` | `{results: [track], error}`: up to 25 iTunes songs |
| `GET /api/songs` | `{sections: {"Chill": [...], "Top Hits": [...], "Synthwave": [...]}}` for the home page |
| `GET /api/lyrics?artist=<a>&title=<t>[&album=<al>&duration=<sec>]` | `{lines: [str], instrumental, error}`; 404 if no lyrics are found |
| `GET /api/home_sections` | `{sections: [{key, title, tracks: [track]}]}` for the local catalogue: Trending Now, Viral on Reels & Shorts, Artists |

Each `track` has this shape: `{id, title, artist, album, cover (600×600), stream_url (30s preview), duration (ms)}`.
Local catalogue tracks also have `video_url` (10 s Canvas loop), `section` and `local: true`, their `id` looks like
`"local-3"`, and `stream_url` is the full-length MP3.
CORS is enabled for `/api/*` only.

### Local catalogue (full songs, covers and Canvas loops)
Put MP3s in `static/media/audio/`, then run:
```bash
pip install -r requirements-dev.txt
python generate_media.py          # add --force to re-render existing covers and videos
```
For each MP3 this writes a 600×600 cover to `static/media/covers/` and a 10-second looping MP4 to
`static/media/video/`. The video pulses to the song's own audio. It also adds or updates the track in the database.
Titles, artists and home-page sections come from the `CATALOG` table at the top of `generate_media.py`. Edit it
and re-run to change them. In the SPA, switch the player to **Video** to see a track's Canvas loop.

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
