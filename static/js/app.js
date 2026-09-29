// =====================================================================
// Sonoria SPA — views, iTunes-backed content, and the persistent player
// =====================================================================

// ===== Global State Manager =====
const GUEST_LIKES_KEY = "guest_liked_songs";     // guests only; never merged into an account
const LEGACY_LIKES_KEY = "sonoria.likedTracks";   // pre-accounts key, migrated once to the guest key
const PLAYER_STORAGE_KEY = "sonoria.player";
const GUEST_RECENT_KEY = "guest_recent_plays";   // guests' listening, for recommendations (accounts use the server)
const RECENT_PLAYS_MAX = 30;
const RECS_REFRESH_EVERY = 2;                    // re-weight the Recommended row after this many new plays
const AUTOPLAY_BATCH = 8;

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
  liked: new Map(),     // Map<trackId, track>, in the order they were liked (see loadLiked)
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
  autoplay: savedPlayer.autoplay !== false,   // keep playing similar songs when the queue runs out
  autoplayLoading: null, // in-flight autoplay fetch (a promise), so we never fetch twice at once
  recentPlays: [],      // [{id, genre, title}], newest first: drives recommendations
  recsLoaded: false,
  recsStalePlays: 0,    // plays since the Recommended row was last refreshed
  userQueue: [],        // "Add to queue" tracks: they play next, before the list continues
  queuedTrack: null,    // the user-queue track playing right now (the list position stays put)
};

function currentTrack() {
  if (state.queuedTrack) return state.queuedTrack;
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
const LYRIC_LEAD_SECONDS = 0.2;      // light a line up just before it is sung
const LYRIC_MANUAL_SCROLL_MS = 4000; // after the user scrolls the lyrics, leave them be this long
const SHARE_MAX_CHARS = 400;
const EQ_MARKUP = '<span class="eq" aria-hidden="true"><i></i><i></i><i></i></span>';
const DOTS_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" class="dots"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';
const QUEUE_ADD_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><line x1="3" y1="6" x2="15" y2="6"/><line x1="3" y1="12" x2="15" y2="12"/><line x1="3" y1="18" x2="11" y2="18"/><line x1="18" y1="13" x2="18" y2="21"/><line x1="14" y1="17" x2="22" y2="17"/></svg>';
const HEART_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 1 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z"/></svg>';
const ARTIST_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><line x1="12" y1="18" x2="12" y2="22"/></svg>';
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
const playerMoreBtn = $("player-more-btn");
const playlistModal = $("playlist-modal");
const genreBadge = $("genre-badge"); // only rendered for signed-in users
const shareModal = $("share-modal");
const lyricsScroller = $("panel-lyrics");
const lyricsBody = $("lyrics-body");
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
    autoplay: state.autoplay,
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

// ===== Liked songs =====
// Guests: localStorage "guest_liked_songs". Signed-in users and artists: the server (/api/likes).
// The two never mix: an account never reads the guest list, so guest likes can't bleed into it.
function loadGuestLikes() {
  let saved = readJSON(GUEST_LIKES_KEY, null);
  const legacy = readJSON(LEGACY_LIKES_KEY, null);
  if (saved === null && Array.isArray(legacy)) {
    saved = legacy; // older builds kept everyone's likes under one key on this device
    writeJSON(GUEST_LIKES_KEY, saved);
  }
  return Array.isArray(saved) ? saved : [];
}

async function loadLiked() {
  try { localStorage.removeItem(LEGACY_LIKES_KEY); } catch { /* storage blocked */ }
  if (!state.signedIn) {
    state.liked = new Map(loadGuestLikes().map((t) => [t.id, t]));
  } else {
    try {
      const resp = await fetch("/api/likes");
      if (!resp.ok) throw new Error(String(resp.status));
      const { tracks } = await resp.json();
      state.liked = new Map(tracks.slice().reverse().map((t) => [t.id, t])); // API is newest first
    } catch {
      showToast("Couldn't load your liked songs right now.", { tone: "error" });
    }
  }
  updateHeart();
  updateLikedCount();
  if (state.currentView === "library") renderLibrary();
}

function saveGuestLikes() {
  writeJSON(GUEST_LIKES_KEY, [...state.liked.values()]);
}

function refreshLikeViews() {
  updateHeart();
  updateLikedCount();
  if (state.currentView === "library") renderLibrary();
}

async function toggleLike(track) {
  const nowLiked = !state.liked.has(track.id);
  // Optimistic: flip it now, undo if the server says no
  if (nowLiked) state.liked.set(track.id, track);
  else state.liked.delete(track.id);
  refreshLikeViews();

  if (!state.signedIn) {
    saveGuestLikes();
    showToast(nowLiked ? "Added to Liked Songs" : "Removed from Liked Songs", { duration: 2000 });
    return;
  }
  try {
    const resp = nowLiked
      ? await fetch("/api/likes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_id: track.id }),
      })
      : await fetch(`/api/likes/${encodeURIComponent(track.id)}`, { method: "DELETE" });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || "Couldn't update your likes.");
    showToast(nowLiked ? "Added to Liked Songs" : "Removed from Liked Songs", { duration: 2000 });
  } catch (err) {
    if (nowLiked) state.liked.delete(track.id);
    else state.liked.set(track.id, track);
    refreshLikeViews();
    showToast(err.message, { tone: "error" });
  }
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

