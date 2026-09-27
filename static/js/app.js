// =====================================================================
// Sonoria SPA — views, iTunes-backed content, and the persistent player
// =====================================================================

// ===== Global State Manager =====
const LIKED_STORAGE_KEY = "sonoria.likedTracks";
const PLAYER_STORAGE_KEY = "sonoria.player";

const savedPlayer = readJSON(PLAYER_STORAGE_KEY, {});

const state = {
  queue: [],            // tracks in the active list, original order: {id, title, artist, album, cover, stream_url, duration}
  order: [],            // indices into queue, in play order (shuffled when shuffle is on)
  pos: -1,              // position within order of the current track
  isPlaying: false,
  shuffle: !!savedPlayer.shuffle,
  repeat: !!savedPlayer.repeat,   // repeat-one: loop the current track
  mode: "audio",        // "audio" | "video" (visualizer stage)
  panel: null,          // null | "queue" | "lyrics"
  liked: loadLiked(),   // Map<trackId, track>, in the order they were liked
  lyricsCache: new Map(),
  currentView: "home",
  homeLoaded: false,    // iTunes-backed home rows
  localLoaded: false,   // locally hosted rows from /api/home_sections
  seeking: false,       // true while the user drags the progress bar
  signedIn: document.body.dataset.signedIn === "true",
  playlists: [],        // the user's playlists: {id, name, track_count}
  playlistTracks: [],   // tracks of the playlist open in the playlist view
  currentPlaylistId: null,
  loadSeq: 0,           // bumps on every track load, so each load records at most one play
  recordedSeq: 0,
  errorStreak: 0,       // consecutive load failures, to stop auto-skip cascades
};

function currentTrack() {
  return state.pos >= 0 ? state.queue[state.order[state.pos]] || null : null;
}

// Moods map to richer iTunes search terms than the chip label alone
const MOOD_TERMS = {
  Relax: "relaxing chill",
  Energize: "energetic pop",
  Focus: "lofi study",
  Workout: "workout",
  Commute: "road trip",
  Party: "party hits",
};

const EXPLORE_GENRES = [
  "Pop", "Hip-Hop", "Bollywood", "Rock", "Lo-fi", "Electronic",
  "R&B", "Indie", "Jazz", "Classical", "K-Pop", "Punjabi",
];

const SEARCH_DEBOUNCE_MS = 400;
const SEARCH_MIN_CHARS = 2;
const SEEK_STEP_SECONDS = 5;
const MAX_AUTO_SKIPS = 3;
const EQ_MARKUP = '<span class="eq" aria-hidden="true"><i></i><i></i><i></i></span>';
const PLUS_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';

const audio = document.getElementById("audio-player");

// ===== DOM refs =====
const $ = (id) => document.getElementById(id);
const appShell = document.querySelector(".app-shell");
const npCover = $("np-cover");
const npTitle = $("np-title");
const npArtist = $("np-artist");
const heartBtn = $("heart-btn");
const playBtn = $("play-btn");
const prevBtn = $("prev-btn");
const nextBtn = $("next-btn");
const shuffleBtn = $("shuffle-btn");
const repeatBtn = $("repeat-btn");
const progressBar = $("progress-bar");
const progressFill = $("progress-fill");
const currTime = $("curr-time");
const durTime = $("dur-time");
const volumeSlider = $("volume-slider");
const muteBtn = $("mute-btn");
const queueBtn = $("queue-btn");
const lyricsBtn = $("lyrics-btn");
const sidePanel = $("side-panel");
const stage = $("stage");
const canvasVideo = $("canvas-video");
const playerAddBtn = $("player-add-btn");
const playlistModal = $("playlist-modal");
const genreBadge = $("genre-badge"); // only rendered for signed-in users
const searchForm = $("search-form");
const searchInput = $("search-input");
const mainWorkspace = $("main-workspace");

// ===== Helpers =====
function readJSON(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage blocked or full — the feature still works for this session.
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

function setCover(el, url) {
  el.style.backgroundImage = url ? `url(${JSON.stringify(url)})` : "";
}

function showMessage(container, text) {
  container.innerHTML = `<p class="empty-msg">${escapeHtml(text)}</p>`;
}

// Paints the warmth-tinted "filled" portion of a range slider (see styles.css --fill)
function paintRange(input, value = input.value) {
  const min = parseFloat(input.min) || 0;
  const max = parseFloat(input.max) || 100;
  const pct = ((parseFloat(value) - min) / (max - min)) * 100;
  input.style.setProperty("--fill", `${pct}%`);
}

function savePlayerPrefs() {
  writeJSON(PLAYER_STORAGE_KEY, {
    shuffle: state.shuffle,
    repeat: state.repeat,
    volume: audio.volume,
    muted: audio.muted,
  });
}

// ===== Toasts =====
function showToast(message, { tone = "info", duration = 3500 } = {}) {
  const stack = $("toast-stack");
  const toast = document.createElement("div");
  toast.className = `toast toast-${tone}`;
  toast.textContent = message;
  stack.appendChild(toast);
  while (stack.children.length > 3) stack.firstElementChild.remove();

  setTimeout(() => {
    toast.classList.add("leaving");
    toast.addEventListener("transitionend", () => toast.remove(), { once: true });
    setTimeout(() => toast.remove(), 400); // fallback when transitions are disabled
  }, duration);
}

// ===== Liked songs (kept in localStorage until the backend can store iTunes likes) =====
function loadLiked() {
  const saved = readJSON(LIKED_STORAGE_KEY, []);
  return new Map((Array.isArray(saved) ? saved : []).map((t) => [t.id, t]));
}

function toggleLike(track) {
  const nowLiked = !state.liked.has(track.id);
  if (nowLiked) {
    state.liked.set(track.id, track);
  } else {
    state.liked.delete(track.id);
  }
  writeJSON(LIKED_STORAGE_KEY, [...state.liked.values()]);
  showToast(nowLiked ? "Added to Liked Songs" : "Removed from Liked Songs", { duration: 2000 });
  updateHeart();
  updateLikedCount();
  if (state.currentView === "library") renderLibrary();
}

function likedTracks() {
  return [...state.liked.values()].reverse(); // most recently liked first
}

// ===== Renderers =====
// "+" (Add to Playlist) button for Sonoria catalogue tracks; iTunes previews can't go in playlists.
function addToPlaylistButton(track) {
  if (!track.local) return null;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "icon-btn add-btn";
  btn.title = "Add to playlist";
  btn.setAttribute("aria-label", `Add “${track.title || "track"}” to a playlist`);
  btn.innerHTML = PLUS_SVG;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    openPlaylistModal(track);
  });
  return btn;
}

