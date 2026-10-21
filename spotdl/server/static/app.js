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
  const SEARCH_DELAY = 400;

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
    format: byId("format"),
    bitrate: byId("bitrate"),
    panel: byId("results-panel"),
    searchNote: byId("search-note"),
    results: byId("results"),
    filters: byId("filters"),
    jobs: byId("jobs"),
    empty: byId("empty"),
    filterEmpty: byId("filter-empty"),
    connection: byId("connection"),
    lock: byId("lock"),
    colophon: byId("colophon"),
    toast: byId("toast"),
  };
  const templates = {
    job: byId("job-template"),
    track: byId("track-template"),
    result: byId("result-template"),
  };

  const state = {
    session: null,
    jobs: new Map(),
    cards: new Map(),
    expanded: new Set(),
    filter: null,
    results: [],
    active: -1,
    searchToken: 0,
    searchTimer: null,
    colors: new Map(),
    toastTimer: null,
    source: null,
    retryTimer: null,
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

  const pad = (number) => String(number).padStart(2, "0");
  const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

  function formatDuration(seconds) {
    return seconds ? `${Math.floor(seconds / 60)}:${pad(seconds % 60)}` : "";
  }

  function formatHours(hours) {
    if (hours >= 48 && hours % 24 === 0) {
      return `${hours / 24} days`;
    }
    return plural(hours, "hour");
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
    for (const track of tracks) {
      done += track.status === "done" ? 1 : 0;
      failed += track.status === "error" ? 1 : 0;
      progress += track.progress;
    }
    return { total, done, failed, progress: total ? Math.round(progress / total) : 0 };
  }

  function kindOf(job) {
    return KINDS[job.kind] || KINDS.track;
  }

  function subtitleOf(job) {
    if (!job.tracks.length) {
      if (job.status === "resolving") {
        return "Looking it up on Spotify";
      }
      return `${job.query.includes("://") ? `${kindOf(job)} link` : "Search"} • ${formatQuality(job)}`;
    }
    const parts = [kindOf(job)];
    if (job.subtitle) {
      parts.push(job.subtitle);
    }
    if (job.stats.total > 1) {
      parts.push(plural(job.stats.total, "song"));
    }
    parts.push(formatQuality(job));
    return parts.join(" • ");
  }

  // The short status shown beside a download's buttons
  function stateOf(job) {
    const stats = job.stats;
    switch (job.status) {
      case "resolving":
        return "Looking up…";
      case "queued":
        return "Waiting…";
      case "running":
        if (stats.total === 1) {
          return `${STEPS[job.tracks[0].message] || "Downloading"}…`;
        }
        return `${stats.done} of ${stats.total}`;
      case "done":
        return stats.failed ? `${stats.failed} failed` : "";
      default:
        return "";
    }
  }

  // Helpers

  function clone(template) {
    return template.content.firstElementChild.cloneNode(true);
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", `#${name}`);
    svg.append(use);
    return svg;
  }

  function coverImage(url) {
    const image = new Image();
    image.alt = "";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => image.replaceWith(icon("i-note")), { once: true });
    image.src = url;
    return image;
  }

  function fillCover(element, url) {
    const key = url || "";
    if (element.dataset.key === key && element.firstChild) {
      return;
    }
    element.dataset.key = key;
    element.replaceChildren(url ? coverImage(url) : icon("i-note"));
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

  // Rows wash in their cover's color, sampled once per cover
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

  // Keep the cover's hue, calm enough to sit behind white text
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
    const l = Math.min(0.4, Math.max(0.24, light * 0.75));
    return `hsl(${Math.round(hue)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;
  }

  // Downloads

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

  function updateEmpty() {
    const empty = state.jobs.size === 0;
    document.body.dataset.empty = String(empty);
    ui.empty.hidden = !empty;
    ui.filters.hidden = empty;

    let visible = 0;
    for (const [id, card] of state.cards) {
      const show = matchesFilter(state.jobs.get(id));
      card.root.hidden = !show;
      visible += show ? 1 : 0;
    }
    ui.filterEmpty.hidden = empty || visible > 0;
  }

  function createCard(job) {
    const root = clone(templates.job);
    const find = (selector) => root.querySelector(selector);
    const card = {
      root,
      cover: find(".job-cover"),
      title: find(".job-title"),
      sub: find(".job-sub"),
      progress: find(".progress-fill"),
      percent: find(".job-percent"),
      error: find(".job-error"),
      state: find(".job-state"),
      save: find(".job-save"),
      saveLabel: find(".job-save span"),
      retry: find(".job-retry"),
      retryLabel: find(".job-retry span"),
      toggle: find(".job-toggle"),
      remove: find(".job-remove"),
      removeLabel: find(".job-remove .confirm-label"),
      tracks: find(".tracks"),
      rows: [],
      tintKey: null,
    };

    root.dataset.id = job.id;
    card.tracks.id = `tracks-${job.id}`;
    card.toggle.setAttribute("aria-controls", card.tracks.id);
    card.toggle.addEventListener("click", () => toggleTracks(job.id));
    card.retry.addEventListener("click", () => retryJob(job.id));
    card.remove.addEventListener("click", () => removeClicked(job.id));
    return card;
  }

  function insertCard(card, job) {
    for (const other of ui.jobs.children) {
      const otherJob = state.jobs.get(other.dataset.id);
      if (otherJob && otherJob.created_at < job.created_at) {
        ui.jobs.insertBefore(card.root, other);
        return;
      }
    }
    ui.jobs.append(card.root);
  }

  function renderCard(card, job) {
    const stats = job.stats;
    const active = ACTIVE.has(job.status);
    const empty = !job.tracks.length;

    card.root.dataset.status = job.status;
    card.root.dataset.empty = String(empty);

    const cover = job.covers[0] || null;
    fillCover(card.cover, cover);
    card.cover.classList.toggle("round-art", job.kind === "artist");
    if (card.tintKey !== cover) {
      card.tintKey = cover;
      tintFor(cover).then((color) => {
        if (card.tintKey === cover && color) {
          card.root.style.setProperty("--tint", color);
        }
      });
    }

    const title = empty ? job.query : job.title || job.query;
    card.title.textContent = title;
    card.title.title = title;
    card.sub.textContent = subtitleOf(job);

    card.progress.style.setProperty("--p", stats.progress);
    card.percent.textContent = `${stats.progress}%`;
    card.error.textContent = job.status === "failed" ? job.error || "Couldn't download this" : "";

    const label = stateOf(job);
    card.state.replaceChildren();
    if (job.status === "resolving" || job.status === "queued") {
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      card.state.append(spinner);
    }
    if (label) {
      card.state.append(label);
    }

    const finished = job.tracks.find((track) => track.status === "done");
    card.save.hidden = active || !finished;
    if (finished) {
      const single = stats.total === 1;
      card.save.href = single
        ? `/api/jobs/${job.id}/tracks/${finished.id}/file`
        : `/api/jobs/${job.id}/archive`;
      card.saveLabel.textContent = single ? "Save" : "Save ZIP";
      card.save.title = single ? `Save ${title}` : `Save ${plural(stats.done, "song")} as a ZIP`;
    }

    card.retry.hidden = active || !(stats.failed > 0 || (job.status === "failed" && !stats.total));
    card.retryLabel.textContent = stats.failed > 0 && stats.total > 1 ? "Retry failed" : "Try again";

    card.toggle.hidden = stats.total < 2;
    const open = state.expanded.has(job.id);
    card.toggle.setAttribute("aria-expanded", String(open));
    card.toggle.setAttribute("aria-label", open ? "Hide songs" : "Show songs");
    card.toggle.title = open ? "Hide songs" : "Show songs";

    if (!card.remove.dataset.confirm) {
      const removeLabel = active ? "Cancel download" : "Remove";
      card.remove.setAttribute("aria-label", removeLabel);
      card.remove.title = removeLabel;
    }
  }

  function createRow() {
    const root = clone(templates.track);
    return {
      root,
      number: root.querySelector(".track-number"),
      title: root.querySelector(".track-title"),
      by: root.querySelector(".track-by"),
      state: root.querySelector(".track-state"),
      save: root.querySelector(".track-save"),
    };
  }

  function renderRow(row, job, track) {
    row.root.dataset.status = track.status;
    row.number.textContent = String(track.id + 1);
    row.title.textContent = track.title;
    row.title.title = track.title;
    row.by.textContent =
      track.status === "error" ? track.error || "Couldn't download this song" : track.artists.join(", ");

    if (track.status === "active") {
      row.state.textContent = `${STEPS[track.message] || "Starting"} ${track.progress}%`;
    } else if (track.status === "error") {
      row.state.textContent = "Failed";
    } else {
      row.state.textContent = formatDuration(track.duration);
    }

    const done = track.status === "done";
    row.save.hidden = !done;
    if (done) {
      row.save.href = `/api/jobs/${job.id}/tracks/${track.id}/file`;
      row.save.setAttribute("aria-label", `Save ${track.title}`);
      row.save.title = "Save";
    }
  }

  function renderTracks(card, job) {
    while (card.rows.length > job.tracks.length) {
      card.rows.pop().root.remove();
    }
    job.tracks.forEach((track, index) => {
      let row = card.rows[index];
      if (!row) {
        row = createRow();
        card.rows.push(row);
        card.tracks.append(row.root);
      }
      renderRow(row, job, track);
    });
  }

  function toggleTracks(id) {
    const card = state.cards.get(id);
    const job = state.jobs.get(id);
    if (!card || !job) {
      return;
    }
    if (state.expanded.has(id)) {
      state.expanded.delete(id);
    } else {
      state.expanded.add(id);
      renderTracks(card, job);
    }
    card.tracks.hidden = !state.expanded.has(id);
    renderCard(card, job);
  }

  function upsertJob(job, { arriving = false } = {}) {
    const previous = state.jobs.get(job.id);
    job.stats = computeStats(job.tracks);
    state.jobs.set(job.id, job);

    let card = state.cards.get(job.id);
    if (!card) {
      card = createCard(job);
      state.cards.set(job.id, card);
      insertCard(card, job);
      if (arriving) {
        card.root.classList.add("is-new");
        card.root.addEventListener("animationend", () => card.root.classList.remove("is-new"), {
          once: true,
        });
      }
    }
    renderCard(card, job);
    if (state.expanded.has(job.id)) {
      renderTracks(card, job);
    }

    if (previous && ACTIVE.has(previous.status) && !ACTIVE.has(job.status)) {
      const title = job.title || job.query;
      if (job.status === "done") {
        toast(`${title} is ready to save`);
      } else if (job.status === "failed") {
        toast(`Couldn't download ${title}`, true);
      }
    }
    updateEmpty();
  }

  function updateTrack(jobId, track) {
    const job = state.jobs.get(jobId);
    const card = state.cards.get(jobId);
    if (!job || !card) {
      return;
    }
    const index = job.tracks.findIndex((existing) => existing.id === track.id);
    if (index < 0) {
      return;
    }
    job.tracks[index] = track;
    job.stats = computeStats(job.tracks);
    renderCard(card, job);
    if (state.expanded.has(jobId) && card.rows[index]) {
      renderRow(card.rows[index], job, track);
    }
  }

  function removeJob(id) {
    const card = state.cards.get(id);
    if (card) {
      card.root.remove();
    }
    state.cards.delete(id);
    state.jobs.delete(id);
    state.expanded.delete(id);
    updateEmpty();
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
    updateEmpty();
  }

  // Download actions

  async function createJob(query) {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ query, format: ui.format.value, bitrate: ui.bitrate.value }),
    });
    upsertJob(job, { arriving: true });
    return job;
  }

  async function retryJob(id) {
    const card = state.cards.get(id);
    setBusy(card.retry, true);
    try {
      upsertJob(await api(`/api/jobs/${id}/retry`, { method: "POST" }));
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(card.retry, false);
    }
  }

  function resetRemove(card) {
    clearTimeout(card.remove.confirmTimer);
    delete card.remove.dataset.confirm;
  }

  async function removeClicked(id) {
    const card = state.cards.get(id);
    const job = state.jobs.get(id);
    if (!card || !job) {
      return;
    }

    // Removing deletes the files, so it takes a second click
    if (!card.remove.dataset.confirm) {
      const active = ACTIVE.has(job.status);
      card.remove.dataset.confirm = "1";
      card.removeLabel.textContent = active ? "Cancel?" : "Remove?";
      card.remove.setAttribute("aria-label", active ? "Confirm cancel" : "Confirm remove");
      card.remove.confirmTimer = setTimeout(() => {
        resetRemove(card);
        const current = state.jobs.get(id);
        if (current) {
          renderCard(card, current);
        }
      }, 3500);
      return;
    }

    resetRemove(card);
    setBusy(card.remove, true);
    try {
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      removeJob(id);
      toast(`Removed ${job.title || job.query}`);
    } catch (error) {
      setBusy(card.remove, false);
      toast(error.message, true);
    }
  }

  // Command bar: a link starts a download, anything else searches Spotify

  function isLink(text) {
    return LINK.test(text.trim());
  }

  function syncEntry() {
    const text = ui.query.value.trim();
    ui.clear.hidden = !text;
    ui.submit.hidden = !text || !isLink(text);
  }

  function closeResults() {
    state.searchToken += 1;
    clearTimeout(state.searchTimer);
    state.results = [];
    state.active = -1;
    ui.results.replaceChildren();
    ui.panel.hidden = true;
    ui.query.setAttribute("aria-expanded", "false");
    ui.query.removeAttribute("aria-activedescendant");
  }

  function openPanel() {
    ui.panel.hidden = false;
    ui.query.setAttribute("aria-expanded", "true");
  }

  function setActive(index) {
    const rows = ui.results.children;
    state.active = index;
    for (let position = 0; position < rows.length; position += 1) {
      rows[position].setAttribute("aria-selected", String(position === index));
    }
    if (index >= 0 && rows[index]) {
      ui.query.setAttribute("aria-activedescendant", rows[index].id);
      rows[index].scrollIntoView({ block: "nearest" });
    } else {
      ui.query.removeAttribute("aria-activedescendant");
    }
  }

  function showSkeleton() {
    const rows = [0, 1, 2].map(() => {
      const row = document.createElement("li");
      row.className = "result skeleton";
      row.setAttribute("aria-hidden", "true");
      const cover = document.createElement("span");
      cover.className = "result-cover";
      const text = document.createElement("span");
      text.className = "result-main";
      for (const size of ["long", "short"]) {
        const line = document.createElement("span");
        line.className = `bar-line ${size}`;
        text.append(line);
      }
      row.append(cover, text);
      return row;
    });
    ui.results.replaceChildren(...rows);
    ui.searchNote.replaceChildren();
    openPanel();
  }

  async function downloadResult(index) {
    const result = state.results[index];
    const row = ui.results.children[index];
    if (!result || !row || row.dataset.added) {
      return;
    }
    const button = row.querySelector(".result-download");
    setBusy(button, true);
    try {
      await createJob(result.url);
      row.dataset.added = "1";
      button.querySelector("use").setAttribute("href", "#i-check");
      button.setAttribute("aria-label", `${result.title} added to Downloads`);
      toast(`Downloading ${result.title}`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(button, false);
    }
  }

  function renderResults(results) {
    state.results = results;
    const rows = results.map((result, index) => {
      const row = clone(templates.result);
      row.id = `result-${index}`;

      const cover = row.querySelector(".result-cover");
      cover.append(result.cover ? coverImage(result.cover) : icon("i-note"));

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
      by.append([result.artists.join(", "), result.album].filter(Boolean).join(" • "));
      row.querySelector(".result-duration").textContent = formatDuration(result.duration);

      const button = row.querySelector(".result-download");
      button.setAttribute("aria-label", `Download ${result.title}`);
      button.title = "Download";

      row.addEventListener("mousemove", () => {
        if (state.active !== index) {
          setActive(index);
        }
      });
      row.addEventListener("click", () => downloadResult(index));
      return row;
    });
    ui.results.replaceChildren(...rows);
    setActive(rows.length ? 0 : -1);
  }

  async function search(text) {
    clearTimeout(state.searchTimer);
    const token = ++state.searchToken;
    showSkeleton();

    try {
      const { results } = await api(`/api/search?q=${encodeURIComponent(text)}`);
      if (token !== state.searchToken) {
        return;
      }
      renderResults(results);
      if (!results.length) {
        const heading = document.createElement("strong");
        heading.textContent = `No songs found for “${text}”`;
        ui.searchNote.replaceChildren(heading, "Check the spelling, or paste a Spotify link instead.");
      }
    } catch (error) {
      if (token === state.searchToken) {
        ui.results.replaceChildren();
        ui.searchNote.textContent = error.message;
      }
    }
  }

  function onInput() {
    syncEntry();
    clearTimeout(state.searchTimer);
    const text = ui.query.value.trim();
    if (!text || isLink(text)) {
      closeResults();
      return;
    }
    if (text.length >= 2) {
      state.searchTimer = setTimeout(() => search(text), SEARCH_DELAY);
    }
  }

  function onKeyDown(event) {
    const count = state.results.length;
    if (event.key === "ArrowDown" && count) {
      event.preventDefault();
      setActive((state.active + 1) % count);
    } else if (event.key === "ArrowUp" && count) {
      event.preventDefault();
      setActive((state.active - 1 + count) % count);
    } else if (event.key === "Escape") {
      if (!ui.panel.hidden) {
        closeResults();
      } else {
        ui.query.value = "";
        syncEntry();
      }
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
      // Enter on a highlighted result downloads it, otherwise search right away
      if (!ui.panel.hidden && state.active >= 0 && state.results.length) {
        downloadResult(state.active);
      } else {
        search(text);
      }
      return;
    }

    setBusy(ui.submit, true);
    try {
      await createJob(text);
      ui.query.value = "";
      syncEntry();
      closeResults();
      toast("Added to Downloads");
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
    closeResults();
    ui.app.hidden = true;
    ui.gate.hidden = false;
    ui.password.focus();
  }

  function unlock() {
    ui.gate.hidden = true;
    ui.app.hidden = false;
    ui.lock.hidden = !state.session.auth_required;
    updateEmpty();
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
      ? `spotDL ${session.version} · finished downloads are kept for ${kept}`
      : `spotDL ${session.version}`;

    if (session.auth_required && !session.authenticated) {
      lock();
    } else {
      unlock();
    }
  }

  ui.form.addEventListener("submit", submitEntry);
  ui.query.addEventListener("input", onInput);
  ui.query.addEventListener("keydown", onKeyDown);
  ui.query.addEventListener("paste", (event) => {
    const pasted = (event.clipboardData && event.clipboardData.getData("text")) || "";
    // Paste a link into the empty bar and it starts right away
    if (!ui.query.value.trim() && isLink(pasted)) {
      setTimeout(() => ui.form.requestSubmit(), 0);
    }
  });
  ui.clear.addEventListener("click", () => {
    ui.query.value = "";
    syncEntry();
    closeResults();
    ui.query.focus();
  });
  document.addEventListener("click", (event) => {
    if (!ui.panel.hidden && !event.target.closest(".command")) {
      closeResults();
    }
  });
  ui.empty.addEventListener("click", (event) => {
    const example = event.target.closest("[data-example]");
    if (example) {
      ui.query.value = example.dataset.example;
      syncEntry();
      ui.query.focus();
      search(example.dataset.example);
    }
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
    updateEmpty();
  });
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