// "⋯" button that opens the track menu (Add to queue, Add to playlist, Like, Go to artist)
function moreButton(track) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "icon-btn more-btn";
  btn.title = "More options";
  btn.setAttribute("aria-label", `More options for “${track.title || "track"}”`);
  btn.setAttribute("aria-haspopup", "menu");
  btn.innerHTML = DOTS_SVG;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    openTrackMenu(track, { anchor: btn });
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
  div.appendChild(moreButton(track));
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
  div.querySelector(".duration").before(moreButton(track));
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
  tr.querySelector(".col-actions").appendChild(moreButton(track));
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
  el.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    openTrackMenu(list[index], { x: e.clientX, y: e.clientY });
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
  state.queuedTrack = null;
  buildOrder(index);
  state.errorStreak = 0;
  loadCurrent();
}

// "Add to queue": plays after the current song, before the rest of the list; never interrupts playback
function addToQueue(track) {
  if (!track?.stream_url) {
    showToast(`No preview available for “${track?.title || "this track"}”.`, { tone: "warn" });
    return;
  }
  if (!currentTrack()) {
    playTrack([track], 0); // nothing playing: start it straight away
    return;
  }
  state.userQueue.push(track);
  showToast(`Added “${track.title}” to the queue`, { duration: 2000 });
  if (state.panel === "queue") renderQueue();
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
  maybePrefetchAutoplay();
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
  if (state.userQueue.length) {
    state.queuedTrack = state.userQueue.shift();
    loadCurrent();
    return;
  }
  const wasQueued = state.queuedTrack;
  state.queuedTrack = null; // the list picks up where it left off
  if (stepTo(1)) return;
  state.queuedTrack = wasQueued; // nothing left: stay on the song that just played
  if (state.autoplay) {
    // Spotify-style: fetch songs like this one and keep going
    const seq = state.loadSeq;
    fetchAutoplay().then((added) => {
      if (state.loadSeq !== seq) return; // the listener picked something else meanwhile
      state.queuedTrack = null;
      if (added && stepTo(1)) return;
      state.queuedTrack = wasQueued;
      endOfQueue(auto);
    });
    return;
  }
  endOfQueue(auto);
}