// Every renderer takes the list the item belongs to so a click queues that exact list.
function renderCard(track, list, index) {
  const div = document.createElement("div");
  div.className = "card";
  div.dataset.trackId = track.id;
  div.tabIndex = 0;
  div.innerHTML = `
    <div class="cover">${EQ_MARKUP}</div>
    <div class="title">${escapeHtml(track.title || "Untitled")}</div>
    <div class="subtitle">${escapeHtml(track.artist)}</div>
  `;
  setCover(div.querySelector(".cover"), track.cover);
  const addBtn = addToPlaylistButton(track);
  if (addBtn) div.appendChild(addBtn);
  bindPlay(div, list, index);
  return div;
}

// Artists row: a round portrait card led by the artist's name, playing their track
function renderArtistCard(track, list, index) {
  const div = renderCard(track, list, index);
  div.querySelector(".title").textContent = track.artist || "Unknown Artist";
  div.querySelector(".subtitle").textContent = track.title || "Untitled";
  return div;
}

function renderQuickTile(track, list, index) {
  const div = document.createElement("div");
  div.className = "quick-tile";
  div.dataset.trackId = track.id;
  div.tabIndex = 0;
  div.innerHTML = `
    <div class="quick-cover"></div>
    <div class="quick-meta">
      <div class="t">${escapeHtml(track.title || "Untitled")}</div>
      <div class="a">${escapeHtml(track.artist)}</div>
    </div>
    ${EQ_MARKUP}
    <span class="quick-play" aria-hidden="true"></span>
  `;
  setCover(div.querySelector(".quick-cover"), track.cover);
  bindPlay(div, list, index);
  return div;
}

function renderChartRow(track, list, index) {
  const div = document.createElement("div");
  div.className = "chart-row";
  div.dataset.trackId = track.id;
  div.tabIndex = 0;
  div.innerHTML = `
    <div class="rank"><span class="idx">${index + 1}</span>${EQ_MARKUP}</div>
    <div class="mini-cover"></div>
    <div class="meta">
      <div class="t">${escapeHtml(track.title || "Untitled")}</div>
      <div class="a">${escapeHtml(track.artist)}</div>
    </div>
    <div class="duration">${formatTime((track.duration || 0) / 1000)}</div>
  `;
  const addBtn = addToPlaylistButton(track);
  if (addBtn) div.querySelector(".duration").before(addBtn);
  setCover(div.querySelector(".mini-cover"), track.cover);
  bindPlay(div, list, index);
  return div;
}

function renderLibraryRow(track, list, index) {
  const tr = document.createElement("tr");
  tr.dataset.trackId = track.id;
  tr.tabIndex = 0;
  tr.innerHTML = `
    <td class="col-index"><span class="idx">${index + 1}</span><span class="row-play">▶</span>${EQ_MARKUP}</td>
    <td>
      <div class="title-cell">
        <div class="mini-cover"></div>
        <div class="meta">
          <div class="t">${escapeHtml(track.title || "Untitled")}</div>
          <div class="a">${escapeHtml(track.artist)}</div>
        </div>
      </div>
    </td>
    <td class="col-album">${escapeHtml(track.album)}</td>
    <td class="col-duration">${formatTime((track.duration || 0) / 1000)}</td>
    <td class="col-actions"></td>
  `;
  const addBtn = addToPlaylistButton(track);
  if (addBtn) tr.querySelector(".col-actions").appendChild(addBtn);
  setCover(tr.querySelector(".mini-cover"), track.cover);
  bindPlay(tr, list, index);
  return tr;
}

function renderQueueItem(track, onClick) {
  const div = document.createElement("div");
  div.className = "queue-item";
  div.dataset.trackId = track.id;
  div.tabIndex = 0;
  div.innerHTML = `
    <div class="mini-cover"></div>
    <div class="meta">
      <div class="t">${escapeHtml(track.title || "Untitled")}</div>
      <div class="a">${escapeHtml(track.artist)}</div>
    </div>
    ${EQ_MARKUP}
  `;
  setCover(div.querySelector(".mini-cover"), track.cover);
  if (onClick) {
    div.addEventListener("click", onClick);
    div.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); }
    });
  }
  return div;
}

function bindPlay(el, list, index) {
  el.addEventListener("click", (e) => {
    if (!e.target.closest("button")) playTrack(list, index);
  });
  el.addEventListener("keydown", (e) => {
    if (e.target !== el) return; // a nested button handles its own keys
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      playTrack(list, index);
    }
  });
}

