"""End-to-end test of the Sonoria SPA audio player, driven through a real browser with Playwright.

Starts the Flask app on a free local port (unless --url is given), then exercises playback,
seeking, volume, queue/shuffle/repeat, the queue and lyrics panels, video mode, error toasts,
and the mobile layout against live iTunes previews and LRCLIB lyrics.

Usage:  python tests/player_test.py [--url http://127.0.0.1:5000] [--browser msedge|chrome|chromium] [--headed]
Exits 0 when every check passes, 1 otherwise.
"""
import argparse
import logging
import os
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def js(page, expr):
    return page.evaluate(expr)


def wait_js(page, expr, timeout=15000):
    page.wait_for_function(expr, timeout=timeout)


def current_id(page):
    return js(page, "currentTrack() && currentTrack().id")


def start_server():
    """Serves the Flask app from a background thread on a free port; returns (server, base_url)."""
    os.chdir(ROOT)  # app.py uses paths relative to the project root
    sys.path.insert(0, str(ROOT))
    from werkzeug.serving import make_server
    from app import app

    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # keep request logs out of the test output
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def run(p, spa_url, shots, channel, headed):
    browser = p.chromium.launch(channel=channel, headless=not headed,
                                args=["--autoplay-policy=no-user-gesture-required"])
    page = browser.new_page(viewport={"width": 1400, "height": 860})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    net_errors = []
    page.on("console", lambda m: m.type == "error" and (net_errors if "Failed to load resource" in m.text else errors).append(m.text))
    page.goto(spa_url)
    page.evaluate("localStorage.clear()")
    page.reload()

    # ---------- Stream + play state ----------
    page.wait_for_selector("#top-charts .chart-row", timeout=20000)
    page.click("#top-charts .chart-row >> nth=0")
    wait_js(page, "!document.getElementById('audio-player').paused && document.getElementById('audio-player').currentTime > 0.5")
    check("track streams from iTunes previewUrl", "itunes" in js(page, "audio.src"), js(page, "audio.src")[:60])
    check("play button shows ⏸ while playing", js(page, "document.getElementById('play-btn').textContent") == "⏸")
    check("chart row marked is-playing", js(page, "document.querySelector('#top-charts .chart-row').classList.contains('is-playing')"))
    check("equalizer visible on playing row",
          js(page, "getComputedStyle(document.querySelector('#top-charts .chart-row .eq')).display") != "none")
    check("body.audio-playing set", js(page, "document.body.classList.contains('audio-playing')"))

    # ---------- Time displays ----------
    page.wait_for_timeout(1500)
    curr, dur = js(page, "[document.getElementById('curr-time').textContent, document.getElementById('dur-time').textContent]")
    check("#curr-time advances in m:ss", curr != "0:00" and ":" in curr, curr)
    check("#dur-time shows ~30s preview length", dur in ("0:29", "0:30", "0:31"), dur)

    # ---------- Seek: click, drag, keyboard ----------
    box = page.locator("#progress-bar").bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.click(box["x"] + box["width"] * 0.5, y)
    ratio = js(page, "audio.currentTime / audio.duration")
    check("click on progress bar seeks to ~50%", 0.45 < ratio < 0.56, f"{ratio:.2f}")

    page.mouse.move(box["x"] + box["width"] * 0.2, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5, y, steps=5)
    mid_drag_fill = js(page, "parseFloat(document.getElementById('progress-fill').style.width)")
    page.mouse.move(box["x"] + box["width"] * 0.8, y, steps=5)
    page.mouse.up()
    ratio = js(page, "audio.currentTime / audio.duration")
    check("fill follows pointer while dragging", 45 < mid_drag_fill < 55, f"{mid_drag_fill:.0f}%")
    check("drag on progress bar seeks to ~80%", 0.75 < ratio < 0.86, f"{ratio:.2f}")

    page.focus("#progress-bar")
    before = js(page, "audio.currentTime")
    page.keyboard.press("ArrowLeft")
    after = js(page, "audio.currentTime")
    check("ArrowLeft on progress bar seeks back 5s", 4 < before - after < 6.5, f"{before:.1f}->{after:.1f}")

    # ---------- Volume ----------
    page.locator("#volume-slider").fill("0.3")
    check("volume slider sets audio.volume", abs(js(page, "audio.volume") - 0.3) < 0.01)
    check("volume slider gradient fill updated",
          js(page, "document.getElementById('volume-slider').style.getPropertyValue('--fill')").startswith("30"))
    page.click("#mute-btn")
    check("mute button mutes", js(page, "audio.muted") and js(page, "document.getElementById('mute-btn').classList.contains('muted')"))
    page.click("#mute-btn")
    check("mute button unmutes", not js(page, "audio.muted"))

    # ---------- Play / pause ----------
    page.click("#play-btn")
    wait_js(page, "audio.paused")
    check("pause: icon ▶ and eq stops",
          js(page, "document.getElementById('play-btn').textContent") == "▶"
          and not js(page, "document.body.classList.contains('audio-playing')"))
    page.click("#play-btn")
    wait_js(page, "!audio.paused")
    check("resume from play button", True)
    page.click("#top-charts .chart-row >> nth=0")
    wait_js(page, "audio.paused")
    check("clicking the live row toggles pause (no restart)", True)
    page.locator("body").press("Space")
    wait_js(page, "!audio.paused")
    check("Space key toggles playback", True)

    # ---------- Next / previous ----------
    first = current_id(page)
    page.click("#next-btn")
    wait_js(page, "!audio.paused && audio.currentTime > 0.2")
    second = current_id(page)
    check("next steps forward in the queue", second != first and js(page, "state.pos") == 1)
    check("highlight moved to row 2",
          js(page, "document.querySelectorAll('#top-charts .chart-row')[1].classList.contains('is-playing')"))
    page.click("#prev-btn")
    wait_js(page, "state.pos === 0")
    check("previous steps back (when <3s in)", current_id(page) == first)
    wait_js(page, "audio.currentTime > 3.2", timeout=10000)
    page.click("#prev-btn")
    check("previous restarts track when >3s in", js(page, "audio.currentTime") < 1 and current_id(page) == first)

    # ---------- Auto-advance on end ----------
    wait_js(page, "isFinite(audio.duration)"); js(page, "audio.currentTime = audio.duration - 0.4")
    wait_js(page, "state.pos === 1 && !audio.paused", timeout=10000)
    check("audio.onended auto-advances to next track", current_id(page) == second)

    # ---------- Shuffle ----------
    cur = current_id(page)
    page.click("#shuffle-btn")
    order = js(page, "state.order")
    check("shuffle: indicator on", js(page, "document.getElementById('shuffle-btn').getAttribute('aria-pressed')") == "true")
    check("shuffle: current track kept first, rest permuted",
          current_id(page) == cur and js(page, "state.pos") == 0 and sorted(order) == list(range(len(order))),
          str(order))
    page.click("#next-btn")
    wait_js(page, "state.pos === 1")
    check("shuffle: next follows shuffled order", current_id(page) == js(page, "state.queue[state.order[1]].id"))
    page.click("#shuffle-btn")
    check("shuffle off restores original order",
          js(page, "state.order.every((v, i) => v === i) && state.queue[state.order[state.pos]].id === currentTrack().id"))

    # ---------- Repeat ----------
    page.click("#repeat-btn")
    cur = current_id(page)
    check("repeat: indicator on + audio.loop", js(page, "audio.loop") and js(page, "document.getElementById('repeat-btn').classList.contains('active')"))
    wait_js(page, "isFinite(audio.duration)"); js(page, "audio.currentTime = audio.duration - 0.4")
    page.wait_for_timeout(1500)
    check("repeat: loops same track instead of advancing", current_id(page) == cur and js(page, "audio.currentTime") < 1.8)
    page.click("#repeat-btn")
    check("repeat off", not js(page, "audio.loop"))

    # ---------- Heart ----------
    page.click("#heart-btn")
    check("heart likes current track",
          js(page, "document.getElementById('heart-btn').textContent") == "♥"
          and js(page, "JSON.parse(localStorage.getItem('sonoria.likedTracks')).length") == 1
          and js(page, "document.getElementById('nav-liked-count').textContent") == "1")

    # ---------- Queue panel ----------
    page.click("#queue-btn")
    page.wait_for_selector("#side-panel:not([hidden])")
    expected_next = js(page, "state.order.length - state.pos - 1")
    shown = js(page, "document.querySelectorAll('#queue-next .queue-item').length")
    check("queue drawer opens with upcoming tracks", shown == expected_next, f"{shown} upcoming")
    check("3-pane grid gains the panel column", js(page, "document.querySelector('.app-shell').classList.contains('panel-open')"))
    target = js(page, "state.queue[state.order[state.pos + 2]].id")
    page.click("#queue-next .queue-item >> nth=1")
    wait_js(page, f"currentTrack().id === {target}")
    check("clicking a queued song jumps to it", True)
    page.screenshot(path=f"{shots}/p4_queue.png")

    # ---------- Lyrics ----------
    page.click("#lyrics-btn")
    wait_js(page, "!document.getElementById('lyrics-body').textContent.includes('Loading')", timeout=15000)
    lyr = js(page, "document.getElementById('lyrics-body').innerText.slice(0, 80)")
    check("lyrics panel loads from /api/lyrics", len(lyr) > 0, lyr.replace("\n", " / "))
    # force a track with known lyrics
    js(page, """fetch('/api/search?q=daft%20punk%20one%20more%20time').then(r => r.json()).then(d => {
        window.__dp = d.results.filter(t => t.stream_url); playTrack(window.__dp, 0); })""")
    wait_js(page, "currentTrack() && /Daft Punk/.test(currentTrack().artist)", timeout=15000)
    wait_js(page, "document.querySelectorAll('#lyrics-body p:not(.gap)').length > 5", timeout=15000)
    check("lyrics refresh when the track changes", True,
          js(page, "document.querySelector('#lyrics-body p').textContent"))
    page.screenshot(path=f"{shots}/p4_lyrics.png")
    page.keyboard.press("Escape")
    check("Escape closes the panel", js(page, "document.getElementById('side-panel').hidden"))

    # ---------- Video mode ----------
    page.click(".mode-switch button[data-mode=video]")
    check("video mode shows visualizer stage",
          js(page, "!document.getElementById('stage').hidden")
          and js(page, "document.getElementById('stage-title').textContent") == js(page, "currentTrack().title"))
    page.wait_for_timeout(600)
    page.screenshot(path=f"{shots}/p4_video.png")
    page.click(".mode-switch button[data-mode=audio]")
    check("audio mode hides stage", js(page, "document.getElementById('stage').hidden"))

    # ---------- Highlight across views ----------
    page.fill("#search-input", "daft punk one more time")
    page.wait_for_selector("#search-results .card", timeout=15000)
    wait_js(page, "document.querySelector('#search-results .card.is-playing') !== null", timeout=5000)
    check("playing indicator appears in Search results", True)
    page.click(".nav-link[data-view=library]")
    check("playing indicator in Library",
          js(page, "document.querySelector('#library-rows tr.is-playing') !== null") ==
          js(page, "state.liked.has(currentTrack().id)"))

    # ---------- Error handling ----------
    js(page, "playTrack([{id: 1, title: 'No Preview Song', artist: 'x'}], 0)")
    page.wait_for_selector(".toast:has-text('No preview available')", timeout=3000)
    check("toast for track without previewUrl", True)
    js(page, """playTrack([{id: 99, title: 'Broken Song', artist: 'x', stream_url: 'https://127.0.0.1:1/broken.m4a'},
                           window.__dp[0]], 0)""")
    page.wait_for_selector(".toast:has-text(\"Couldn't load the preview\")", timeout=10000)
    check("toast for broken preview URL", True)
    wait_js(page, "state.pos === 1 && !audio.paused", timeout=10000)
    check("broken track auto-skips to next playable", True)
    page.wait_for_timeout(300)
    page.screenshot(path=f"{shots}/p4_toast.png")

    # ---------- End of queue ----------
    wait_js(page, "isFinite(audio.duration)"); js(page, "audio.currentTime = audio.duration - 0.3")
    wait_js(page, "audio.paused", timeout=8000)
    check("end of queue stops cleanly", js(page, "state.pos") == 1 and js(page, "audio.currentTime") < 1)

    # ---------- Mobile ----------
    m = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    m.on("pageerror", lambda e: errors.append("mobile: " + str(e)))
    m.goto(spa_url)
    m.wait_for_selector("#top-charts .chart-row", timeout=20000)
    m.tap("#top-charts .chart-row >> nth=0")
    m.wait_for_function("!audio.paused", timeout=15000)
    m.wait_for_timeout(500)
    m.screenshot(path=f"{shots}/p4_mobile.png")
    overflow = m.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check("mobile: no horizontal overflow", overflow <= 0, str(overflow))
    m.tap("#queue-btn")
    m.wait_for_timeout(300)
    m.screenshot(path=f"{shots}/p4_mobile_queue.png")
    check("mobile: queue panel opens as overlay", m.evaluate("getComputedStyle(document.getElementById('side-panel')).position") == "fixed")

    check("no JS errors", not errors, "; ".join(errors)[:300])
    print("  expected network failures:", net_errors)

    browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="Base URL of an already running server (default: start one automatically)")
    parser.add_argument("--browser", default=os.environ.get("SONORIA_BROWSER", "msedge"),
                        help="Browser channel: msedge (default), chrome, or chromium (Playwright's bundled build)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window while testing")
    parser.add_argument("--screenshots", default=str(ROOT / "tests" / "screenshots"),
                        help="Folder for screenshots (default: tests/screenshots)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # check names contain ▶ / ⏸ / ♥
    Path(args.screenshots).mkdir(parents=True, exist_ok=True)

    server = None
    base = args.url
    if not base:
        server, base = start_server()
        print(f"Started Sonoria on {base}", flush=True)

    channel = None if args.browser == "chromium" else args.browser
    try:
        with sync_playwright() as p:
            run(p, base.rstrip("/") + "/spa", args.screenshots, channel, args.headed)
    finally:
        if server:
            server.shutdown()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("Failed:", ", ".join(name for name, _, _ in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