function endOfQueue(auto) {
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
  if (audio.currentTime > 3) {
    audio.currentTime = 0;
    return;
  }
  if (state.queuedTrack) {
    state.queuedTrack = null; // back to the list's song
    loadCurrent();
    return;
  }
  if (!stepTo(-1)) audio.currentTime = 0;
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
  playerMoreBtn.hidden = false;
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
  const canSkip = state.errorStreak < MAX_AUTO_SKIPS && (state.userQueue.length > 0 || state.pos < state.order.length - 1);
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
  if (e.key === "Escape" && state.panel && !document.querySelector("dialog[open]")) {
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

// Full-bleed: fill the stage, unless the clip's shape is far off (e.g. a vertical clip on a wide screen),
// in which case show all of it with feathered edges over the blurred cover.
function fitCanvas() {
  const vw = canvasVideo.videoWidth;
  const vh = canvasVideo.videoHeight;
  if (!vw || !vh || !stage.clientHeight) return;
  const mismatch = (vw / vh) / (stage.clientWidth / stage.clientHeight);
  stage.classList.toggle("canvas-contain", mismatch < 0.7 || mismatch > 1.45);
}
canvasVideo.addEventListener("loadedmetadata", fitCanvas);
window.addEventListener("resize", fitCanvas);
canvasVideo.addEventListener("error", () => {
  // Missing or broken clip: fall back to the cover art and CSS visualizer bars
  if (canvasVideo.getAttribute("src")) stage.classList.remove("has-canvas", "canvas-ready");
});

function setMode(mode) {
  state.mode = mode;
  stage.hidden = mode !== "video";
  if (mode === "video") requestAnimationFrame(fitCanvas);
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

  const userBox = $("queue-user");
  const userLabel = $("queue-user-label");
  userBox.innerHTML = "";
  userLabel.hidden = !state.userQueue.length;

  if (!current) {
    showMessage(now, "Nothing playing yet.");
    $("queue-next-label").hidden = true;
    return;
  }
  now.appendChild(renderQueueItem(current));

  $("queue-user-count").textContent = `(${state.userQueue.length})`;
  state.userQueue.forEach((track, i) => {
    const item = renderQueueItem(track, () => {
      state.queuedTrack = track;
      state.userQueue.splice(0, i + 1);
      state.errorStreak = 0;
      loadCurrent();
    });
    item.classList.add("user-queued");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn queue-remove";
    remove.setAttribute("aria-label", `Remove “${track.title}” from the queue`);
    remove.title = "Remove from queue";
    remove.textContent = "✕";
    remove.addEventListener("click", (e) => {
      e.stopPropagation();
      state.userQueue.splice(i, 1);
      renderQueue();
    });
    item.appendChild(remove);
    userBox.appendChild(item);
  });

  const upcoming = state.order.slice(state.pos + 1);
  const label = $("queue-next-label");
  label.hidden = false;
  label.textContent = state.shuffle ? `Next up · shuffled (${upcoming.length})` : `Next up (${upcoming.length})`;

  if (!upcoming.length) {
    showMessage(next, state.repeat ? "Repeating the current track." : "End of queue.");
  }
  let autoplayLabelShown = false;
  upcoming.slice(0, 50).forEach((queueIndex, offset) => {
    const targetPos = state.pos + 1 + offset;
    if (state.queue[queueIndex].autoplay && !autoplayLabelShown) {
      autoplayLabelShown = true;
      next.insertAdjacentHTML("beforeend", '<div class="autoplay-label">Autoplay · similar to what you\'re playing</div>');
    }
    next.appendChild(renderQueueItem(state.queue[queueIndex], () => {
      state.pos = targetPos;
      state.queuedTrack = null;
      state.errorStreak = 0;
      loadCurrent();
    }));
  });
  highlightPlaying();
}

let lyricsRequestId = 0;

// What the lyrics panel is showing: synced = [{time, text}] when timed lyrics match the playing audio
const lyricsView = { trackId: null, synced: null, activeIndex: -1, userScrollUntil: 0 };
let lyricsTicking = false;

async function loadLyrics() {
  const head = $("lyrics-head");
  const body = lyricsBody;
  const t = currentTrack();
  lyricsView.trackId = t ? t.id : null;
  lyricsView.synced = null;
  lyricsView.activeIndex = -1;
  body.classList.remove("is-synced");
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

    // Timings only line up with full-length local recordings; iTunes previews are a 30 s excerpt.
    const synced = t.local && Array.isArray(data.synced) && data.synced.length ? data.synced : null;
    lyricsView.synced = synced;
    body.classList.toggle("is-synced", !!synced);
    if (synced) {
      head.insertAdjacentHTML("beforeend", '<div class="lyrics-sync-badge">Live synced</div>');
      body.innerHTML = synced
        .map((line, i) => line.text
          ? `<p class="lyric-line" data-index="${i}" tabindex="0">${escapeHtml(line.text)}</p>`
          : `<p class="lyric-line break" data-index="${i}" aria-hidden="true">♪ ♪ ♪</p>`)
        .join("") +
        '<p class="lyrics-note">Tap a line, or select a few, to share them.</p>';
    } else {
      const note = t.local
        ? "Timed lyrics aren't available for this recording. Tap a line to share it."
        : "Lyrics are for the full song; the preview plays a 30-second excerpt, so they aren't synced. Tap a line to share it.";
      body.innerHTML = data.lines
        .map((line) => (line.trim() ? `<p class="lyric-line" tabindex="0">${escapeHtml(line)}</p>` : '<p class="gap"></p>'))
        .join("") +
        `<p class="lyrics-note">${note}</p>`;
    }
    lyricsScroller.scrollTop = 0;
    lyricsView.userScrollUntil = 0;
    syncLyrics({ instant: true });
    startLyricsClock();
  };

  if (state.lyricsCache.has(t.id)) return renderLyrics(state.lyricsCache.get(t.id));

  const requestId = ++lyricsRequestId;
  showMessage(body, "Loading lyrics…");
  const params = new URLSearchParams({ artist: t.artist || "", title: t.title || "" });
  if (t.album) params.set("album", t.album);
  if (t.duration) params.set("duration", String(Math.round(t.duration / 1000)));
  if (t.local) params.set("track_id", t.id);

  try {
    const resp = await fetch(`/api/lyrics?${params}`);
    const data = await resp.json();
    if (resp.ok || resp.status === 404) state.lyricsCache.set(t.id, data); // don't cache outages
    if (requestId === lyricsRequestId) renderLyrics(data);
  } catch {
    if (requestId === lyricsRequestId) showMessage(body, "Could not reach the lyrics service right now.");
  }
}

// ===== Synced lyrics: highlight the current line and keep it in view =====
function currentLyricIndex(synced, seconds) {
  let lo = 0;
  let hi = synced.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (synced[mid].time <= seconds) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

function syncLyrics({ instant = false } = {}) {
  const synced = lyricsView.synced;
  const t = currentTrack();
  if (!synced || state.panel !== "lyrics" || !t || t.id !== lyricsView.trackId) return;

  const index = currentLyricIndex(synced, audio.currentTime + LYRIC_LEAD_SECONDS);
  if (index === lyricsView.activeIndex && !instant) return;
  lyricsView.activeIndex = index;

  const lines = lyricsBody.querySelectorAll(".lyric-line");
  lines.forEach((el, i) => {
    el.classList.toggle("active", i === index);
    el.classList.toggle("past", i < index);
  });
  const active = lines[index];
  if (active && Date.now() > lyricsView.userScrollUntil) {
    // Keep the sung line a little above the middle, like karaoke
    const top = active.offsetTop - lyricsScroller.clientHeight * 0.38;
    lyricsScroller.scrollTo({ top: Math.max(0, top), behavior: instant ? "auto" : "smooth" });
  }
}

function startLyricsClock() {
  if (lyricsTicking) return;
  lyricsTicking = true;
  const tick = () => {
    if (audio.paused || !lyricsView.synced || state.panel !== "lyrics") {
      lyricsTicking = false;
      return;
    }
    syncLyrics();
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

audio.addEventListener("play", startLyricsClock);
audio.addEventListener("seeked", () => {
  lyricsView.userScrollUntil = 0; // a seek means "take me there"
  syncLyrics({ instant: true });
});

// Scrolling the lyrics by hand pauses auto-scroll for a moment
["wheel", "touchmove"].forEach((type) => {
  lyricsScroller.addEventListener(type, () => {
    lyricsView.userScrollUntil = Date.now() + LYRIC_MANUAL_SCROLL_MS;
  }, { passive: true });
});

// ===== Share Lyrics overlay =====
let shareQuote = null; // {text, title, artist, cover}

function selectedLyricsText() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !lyricsBody.contains(selection.anchorNode)) return "";
  return selection.toString().replace(/\n{2,}/g, "\n").trim();
}

function openShareModal(text) {
  const t = currentTrack();
  const lyricsTrack = t && t.id === lyricsView.trackId ? t : null;
  const clean = text.trim();
  if (!clean) return;
  shareQuote = {
    text: clean.length > SHARE_MAX_CHARS ? `${clean.slice(0, SHARE_MAX_CHARS - 1).trimEnd()}…` : clean,
    title: lyricsTrack?.title || $("lyrics-head").querySelector(".t")?.textContent || "",
    artist: lyricsTrack?.artist || $("lyrics-head").querySelector(".a")?.textContent || "",
    cover: lyricsTrack?.cover || "",
  };

  $("quote-text").textContent = shareQuote.text;
  $("quote-title").textContent = shareQuote.title;
  $("quote-artist").textContent = shareQuote.artist;
  setCover($("quote-cover"), shareQuote.cover);
  $("quote-card").style.setProperty("--quote-cover", shareQuote.cover ? `url(${JSON.stringify(shareQuote.cover)})` : "none");
  resetCopyButton();
  $("share-native").hidden = typeof navigator.share !== "function";

  if (!shareModal.open) shareModal.showModal();
  $("share-copy").focus();
}

function shareMessage() {
  const q = shareQuote;
  const credit = [q.title, q.artist].filter(Boolean).join(" · ");
  return `“${q.text}”\n— ${credit}\n🎧 Listening on Sonoria`;
}

function resetCopyButton() {
  const btn = $("share-copy");
  btn.classList.remove("copied");
  btn.querySelector(".share-label").textContent = "Copy quote";
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older browsers / non-secure origins: copy from a hidden textarea inside the dialog
    // (the rest of the page is inert while the modal is open).
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    shareModal.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

$("share-copy").addEventListener("click", async () => {
  const btn = $("share-copy");
  const ok = await copyToClipboard(shareMessage());
  btn.classList.toggle("copied", ok);
  btn.querySelector(".share-label").textContent = ok ? "Copied!" : "Couldn't copy";
  setTimeout(resetCopyButton, 1800);
});

function openShareWindow(url) {
  window.open(url, "_blank", "noopener,noreferrer");
}

$("share-x").addEventListener("click", () => {
  openShareWindow(`https://x.com/intent/tweet?text=${encodeURIComponent(shareMessage())}`);
});
$("share-whatsapp").addEventListener("click", () => {
  openShareWindow(`https://wa.me/?text=${encodeURIComponent(shareMessage())}`);
});
$("share-native").addEventListener("click", () => {
  navigator.share({ title: `${shareQuote.title} lyrics`, text: shareMessage() }).catch(() => {
    // Dismissed or unsupported target; nothing to do
  });
});
$("share-modal-close").addEventListener("click", () => shareModal.close());
shareModal.addEventListener("click", (e) => {
  if (e.target === shareModal) shareModal.close(); // backdrop
});

// Click a line (or finish selecting some text) in the lyrics panel to share it
lyricsBody.addEventListener("click", (e) => {
  const selected = selectedLyricsText();
  if (selected) return openShareModal(selected);
  const line = e.target.closest(".lyric-line");
  if (line && !line.classList.contains("break")) openShareModal(line.textContent);
});
lyricsBody.addEventListener("keydown", (e) => {
  const line = e.target.closest(".lyric-line");
  if (line && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    openShareModal(line.textContent);
  }
});

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
  if (!state.recsLoaded || state.recsStalePlays > 0) loadRecommendations();
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
  const topCharts = $("top-charts");

  showMessage(jumpBackIn, "Loading...");
  topCharts.innerHTML = "";

  try {
    const resp = await fetch("/api/songs");
    const { sections } = await resp.json();
    const chill = (sections["Chill"] || []).slice(0, 6);
    const charts = sections["Top Charts"] || [];

    jumpBackIn.innerHTML = "";
    if (!chill.length && !charts.length) {
      return showMessage(jumpBackIn, "Could not reach the music service right now.");
    }

    chill.forEach((t, i) => jumpBackIn.appendChild(renderQuickTile(t, chill, i)));
    charts.forEach((t, i) => topCharts.appendChild(renderChartRow(t, charts, i)));
    state.homeLoaded = true;
    highlightPlaying();
  } catch {
    showMessage(jumpBackIn, "Could not reach the music service right now.");
  }
}

// ===== Recommendations: re-weighted by what you listen to =====
function loadRecentPlays() {
  // Guests keep a short history on this device; accounts start fresh (the server has their history)
  const saved = state.signedIn ? [] : readJSON(GUEST_RECENT_KEY, []);
  state.recentPlays = Array.isArray(saved) ? saved.slice(0, RECENT_PLAYS_MAX) : [];
}

function rememberPlay(track) {
  const entry = { id: String(track.id), genre: track.genre || "", title: track.title || "" };
  state.recentPlays = [entry, ...state.recentPlays.filter((p) => p.id !== entry.id)].slice(0, RECENT_PLAYS_MAX);
  if (!state.signedIn) writeJSON(GUEST_RECENT_KEY, state.recentPlays);
  state.recsStalePlays += 1;
  // Refresh the row in place while it's on screen, unless the listener is pointing at it
  if (state.currentView === "home" && state.recsStalePlays >= RECS_REFRESH_EVERY && !$("recommended").matches(":hover")) {
    loadRecommendations({ quiet: true });
  }
}

function recommendationParams({ limit, seed = null, exclude = [] }) {
  const params = new URLSearchParams({ limit: String(limit) });
  const genres = state.recentPlays.map((p) => p.genre).filter(Boolean);
  if (genres.length) params.set("recent", genres.join(","));
  const ids = new Set([...exclude.map(String), ...state.recentPlays.slice(0, 20).map((p) => p.id)]);
  if (ids.size) params.set("exclude", [...ids].join(","));
  state.recentPlays.slice(0, 15).forEach((p) => p.title && params.append("exclude_title", p.title));
  if (seed) {
    params.set("seed", String(seed.id));
    if (seed.genre) params.set("seed_genre", seed.genre);
    if (seed.title) params.set("seed_title", seed.title);
  }
  return params;
}

let recsRequestId = 0;

async function loadRecommendations({ quiet = false } = {}) {
  const row = $("recommended");
  const reason = $("recommended-reason");
  const requestId = ++recsRequestId;
  if (!quiet || !row.children.length) showMessage(row, "Finding songs for you…");
  try {
    const resp = await fetch(`/api/recommendations?${recommendationParams({ limit: 12 })}`);
    if (!resp.ok) throw new Error(String(resp.status));
    const { tracks, reason: why } = await resp.json();
    if (requestId !== recsRequestId) return;
    state.recsLoaded = true;
    state.recsStalePlays = 0;
    reason.textContent = why || "";
    if (!tracks.length) return showMessage(row, "Play a few songs and we'll find more like them.");
    row.classList.remove("refreshed");
    row.innerHTML = "";
    tracks.forEach((t, i) => row.appendChild(renderCard(t, tracks, i)));
    void row.offsetWidth;
    row.classList.add("refreshed");
    highlightPlaying();
  } catch {
    if (requestId === recsRequestId && !row.querySelector(".card")) {
      showMessage(row, "Could not load recommendations right now.");
    }
  }
}

// ===== Autoplay: when the list and your queue run out, keep going with similar songs =====
function maybePrefetchAutoplay() {
  const remaining = state.order.length - state.pos - 1;
  if (state.autoplay && !state.userQueue.length && remaining <= 1 && currentTrack()) fetchAutoplay();
}

function fetchAutoplay() {
  if (state.autoplayLoading) return state.autoplayLoading;
  const seed = currentTrack();
  const params = recommendationParams({ limit: AUTOPLAY_BATCH, seed, exclude: state.queue.map((t) => t.id) });
  state.autoplayLoading = fetch(`/api/recommendations?${params}`)
    .then((resp) => (resp.ok ? resp.json() : { tracks: [] }))
    .then(({ tracks }) => {
      const inQueue = new Set(state.queue.map((t) => String(t.id)));
      const fresh = tracks.filter((t) => t.stream_url && !inQueue.has(String(t.id)));
      fresh.forEach((t) => {
        state.queue.push({ ...t, autoplay: true });
        state.order.push(state.queue.length - 1);
      });
      if (fresh.length && state.panel === "queue") renderQueue();
      return fresh.length;
    })
    .catch(() => 0)
    .finally(() => { state.autoplayLoading = null; });
  return state.autoplayLoading;
}

function setAutoplay(on) {
  state.autoplay = on;
  $("autoplay-toggle").checked = on;
  savePlayerPrefs();
  if (on) {
    maybePrefetchAutoplay();
  } else {
    // Drop autoplay songs that haven't played yet
    state.order = state.order.filter((queueIndex, pos) => pos <= state.pos || !state.queue[queueIndex].autoplay);
  }
  if (state.panel === "queue") renderQueue();
  showToast(on ? "Autoplay on: similar songs keep playing" : "Autoplay off", { duration: 1800 });
}

$("autoplay-toggle").checked = state.autoplay;
$("autoplay-toggle").addEventListener("change", (e) => setAutoplay(e.target.checked));

// ===== Genre badge =====
async function recordPlay(track) {
  if (!track) return;
  rememberPlay(track);
  if (!track.local && !state.signedIn) return; // guests only bump catalogue play counts
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

// ===== Track menu (⋯ buttons, right-click, player) =====
const trackMenu = document.createElement("div");
trackMenu.className = "track-menu";
trackMenu.setAttribute("role", "menu");
trackMenu.hidden = true;
document.body.appendChild(trackMenu);
let trackMenuReturnFocus = null;
let trackMenuAnchor = null;   // the ⋯ button it hangs from (null for right-click menus)
let trackMenuOpenedAt = 0;

function trackMenuItems(track) {
  const items = [
    { label: "Add to queue", icon: QUEUE_ADD_SVG, run: () => addToQueue(track) },
  ];
  if (track.local) items.push({ label: "Add to playlist", icon: PLUS_SVG, run: () => openPlaylistModal(track) });
  items.push({
    label: state.liked.has(track.id) ? "Remove from Liked Songs" : "Add to Liked Songs",
    icon: HEART_SVG,
    run: () => toggleLike(track),
  });
  if (track.local && track.artist_id) {
    items.push({ label: "Go to artist", icon: ARTIST_SVG, run: () => location.assign(`/artist/${track.artist_id}`) });
  }
  return items;
}

function openTrackMenu(track, { anchor = null, x = 0, y = 0 } = {}) {
  if (!track) return;
  trackMenu.innerHTML = `<div class="track-menu-head">${escapeHtml(track.title || "Untitled")}</div>`;
  trackMenuItems(track).forEach((item) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("role", "menuitem");
    btn.innerHTML = `${item.icon}<span>${escapeHtml(item.label)}</span>`;
    btn.addEventListener("click", () => {
      closeTrackMenu();
      item.run();
    });
    trackMenu.appendChild(btn);
  });

  trackMenu.hidden = false;
  trackMenuReturnFocus = anchor || document.activeElement;
  trackMenuAnchor = anchor;
  trackMenuOpenedAt = performance.now();
  positionTrackMenu(x, y);
  trackMenu.querySelector("[role=menuitem]").focus({ preventScroll: true });
}

function positionTrackMenu(x = 0, y = 0) {
  const menuRect = trackMenu.getBoundingClientRect();
  if (trackMenuAnchor) {
    const r = trackMenuAnchor.getBoundingClientRect();
    x = r.right - menuRect.width;
    y = r.bottom + 6;
    if (y + menuRect.height > window.innerHeight - 8) y = r.top - menuRect.height - 6; // flip above
  }
  trackMenu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menuRect.width - 8))}px`;
  trackMenu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menuRect.height - 8))}px`;
}

