"""Builds cover art, 10-second Canvas loops, and database rows for every MP3 in static/media/audio/.

For each track:
  * static/media/covers/<slug>.jpg - 600x600 Violet Dusk cover with the track name
  * static/media/video/<slug>.mp4  - 10 s seamless loop: the cover pulsing to the song's own
                                     loudness, ringed by a spectrum visualizer
  * a Track row (section trending / viral / artist) owned by an artist User

Usage:  python generate_media.py [--force]     (--force re-renders covers and videos that already exist)
"""
import argparse
import hashlib
import math
import re
import os
import secrets
from pathlib import Path

import numpy as np
from moviepy import AudioFileClip, VideoClip
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# =====================================================================
# EDIT HERE: one entry per MP3 file in static/media/audio/
#   title   - name printed on the cover and shown in the app
#   artist  - artist name; None shows "Unknown Artist" (and no artist line on the cover)
#   album   - optional
#   section - "trending" (Trending Now), "viral" (Viral on Reels & Shorts) or "artist" (Artists)
#   genre   - optional: Romantic, HipHop, EDM, Devotional, Retro, Rock or Other
# After editing, run:  python generate_media.py --force
# The MP3 tags are missing or carry download-site spam ("- PagalNew"), so this table is the source of truth.
# =====================================================================
CATALOG = {
    "Billo Rani.mp3":              dict(title="Billo Rani", artist="Anand Raj Anand", album="Goal", section="trending", genre="Retro"),
    "Challa Jab Tak Hai Jaan.mp3": dict(title="Challa", artist="Rabbi Shergill", album="Jab Tak Hai Jaan", section="trending", genre="Rock"),
    "Babli tero mobile.mp3":       dict(title="Babli Tero Mobile", artist="Gajendra rana and Meena rana" , section="trending"),
    "Baby Doll.mp3":               dict(title="Baby Doll", artist="Dominic Fike", section="trending"),
    "Udi Udi.mp3":                 dict(title="Udi Udi", artist="Aneesh Pojari", section="trending"),
    "End_of_Beginning.mp3":        dict(title="End of Beginning", artist="Djo", album="Decide", section="viral"),
    "Earrings.mp3":                dict(title="Earrings", artist="Malcomm Tod", section="viral"),
    "Honeypie.mp3":                dict(title="Honeypie", artist="Jawny", section="viral"),
    "White keys.mp3":              dict(title="White Keys", artist="Dominic Fike", section="viral"),
    "I love you baby.mp3":         dict(title="I Love You Baby", artist="Emilee Flood", section="viral", genre="Romantic"),
    "Perfect.mp3":                 dict(title="Perfect", artist="Ed Sheeran", album="÷", section="artist", genre="Romantic"),
    "Baby.mp3":                    dict(title="Baby", artist="Justin Bieber", album="My World 2.0", section="artist"),
    "Nayan Ne Bandh Rakhine.mp3":  dict(title="Nayan Ne Bandh Rakhine", artist="Darshan Raval", section="artist", genre="Romantic"),
    "Pani Da Rang.mp3":            dict(title="Pani Da Rang", artist="Ayushmann Khurrana", album="Vicky Donor", section="artist", genre="Romantic"),
    "Kabhi Kabhi aditi.mp3":       dict(title="Kabhi Kabhi Aditi", artist="Rashid Ali", album="Jaane Tu... Ya Jaane Na", section="artist", genre="Romantic"),
}


ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "static" / "media"
AUDIO_DIR, COVER_DIR, VIDEO_DIR = MEDIA / "audio", MEDIA / "covers", MEDIA / "video"

# Violet Dusk palette
PLUM, MAUVE, PEACH = (0x50, 0x2D, 0x55), (0x93, 0x50, 0x73), (0xF6, 0xDB, 0xC0)
CREAM, DEEP = (0xF8, 0xF4, 0xE9), (0x1F, 0x10, 0x22)

COVER_SIZE = 600
VIDEO_SECONDS, VIDEO_FPS = 10, 24
LOOP_FADE_SECONDS = 1.0   # envelope crossfade that makes the last frame flow into the first
SAMPLE_RATE = 22050
RING_BARS = 72
UNKNOWN_ARTIST = "Unknown Artist"

def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def track_info(mp3):
    """Catalogue entry for an MP3, or a sensible default for files added later."""
    info = dict(CATALOG.get(mp3.name) or {"title": re.sub(r"[_-]+", " ", mp3.stem).strip().title(), "artist": None})
    info.setdefault("section", "trending")
    info.setdefault("album", None)
    info.setdefault("genre", "Other")
    info["slug"] = slugify(mp3.stem)
    return info


def track_seed(title):
    """Stable per-track randomness so re-runs produce identical art."""
    return int(hashlib.md5(title.encode("utf-8")).hexdigest()[:8], 16)


