"""Builds cover art, 20-second Canvas loops, and database rows for every MP3 in static/media/audio/.

For each track:
  * static/media/covers/<slug>.(jpg|jpeg|png|webp) - your own cover if one exists, otherwise a generated
                                     600x600 cover in the track's own palette and font
  * static/media/video/<slug>.mp4  - your own canvas video if one exists, otherwise a generated 20 s seamless
                                     loop: the cover pulsing to the song's loudness with one of four visualizers
  * a Track row (section trending / viral / artist, cover_art, canvas_video) owned by an artist User

Files this script generates are listed in static/media/.generated.json. It never overwrites anything else,
so covers and videos you drop in yourself are always kept, even with --force.

Usage:  python generate_media.py [--force] [--seed N]
        --force  re-render the covers and videos this script generated (yours are left alone)
        --seed   pick a different set of looks (default 0; the same seed always gives the same art)
"""
import argparse
import colorsys
import hashlib
import json
import math
import re
import os
import random
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from moviepy import AudioFileClip, VideoClip
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from catalog import CATALOG_FILE, exact_case_exists, write_catalog

# =====================================================================
# EDIT HERE: one entry per MP3 file in static/media/audio/
#   title   - name printed on the cover and shown in the app
#   artist  - artist name; None shows "Unknown Artist" (and no artist line on the cover)
#   album   - optional
#   section - "trending" (Trending Now), "viral" (Viral on Reels & Shorts) or "artist" (Artists)
#   genre   - drives listener badges (see badges.py): Romantic, Sad, Classical, Retro, Rock, Devotional,
#             Party, Folk, Pop, Indie, EDM, Bollywood, Punjabi, Lofi, HipHop, or Other (counts toward no badge)
# After editing, run:  python generate_media.py --force
# The MP3 tags are missing or carry download-site spam ("- PagalNew"), so this table is the source of truth.
# =====================================================================
CATALOG = {
    "Billo Rani.mp3":              dict(title="Billo Rani", artist="Anand Raj Anand", album="Goal", section="trending", genre="Party"),
    "Challa Jab Tak Hai Jaan.mp3": dict(title="Challa", artist="Rabbi Shergill", album="Jab Tak Hai Jaan", section="trending", genre="Rock"),
    "Babli tero mobile.mp3":       dict(title="Babli Tero Mobile", artist="Gajendra rana and Meena rana", section="trending", genre="Folk"),
    "Baby Doll.mp3":               dict(title="Baby Doll", artist="Dominic Fike", section="trending", genre="Indie"),
    "Udi Udi.mp3":                 dict(title="Udi Udi", artist="Aneesh Pojari", section="trending"),
    "End_of_Beginning.mp3":        dict(title="End of Beginning", artist="Djo", album="Decide", section="viral", genre="Indie"),
    "Earrings.mp3":                dict(title="Earrings", artist="Malcomm Tod", section="viral", genre="Indie"),
    "Honeypie.mp3":                dict(title="Honeypie", artist="Jawny", section="viral", genre="Indie"),
    "White keys.mp3":              dict(title="White Keys", artist="Dominic Fike", section="viral", genre="Indie"),
    "I love you baby.mp3":         dict(title="I Love You Baby", artist="Emilee Flood", section="viral", genre="Romantic"),
    "Perfect.mp3":                 dict(title="Perfect", artist="Ed Sheeran", album="÷", section="artist", genre="Romantic"),
    "Baby.mp3":                    dict(title="Baby", artist="Justin Bieber", album="My World 2.0", section="artist", genre="Pop"),
    "Nayan Ne Bandh Rakhine.mp3":  dict(title="Nayan Ne Bandh Rakhine", artist="Darshan Raval", section="artist", genre="Romantic"),
    "Pani Da Rang.mp3":            dict(title="Pani Da Rang", artist="Ayushmann Khurrana", album="Vicky Donor", section="artist", genre="Sad"),
    "Kabhi Kabhi aditi.mp3":       dict(title="Kabhi Kabhi Aditi", artist="Rashid Ali", album="Jaane Tu... Ya Jaane Na", section="artist", genre="Bollywood"),
}


ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "static" / "media"
AUDIO_DIR, COVER_DIR, VIDEO_DIR = MEDIA / "audio", MEDIA / "covers", MEDIA / "video"