// Scrolling keeps a ⋯ menu pinned to its button until the button leaves the screen;
// a right-click menu closes, except for scroll-snap settling right after it opened.
function onScrollWithMenu() {
  if (trackMenu.hidden) return;
  if (trackMenuAnchor) {
    const r = trackMenuAnchor.getBoundingClientRect();
    const visible = trackMenuAnchor.isConnected && r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;
    if (visible) positionTrackMenu();
    else closeTrackMenu({ restoreFocus: false });
  } else if (performance.now() - trackMenuOpenedAt > 250) {
    closeTrackMenu({ restoreFocus: false });
  }
}

function closeTrackMenu({ restoreFocus = true } = {}) {
  if (trackMenu.hidden) return;
  trackMenu.hidden = true;
  if (restoreFocus && trackMenuReturnFocus?.isConnected) trackMenuReturnFocus.focus();
}

trackMenu.addEventListener("keydown", (e) => {
  const items = [...trackMenu.querySelectorAll("[role=menuitem]")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    closeTrackMenu();
  } else if (e.key === "Tab") {
    closeTrackMenu({ restoreFocus: false });
  }
});
document.addEventListener("pointerdown", (e) => {
  if (!trackMenu.hidden && !trackMenu.contains(e.target) && !e.target.closest(".more-btn")) {
    closeTrackMenu({ restoreFocus: false });
  }
});
["resize", "blur"].forEach((type) => window.addEventListener(type, () => closeTrackMenu({ restoreFocus: false })));
document.addEventListener("scroll", onScrollWithMenu, true);

playerMoreBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const t = currentTrack();
  if (t) openTrackMenu(t, { anchor: playerMoreBtn });
});
$("queue-clear").addEventListener("click", () => {
  state.userQueue = [];
  renderQueue();
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
loadLiked();
loadRecentPlays();
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