// ===== Queue management =====
function shuffled(items) {
  const a = items.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// Rebuilds the play order around queue[startIndex]; with shuffle on, that track plays first.
function buildOrder(startIndex) {
  const indices = state.queue.map((_, i) => i);
  if (state.shuffle) {
    state.order = [startIndex, ...shuffled(indices.filter((i) => i !== startIndex))];
    state.pos = 0;
  } else {
    state.order = indices;
    state.pos = startIndex;
  }
}

// Entry point from every card / row / tile: clicking the current track toggles it instead.
function playTrack(list, index) {
  const track = list[index];
  if (!track) return;
  const current = currentTrack();
  if (current && current.id === track.id && audio.src) {
    togglePlayPause();
    return;
  }
  if (!track.stream_url) {
    showToast(`No preview available for “${track.title || "this track"}”.`, { tone: "warn" });
    return;
  }
  state.queue = list.slice();
  buildOrder(index);
  state.errorStreak = 0;
  loadCurrent();
}

function loadCurrent() {
  const track = currentTrack();
  if (!track) return;

  audio.src = track.stream_url;
  state.loadSeq += 1;
  resetProgress();
  audio.play().catch(handlePlayRejection);

  updateNowPlayingUI();
  if (state.panel === "queue") renderQueue();
  if (state.panel === "lyrics") loadLyrics();
}

function handlePlayRejection(err) {
  // AbortError: a newer src replaced this one mid-load. NotAllowedError: autoplay blocked.
  if (err && err.name === "NotAllowedError") {
    showToast("Press play to start listening.");
  }
  // Media decode / network failures surface through the audio "error" event instead.
}

// Moves to the next position that has a playable preview. Returns false at the end of the queue.
function stepTo(direction) {
  let pos = state.pos + direction;
  while (pos >= 0 && pos < state.order.length) {
    if (state.queue[state.order[pos]]?.stream_url) {
      state.pos = pos;
      loadCurrent();
      return true;
    }
    pos += direction;
  }
  return false;
}

function playNext({ auto = false } = {}) {
  if (!currentTrack()) return;
  if (stepTo(1)) return;
  if (auto) {
    // Finished the queue: park at the start of the last track, ready to replay
    audio.pause();
    audio.currentTime = 0;
  } else {
    showToast("You've reached the end of the queue.");
  }
}

function playPrev() {
  if (!currentTrack()) return;
  // Restart the current track if we're more than a few seconds in, like most players
  if (audio.currentTime > 3 || !stepTo(-1)) {
    audio.currentTime = 0;
  }
}

function togglePlayPause() {
  if (!currentTrack()) {
    showToast("Pick a track to start listening.");
    return;
  }
  if (audio.paused) {
    audio.play().catch(handlePlayRejection);
  } else {
    audio.pause();
  }
}

function toggleShuffle() {
  state.shuffle = !state.shuffle;
  const current = currentTrack();
  if (current) buildOrder(state.order[state.pos]);
  updateModeButtons();
  if (state.panel === "queue") renderQueue();
  savePlayerPrefs();
  showToast(state.shuffle ? "Shuffle on" : "Shuffle off", { duration: 1500 });
}

function toggleRepeat() {
  state.repeat = !state.repeat;
  audio.loop = state.repeat;
  updateModeButtons();
  savePlayerPrefs();
  showToast(state.repeat ? "Repeating this track" : "Repeat off", { duration: 1500 });
}

// ===== Now-playing UI =====
function updateNowPlayingUI() {
  const t = currentTrack();
  if (!t) return;
  setCover(npCover, t.cover);
  npTitle.textContent = t.title || "Untitled";
  npArtist.textContent = t.artist || "";
  document.title = `${t.title} · ${t.artist} — Sonoria`;
  playerAddBtn.hidden = !t.local;
  updateHeart();
  updateStage();
  updateMediaSession();
  highlightPlaying();
}

function updateHeart() {
  const t = currentTrack();
  const liked = !!t && state.liked.has(t.id);
  heartBtn.textContent = liked ? "♥" : "♡";
  heartBtn.classList.toggle("liked", liked);
  heartBtn.setAttribute("aria-pressed", String(liked));
  heartBtn.setAttribute("aria-label", liked ? "Remove from Liked Songs" : "Add to Liked Songs");
}

function updatePlayButton() {
  const playing = state.isPlaying;
  playBtn.textContent = playing ? "⏸" : "▶";
  playBtn.setAttribute("aria-label", playing ? "Pause" : "Play");
  playBtn.title = playing ? "Pause (Space)" : "Play (Space)";
  document.body.classList.toggle("audio-playing", playing);
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = playing ? "playing" : "paused";
}

function updateModeButtons() {
  shuffleBtn.classList.toggle("active", state.shuffle);
  shuffleBtn.setAttribute("aria-pressed", String(state.shuffle));
  repeatBtn.classList.toggle("active", state.repeat);
  repeatBtn.setAttribute("aria-pressed", String(state.repeat));
}

// Marks every rendered copy of the current track (cards, tiles, chart/table rows, queue)
function highlightPlaying() {
  const t = currentTrack();
  const id = t ? String(t.id) : null;
  document.querySelectorAll("[data-track-id]").forEach((el) => {
    el.classList.toggle("is-playing", el.dataset.trackId === id);
  });
}

// ===== Progress bar (custom, click + drag to seek) =====
function resetProgress() {
  progressFill.style.width = "0%";
  currTime.textContent = "0:00";
  durTime.textContent = "0:00";
  progressBar.setAttribute("aria-valuenow", "0");
  progressBar.setAttribute("aria-valuetext", "0:00");
}

function renderProgress(seconds) {
  const duration = audio.duration;
  const pct = isFinite(duration) && duration > 0 ? (seconds / duration) * 100 : 0;
  progressFill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  currTime.textContent = formatTime(seconds);
  progressBar.setAttribute("aria-valuenow", String(Math.floor(seconds)));
  progressBar.setAttribute("aria-valuetext", formatTime(seconds));
}

function seekRatioFromEvent(e) {
  const rect = progressBar.getBoundingClientRect();
  return Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
}

progressBar.addEventListener("pointerdown", (e) => {
  if (!isFinite(audio.duration)) return;
  state.seeking = true;
  progressBar.setPointerCapture(e.pointerId);
  progressBar.classList.add("dragging");
  renderProgress(seekRatioFromEvent(e) * audio.duration);
});

progressBar.addEventListener("pointermove", (e) => {
  if (!state.seeking) return;
  renderProgress(seekRatioFromEvent(e) * audio.duration);
});

function endSeek(e, commit) {
  if (!state.seeking) return;
  state.seeking = false;
  progressBar.classList.remove("dragging");
  if (commit && isFinite(audio.duration)) {
    audio.currentTime = seekRatioFromEvent(e) * audio.duration;
  }
  renderProgress(audio.currentTime);
}

progressBar.addEventListener("pointerup", (e) => endSeek(e, true));
progressBar.addEventListener("pointercancel", (e) => endSeek(e, false));

progressBar.addEventListener("keydown", (e) => {
  if (!isFinite(audio.duration)) return;
  const jumps = {
    ArrowRight: SEEK_STEP_SECONDS, ArrowUp: SEEK_STEP_SECONDS,
    ArrowLeft: -SEEK_STEP_SECONDS, ArrowDown: -SEEK_STEP_SECONDS,
  };
  if (e.key in jumps) {
    audio.currentTime = Math.min(audio.duration, Math.max(0, audio.currentTime + jumps[e.key]));
  } else if (e.key === "Home") {
    audio.currentTime = 0;
  } else if (e.key === "End") {
    audio.currentTime = Math.max(0, audio.duration - 0.5);
  } else {
    return;
  }
  e.preventDefault();
  renderProgress(audio.currentTime);
});

// ===== Volume =====
function updateVolumeUI() {
  const muted = audio.muted || audio.volume === 0;
  paintRange(volumeSlider, audio.muted ? 0 : audio.volume);
  muteBtn.classList.toggle("muted", muted);
  muteBtn.setAttribute("aria-label", audio.muted ? "Unmute" : "Mute");
  muteBtn.title = audio.muted ? "Unmute" : "Mute";
}

volumeSlider.addEventListener("input", () => {
  audio.volume = parseFloat(volumeSlider.value);
  audio.muted = audio.volume === 0;
  updateVolumeUI();
});

muteBtn.addEventListener("click", () => {
  if (audio.muted && audio.volume === 0) {
    audio.volume = 0.5;
    volumeSlider.value = "0.5";
  }
  audio.muted = !audio.muted;
  updateVolumeUI();
});

audio.addEventListener("volumechange", () => {
  updateVolumeUI();
  savePlayerPrefs();
});

// ===== Audio element event wiring =====
audio.addEventListener("play", () => { state.isPlaying = true; updatePlayButton(); syncCanvas(); });
audio.addEventListener("pause", () => { state.isPlaying = false; updatePlayButton(); syncCanvas(); });
audio.addEventListener("waiting", () => playBtn.classList.add("loading"));
audio.addEventListener("playing", () => {
  playBtn.classList.remove("loading");
  state.errorStreak = 0;
  if (state.recordedSeq !== state.loadSeq) {
    state.recordedSeq = state.loadSeq;
    recordPlay(currentTrack());
  }
});
audio.addEventListener("canplay", () => playBtn.classList.remove("loading"));

audio.addEventListener("loadedmetadata", () => {
  durTime.textContent = formatTime(audio.duration);
  progressBar.setAttribute("aria-valuemax", String(Math.floor(audio.duration)));
});

audio.addEventListener("timeupdate", () => {
  if (!state.seeking) renderProgress(audio.currentTime);
});

// Queue auto-advance (repeat-one uses audio.loop, so "ended" never fires while it is on)
audio.onended = () => playNext({ auto: true });

audio.addEventListener("error", () => {
  const track = currentTrack();
  if (!track || !audio.getAttribute("src")) return;
  playBtn.classList.remove("loading");
  state.isPlaying = false;
  updatePlayButton();
  syncCanvas();

  state.errorStreak += 1;
  const canSkip = state.errorStreak < MAX_AUTO_SKIPS && state.pos < state.order.length - 1;
  showToast(
    (track.local ? `Couldn't load “${track.title}”.` : `Couldn't load the preview for “${track.title}”.`) +
      (canSkip ? " Skipping ahead…" : ""),
    { tone: "error" },
  );
  if (canSkip) setTimeout(() => playNext({ auto: true }), 900);
});

playBtn.addEventListener("click", togglePlayPause);
nextBtn.addEventListener("click", () => playNext());
prevBtn.addEventListener("click", playPrev);
shuffleBtn.addEventListener("click", toggleShuffle);
repeatBtn.addEventListener("click", toggleRepeat);

heartBtn.addEventListener("click", () => {
  const t = currentTrack();
  if (t) {
    toggleLike(t);
  } else {
    showToast("Play a track to add it to Liked Songs.");
  }
});

// Space toggles playback unless the user is typing or focused on a control
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.panel && !playlistModal.open) {
    closePanel();
    return;
  }
  if (e.code !== "Space" || e.repeat) return;
  const target = e.target;
  if (target.closest("input, textarea, select, button, [role=slider], [contenteditable], [tabindex]")) return;
  e.preventDefault();
  togglePlayPause();
});