COVER_SIZE = 600
VIDEO_SECONDS, VIDEO_FPS = 20, 24
LOOP_FADE_SECONDS = 1.0   # envelope crossfade that makes the last frame flow into the first
SAMPLE_RATE = 22050
RING_BARS = 72
WAVE_POINTS = 160         # samples per frame for the oscilloscope visualizer

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


def track_seed(title, seed=0):
    """Stable per-track randomness (shifted by --seed) so re-runs produce identical art."""
    return int(hashlib.md5(f"{seed}:{title}".encode("utf-8")).hexdigest()[:8], 16)


# ---------- Visual styles ----------
# Every track gets its own palette, title font, gradient, cover pattern and video visualizer.
# The lists are shuffled by --seed and walked with different strides, so tracks next to each other
# never share a combination. The same seed always gives the same look.

def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


PALETTES = [  # name, gradient stops (dark -> light), accent for text and visualizers
    ("Violet Dusk", ["#502D55", "#935073", "#C4868C"], "#F6DBC0"),
    ("Midnight Ocean", ["#0B1D3A", "#1F4E79", "#3FA7B5"], "#D8F3F0"),
    ("Ember Sunset", ["#3A0F1E", "#B23A48", "#F28C38"], "#FFE3C2"),
    ("Neon Orchid", ["#1A0B2E", "#6A1B9A", "#D65BE8"], "#F8D7FF"),
    ("Forest Mist", ["#0F2A24", "#2E6B55", "#8DBF8B"], "#EAF5DC"),
    ("Gold Noir", ["#141210", "#3D2E12", "#B8911F"], "#FFF1C1"),
    ("Rose Quartz", ["#3B1C32", "#A64D79", "#F2A7C3"], "#FFE9F1"),
    ("Electric Teal", ["#041C24", "#0E6E73", "#2FC9AE"], "#E0FFF8"),
    ("Crimson Night", ["#1B0A12", "#6B1030", "#D7263D"], "#FFD6DC"),
    ("Lavender Haze", ["#2A2250", "#6C5B9E", "#B8A9E8"], "#F4F0FF"),
    ("Desert Dune", ["#2B1B12", "#8C5A3C", "#E0A96D"], "#FFF4E4"),
    ("Arctic Blue", ["#0E1A2B", "#35598F", "#8FB8E8"], "#EEF6FF"),
    ("Cobalt Flame", ["#0A1033", "#2438A6", "#E8615E"], "#FFE1E1"),
    ("Sage Blush", ["#1E2A24", "#5E7D66", "#E0A9AE"], "#FFF0F0"),
    ("Indigo Coral", ["#1B1440", "#4B3A9E", "#F07E6A"], "#FFE8E3"),
]

# Title fonts: (file, uppercase?). Only the ones installed are used; DejaVu covers Linux.
TITLE_FONTS = [
    ("seguibl.ttf", False), ("georgiab.ttf", False), ("impact.ttf", True), ("GILSANUB.TTF", False),
    ("bahnschrift.ttf", True), ("palab.ttf", False), ("trebucbd.ttf", False), ("FRAMDCN.TTF", True),
    ("segoeprb.ttf", False), ("constanb.ttf", False),
    ("DejaVuSans-Bold.ttf", False), ("DejaVuSerif-Bold.ttf", False), ("DejaVuSansCondensed-Bold.ttf", True),
]
COVER_PATTERNS = ["bars", "wave", "rings", "dots", "line"]
VIDEO_STYLES = ["ring", "bars", "wave", "pulse"]
GRADIENTS = ["linear", "radial"]
LAYOUTS = ["left", "center"]


def jitter_hue(rgb, shift):
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return tuple(int(round(c * 255)) for c in colorsys.hls_to_rgb((h + shift) % 1.0, l, s))