# ---------- Fonts ----------

FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")]
BOLD_FONTS = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]
REGULAR_FONTS = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Arial.ttf"]


def load_font(candidates, size):
    for folder in FONT_DIRS:
        for name in candidates:
            if (folder / name).exists():
                return ImageFont.truetype(str(folder / name), size)
    return ImageFont.load_default(size)


def wrap_text(draw, text, font, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + [line] if line else lines


# ---------- Cover art ----------

def gradient(size, angle, stops):
    """Linear gradient across the square at `angle` radians through the given RGB stops."""
    y, x = np.mgrid[0:size, 0:size] / (size - 1)
    t = (x - 0.5) * math.cos(angle) + (y - 0.5) * math.sin(angle)
    t = (t - t.min()) / (t.max() - t.min())
    positions = np.linspace(0, 1, len(stops))
    channels = [np.interp(t, positions, [c[i] for c in stops]) for i in range(3)]
    return Image.fromarray(np.stack(channels, axis=-1).astype(np.uint8))


def make_cover(info, out_path):
    rng = np.random.default_rng(track_seed(info["title"]))
    size, pad = COVER_SIZE, 44
    img = gradient(size, rng.uniform(0.35, 1.2), [PLUM, MAUVE, (0xC4, 0x86, 0x8C)]).convert("RGBA")

    # Soft peach glow + vinyl-style rings, positioned per track
    fx = ImageDraw.Draw(overlay := Image.new("RGBA", (size, size)))
    gx, gy = rng.uniform(0.55, 0.85) * size, rng.uniform(0.15, 0.4) * size
    fx.ellipse((gx - 170, gy - 170, gx + 170, gy + 170), fill=PEACH + (95,))
    img.alpha_composite(overlay.filter(ImageFilter.GaussianBlur(70)))
    fx = ImageDraw.Draw(overlay := Image.new("RGBA", (size, size)))
    for r in range(60, 260, 22):
        fx.ellipse((gx - r, gy - r, gx + r, gy + r), outline=PEACH + (34,), width=2)
    img.alpha_composite(overlay)

    # Darken the lower third so the title always reads
    shade = np.linspace(0, 1, size) ** 2.2 * 150
    img.alpha_composite(Image.fromarray(
        np.dstack([np.full((size, size, 3), DEEP, np.uint8), np.repeat(shade[:, None], size, 1).astype(np.uint8)])))

    draw = ImageDraw.Draw(img)
    draw.text((pad, pad - 6), "S O N O R I A", font=load_font(BOLD_FONTS, 18), fill=PEACH + (215,))

    # Title: largest size that fits in at most three lines (smaller cap for long titles)
    max_w = size - 2 * pad
    for font_size in range(68, 30, -4):
        title_font = load_font(BOLD_FONTS, font_size)
        lines = wrap_text(draw, info["title"], title_font, max_w)
        if len(lines) <= 2 or (len(lines) == 3 and font_size <= 56):
            break
    line_h = int(font_size * 1.08)
    artist = info["artist"]
    title_top = size - pad - (40 if artist else 0) - line_h * len(lines)

    # A waveform strip unique to the track, sitting just above the title block
    fx = ImageDraw.Draw(overlay := Image.new("RGBA", (size, size)))
    bars, base_y = 46, min(size * 0.55, title_top - 44)
    heights = np.abs(np.sin(np.linspace(0, rng.uniform(3, 7) * math.pi, bars))) * 0.6 + rng.uniform(0.1, 0.4, bars)
    step = (size - 2 * pad) / bars
    for i, h in enumerate(heights):
        x = pad + i * step + step / 2
        half = h * 30
        fx.rounded_rectangle((x - step * 0.28, base_y - half, x + step * 0.28, base_y + half), radius=3, fill=PEACH + (70,))
    img.alpha_composite(overlay)
    draw = ImageDraw.Draw(img)

    y = title_top
    for line in lines:
        draw.text((pad, y), line, font=title_font, fill=PEACH)
        y += line_h
    if artist:
        draw.text((pad, y + 8), artist, font=load_font(REGULAR_FONTS, 26), fill=CREAM + (210,))

    img.convert("RGB").save(out_path, "JPEG", quality=92, optimize=True)


# ---------- Audio analysis ----------

def analyse_audio(mp3, frames_needed):
    """Per-video-frame loudness and spectrum bands from the song's hook region.

    Returns (envelope[frames_needed], bands[frames_needed, n_bands]), both normalised to 0..1.
    """
    with AudioFileClip(str(mp3)) as clip:
        seconds = frames_needed / VIDEO_FPS
        start = min(max(0.0, clip.duration * 0.35), max(0.0, clip.duration - seconds))
        samples = clip.subclipped(start, min(clip.duration, start + seconds)).to_soundarray(fps=SAMPLE_RATE)
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples

    hop, win = SAMPLE_RATE / VIDEO_FPS, 2048
    edges = np.geomspace(60, 9000, RING_BARS // 4 + 1)       # log-spaced bands, mirrored around the ring later
    freqs = np.fft.rfftfreq(win, 1 / SAMPLE_RATE)
    window = np.hanning(win)
    padded = np.pad(mono, (win, win))
    env, bands = np.zeros(frames_needed), np.zeros((frames_needed, len(edges) - 1))
    for i in range(frames_needed):
        centre = int(i * hop) + win
        chunk = padded[centre - win // 2: centre + win // 2]
        env[i] = np.sqrt(np.mean(chunk ** 2))
        spectrum = np.abs(np.fft.rfft(chunk * window))
        bands[i] = [spectrum[(freqs >= lo) & (freqs < hi)].mean() for lo, hi in zip(edges[:-1], edges[1:])]

    def normalise(a):
        # Stretch each series between its quiet floor and loud peak so the motion is clearly visible
        lo, hi = np.percentile(a, 10, axis=0), np.percentile(a, 97, axis=0)
        return np.clip((a - lo) / (hi - lo + 1e-9), 0, 1)

    return normalise(env), normalise(np.log1p(bands))


def loop_crossfade(values, frames):
    """Folds the extra tail onto the head so values[frames-1] -> values[0] is continuous."""
    fade = len(values) - frames
    out = values[:frames].copy()
    w = np.linspace(0, 1, fade, endpoint=False).reshape(-1, *([1] * (values.ndim - 1)))
    out[:fade] = values[:fade] * w + values[frames:frames + fade] * (1 - w)
    return out


def smooth(values, attack=0.6, release=0.18):
    """Fast rise, slow fall - makes the pulse feel like a meter instead of flicker. Runs twice so the loop start is settled."""
    out, prev = np.empty_like(values), values[-1]
    for _ in range(2):
        for i, v in enumerate(values):
            prev = prev + (v - prev) * np.where(v > prev, attack, release)
            out[i] = prev
    return out


# ---------- Canvas video ----------

def make_video(info, mp3, cover_path, out_path):
    frames = VIDEO_SECONDS * VIDEO_FPS
    env, bands = analyse_audio(mp3, frames + int(LOOP_FADE_SECONDS * VIDEO_FPS))
    env, bands = smooth(loop_crossfade(env, frames)), smooth(loop_crossfade(bands, frames))
    ring = np.concatenate([bands, bands[:, ::-1]] * 2, axis=1)   # mirror so the ring is symmetric

    size, centre = COVER_SIZE, COVER_SIZE / 2
    cover = Image.open(cover_path).convert("RGB")
    background = Image.blend(cover.filter(ImageFilter.GaussianBlur(28)), Image.new("RGB", cover.size, DEEP), 0.45)
    art_base = 270
    mask = Image.new("L", (art_base * 2, art_base * 2))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, art_base * 2 - 1, art_base * 2 - 1), radius=36, fill=255)
    art_big = cover.resize((art_base * 2, art_base * 2), Image.LANCZOS)
    inner_r = art_base * 0.5 * math.sqrt(2) * 1.07 + 6
    angles = np.linspace(0, 2 * math.pi, RING_BARS, endpoint=False) - math.pi / 2

    def frame(t):
        i = min(int(round(t * VIDEO_FPS)), frames - 1)
        level = env[i]
        img = background.copy().convert("RGBA")

        # Glow behind the art, breathing with loudness
        glow = Image.new("RGBA", img.size)
        g = 150 + 50 * level
        ImageDraw.Draw(glow).ellipse((centre - g * 1.4, centre - g * 1.4, centre + g * 1.4, centre + g * 1.4),
                                     fill=MAUVE + (int(90 + 110 * level),))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(40)))

        # Spectrum ring, slowly turning a whole number of bar-steps so the loop is seamless
        spin = 2 * math.pi * (t / VIDEO_SECONDS) * (8 / RING_BARS)
        bars = Image.new("RGBA", img.size)
        bd = ImageDraw.Draw(bars)
        for a, h in zip(angles + spin, ring[i]):
            length = 5 + 40 * h
            x0, y0 = centre + inner_r * math.cos(a), centre + inner_r * math.sin(a)
            x1, y1 = centre + (inner_r + length) * math.cos(a), centre + (inner_r + length) * math.sin(a)
            bd.line((x0, y0, x1, y1), fill=PEACH + (int(150 + 105 * h),), width=5)
        img.alpha_composite(bars.filter(ImageFilter.GaussianBlur(3)))
        img.alpha_composite(bars)

        # Cover art, scaling with the beat
        side = int(art_base * (1.0 + 0.07 * level))
        art = art_big.resize((side, side), Image.BILINEAR)
        shadow = Image.new("RGBA", img.size)
        o = int(centre - side / 2)
        ImageDraw.Draw(shadow).rounded_rectangle((o, o + 12, o + side, o + side + 12), radius=24, fill=(0, 0, 0, 150))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
        img.paste(art, (o, o), mask.resize((side, side), Image.BILINEAR))
        return np.asarray(img.convert("RGB"))

    VideoClip(frame, duration=VIDEO_SECONDS).write_videofile(
        str(out_path), fps=VIDEO_FPS, codec="libx264", audio=False, preset="medium", logger=None,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "26", "-movflags", "+faststart"],
    )


