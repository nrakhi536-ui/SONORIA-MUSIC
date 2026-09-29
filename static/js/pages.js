// =====================================================================
// Sonoria server-rendered pages: tabs, count-up stats, animated bars,
// the mini player (live equalizer + stream counters), uploads and confirms.
// =====================================================================
(() => {
  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const number = new Intl.NumberFormat();

  function showToast(message, tone = "info") {
    const stack = $("toast-stack");
    const toast = document.createElement("div");
    toast.className = `toast toast-${tone}`;
    toast.textContent = message;
    stack.appendChild(toast);
    setTimeout(() => {
      toast.classList.add("leaving");
      setTimeout(() => toast.remove(), 400);
    }, 3000);
  }

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00";
    return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  }

  // ----- Count-up numbers -----
  function countUp(el) {
    const target = parseFloat(el.dataset.count);
    if (!isFinite(target)) return;
    const decimals = String(el.dataset.count).includes(".") ? 1 : 0;
    const show = (v) => { el.textContent = decimals ? v.toFixed(decimals) : number.format(Math.round(v)); };
    if (reduceMotion || target === 0) return show(target);
    const start = performance.now();
    const duration = Math.min(1400, 500 + target * 8);
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      show(target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ----- Bars grow to their share when they come into view -----
  function fillBar(bar) {
    const value = parseFloat(bar.dataset.bar) || 0;
    const max = parseFloat(bar.dataset.barMax) || 1;
    bar.style.setProperty("--w", `${Math.max(value > 0 ? 3 : 0, Math.min(100, (value / max) * 100))}%`);
  }

  const seen = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const el = entry.target;
      if (el.dataset.count !== undefined) countUp(el);
      if (el.dataset.bar !== undefined) fillBar(el);
      seen.unobserve(el);
    });
  }, { threshold: 0.2 });

  function observeIn(root) {
    root.querySelectorAll("[data-count], [data-bar]").forEach((el) => seen.observe(el));
  }
  observeIn(document);

  // ----- Tabs -----
  document.querySelectorAll("[data-tabs]").forEach((tabs) => {
    const list = tabs.querySelector(".tab-list");
    const buttons = [...list.querySelectorAll("[role=tab]")];
    const panels = [...tabs.querySelectorAll(":scope > [data-panel]")];

    function moveIndicator(btn) {
      list.style.setProperty("--tab-x", `${btn.offsetLeft - 4}px`);
      list.style.setProperty("--tab-w", `${btn.offsetWidth}px`);
    }

    function select(name, { focus = false, remember = true } = {}) {
      const btn = buttons.find((b) => b.dataset.tab === name) || buttons[0];
      buttons.forEach((b) => {
        const on = b === btn;
        b.setAttribute("aria-selected", String(on));
        b.tabIndex = on ? 0 : -1;
      });
      panels.forEach((panel) => {
        const on = panel.dataset.panel === btn.dataset.tab;
        if (on && panel.hidden) {
          panel.hidden = false;
          panel.classList.remove("entering");
          void panel.offsetWidth;
          panel.classList.add("entering");
          observeIn(panel); // count-ups and bars inside run when first shown
        } else if (!on) {
          panel.hidden = true;
        }
      });
      moveIndicator(btn);
      if (focus) btn.focus();
      if (remember) {
        const url = new URL(location.href);
        url.searchParams.set("tab", btn.dataset.tab);
        url.searchParams.delete("error");
        history.replaceState(null, "", url);
      }
    }

    buttons.forEach((btn, i) => {
      btn.id = btn.id || `tab-${btn.dataset.tab}`;
      btn.addEventListener("click", () => select(btn.dataset.tab));
      btn.addEventListener("keydown", (e) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
        if (step) {
          e.preventDefault();
          select(buttons[(i + step + buttons.length) % buttons.length].dataset.tab, { focus: true });
        }
      });
    });
    panels.forEach((panel) => panel.setAttribute("aria-labelledby", `tab-${panel.dataset.panel}`));

    const initial = new URLSearchParams(location.search).get("tab") || tabs.dataset.initial;
    select(initial, { remember: false });
    window.addEventListener("resize", () => moveIndicator(buttons.find((b) => b.getAttribute("aria-selected") === "true")));
    document.fonts?.ready.then(() => moveIndicator(buttons.find((b) => b.getAttribute("aria-selected") === "true")));

    document.querySelectorAll("[data-goto-tab]").forEach((el) => {
      el.addEventListener("click", () => {
        select(el.dataset.gotoTab);
        tabs.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      });
    });
  });

  // ----- Mini player -----
  const audio = $("page-audio");
  const tracksEl = $("page-tracks");
  const tracks = tracksEl ? JSON.parse(tracksEl.textContent) : [];
  const player = { index: -1, recorded: new Set() };

  function highlight() {
    const id = tracks[player.index]?.id;
    document.querySelectorAll("[data-track-id]").forEach((el) => {
      el.classList.toggle("is-playing", el.dataset.trackId === id);
    });
  }

  function playIndex(index) {
    const t = tracks[index];
    if (!t || !t.stream_url) return;
    if (index === player.index && audio.src) {
      audio.paused ? audio.play() : audio.pause();
      return;
    }
    player.index = index;
    audio.src = t.stream_url;
    audio.play().catch(() => showToast("Press play to start listening."));
    $("pp-title").textContent = t.title;
    $("pp-artist").textContent = t.artist || "";
    $("pp-cover").style.backgroundImage = t.cover ? `url(${JSON.stringify(t.cover)})` : "";
    $("page-player").hidden = false;
    document.body.classList.add("has-player");
    highlight();
  }

  // Count one stream per track per page visit, then bump every counter on the page
  async function recordStream(t) {
    if (player.recorded.has(t.id)) return;
    player.recorded.add(t.id);
    try {
      const resp = await fetch("/api/track/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_id: t.id }),
      });
      if (!resp.ok) return;
    } catch {
      return;
    }
    t.play_count = (t.play_count || 0) + 1;
    document.querySelectorAll(`[data-streams="${t.id}"]`).forEach((el) => {
      el.textContent = number.format(t.play_count);
      el.classList.remove("bump");
      void el.offsetWidth;
      el.classList.add("bump");
    });
    refreshPerformance(t);
    document.querySelectorAll("[data-live-total]").forEach((el) => {
      const total = (parseInt(el.textContent.replace(/\D/g, ""), 10) || 0) + 1;
      el.textContent = number.format(total);
      el.classList.remove("bump");
      void el.offsetWidth;
      el.classList.add("bump");
    });
  }

  // Performance chart: re-scale every bar and tooltip after a new stream
  function refreshPerformance(t) {
    const fill = document.querySelector(`.perf-row[data-track-id="${t.id}"] .bar-fill`);
    if (!fill) return;
    fill.dataset.bar = t.play_count;
    const rows = [...document.querySelectorAll(".perf-row")];
    const values = rows.map((row) => parseFloat(row.querySelector(".bar-fill").dataset.bar) || 0);
    const max = Math.max(1, ...values);
    const total = values.reduce((a, b) => a + b, 0);
    rows.forEach((row, i) => {
      const bar = row.querySelector(".bar-fill");
      bar.dataset.barMax = max;
      fillBar(bar);
      const share = total ? Math.round((1000 * values[i]) / total) / 10 : 0;
      row.querySelector(".perf-tip").textContent = `${number.format(values[i])} streams · ${share}% of total`;
    });
  }

  if (tracks.length) {
    document.addEventListener("click", (e) => {
      const trigger = e.target.closest("[data-play]");
      if (trigger && !trigger.disabled) {
        playIndex(tracks.findIndex((t) => t.id === trigger.dataset.play));
      }
      if (e.target.closest("[data-play-all]")) playIndex(0);
    });
    $("pp-toggle").addEventListener("click", () => (audio.paused ? audio.play() : audio.pause()));
    audio.addEventListener("play", () => document.body.classList.add("audio-playing"));
    audio.addEventListener("pause", () => document.body.classList.remove("audio-playing"));
    audio.addEventListener("playing", () => recordStream(tracks[player.index]));
    audio.addEventListener("ended", () => {
      const next = tracks.findIndex((t, i) => i > player.index && t.stream_url);
      if (next >= 0) playIndex(next);
    });
    audio.addEventListener("error", () => {
      if (audio.getAttribute("src")) showToast("Couldn't load this track.", "error");
    });
    audio.addEventListener("loadedmetadata", () => {
      $("pp-duration").textContent = formatTime(audio.duration);
      $("pp-bar").setAttribute("aria-valuemax", String(Math.floor(audio.duration)));
    });
    audio.addEventListener("timeupdate", () => {
      const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
      $("pp-fill").style.width = `${pct}%`;
      $("pp-time").textContent = formatTime(audio.currentTime);
      $("pp-bar").setAttribute("aria-valuenow", String(Math.floor(audio.currentTime)));
    });
    const seek = (e) => {
      const rect = $("pp-bar").getBoundingClientRect();
      if (isFinite(audio.duration)) audio.currentTime = ((e.clientX - rect.left) / rect.width) * audio.duration;
    };
    $("pp-bar").addEventListener("click", seek);
    $("pp-bar").addEventListener("keydown", (e) => {
      const jump = { ArrowRight: 5, ArrowLeft: -5 }[e.key];
      if (jump && isFinite(audio.duration)) {
        e.preventDefault();
        audio.currentTime = Math.min(audio.duration, Math.max(0, audio.currentTime + jump));
      }
    });
  }

  // Older pages with plain <audio controls>: only one plays at a time
  document.querySelectorAll(".page-content audio").forEach((el) => {
    el.addEventListener("play", () => {
      document.querySelectorAll("audio").forEach((other) => { if (other !== el) other.pause(); });
    });
  });

  // ----- Confirm before destructive forms -----
  const confirmModal = $("confirm-modal");
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (form.dataset.confirmed) return;
      e.preventDefault();
      $("confirm-text").textContent = form.dataset.confirm;
      confirmModal.showModal();
      $("confirm-ok").onclick = () => {
        form.dataset.confirmed = "1";
        confirmModal.close();
        form.requestSubmit();
      };
    });
  });
  $("confirm-cancel").addEventListener("click", () => confirmModal.close());
  confirmModal.addEventListener("click", (e) => { if (e.target === confirmModal) confirmModal.close(); });

  // ----- Upload: drag & drop, validation, progress -----
  const form = $("upload-form");
  if (form) {
    const fileInput = $("upload-file");
    const dropzone = $("dropzone");
    const error = $("upload-error");
    const MAX_BYTES = 20 * 1024 * 1024;
    const ALLOWED = ["mp3", "wav", "m4a"];

    function chosen(file) {
      $("drop-title").textContent = file ? file.name : "Drop an audio file here, or click to choose";
      dropzone.classList.toggle("has-file", !!file);
      const title = $("upload-title");
      if (file && !title.value.trim()) {
        title.value = file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
      }
    }

    fileInput.addEventListener("change", () => chosen(fileInput.files[0]));
    ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    }));
    ["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, () => dropzone.classList.remove("dragover")));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        chosen(fileInput.files[0]);
      }
    });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const file = fileInput.files[0];
      const title = $("upload-title").value.trim();
      error.textContent = "";
      if (!file) return (error.textContent = "Choose an audio file first.");
      if (!ALLOWED.includes(file.name.split(".").pop().toLowerCase())) return (error.textContent = "Only MP3, WAV or M4A files.");
      if (file.size > MAX_BYTES) return (error.textContent = "That file is over the 20 MB limit.");
      if (!title) return (error.textContent = "Give the track a title.");

      const submit = $("upload-submit");
      submit.disabled = true;
      $("upload-progress").hidden = false;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", form.action);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", (p) => {
        if (p.lengthComputable) $("upload-bar").style.width = `${(p.loaded / p.total) * 100}%`;
      });
      xhr.addEventListener("load", () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch { /* non-JSON error page */ }
        if (xhr.status === 201) {
          showToast(`“${data.track.title}” is live!`);
          setTimeout(() => location.assign("/artist/dashboard?tab=tracks"), 700);
        } else {
          error.textContent = data.error || `Upload failed (${xhr.status}).`;
          submit.disabled = false;
          $("upload-progress").hidden = true;
        }
      });
      xhr.addEventListener("error", () => {
        error.textContent = "Upload failed. Check your connection and try again.";
        submit.disabled = false;
        $("upload-progress").hidden = true;
      });
      xhr.send(new FormData(form));
    });
  }
})();