def assign_styles(infos, seed=0):
    """One style dict per track, in the same order as infos."""
    shuffler = random.Random(seed)
    lists = {
        "palette": PALETTES[:], "font": [f for f in TITLE_FONTS if find_font(f[0])] or [(BOLD_FONTS[0], False)],
        "pattern": COVER_PATTERNS[:], "video": VIDEO_STYLES[:], "gradient": GRADIENTS[:], "layout": LAYOUTS[:],
    }
    for items in lists.values():
        shuffler.shuffle(items)
    strides = {"palette": 1, "font": 3, "pattern": 2, "video": 3, "gradient": 1, "layout": 1}

    styles = []
    for n, info in enumerate(infos):
        rng = random.Random(track_seed(info["title"], seed))
        pick = {key: items[(n * strides[key]) % len(items)] for key, items in lists.items()}
        pick["layout"] = lists["layout"][(n // 2) % len(lists["layout"])]  # not in lockstep with gradient
        name, stops, accent = pick["palette"]
        shift = rng.uniform(-0.03, 0.03)
        styles.append({
            "palette": name,
            "stops": [jitter_hue(hex_rgb(c), shift) for c in stops],
            "accent": hex_rgb(accent),
            "font": pick["font"][0],
            "uppercase": pick["font"][1],
            "pattern": pick["pattern"],
            "video": pick["video"],
            "gradient": pick["gradient"],
            "layout": pick["layout"],
            "angle": rng.uniform(0, 2 * math.pi),
            "glow": (rng.uniform(0.2, 0.8), rng.uniform(0.12, 0.4)),
        })
    return styles


# ---------- Fonts ----------

FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")]
BOLD_FONTS = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]
REGULAR_FONTS = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Arial.ttf"]


def find_font(name):
    for folder in FONT_DIRS:
        if (folder / name).exists():
            return folder / name
    return None


def load_font(candidates, size):
    for name in candidates:
        path = find_font(name)
        if path:
            return ImageFont.truetype(str(path), size)
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


# ---------- Audio analysis ----------

def song_envelope(mp3, points):
    """Loudness across the whole song in `points` buckets (0..1) - the shape drawn on the cover.

    Decodes with ffmpeg directly: MoviePy's to_soundarray() returns near-silent garbage at low sample rates.
    """
    raw = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    mono = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    buckets = np.array_split(mono, points)
    env = np.array([np.sqrt(np.mean(b ** 2)) if len(b) else 0.0 for b in buckets])
    lo, hi = np.percentile(env, 5), np.percentile(env, 98)
    return np.clip((env - lo) / (hi - lo + 1e-9), 0.05, 1)


def analyse_audio(mp3, frames_needed):
    """Per-video-frame loudness, spectrum bands and waveform from the song's hook region.

    Returns (envelope[frames], bands[frames, n_bands], wave[frames, WAVE_POINTS]);
    envelope and bands are 0..1, wave is -1..1.
    """
    with AudioFileClip(str(mp3)) as clip:
        seconds = frames_needed / VIDEO_FPS
        start = min(max(0.0, clip.duration * 0.35), max(0.0, clip.duration - seconds))
        samples = clip.subclipped(start, min(clip.duration, start + seconds)).to_soundarray(fps=SAMPLE_RATE)
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples

    hop, win = SAMPLE_RATE / VIDEO_FPS, 2048
    edges = np.geomspace(60, 9000, RING_BARS // 4 + 1)       # log-spaced bands, mirrored by the visualizers
    freqs = np.fft.rfftfreq(win, 1 / SAMPLE_RATE)
    window = np.hanning(win)
    padded = np.pad(mono, (win, win))
    env, bands = np.zeros(frames_needed), np.zeros((frames_needed, len(edges) - 1))
    wave = np.zeros((frames_needed, WAVE_POINTS))
    for i in range(frames_needed):
        centre = int(i * hop) + win
        chunk = padded[centre - win // 2: centre + win // 2]
        env[i] = np.sqrt(np.mean(chunk ** 2))
        spectrum = np.abs(np.fft.rfft(chunk * window))
        bands[i] = [spectrum[(freqs >= lo) & (freqs < hi)].mean() for lo, hi in zip(edges[:-1], edges[1:])]
        wave[i] = chunk[: (win // WAVE_POINTS) * WAVE_POINTS].reshape(WAVE_POINTS, -1).mean(axis=1)

    def normalise(a):
        # Stretch each series between its quiet floor and loud peak so the motion is clearly visible
        lo, hi = np.percentile(a, 10, axis=0), np.percentile(a, 97, axis=0)
        return np.clip((a - lo) / (hi - lo + 1e-9), 0, 1)

    peak = np.percentile(np.abs(wave), 99) + 1e-9
    return normalise(env), normalise(np.log1p(bands)), np.clip(wave / peak, -1, 1)


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


# ---------- Cover art ----------

def gradient(size, stops, kind, angle, centre):
    """A linear (at `angle` radians) or radial (from `centre`, 0..1 coords) gradient through RGB stops."""
    y, x = np.mgrid[0:size, 0:size] / (size - 1)
    if kind == "radial":
        t = np.hypot(x - centre[0], y - centre[1])
    else:
        t = (x - 0.5) * math.cos(angle) + (y - 0.5) * math.sin(angle)
    t = (t - t.min()) / (t.max() - t.min())
    if kind == "radial":
        stops = stops[::-1]  # light at the centre
    positions = np.linspace(0, 1, len(stops))
    channels = [np.interp(t, positions, [c[i] for c in stops]) for i in range(3)]
    return Image.fromarray(np.stack(channels, axis=-1).astype(np.uint8))


def draw_cover_pattern(size, pattern, env, accent, box, centre):
    """The song's own loudness shape, drawn in one of several styles inside box = (left, top, right, bottom)."""
    layer = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(layer)
    left, top, right, bottom = box
    width, mid = right - left, (top + bottom) / 2
    amp = (bottom - top) / 2
    xs = np.linspace(left, right, len(env))

    if pattern == "bars":
        step = width / len(env)
        for x, h in zip(xs, env):
            d.rounded_rectangle((x - step * 0.3, mid - h * amp, x + step * 0.3, mid + h * amp), radius=3, fill=accent + (78,))
    elif pattern == "wave":
        for k, alpha in enumerate((40, 60, 90)):
            shift = int(len(env) * 0.12 * k)
            vals = np.roll(env, shift) * (1 - 0.18 * k)
            pts = [(x, bottom - v * amp * 1.8) for x, v in zip(xs, vals)]
            d.polygon([(left, bottom)] + pts + [(right, bottom)], fill=accent + (alpha,))
    elif pattern == "rings":
        cx, cy = centre
        angles = np.linspace(0, 2 * math.pi, len(env), endpoint=False)
        closed = np.append(env, env[0])
        for k, (r0, spread) in enumerate(((70, 40), (125, 50), (185, 55))):
            pts = [(cx + (r0 + v * spread) * math.cos(a), cy + (r0 + v * spread) * math.sin(a))
                   for a, v in zip(np.append(angles, angles[0]), closed)]
            d.line(pts, fill=accent + (110 - 25 * k,), width=3, joint="curve")
    elif pattern == "dots":
        rows, step = 9, width / len(env)
        for x, v in zip(xs, env):
            lit = max(1, int(round(v * rows)))
            for r in range(rows):
                y = bottom - r * (bottom - top) / rows
                d.ellipse((x - step * 0.28, y - step * 0.28, x + step * 0.28, y + step * 0.28),
                          fill=accent + (165 if r < lit else 28,))
    elif pattern == "line":
        pts = [(x, mid - v * amp * math.sin(i * 0.9)) for i, (x, v) in enumerate(zip(xs, env))]
        d.line(pts, fill=accent + (200,), width=4, joint="curve")
        glow = layer.filter(ImageFilter.GaussianBlur(8))
        glow.alpha_composite(layer)
        return glow
    return layer


def make_cover(info, style, env, out_path):
    size, pad = COVER_SIZE, 44
    stops, accent = style["stops"], style["accent"]
    deep = tuple(int(c * 0.45) for c in stops[0])
    img = gradient(size, stops, style["gradient"], style["angle"], style["glow"]).convert("RGBA")

    # Soft accent glow, positioned per track
    gx, gy = style["glow"][0] * size, style["glow"][1] * size
    overlay = Image.new("RGBA", (size, size))
    ImageDraw.Draw(overlay).ellipse((gx - 170, gy - 170, gx + 170, gy + 170), fill=accent + (80,))
    img.alpha_composite(overlay.filter(ImageFilter.GaussianBlur(70)))

    # Darken the lower part so the title always reads
    shade = np.linspace(0, 1, size) ** 2.0 * 170
    img.alpha_composite(Image.fromarray(
        np.dstack([np.full((size, size, 3), deep, np.uint8), np.repeat(shade[:, None], size, 1).astype(np.uint8)])))

    # Title: largest size where every line fits and there are at most three lines
    draw = ImageDraw.Draw(img)
    title = info["title"].upper() if style["uppercase"] else info["title"]
    max_w = size - 2 * pad
    for font_size in range(72, 28, -4):
        title_font = load_font([style["font"], *BOLD_FONTS], font_size)
        lines = wrap_text(draw, title, title_font, max_w)
        fits = all(draw.textlength(line, font=title_font) <= max_w for line in lines)
        if fits and (len(lines) <= 2 or (len(lines) == 3 and font_size <= 56)):
            break
    ascent, descent = title_font.getmetrics()
    line_h = int((ascent + descent) * 0.98)
    artist = info["artist"]
    artist_font = load_font(REGULAR_FONTS, 26)
    title_top = size - pad - (42 if artist else 0) - line_h * len(lines)

    # The song's loudness shape: a band above the title, or rings around the glow
    band_bottom = min(size * 0.62, title_top - 24)
    box = (pad, max(pad + 40, band_bottom - 150), size - pad, band_bottom)
    img.alpha_composite(draw_cover_pattern(size, style["pattern"], env, accent, box, (gx, gy)))

    draw = ImageDraw.Draw(img)
    draw.text((pad, pad - 6), "S O N O R I A", font=load_font(BOLD_FONTS, 18), fill=accent + (215,))

    def x_for(text, font):
        return (size - draw.textlength(text, font=font)) / 2 if style["layout"] == "center" else pad

    y = title_top
    for line in lines:
        draw.text((x_for(line, title_font), y), line, font=title_font, fill=accent)
        y += line_h
    if artist:
        draw.text((x_for(artist, artist_font), y + 8), artist, font=artist_font, fill=(248, 244, 233, 215))

    img.convert("RGB").save(out_path, "JPEG", quality=92, optimize=True)


# ---------- Canvas video ----------

def make_video(style, mp3, cover_path, out_path):
    frames = VIDEO_SECONDS * VIDEO_FPS
    env, bands, wave = analyse_audio(mp3, frames + int(LOOP_FADE_SECONDS * VIDEO_FPS))
    env, bands = smooth(loop_crossfade(env, frames)), smooth(loop_crossfade(bands, frames))
    wave = loop_crossfade(wave, frames)
    mirrored = np.concatenate([bands, bands[:, ::-1]], axis=1)          # 36 bands, symmetric
    ring = np.concatenate([mirrored, mirrored], axis=1)                   # 72 around the circle

    size, centre = COVER_SIZE, COVER_SIZE / 2
    accent, glow_rgb = style["accent"], style["stops"][1]
    deep = tuple(int(c * 0.35) for c in style["stops"][0])
    kind = style["video"]
    cover = Image.open(cover_path).convert("RGB")
    background = Image.blend(cover.filter(ImageFilter.GaussianBlur(28)), Image.new("RGB", cover.size, deep), 0.5)

    art_base = {"ring": 270, "bars": 250, "wave": 260, "pulse": 240}[kind]
    art_cy = {"bars": centre - 40}.get(kind, centre)
    mask = Image.new("L", (art_base * 2, art_base * 2))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, art_base * 2 - 1, art_base * 2 - 1), radius=36, fill=255)
    art_big = cover.resize((art_base * 2, art_base * 2), Image.LANCZOS)
    inner_r = art_base * 0.5 * math.sqrt(2) * 1.07 + 6
    angles = np.linspace(0, 2 * math.pi, RING_BARS, endpoint=False) - math.pi / 2

    def visualizer(t, i, level):
        layer = Image.new("RGBA", (size, size))
        d = ImageDraw.Draw(layer)
        if kind == "ring":
            # Spectrum ring, turning a whole number of bar-steps per loop so it is seamless
            spin = 2 * math.pi * (t / VIDEO_SECONDS) * (8 / RING_BARS)
            for a, h in zip(angles + spin, ring[i]):
                length = 5 + 40 * h
                d.line((centre + inner_r * math.cos(a), centre + inner_r * math.sin(a),
                        centre + (inner_r + length) * math.cos(a), centre + (inner_r + length) * math.sin(a)),
                       fill=accent + (int(150 + 105 * h),), width=5)
        elif kind == "bars":
            # Spectrum bars along the bottom with a faint reflection
            n, base = mirrored.shape[1], size - 70
            step = (size - 60) / n
            for k, h in enumerate(mirrored[i]):
                x = 30 + k * step + step / 2
                top = base - (8 + 110 * h)
                d.rounded_rectangle((x - step * 0.34, top, x + step * 0.34, base), radius=3, fill=accent + (int(160 + 95 * h),))
                d.rounded_rectangle((x - step * 0.34, base + 4, x + step * 0.34, base + 4 + (base - top) * 0.3),
                                    radius=3, fill=accent + (45,))
        elif kind == "wave":
            # Oscilloscope traces of the actual waveform, echoed behind the art
            xs = np.linspace(0, size, WAVE_POINTS)
            for k, (alpha, gain) in enumerate(((230, 150), (110, 110), (60, 80))):
                w = wave[(i - 2 * k) % frames]
                d.line([(x, centre + v * gain) for x, v in zip(xs, w)], fill=accent + (alpha,), width=4 - k, joint="curve")
        elif kind == "pulse":
            # Rings expanding from the art; a whole number per loop so the last frame matches the first
            period = VIDEO_SECONDS / round(VIDEO_SECONDS / 1.25)
            for k in range(4):
                phase = ((t / period) + k / 4) % 1.0
                r = inner_r * 0.85 + phase * (size * 0.5 - inner_r * 0.85 + 30)
                alpha = int((1 - phase) * (70 + 150 * level))
                d.ellipse((centre - r, centre - r, centre + r, centre + r), outline=accent + (alpha,), width=4)
        blurred = layer.filter(ImageFilter.GaussianBlur(3))
        blurred.alpha_composite(layer)
        return blurred

    def frame(t):
        i = min(int(round(t * VIDEO_FPS)), frames - 1)
        level = env[i]
        img = background.copy().convert("RGBA")

        # Glow behind the art, breathing with loudness
        glow = Image.new("RGBA", img.size)
        g = (150 + 50 * level) * 1.4
        ImageDraw.Draw(glow).ellipse((centre - g, art_cy - g, centre + g, art_cy + g),
                                     fill=glow_rgb + (int(90 + 110 * level),))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(40)))
        img.alpha_composite(visualizer(t, i, level))

        # Cover art, scaling with the beat
        side = int(art_base * (1.0 + 0.07 * level))
        art = art_big.resize((side, side), Image.BILINEAR)
        ox, oy = int(centre - side / 2), int(art_cy - side / 2)
        shadow = Image.new("RGBA", img.size)
        ImageDraw.Draw(shadow).rounded_rectangle((ox, oy + 12, ox + side, oy + side + 12), radius=24, fill=(0, 0, 0, 150))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
        img.paste(art, (ox, oy), mask.resize((side, side), Image.BILINEAR))
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


def verify_database_paths():
    """Checks every catalogue row's audio, cover and video path against the files on disk."""
    from app import app
    from models import Track

    with app.app_context():
        tracks = Track.query.filter(Track.section.isnot(None)).order_by(Track.id).all()
        problems = [f"  {t.title}: {field} {path!r}"
                    for t in tracks
                    for field, path in (("audio", t.stream_url), ("cover", t.cover_art), ("video", t.canvas_video))
                    if not path or not exact_case_exists(path)]
    if problems:
        raise SystemExit("Database paths that don't match a file on disk exactly:\n" + "\n".join(problems))
    print(f"Verified: all {len(tracks) * 3} audio/cover/video paths in the database match files on disk exactly.")


def seed_database(entries):
    """Upserts the catalogue into the database (the same code the app runs on startup, see catalog.py)."""
    from app import app
    from catalog import sync_catalog
    from models import Track

    with app.app_context():
        report = sync_catalog(entries)
        total = Track.query.filter(Track.section.isnot(None)).count()
    print(f"Database: {report['created']} created, {report['updated']} updated, {total} local catalogue tracks in total.")


COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MANIFEST = MEDIA / ".generated.json"


def file_hash(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


def load_manifest():
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def existing_cover(slug):
    """The track's cover in any supported format, preferring one you supplied."""
    for ext in COVER_EXTENSIONS:
        path = COVER_DIR / f"{slug}{ext}"
        if path.exists():
            return path
    return None


def is_generated(path, manifest):
    """True if this script made the file and nobody has replaced it since."""
    key = path.relative_to(MEDIA).as_posix()
    return path.exists() and manifest.get(key) == file_hash(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-render covers and videos that already exist")
    parser.add_argument("--seed", type=int, default=0, help="choose a different set of looks (default 0)")
    args = parser.parse_args()

    COVER_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    mp3s = sorted(AUDIO_DIR.glob("*.mp3"), key=lambda p: p.name.lower())
    if not mp3s:
        raise SystemExit(f"No MP3 files found in {AUDIO_DIR}")

    infos = [track_info(mp3) for mp3 in mp3s]
    styles = assign_styles(infos, args.seed)
    manifest = load_manifest()
    entries = []
    for n, (mp3, info, style) in enumerate(zip(mp3s, infos, styles), 1):
        with AudioFileClip(str(mp3)) as clip:
            duration = clip.duration

        status = []
        cover_path = existing_cover(info["slug"])
        if cover_path is None or (args.force and is_generated(cover_path, manifest)):
            cover_path = cover_path or COVER_DIR / f"{info['slug']}.jpg"
            make_cover(info, style, song_envelope(mp3, 56), cover_path)
            manifest[cover_path.relative_to(MEDIA).as_posix()] = file_hash(cover_path)
            status.append("rendered cover")
        elif not is_generated(cover_path, manifest):
            status.append("your cover")

        video_path = VIDEO_DIR / f"{info['slug']}.mp4"
        if not video_path.exists() or (args.force and is_generated(video_path, manifest)):
            make_video(style, mp3, cover_path, video_path)
            manifest[video_path.relative_to(MEDIA).as_posix()] = file_hash(video_path)
            status.append("rendered video")
        elif not is_generated(video_path, manifest):
            status.append("your video")
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        print(f"[{n:2}/{len(mp3s)}] {info['title']:<24} {duration / 60:4.1f} min  {info['section']:<8} "
              f"{', '.join(status) or 'up to date'}")

        entries.append({
            **info,
            "file": mp3.name,
            "duration_ms": int(duration * 1000),
            "stream_url": site_path(mp3),
            "cover": site_path(cover_path),
            "video_url": site_path(video_path),
        })

    write_catalog(entries)
    print(f"Wrote {CATALOG_FILE.relative_to(ROOT).as_posix()}: commit it with the media so deploys seed themselves.")
    seed_database(entries)
    verify_database_paths()


if __name__ == "__main__":
    main()