// ===== OS media controls (lock screen, keyboard media keys) =====
function updateMediaSession() {
  const t = currentTrack();
  if (!("mediaSession" in navigator) || !t) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: t.title || "Untitled",
    artist: t.artist || "",
    album: t.album || "",
    artwork: t.cover ? [{ src: t.cover, sizes: "600x600", type: "image/jpeg" }] : [],
  });
}

if ("mediaSession" in navigator) {
  const handlers = {
    play: () => audio.play().catch(handlePlayRejection),
    pause: () => audio.pause(),
    previoustrack: playPrev,
    nexttrack: () => playNext(),
    seekto: (d) => { if (isFinite(d.seekTime)) audio.currentTime = d.seekTime; },
  };
  Object.entries(handlers).forEach(([action, fn]) => {
    try { navigator.mediaSession.setActionHandler(action, fn); } catch { /* unsupported action */ }
  });
}

// ===== Audio / Video (visualizer) mode =====
function buildStageBars() {
  const bars = $("stage-bars");
  for (let i = 0; i < 32; i++) {
    const bar = document.createElement("i");
    bar.style.setProperty("--d", `${(Math.random() * -1.2).toFixed(2)}s`);
    bar.style.setProperty("--s", `${(0.7 + Math.random() * 0.7).toFixed(2)}s`);
    bars.appendChild(bar);
  }
}