# ---------- Database ----------

def site_path(file):
    """The file's exact on-disk path as a site-relative string, e.g. /static/media/audio/Baby Doll.mp3.

    Stored in the database unencoded so it matches the filename character for character;
    Track.to_dict() percent-encodes it into a URL for the API.
    """
    return "/" + file.relative_to(ROOT).as_posix()


def exact_case_exists(stored_path):
    """True when stored_path names an existing file with exactly this spelling and case.

    Windows ignores case, so Path.exists() alone would pass /static/Media/... or baby doll.MP3,
    which then 404 on a case-sensitive host.
    """
    current = ROOT
    for part in stored_path.lstrip("/").split("/"):
        if not current.is_dir() or part not in os.listdir(current):
            return False
        current = current / part
    return current.is_file()


def verify_database_paths():
    """Checks every catalogue row's audio, cover and video path against the files on disk."""
    from app import app
    from models import Track

    with app.app_context():
        tracks = Track.query.filter(Track.section.isnot(None)).order_by(Track.id).all()
        problems = [f"  {t.title}: {field} {path!r}"
                    for t in tracks
                    for field, path in (("audio", t.stream_url), ("cover", t.cover), ("video", t.video_url))
                    if not path or not exact_case_exists(path)]
    if problems:
        raise SystemExit("Database paths that don't match a file on disk exactly:\n" + "\n".join(problems))
    print(f"Verified: all {len(tracks) * 3} audio/cover/video paths in the database match files on disk exactly.")


