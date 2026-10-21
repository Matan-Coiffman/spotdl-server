"use strict";

(() => {
  const KINDS = {
    track: "Song",
    album: "Album",
    playlist: "Playlist",
    artist: "Artist",
    user: "Playlists",
  };
  // spotDL's progress messages, in plainer words
  const STEPS = {
    "Getting metadata": "Looking up",
    "Getting lyrics": "Looking up",
    "Searching for song": "Matching",
    "Getting audio meta": "Matching",
    Downloading: "Downloading",
    Converting: "Converting",
    "Embedding metadata": "Tagging",
  };
  const ACTIVE = new Set(["resolving", "queued", "running"]);
  const LOSSLESS = new Set(["flac", "wav"]);
  const LINK =
    /^(https?:\/\/|spotify:(track|album|playlist|artist):|(www\.|m\.)?(open\.spotify\.com|spotify\.link|music\.youtube\.com|youtube\.com|youtu\.be)\/)/i;
  const SEARCH_DELAY = 450;
  const DEFAULT_TINT = "#535353";

  const narrow = window.matchMedia("(max-width: 768px)");
  const byId = (id) => document.getElementById(id);
  const ui = {
    app: byId("app"),
    gate: byId("gate"),
    gateForm: byId("gate-form"),
    gateNote: byId("gate-note"),
    password: byId("password"),
    form: byId("entry-form"),
    query: byId("query"),
    clear: byId("clear"),
    submit: byId("submit"),
    home: byId("home-button"),
    format: byId("format"),
    bitrate: byId("bitrate"),
    connection: byId("connection"),
    lock: byId("lock"),
    filters: byId("filters"),
    jobs: byId("jobs"),
    libraryEmpty: byId("library-empty"),
    start: byId("start-button"),
    colophon: byId("colophon"),
    main: byId("main"),
    homeView: byId("home-view"),
    searchView: byId("search-view"),
    searchBack: byId("search-back"),
    searchNote: byId("search-note"),
    results: byId("results"),
    jobView: byId("job-view"),
    jobBack: byId("job-back"),
    jobCover: byId("job-cover"),
    jobTitle: byId("job-title"),
    jobMeta: byId("job-meta"),
    jobDownload: byId("job-download"),
    jobRing: byId("job-ring"),
    jobRingLabel: byId("job-ring-label"),
    jobRetry: byId("job-retry"),
    jobRetryLabel: byId("job-retry-label"),
    jobRemove: byId("job-remove"),
    jobRemoveLabel: byId("job-remove-label"),
    jobStatus: byId("job-status"),
    tracksHead: byId("tracks-head"),
    tracks: byId("tracks"),
    toast: byId("toast"),
  };
  const templates = {
    item: byId("item-template"),
    track: byId("track-template"),
    result: byId("result-template"),
  };

  const state = {
    session: null,
    jobs: new Map(),
    items: new Map(),
    selected: null,
    filter: null,
    rows: [],
    rowsJob: null,
    coverKey: null,
    colors: new Map(),
    source: null,
    retryTimer: null,
    searchTimer: null,
    searchToken: 0,
    toastTimer: null,
    booted: false,
  };

  // Server

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, options = {}) {
    const init = { credentials: "same-origin", ...options };
    if (options.body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
    }

    let response;
    try {
      response = await fetch(path, init);
    } catch {
      throw new ApiError("Can't reach the server. Check your connection and try again.", 0);
    }

    if (response.status === 204) {
      return null;
    }

    let data = null;
    try {
      data = await response.json();
    } catch {
      // Not every error comes back as JSON
    }

    if (!response.ok) {
      if (response.status === 401 && state.session && state.session.auth_required) {
        state.session.authenticated = false;
        lock();
      }
      const detail =
        data && typeof data.detail === "string"
          ? data.detail
          : `Something went wrong (error ${response.status}). Try again.`;
      throw new ApiError(detail, response.status);
    }

    return data;
  }

  const prefs = {
    get(key) {
      try {
        return localStorage.getItem(`spotdl:${key}`);
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(`spotdl:${key}`, value);
      } catch {
        // Storage can be unavailable, e.g. in private windows
      }
    },
  };

  // Formatting

  const pad = (number, width = 2) => String(number).padStart(width, "0");
  const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

  function formatDuration(seconds) {
    if (!seconds) {
      return "";
    }
    return `${Math.floor(seconds / 60)}:${pad(seconds % 60)}`;
  }

  function formatHours(hours) {
    if (hours >= 48 && hours % 24 === 0) {
      return `${hours / 24} days`;
    }
    return plural(hours, "hour");
  }

  function formatSize(bytes) {
    if (bytes >= 1e9) {
      return `${(bytes / 1e9).toFixed(1)} GB`;
    }
    return `${Math.max(0.1, bytes / 1e6).toFixed(1)} MB`;
  }

  function formatQuality(job) {
    const format = job.format.toUpperCase();
    return job.bitrate === "auto" ? format : `${format} ${job.bitrate.replace("k", " kbps")}`;
  }

  function computeStats(tracks) {
    const total = tracks.length;
    let done = 0;
    let failed = 0;
    let progress = 0;
    let size = 0;
    for (const track of tracks) {
      done += track.status === "done" ? 1 : 0;
      failed += track.status === "error" ? 1 : 0;
      progress += track.progress;
      size += track.size || 0;
    }
    return { total, done, failed, size, progress: total ? Math.round(progress / total) : 0 };
  }

  function kindOf(job) {
    return KINDS[job.kind] || KINDS.track;
  }

  // One line saying where a download is at, and its tone
  function statusOf(job) {
    const stats = job.stats;
    switch (job.status) {
      case "resolving":
        return ["Looking it up on Spotify…", ""];
      case "queued":
        return ["Waiting for a free slot…", ""];
      case "running":
        if (stats.total === 1) {
          return [`${STEPS[job.tracks[0].message] || "Downloading"}…`, "green"];
        }
        return [`Downloading ${stats.done} of ${plural(stats.total, "song")}`, "green"];
      case "done":
        if (stats.failed) {
          return [`${stats.done} of ${plural(stats.total, "song")} downloaded`, ""];
        }
        return ["Downloaded", ""];
      case "failed":
        return [job.error || "Couldn't download this", "red"];
      default:
        return ["Cancelled", ""];
    }
  }

  function subtitleOf(job) {
    if (!job.tracks.length) {
      return job.status === "resolving" ? "Looking up…" : kindOf(job);
    }
    if (job.subtitle) {
      return `${kindOf(job)} • ${job.subtitle}`;
    }
    return `${kindOf(job)} • ${plural(job.stats.total, "song")}`;
  }

  // Small helpers

  function clone(template) {
    return template.content.firstElementChild.cloneNode(true);
  }

  function coverImage(url) {
    const image = new Image();
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => image.remove(), { once: true });
    image.src = url;
    return image;
  }

  function fillCover(element, url, kind) {
    const key = `${url || ""}|${kind}`;
    if (element.dataset.key === key) {
      return;
    }
    element.dataset.key = key;
    element.classList.toggle("round-art", kind === "artist");
    if (url) {
      element.replaceChildren(coverImage(url));
    } else {
      element.replaceChildren(icon("i-note"));
    }
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", `#${name}`);
    svg.append(use);
    return svg;
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
  }

  function toast(text, isError = false) {
    clearTimeout(state.toastTimer);
    ui.toast.textContent = text;
    ui.toast.classList.toggle("is-error", isError);
    ui.toast.hidden = false;
    state.toastTimer = setTimeout(() => {
      ui.toast.hidden = true;
    }, isError ? 6000 : 3000);
  }

  // The header tints to the cover, like an album page
  function tintFor(url) {
    if (!url) {
      return Promise.resolve(null);
    }
    if (state.colors.has(url)) {
      return Promise.resolve(state.colors.get(url));
    }

    return new Promise((resolve) => {
      const done = (color) => {
        state.colors.set(url, color);
        resolve(color);
      };
      const image = new Image();
      image.crossOrigin = "anonymous";
      image.referrerPolicy = "no-referrer";
      image.addEventListener(
        "load",
        () => {
          try {
            const size = 12;
            const canvas = document.createElement("canvas");
            canvas.width = size;
            canvas.height = size;
            const context = canvas.getContext("2d", { willReadFrequently: true });
            context.drawImage(image, 0, 0, size, size);
            const data = context.getImageData(0, 0, size, size).data;
            let red = 0;
            let green = 0;
            let blue = 0;
            for (let index = 0; index < data.length; index += 4) {
              red += data[index];
              green += data[index + 1];
              blue += data[index + 2];
            }
            const pixels = data.length / 4;
            done(toTint(red / pixels, green / pixels, blue / pixels));
          } catch {
            done(null);
          }
        },
        { once: true },
      );
      image.addEventListener("error", () => done(null), { once: true });
      image.src = url;
    });
  }

  // Keep the cover's hue, but dark and calm enough for white text
  function toTint(red, green, blue) {
    const r = red / 255;
    const g = green / 255;
    const b = blue / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const light = (max + min) / 2;
    let hue = 0;
    let saturation = 0;
    if (max !== min) {
      const delta = max - min;
      saturation = light > 0.5 ? delta / (2 - max - min) : delta / (max + min);
      if (max === r) {
        hue = (g - b) / delta + (g < b ? 6 : 0);
      } else if (max === g) {
        hue = (b - r) / delta + 2;
      } else {
        hue = (r - g) / delta + 4;
      }
      hue *= 60;
    }
    const s = Math.min(0.6, Math.max(0.12, saturation * 1.2));
    const l = Math.min(0.36, Math.max(0.22, light * 0.7));
    return `hsl(${Math.round(hue)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;
  }

  // Views

  function setView(view) {
    document.body.dataset.view = view;
    ui.homeView.hidden = view !== "home";
    ui.searchView.hidden = view !== "search";
    ui.jobView.hidden = view !== "job";
  }

  function goHome() {
    state.selected = null;
    markSelected();
    setView("home");
  }

  // Your Downloads

  function matchesFilter(job) {
    switch (state.filter) {
      case "active":
        return ACTIVE.has(job.status);
      case "done":
        return job.status === "done";
      case "failed":
        return job.status === "failed" || job.stats.failed > 0;
      default:
        return true;
    }
  }

  function applyFilter() {
    for (const [id, item] of state.items) {
      item.root.hidden = !matchesFilter(state.jobs.get(id));
    }
  }

  function markSelected() {
    for (const [id, item] of state.items) {
      item.button.setAttribute("aria-current", String(id === state.selected));
    }
  }

  function createItem(job) {
    const root = clone(templates.item);
    const item = {
      root,
      button: root.querySelector(".item-button"),
      cover: root.querySelector(".item-cover"),
      title: root.querySelector(".item-title"),
      sub: root.querySelector(".item-sub"),
      bar: root.querySelector(".item-bar"),
    };
    item.button.addEventListener("click", () => selectJob(job.id));
    return item;
  }

  function insertItem(item, job) {
    for (const other of ui.jobs.children) {
      const otherJob = state.jobs.get(other.dataset.id);
      if (otherJob && otherJob.created_at < job.created_at) {
        ui.jobs.insertBefore(item.root, other);
        return;
      }
    }
    ui.jobs.append(item.root);
  }

  function renderItem(item, job) {
    item.root.dataset.id = job.id;
    item.root.dataset.status = job.status;
    fillCover(item.cover, job.covers[0], job.kind);

    const title = job.tracks.length ? job.title || job.query : job.query;
    item.title.textContent = title;
    item.button.title = title;

    const [status] = statusOf(job);
    item.sub.textContent =
      job.status === "running" || job.status === "failed" ? status : subtitleOf(job);
    item.bar.style.setProperty("--p", job.stats.progress);
    item.button.setAttribute("aria-current", String(job.id === state.selected));
  }

  function updateLibraryEmpty() {
    ui.libraryEmpty.hidden = state.jobs.size > 0;
    ui.filters.hidden = state.jobs.size === 0;
  }

  // Download page

  function selectJob(id) {
    const job = state.jobs.get(id);
    if (!job) {
      return;
    }
    state.selected = id;
    markSelected();
    renderJob(job, { rebuild: true });
    setView("job");
    ui.main.scrollTop = 0;
    if (narrow.matches) {
      window.scrollTo(0, 0);
    }
  }

  function renderHeader(job) {
    const stats = job.stats;
    const empty = !job.tracks.length;
    ui.jobView.dataset.empty = String(empty);

    const cover = job.covers[0] || null;
    const coverKey = `${job.id}|${cover}`;
    if (state.coverKey !== coverKey) {
      state.coverKey = coverKey;
      fillCover(ui.jobCover, cover, job.kind);
      ui.jobView.style.setProperty("--tint", DEFAULT_TINT);
      tintFor(cover).then((color) => {
        if (state.coverKey === coverKey) {
          ui.jobView.style.setProperty("--tint", color || DEFAULT_TINT);
        }
      });
    }

    const title = empty ? job.query : job.title || job.query;
    ui.jobTitle.textContent = title;
    ui.jobTitle.dataset.length = title.length > 40 ? "xlong" : title.length > 18 ? "long" : "";

    const meta = [empty ? (job.query.includes("://") ? `${kindOf(job)} link` : "Search") : kindOf(job)];
    if (job.subtitle) {
      const who = document.createElement("strong");
      who.textContent = job.subtitle;
      meta.push(who);
    }
    if (stats.total > 1) {
      meta.push(plural(stats.total, "song"));
    }
    meta.push(formatQuality(job));
    if (stats.size) {
      meta.push(formatSize(stats.size));
    }
    const parts = [];
    meta.forEach((part, index) => {
      if (index) {
        parts.push(" • ");
      }
      parts.push(part);
    });
    ui.jobMeta.replaceChildren(...parts);
  }

  function renderActions(job) {
    const stats = job.stats;
    const active = ACTIVE.has(job.status);
    const finished = job.tracks.find((track) => track.status === "done");

    ui.jobDownload.hidden = active || !finished;
    if (finished) {
      const single = stats.total === 1;
      ui.jobDownload.href = single
        ? `/api/jobs/${job.id}/tracks/${finished.id}/file`
        : `/api/jobs/${job.id}/archive`;
      const label = single ? `Download ${job.title}` : `Download ${plural(stats.done, "song")} as a ZIP`;
      ui.jobDownload.setAttribute("aria-label", label);
      ui.jobDownload.title = label;
    }

    ui.jobRing.hidden = !active;
    const waiting = job.status !== "running";
    ui.jobRing.classList.toggle("is-waiting", waiting);
    ui.jobRing.style.setProperty("--p", stats.progress);
    ui.jobRing.setAttribute("aria-valuenow", String(stats.progress));
    ui.jobRingLabel.textContent = waiting ? "" : `${stats.progress}%`;

    ui.jobRetry.hidden = active || !(stats.failed > 0 || (job.status === "failed" && !stats.total));
    ui.jobRetryLabel.textContent =
      stats.failed > 0 && stats.total > 1 ? `Retry ${stats.failed} failed` : "Try again";

    if (!ui.jobRemove.dataset.confirm) {
      const label = active ? "Cancel download" : "Remove download";
      ui.jobRemove.setAttribute("aria-label", label);
      ui.jobRemove.title = label;
    }

    const [status, tone] = statusOf(job);
    ui.jobStatus.textContent = status;
    ui.jobStatus.classList.toggle("is-error", tone === "red");
  }

  function createRow() {
    const root = clone(templates.track);
    return {
      root,
      number: root.querySelector(".track-number"),
      cover: root.querySelector(".track-cover"),
      title: root.querySelector(".track-title"),
      by: root.querySelector(".track-by"),
      album: root.querySelector(".track-album"),
      state: root.querySelector(".track-state"),
      bar: root.querySelector(".mini-bar"),
      download: root.querySelector(".track-download"),
    };
  }

  function renderRow(row, job, track) {
    row.root.dataset.status = track.status;
    row.number.textContent = String(track.id + 1);
    row.title.textContent = track.title;
    row.title.title = track.title;
    row.by.textContent = track.status === "error" ? track.error || "Couldn't download this song" : track.artists.join(", ");
    row.album.textContent = track.album || "";
    if (job.kind !== "album" && job.kind !== "track") {
      fillCover(row.cover, track.cover, "track");
    }

    if (track.status === "active") {
      row.state.textContent = STEPS[track.message] || "Starting";
    } else if (track.status === "error") {
      row.state.textContent = "Failed";
    } else {
      row.state.textContent = formatDuration(track.duration);
    }
    row.bar.style.setProperty("--p", track.progress);

    const done = track.status === "done";
    row.download.hidden = !done;
    if (done) {
      row.download.href = `/api/jobs/${job.id}/tracks/${track.id}/file`;
      row.download.setAttribute("aria-label", `Download ${track.title}`);
      row.download.title = `Download ${track.title}`;
    }
  }

  function renderTracks(job, rebuild) {
    const covers = job.kind !== "album" && job.kind !== "track";
    const album = job.kind === "playlist" || job.kind === "artist" || job.kind === "user";
    for (const element of [ui.tracks, ui.tracksHead]) {
      element.classList.toggle("with-covers", covers);
      element.classList.toggle("with-album", album);
    }
    ui.tracksHead.hidden = job.tracks.length === 0;

    if (rebuild || state.rowsJob !== job.id) {
      state.rows = [];
      state.rowsJob = job.id;
      ui.tracks.replaceChildren();
    }

    while (state.rows.length > job.tracks.length) {
      state.rows.pop().root.remove();
    }
    job.tracks.forEach((track, index) => {
      let row = state.rows[index];
      if (!row) {
        row = createRow();
        state.rows.push(row);
        ui.tracks.append(row.root);
      }
      renderRow(row, job, track);
    });
  }

  function renderJob(job, { rebuild = false } = {}) {
    renderHeader(job);
    renderActions(job);
    renderTracks(job, rebuild);
  }

  // Keeping the state in sync

  function upsertJob(job) {
    const previous = state.jobs.get(job.id);
    job.stats = computeStats(job.tracks);
    state.jobs.set(job.id, job);

    let item = state.items.get(job.id);
    if (!item) {
      item = createItem(job);
      state.items.set(job.id, item);
      insertItem(item, job);
    }
    renderItem(item, job);
    item.root.hidden = !matchesFilter(job);

    if (state.selected === job.id) {
      renderJob(job);
    }

    if (previous && ACTIVE.has(previous.status) && !ACTIVE.has(job.status)) {
      const title = job.title || job.query;
      if (job.status === "done") {
        toast(`${title} is ready to download`);
      } else if (job.status === "failed") {
        toast(`Couldn't download ${title}`, true);
      }
    }
    updateLibraryEmpty();
  }

  function updateTrack(jobId, track) {
    const job = state.jobs.get(jobId);
    if (!job) {
      return;
    }
    const index = job.tracks.findIndex((existing) => existing.id === track.id);
    if (index < 0) {
      return;
    }

    job.tracks[index] = track;
    job.stats = computeStats(job.tracks);
    renderItem(state.items.get(jobId), job);

    if (state.selected === jobId) {
      renderHeader(job);
      renderActions(job);
      const row = state.rowsJob === jobId ? state.rows[index] : null;
      if (row) {
        renderRow(row, job, track);
      }
    }
  }

  function removeJob(id) {
    const item = state.items.get(id);
    if (item) {
      item.root.remove();
    }
    state.items.delete(id);
    state.jobs.delete(id);

    if (state.selected === id) {
      const next = ui.jobs.querySelector(".library-item:not([hidden])");
      if (next && !narrow.matches) {
        selectJob(next.dataset.id);
      } else {
        goHome();
      }
    }
    updateLibraryEmpty();
  }

  function replaceJobs(jobs) {
    const ids = new Set(jobs.map((job) => job.id));
    for (const id of [...state.jobs.keys()]) {
      if (!ids.has(id)) {
        removeJob(id);
      }
    }
    for (const job of jobs) {
      upsertJob(job);
    }

    // Open the newest download on first load, like resuming where you left off
    if (!state.booted) {
      state.booted = true;
      if (jobs.length && !narrow.matches && document.body.dataset.view === "home") {
        selectJob(jobs[0].id);
      }
    }
    updateLibraryEmpty();
  }

  // Actions

  async function createJob(query) {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ query, format: ui.format.value, bitrate: ui.bitrate.value }),
    });
    upsertJob(job);
    return job;
  }

  async function retryJob() {
    const id = state.selected;
    setBusy(ui.jobRetry, true);
    try {
      upsertJob(await api(`/api/jobs/${id}/retry`, { method: "POST" }));
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(ui.jobRetry, false);
    }
  }

  function resetRemove() {
    clearTimeout(ui.jobRemove.confirmTimer);
    delete ui.jobRemove.dataset.confirm;
  }

  async function removeClicked() {
    const id = state.selected;
    const job = state.jobs.get(id);
    if (!job) {
      return;
    }

    // Removing deletes the files, so it takes a second click
    if (!ui.jobRemove.dataset.confirm) {
      const active = ACTIVE.has(job.status);
      ui.jobRemove.dataset.confirm = "1";
      ui.jobRemoveLabel.textContent = active ? "Cancel?" : "Remove?";
      ui.jobRemove.setAttribute("aria-label", active ? "Confirm cancel" : "Confirm remove");
      ui.jobRemove.confirmTimer = setTimeout(() => {
        resetRemove();
        const current = state.jobs.get(state.selected);
        if (current) {
          renderActions(current);
        }
      }, 3500);
      return;
    }

    resetRemove();
    setBusy(ui.jobRemove, true);
    try {
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      removeJob(id);
      toast(`Removed ${job.title || job.query}`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(ui.jobRemove, false);
    }
  }

  // Search: a link starts a download, anything else searches Spotify

  function isLink(text) {
    return LINK.test(text.trim());
  }

  function syncEntry() {
    const text = ui.query.value.trim();
    ui.clear.hidden = !text;
    ui.submit.hidden = !text || !isLink(text);
  }

  function leaveSearch() {
    state.searchToken += 1;
    clearTimeout(state.searchTimer);
    ui.results.replaceChildren();
    if (document.body.dataset.view === "search") {
      if (state.selected && state.jobs.has(state.selected)) {
        selectJob(state.selected);
      } else {
        goHome();
      }
    }
  }

  function showSkeleton() {
    const rows = [0, 1, 2, 3].map(() => {
      const row = document.createElement("li");
      row.className = "result skeleton";
      row.setAttribute("aria-hidden", "true");
      const cover = document.createElement("span");
      cover.className = "track-cover";
      const text = document.createElement("span");
      text.className = "track-main";
      for (const size of ["long", "short"]) {
        const bar = document.createElement("span");
        bar.className = `bar ${size}`;
        text.append(bar);
      }
      row.append(cover, text);
      return row;
    });
    ui.results.replaceChildren(...rows);
  }

  function renderResults(results) {
    const rows = results.map((result) => {
      const row = clone(templates.result);
      const cover = row.querySelector(".result-cover");
      if (result.cover) {
        cover.append(coverImage(result.cover));
      }

      const title = row.querySelector(".result-title");
      title.textContent = result.title;
      title.title = result.title;

      const by = row.querySelector(".result-by");
      if (result.explicit) {
        const badge = document.createElement("abbr");
        badge.className = "explicit";
        badge.title = "Explicit";
        badge.textContent = "E";
        by.append(badge);
      }
      by.append(result.artists.join(", "));
      row.querySelector(".result-album").textContent = result.album || "";
      row.querySelector(".result-duration").textContent = formatDuration(result.duration);

      const button = row.querySelector(".result-download");
      button.setAttribute("aria-label", `Download ${result.title}`);
      button.title = "Download";
      button.addEventListener("click", async () => {
        if (row.dataset.added) {
          return;
        }
        setBusy(button, true);
        try {
          await createJob(result.url);
          row.dataset.added = "1";
          button.querySelector("use").setAttribute("href", "#i-check");
          button.setAttribute("aria-label", `${result.title} added to Your Downloads`);
          button.title = "Added to Your Downloads";
          toast("Added to Your Downloads");
        } catch (error) {
          toast(error.message, true);
        } finally {
          setBusy(button, false);
        }
      });
      return row;
    });
    ui.results.replaceChildren(...rows);
  }

  async function search(text) {
    clearTimeout(state.searchTimer);
    const token = ++state.searchToken;
    setView("search");
    showSkeleton();
    ui.searchNote.className = "search-note";
    ui.searchNote.textContent = "";

    try {
      const { results } = await api(`/api/search?q=${encodeURIComponent(text)}`);
      if (token !== state.searchToken) {
        return;
      }
      renderResults(results);
      if (!results.length) {
        const heading = document.createElement("span");
        heading.className = "empty-title";
        heading.textContent = `No results found for “${text}”`;
        ui.searchNote.className = "search-note is-empty";
        ui.searchNote.replaceChildren(
          heading,
          "Check the spelling, try fewer words, or paste a Spotify link instead.",
        );
      }
    } catch (error) {
      if (token === state.searchToken) {
        ui.results.replaceChildren();
        ui.searchNote.className = "search-note";
        ui.searchNote.textContent = error.message;
      }
    }
  }

  function onInput() {
    syncEntry();
    clearTimeout(state.searchTimer);
    const text = ui.query.value.trim();
    if (!text) {
      leaveSearch();
      return;
    }
    if (!isLink(text) && text.length >= 2) {
      state.searchTimer = setTimeout(() => search(text), SEARCH_DELAY);
    }
  }

  async function submitEntry(event) {
    event.preventDefault();
    const text = ui.query.value.trim();
    if (!text) {
      ui.query.focus();
      return;
    }
    if (!isLink(text)) {
      search(text);
      return;
    }

    setBusy(ui.submit, true);
    try {
      const job = await createJob(text);
      ui.query.value = "";
      syncEntry();
      ui.results.replaceChildren();
      selectJob(job.id);
      toast("Added to Your Downloads");
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(ui.submit, false);
    }
  }

  // Live updates

  function setConnected(connected) {
    ui.connection.hidden = connected;
  }

  function disconnect() {
    clearTimeout(state.retryTimer);
    if (state.source) {
      state.source.close();
      state.source = null;
    }
  }

  function connect() {
    disconnect();
    const source = new EventSource("/api/events");
    state.source = source;

    source.addEventListener("snapshot", (event) => {
      setConnected(true);
      replaceJobs(JSON.parse(event.data).jobs);
    });
    source.addEventListener("job", (event) => upsertJob(JSON.parse(event.data)));
    source.addEventListener("track", (event) => {
      const data = JSON.parse(event.data);
      updateTrack(data.job_id, data.track);
    });
    source.addEventListener("removed", (event) => removeJob(JSON.parse(event.data).id));
    source.addEventListener("open", () => setConnected(true));
    source.addEventListener("error", () => {
      setConnected(false);
      if (source.readyState === EventSource.CLOSED) {
        // The server refused the stream, maybe the session ended
        state.retryTimer = setTimeout(recheck, 3000);
      }
    });
  }

  async function recheck() {
    try {
      state.session = await api("/api/session");
    } catch {
      state.retryTimer = setTimeout(recheck, 5000);
      return;
    }

    if (state.session.auth_required && !state.session.authenticated) {
      lock();
    } else {
      connect();
    }
  }

  // Password

  function lock() {
    disconnect();
    setConnected(true);
    for (const id of [...state.jobs.keys()]) {
      removeJob(id);
    }
    state.booted = false;
    ui.app.hidden = true;
    ui.gate.hidden = false;
    ui.password.focus();
  }

  function unlock() {
    ui.gate.hidden = true;
    ui.app.hidden = false;
    ui.lock.hidden = !state.session.auth_required;
    connect();
    if (window.matchMedia("(pointer: fine)").matches) {
      ui.query.focus();
    }
  }

  async function submitPassword(event) {
    event.preventDefault();
    const button = ui.gateForm.querySelector("button");
    setBusy(button, true);
    try {
      await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ password: ui.password.value }),
      });
      state.session.authenticated = true;
      ui.password.value = "";
      ui.gateNote.textContent = "Ask whoever runs this server for the password.";
      ui.gateNote.classList.remove("is-error");
      unlock();
    } catch (error) {
      ui.gateNote.textContent = error.status === 401 ? "Incorrect password." : error.message;
      ui.gateNote.classList.add("is-error");
      ui.password.select();
    } finally {
      setBusy(button, false);
    }
  }

  // Start

  function fillSelect(select, values, selected, label) {
    select.replaceChildren(...values.map((value) => new Option(label(value), value)));
    select.value = values.includes(selected) ? selected : values[0];
  }

  function syncQuality() {
    const lossless = LOSSLESS.has(ui.format.value);
    ui.bitrate.disabled = lossless;
    ui.bitrate.title = lossless ? "Lossless formats keep the source quality" : "Quality";
  }

  async function boot() {
    try {
      state.session = await api("/api/session");
    } catch (error) {
      ui.gate.hidden = true;
      ui.app.hidden = false;
      toast(error.message, true);
      setTimeout(boot, 5000);
      return;
    }

    const session = state.session;
    fillSelect(ui.format, session.formats, prefs.get("format") || session.defaults.format, (value) =>
      value.toUpperCase(),
    );
    fillSelect(ui.bitrate, session.bitrates, prefs.get("bitrate") || session.defaults.bitrate, (value) =>
      value === "auto" ? "Auto quality" : value.replace("k", " kbps"),
    );
    syncQuality();

    const kept = session.retention_hours > 0 ? formatHours(session.retention_hours) : "";
    ui.colophon.textContent = kept
      ? `spotDL ${session.version} · downloads are kept for ${kept}`
      : `spotDL ${session.version}`;

    if (session.auth_required && !session.authenticated) {
      lock();
    } else {
      unlock();
    }
  }

  ui.form.addEventListener("submit", submitEntry);
  ui.query.addEventListener("input", onInput);
  ui.query.addEventListener("paste", (event) => {
    const pasted = (event.clipboardData && event.clipboardData.getData("text")) || "";
    // Paste a link into the empty field and it starts right away
    if (!ui.query.value.trim() && isLink(pasted)) {
      setTimeout(() => ui.form.requestSubmit(), 0);
    }
  });
  ui.query.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      ui.query.value = "";
      syncEntry();
      leaveSearch();
    }
  });
  ui.clear.addEventListener("click", () => {
    ui.query.value = "";
    syncEntry();
    leaveSearch();
    ui.query.focus();
  });
  ui.home.addEventListener("click", () => {
    ui.query.value = "";
    syncEntry();
    state.searchToken += 1;
    goHome();
  });
  ui.searchBack.addEventListener("click", () => {
    ui.query.value = "";
    syncEntry();
    leaveSearch();
  });
  ui.jobBack.addEventListener("click", goHome);
  ui.start.addEventListener("click", () => {
    window.scrollTo(0, 0);
    ui.query.focus();
  });
  ui.filters.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) {
      return;
    }
    state.filter = state.filter === chip.dataset.filter ? null : chip.dataset.filter;
    for (const other of ui.filters.querySelectorAll(".chip")) {
      other.setAttribute("aria-pressed", String(other.dataset.filter === state.filter));
    }
    applyFilter();
  });
  ui.jobRetry.addEventListener("click", retryJob);
  ui.jobRemove.addEventListener("click", removeClicked);
  ui.format.addEventListener("change", () => {
    prefs.set("format", ui.format.value);
    syncQuality();
  });
  ui.bitrate.addEventListener("change", () => prefs.set("bitrate", ui.bitrate.value));
  ui.gateForm.addEventListener("submit", submitPassword);
  ui.lock.addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST" });
    } finally {
      state.session.authenticated = false;
      lock();
    }
  });

  boot();
})();