function updateStage() {
  const t = currentTrack();
  if (!t) return;
  setCover($("stage-cover"), t.cover);
  setCover($("stage-bg"), t.cover);
  $("stage-title").textContent = t.title || "Untitled";
  $("stage-artist").textContent = t.artist || "";
  updateCanvas(t);
}

// ===== Canvas: the track's looping 10 s MP4, shown in the stage while the song plays =====
function updateCanvas(track) {
  const url = track.video_url || "";
  if (canvasVideo.getAttribute("src") === url && url) return syncCanvas();

  stage.classList.remove("canvas-ready");
  stage.classList.toggle("has-canvas", !!url);
  if (url) {
    canvasVideo.src = url;
  } else {
    canvasVideo.pause();
    canvasVideo.removeAttribute("src");
    canvasVideo.load(); // drop the previous clip's buffered frames
  }
  syncCanvas();
}

// The clip is decorative: it only runs while the song plays and the stage is on screen.
function syncCanvas() {
  const shouldPlay = state.isPlaying && state.mode === "video" && stage.classList.contains("has-canvas");
  if (shouldPlay && canvasVideo.paused) {
    canvasVideo.play().catch(() => { /* superseded by a newer src, or autoplay blocked */ });
  } else if (!shouldPlay && !canvasVideo.paused) {
    canvasVideo.pause();
  }
}

canvasVideo.addEventListener("playing", () => stage.classList.add("canvas-ready"));
canvasVideo.addEventListener("error", () => {
  // Missing or broken clip: fall back to the cover art and CSS visualizer bars
  if (canvasVideo.getAttribute("src")) stage.classList.remove("has-canvas", "canvas-ready");
});

function setMode(mode) {
  state.mode = mode;
  stage.hidden = mode !== "video";
  document.querySelectorAll(".mode-switch button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  });
  syncCanvas();
}

document.querySelectorAll(".mode-switch button").forEach((b) => {
  b.addEventListener("click", () => setMode(b.dataset.mode));
});

// ===== Right panel: Queue / Lyrics =====
function openPanel(name) {
  state.panel = name;
  sidePanel.hidden = false;
  appShell.classList.add("panel-open");
  $("panel-queue").hidden = name !== "queue";
  $("panel-lyrics").hidden = name !== "lyrics";
  document.querySelectorAll(".panel-tab").forEach((tab) => {
    const active = tab.dataset.panel === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  queueBtn.classList.toggle("active", name === "queue");
  queueBtn.setAttribute("aria-pressed", String(name === "queue"));
  lyricsBtn.classList.toggle("active", name === "lyrics");
  lyricsBtn.setAttribute("aria-pressed", String(name === "lyrics"));

  if (name === "queue") renderQueue();
  if (name === "lyrics") loadLyrics();
}

function closePanel() {
  state.panel = null;
  sidePanel.hidden = true;
  appShell.classList.remove("panel-open");
  [queueBtn, lyricsBtn].forEach((b) => {
    b.classList.remove("active");
    b.setAttribute("aria-pressed", "false");
  });
}

queueBtn.addEventListener("click", () => (state.panel === "queue" ? closePanel() : openPanel("queue")));
lyricsBtn.addEventListener("click", () => (state.panel === "lyrics" ? closePanel() : openPanel("lyrics")));
$("panel-close").addEventListener("click", closePanel);
document.querySelectorAll(".panel-tab").forEach((tab) => {
  tab.addEventListener("click", () => openPanel(tab.dataset.panel));
});

function renderQueue() {
  const now = $("queue-now");
  const next = $("queue-next");
  const current = currentTrack();
  now.innerHTML = "";
  next.innerHTML = "";

  if (!current) {
    showMessage(now, "Nothing playing yet.");
    $("queue-next-label").hidden = true;
    return;
  }
  now.appendChild(renderQueueItem(current));

  const upcoming = state.order.slice(state.pos + 1);
  const label = $("queue-next-label");
  label.hidden = false;
  label.textContent = state.shuffle ? `Next up · shuffled (${upcoming.length})` : `Next up (${upcoming.length})`;

  if (!upcoming.length) {
    showMessage(next, state.repeat ? "Repeating the current track." : "End of queue.");
  }
  upcoming.slice(0, 50).forEach((queueIndex, offset) => {
    const targetPos = state.pos + 1 + offset;
    next.appendChild(renderQueueItem(state.queue[queueIndex], () => {
      state.pos = targetPos;
      state.errorStreak = 0;
      loadCurrent();
    }));
  });
  highlightPlaying();
}

let lyricsRequestId = 0;

async function loadLyrics() {
  const head = $("lyrics-head");
  const body = $("lyrics-body");
  const t = currentTrack();
  if (!t) {
    head.innerHTML = "";
    showMessage(body, "Play a song to see its lyrics.");
    return;
  }

  head.innerHTML = `
    <div class="t">${escapeHtml(t.title)}</div>
    <div class="a">${escapeHtml(t.artist)}</div>
  `;

  const renderLyrics = (data) => {
    if (data.error) return showMessage(body, data.error);
    if (data.instrumental) return showMessage(body, "♪ This track is instrumental.");
    body.innerHTML = data.lines
      .map((line) => (line.trim() ? `<p>${escapeHtml(line)}</p>` : '<p class="gap"></p>'))
      .join("") +
      (t.local ? "" : '<p class="lyrics-note">Lyrics are for the full song; the preview plays a 30-second excerpt.</p>');
    body.scrollTop = 0;
  };

  if (state.lyricsCache.has(t.id)) return renderLyrics(state.lyricsCache.get(t.id));

  const requestId = ++lyricsRequestId;
  showMessage(body, "Loading lyrics…");
  const params = new URLSearchParams({ artist: t.artist || "", title: t.title || "" });
  if (t.album) params.set("album", t.album);
  if (t.duration) params.set("duration", String(Math.round(t.duration / 1000)));

  try {
    const resp = await fetch(`/api/lyrics?${params}`);
    const data = await resp.json();
    if (resp.ok || resp.status === 404) state.lyricsCache.set(t.id, data); // don't cache outages
    if (requestId === lyricsRequestId) renderLyrics(data);
  } catch {
    if (requestId === lyricsRequestId) showMessage(body, "Could not reach the lyrics service right now.");
  }
}

// ===== View switching =====
function showView(view) {
  state.currentView = view;
  document.querySelectorAll(".view").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });
  mainWorkspace.scrollTop = 0;
  if (state.mode === "video") setMode("audio"); // navigating brings the workspace back

  if (view === "home") loadHome();
  if (view === "library") renderLibrary();
  if (view !== "playlist") state.currentPlaylistId = null;
}

function setActiveNav(link) {
  document.querySelectorAll(".nav-link[data-view]").forEach((l) => l.classList.remove("active"));
  if (link) link.classList.add("active");
}

function setActiveMood(mood) {
  document.querySelectorAll(".mood-chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.mood === mood);
  });
}