def seed_database(entries):
    """Upserts one approved Track per MP3 (keyed by its audio filename), each owned by an artist User."""
    from app import app
    from models import db, User, Track, upgrade_schema

    with app.app_context():
        db.create_all()
        upgrade_schema()
        created = updated = 0
        for info in entries:
            artist_name = info["artist"] or UNKNOWN_ARTIST
            artist = User.query.filter_by(username=artist_name).first()
            if not artist:
                artist = User(username=artist_name, role="artist")
                artist.set_password(secrets.token_urlsafe(24))   # catalogue account; nobody logs in as it
                db.session.add(artist)
                db.session.flush()

            track = Track.query.filter(Track.section.isnot(None), Track.audio_file == info["file"]).first()
            if track:
                updated += 1
            else:
                track = Track(play_count=0)
                db.session.add(track)
                created += 1
            track.title = info["title"]
            track.artist_id = artist.id
            track.album = info["album"]
            track.genre = info["genre"]
            track.section = info["section"]
            track.stream_url = info["stream_url"]
            track.cover = info["cover"]
            track.video_url = info["video_url"]
            track.audio_file = info["file"]
            track.duration_ms = info["duration_ms"]
            track.approved = True
        db.session.commit()
        total = Track.query.filter(Track.section.isnot(None)).count()
    print(f"Database: {created} created, {updated} updated, {total} local catalogue tracks in total.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-render covers and videos that already exist")
    args = parser.parse_args()

    COVER_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    mp3s = sorted(AUDIO_DIR.glob("*.mp3"), key=lambda p: p.name.lower())
    if not mp3s:
        raise SystemExit(f"No MP3 files found in {AUDIO_DIR}")

    entries = []
    for n, mp3 in enumerate(mp3s, 1):
        info = track_info(mp3)
        cover_path = COVER_DIR / f"{info['slug']}.jpg"
        video_path = VIDEO_DIR / f"{info['slug']}.mp4"
        with AudioFileClip(str(mp3)) as clip:
            duration = clip.duration

        status = []
        if args.force or not cover_path.exists():
            make_cover(info, cover_path)
            status.append("cover")
        if args.force or not video_path.exists():
            make_video(info, mp3, cover_path, video_path)
            status.append("video")
        print(f"[{n:2}/{len(mp3s)}] {info['title']:<24} {duration / 60:4.1f} min  "
              f"{info['section']:<8} {'rendered ' + ' + '.join(status) if status else 'up to date'}")

        entries.append({
            **info,
            "file": mp3.name,
            "duration_ms": int(duration * 1000),
            "stream_url": site_path(mp3),
            "cover": site_path(cover_path),
            "video_url": site_path(video_path),
        })

    seed_database(entries)
    verify_database_paths()


if __name__ == "__main__":
    main()