document.querySelectorAll(".nav-link[data-view]").forEach((link) => {
  link.addEventListener("click", (e) => {
    e.preventDefault();
    setActiveNav(link);
    if (link.dataset.view === "home") setActiveMood("all");
    history.replaceState(null, "", `#${link.dataset.view}`);
    showView(link.dataset.view);
  });
});

// ===== Mood chip filtering =====
document.querySelectorAll(".mood-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const mood = chip.dataset.mood;
    setActiveMood(mood);
    searchInput.value = "";
    if (mood === "all") {
      setActiveNav(document.querySelector('.nav-link[data-view="home"]'));
      showView("home");
    } else {
      runSearch(MOOD_TERMS[mood] || mood, `${mood} vibes`);
    }
  });
});

// ===== Search integration (real-time, debounced) =====
let searchTimer = null;
let searchRequestId = 0;

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (!q) {
    // Cleared the box: return to home rather than leaving stale results up
    if (state.currentView === "search") {
      setActiveMood("all");
      setActiveNav(document.querySelector('.nav-link[data-view="home"]'));
      showView("home");
    }
    return;
  }
  if (q.length < SEARCH_MIN_CHARS) return;
  searchTimer = setTimeout(() => runSearch(q), SEARCH_DEBOUNCE_MS);
});

searchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q) runSearch(q);
});

async function runSearch(query, heading) {
  const requestId = ++searchRequestId;
  if (!heading) setActiveMood(null); // a typed search isn't a mood filter
  setActiveNav(null);
  showView("search");

  const title = $("search-title");
  const resultsGrid = $("search-results");
  title.textContent = heading || `Results for “${query}”`;
  showMessage(resultsGrid, "Searching...");

  try {
    const resp = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
    const data = await resp.json();
    if (requestId !== searchRequestId) return; // a newer search has started

    if (data.error) return showMessage(resultsGrid, data.error);
    if (!data.results.length) return showMessage(resultsGrid, `No results found for “${query}”.`);

    resultsGrid.innerHTML = "";
    data.results.forEach((t, i) => resultsGrid.appendChild(renderCard(t, data.results, i)));
    highlightPlaying();
  } catch {
    if (requestId === searchRequestId) {
      showMessage(resultsGrid, "Could not reach the search service right now.");
    }
  }
}

// ===== Explore =====
function renderExplore() {
  const grid = $("genre-grid");
  EXPLORE_GENRES.forEach((genre, i) => {
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = `genre-tile tone-${i % 4}`;
    tile.textContent = genre;
    tile.addEventListener("click", () => {
      setActiveMood(null);
      runSearch(genre, genre);
    });
    grid.appendChild(tile);
  });
}

// ===== Library rendering =====
function updateLikedCount() {
  $("nav-liked-count").textContent = state.liked.size;
}

function renderLibrary() {
  const rows = $("library-rows");
  const tracks = likedTracks();
  const totalSeconds = tracks.reduce((sum, t) => sum + (t.duration || 0), 0) / 1000;
  const n = tracks.length;

  $("liked-count").textContent =
    `${n} Track${n !== 1 ? "s" : ""}` + (n ? ` · ${Math.max(1, Math.round(totalSeconds / 60))} min` : "");
  $("library-empty").hidden = n > 0;
  $("liked-play").disabled = n === 0;

  rows.innerHTML = "";
  tracks.forEach((t, i) => rows.appendChild(renderLibraryRow(t, tracks, i)));
  highlightPlaying();
}

$("liked-play").addEventListener("click", () => {
  const tracks = likedTracks();
  if (tracks.length) playTrack(tracks, 0);
});

// ===== Home population =====
function loadHome() {
  loadLocalSections();
  loadItunesSections();
}

// Locally hosted catalogue: section key from /api/home_sections -> [container id, card renderer]
const LOCAL_SECTION_ROWS = {
  trending: ["trending-now", renderCard],
  viral: ["viral-reels", renderCard],
  artist: ["artists", renderArtistCard],
};

async function loadLocalSections() {
  if (state.localLoaded) return;
  const rows = Object.values(LOCAL_SECTION_ROWS).map(([id]) => $(id));
  rows.forEach((row) => showMessage(row, "Loading..."));

  try {
    const resp = await fetch("/api/home_sections");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const { sections } = await resp.json();
    sections.forEach(({ key, tracks }) => {
      const [id, render] = LOCAL_SECTION_ROWS[key] || [];
      if (!id) return;
      const row = $(id);
      row.innerHTML = "";
      if (!tracks.length) return showMessage(row, "No tracks here yet.");
      tracks.forEach((t, i) => row.appendChild(render(t, tracks, i)));
    });
    state.localLoaded = true;
    highlightPlaying();
  } catch {
    rows.forEach((row) => showMessage(row, "Could not load the Sonoria catalogue right now."));
  }
}

// iTunes-backed rows (Phase 1's /api/songs fallback route)
async function loadItunesSections() {
  if (state.homeLoaded) return;

  const jumpBackIn = $("jump-back-in");
  const recommended = $("recommended");
  const topCharts = $("top-charts");

  showMessage(jumpBackIn, "Loading...");
  recommended.innerHTML = "";
  topCharts.innerHTML = "";

  try {
    const resp = await fetch("/api/songs");
    const { sections } = await resp.json();
    const chill = (sections["Chill"] || []).slice(0, 6);
    const hits = sections["Top Hits"] || [];
    const charts = sections["Synthwave"] || [];

    jumpBackIn.innerHTML = "";
    if (!chill.length && !hits.length && !charts.length) {
      return showMessage(jumpBackIn, "Could not reach the music service right now.");
    }

    chill.forEach((t, i) => jumpBackIn.appendChild(renderQuickTile(t, chill, i)));
    hits.forEach((t, i) => recommended.appendChild(renderCard(t, hits, i)));
    charts.forEach((t, i) => topCharts.appendChild(renderChartRow(t, charts, i)));
    state.homeLoaded = true;
    highlightPlaying();
  } catch {
    showMessage(jumpBackIn, "Could not reach the music service right now.");
  }
}

// ===== Genre badge =====
async function recordPlay(track) {
  if (!track || (!track.local && !state.signedIn)) return; // guests only bump catalogue play counts
  try {
    const resp = await fetch("/api/track/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: track.id, genre: track.genre || null }),
    });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.recorded) updateBadge(data.badge);
  } catch {
    // Stats are best-effort; playback carries on regardless.
  }
}

function updateBadge(badge) {
  if (!genreBadge) return;
  if (!badge) {
    genreBadge.hidden = true;
    return;
  }
  const previous = genreBadge.hidden ? "" : genreBadge.textContent.trim();
  genreBadge.textContent = badge.badge;
  genreBadge.title = "Your listener badge" + (badge.genre ? ` · top genre: ${badge.genre}` : "");
  genreBadge.hidden = false;
  if (previous === badge.badge) return;

  genreBadge.classList.remove("updated");
  void genreBadge.offsetWidth; // restart the pop animation
  genreBadge.classList.add("updated");
  showToast(previous ? `New badge unlocked: ${badge.badge}` : `You earned a badge: ${badge.badge}`, { tone: "info" });
}

// ===== Playlists: API =====
async function apiJSON(url, options = {}) {
  const resp = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let data = {};
  try { data = await resp.json(); } catch { /* empty or non-JSON body */ }
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

async function loadPlaylists() {
  if (!state.signedIn) return;
  try {
    const { playlists } = await apiJSON("/api/playlists");
    state.playlists = playlists;
    renderSidebarPlaylists();
  } catch {
    // Sidebar list is a convenience; the modal reports its own errors.
  }
}

function renderSidebarPlaylists() {
  const box = $("sidebar-playlists");
  box.innerHTML = "";
  state.playlists.forEach((pl) => {
    const link = document.createElement("a");
    link.className = "nav-link";
    link.dataset.view = "playlist";
    link.dataset.playlistId = pl.id;
    link.href = `#playlist-${pl.id}`;
    link.innerHTML = `
      <span class="playlist-dot" aria-hidden="true">♪</span>
      <span class="pl-name">${escapeHtml(pl.name)}</span>
      <span class="nav-count">${pl.track_count}</span>
    `;
    link.classList.toggle("active", state.currentView === "playlist" && state.currentPlaylistId === pl.id);
    link.addEventListener("click", (e) => {
      e.preventDefault();
      history.replaceState(null, "", `#playlist-${pl.id}`);
      openPlaylist(pl.id);
    });
    box.appendChild(link);
  });
}

// ===== Playlist view =====
async function openPlaylist(id) {
  showView("playlist");
  state.currentPlaylistId = id;
  setActiveNav(document.querySelector(`.nav-link[data-playlist-id="${id}"]`));

  const rows = $("playlist-rows");
  rows.innerHTML = "";
  $("playlist-empty").hidden = true;
  $("playlist-play").disabled = true;
  const known = state.playlists.find((pl) => pl.id === id);
  $("playlist-title").textContent = known ? known.name : "Playlist";
  $("playlist-stats").textContent = "Loading…";

  try {
    const data = await apiJSON(`/api/playlists/${id}`);
    if (state.currentPlaylistId !== id) return; // user moved on while this loaded
    renderPlaylistView(data);
  } catch (err) {
    if (state.currentPlaylistId !== id) return;
    $("playlist-stats").textContent = err.message;
  }
}

function renderPlaylistView(data) {
  const tracks = data.tracks;
  const n = tracks.length;
  const totalSeconds = tracks.reduce((sum, t) => sum + (t.duration || 0), 0) / 1000;
  state.playlistTracks = tracks;

  $("playlist-title").textContent = data.name;
  $("playlist-stats").textContent =
    `${n} Track${n !== 1 ? "s" : ""}` + (n ? ` · ${Math.max(1, Math.round(totalSeconds / 60))} min` : "");
  const art = $("playlist-art");
  setCover(art, tracks[0]?.cover);
  art.classList.toggle("has-art", !!tracks[0]?.cover);
  $("playlist-empty").hidden = n > 0;
  $("playlist-play").disabled = n === 0;

  const rows = $("playlist-rows");
  rows.innerHTML = "";
  tracks.forEach((t, i) => rows.appendChild(renderLibraryRow(t, tracks, i)));
  highlightPlaying();
}

$("playlist-play").addEventListener("click", () => {
  if (state.playlistTracks.length) playTrack(state.playlistTracks, 0);
});

// ===== Add to Playlist modal =====
let modalTrack = null;       // the track being added, or null when opened from "New Playlist"
let modalRequestId = 0;

function openPlaylistModal(track) {
  if (!state.signedIn) {
    showToast("Log in to create playlists and save songs to them.", { tone: "warn" });
    return;
  }
  modalTrack = track;
  const cover = $("playlist-modal-cover");
  cover.hidden = !track;
  setCover(cover, track?.cover);
  $("playlist-modal-title").textContent = track ? "Add to Playlist" : "New Playlist";
  $("playlist-modal-subtitle").textContent = track
    ? `${track.title || "Untitled"} · ${track.artist || ""}`
    : "Name it, then add songs with the + button.";
  $("playlist-name-input").value = "";
  $("playlist-modal-error").textContent = "";
  $("playlist-create-btn").disabled = false;

  if (!playlistModal.open) playlistModal.showModal();
  $("playlist-name-input").focus();
  renderModalList();
}

function closePlaylistModal() {
  if (playlistModal.open) playlistModal.close();
}

async function renderModalList() {
  const list = $("playlist-modal-list");
  const requestId = ++modalRequestId;
  showMessage(list, "Loading your playlists…");
  try {
    const query = modalTrack ? `?track_id=${encodeURIComponent(modalTrack.id)}` : "";
    const { playlists } = await apiJSON(`/api/playlists${query}`);
    if (requestId !== modalRequestId) return;
    state.playlists = playlists;
    renderSidebarPlaylists();

    if (!playlists.length) return showMessage(list, "No playlists yet. Create your first one above.");
    list.innerHTML = "";
    playlists.forEach((pl) => list.appendChild(renderModalRow(pl)));
  } catch (err) {
    if (requestId === modalRequestId) showMessage(list, err.message);
  }
}

function renderModalRow(pl) {
  const row = document.createElement("div");
  row.className = "modal-row";
  row.innerHTML = `
    <div class="pl-icon" aria-hidden="true">♪</div>
    <div class="pl-meta">
      <div class="pl-name">${escapeHtml(pl.name)}</div>
      <div class="pl-count">${pl.track_count} track${pl.track_count !== 1 ? "s" : ""}</div>
    </div>
  `;
  const btn = document.createElement("button");
  btn.type = "button";
  if (!modalTrack) {
    btn.className = "pill-btn ghost";
    btn.textContent = "Open";
    btn.addEventListener("click", () => {
      closePlaylistModal();
      history.replaceState(null, "", `#playlist-${pl.id}`);
      openPlaylist(pl.id);
    });
  } else if (pl.contains_track) {
    btn.className = "pill-btn done";
    btn.textContent = "Added";
    btn.disabled = true;
  } else {
    btn.className = "pill-btn";
    btn.textContent = "Add";
    btn.setAttribute("aria-label", `Add to ${pl.name}`);
    btn.addEventListener("click", () => addTrackToPlaylist(pl, btn));
  }
  row.appendChild(btn);
  return row;
}

async function addTrackToPlaylist(pl, btn) {
  const track = modalTrack;
  btn.disabled = true;
  try {
    const data = await apiJSON(`/api/playlists/${pl.id}/tracks`, {
      method: "POST",
      body: JSON.stringify({ track_id: track.id }),
    });
    showToast(data.added ? `Added to ${pl.name}` : `Already in ${pl.name}`, { duration: 2200 });
    closePlaylistModal();
    afterPlaylistChange(pl.id);
  } catch (err) {
    btn.disabled = false;
    $("playlist-modal-error").textContent = err.message;
  }
}

$("playlist-create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("playlist-name-input");
  const name = input.value.trim();
  const error = $("playlist-modal-error");
  if (!name) {
    error.textContent = "Give the playlist a name.";
    input.focus();
    return;
  }
  const createBtn = $("playlist-create-btn");
  createBtn.disabled = true;
  error.textContent = "";
  const track = modalTrack;
  try {
    const data = await apiJSON("/api/playlists", {
      method: "POST",
      body: JSON.stringify(track ? { name, track_id: track.id } : { name }),
    });
    const pl = data.playlist;
    showToast(track ? `Created “${pl.name}” and added “${track.title}”` : `Created “${pl.name}”`, { duration: 2600 });
    closePlaylistModal();
    await afterPlaylistChange(pl.id);
    if (!track) {
      history.replaceState(null, "", `#playlist-${pl.id}`);
      openPlaylist(pl.id);
    }
  } catch (err) {
    error.textContent = err.message;
  } finally {
    createBtn.disabled = false;
  }
});

// Refresh the sidebar counts, and the open playlist if it is the one that changed
async function afterPlaylistChange(playlistId) {
  await loadPlaylists();
  if (state.currentView === "playlist" && state.currentPlaylistId === playlistId) openPlaylist(playlistId);
}

$("playlist-name-input").addEventListener("input", () => { $("playlist-modal-error").textContent = ""; });
$("playlist-modal-close").addEventListener("click", closePlaylistModal);
playlistModal.addEventListener("click", (e) => {
  if (e.target === playlistModal) closePlaylistModal(); // click on the backdrop
});
$("new-playlist-btn").addEventListener("click", () => openPlaylistModal(null));
playerAddBtn.addEventListener("click", () => {
  const t = currentTrack();
  if (t) openPlaylistModal(t);
});

// ===== Init =====
if (typeof savedPlayer.volume === "number") {
  audio.volume = Math.min(1, Math.max(0, savedPlayer.volume));
  volumeSlider.value = String(audio.volume);
}
audio.muted = !!savedPlayer.muted;
audio.loop = state.repeat;
updateVolumeUI();
updateModeButtons();
updatePlayButton();
updateLikedCount();
buildStageBars();
renderExplore();

// Honour #explore / #library / #playlist-<id> deep links on load; default to home
const hash = location.hash.slice(1);
const playlistMatch = /^playlist-(\d+)$/.exec(hash);
if (playlistMatch && state.signedIn) {
  loadPlaylists().then(() => openPlaylist(Number(playlistMatch[1])));
} else {
  const initialView = ["explore", "library"].includes(hash) ? hash : "home";
  setActiveNav(document.querySelector(`.nav-link[data-view="${initialView}"]`));
  showView(initialView);
  loadPlaylists();
}
