// musicbrainz.js — MusicBrainz search & discovery tab. Search the MusicBrainz
// database for recordings/albums/artists, drill into an artist's discography
// or an album's tracklist, and queue a recording or album for download via
// Soulseek (it then shows up in the Transfers tab).

import * as api from "./api.js";
import { esc, renderEmpty, showToast } from "./components.js";

const LIMIT = 20;

// Cover Art Archive keys cover art by release-group MBID (and redirects to
// archive.org for the actual image, which an <img> follows transparently).
// `front-250` is the in-row thumb; `front-500` the lightbox view.
const COVER_ART_ARCHIVE = "https://coverartarchive.org";

// Module-local view state, so renderMusicBrainz() (called from state.js's
// global render()) can idempotently re-render without losing the tab's
// current results or drill-down view.
let lastResults = null;
let detail = null;

function coverUrl(mbid, size) {
  if (!mbid) return null;
  return `${COVER_ART_ARCHIVE}/release-group/${encodeURIComponent(mbid)}/front-${size}`;
}

// The backend endpoints are being built in parallel; accept both a bare
// array body and a wrapped {results}/{items} shape defensively.
function normalizeList(body) {
  if (Array.isArray(body)) return body;
  if (body && Array.isArray(body.results)) return body.results;
  if (body && Array.isArray(body.items)) return body.items;
  return [];
}

function setStatus(text, isError = false) {
  const el = document.getElementById("mb-search-status");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("error", isError);
}

function hideDetail() {
  detail = null;
  const panel = document.getElementById("mb-detail-panel");
  const container = document.getElementById("mb-detail");
  if (panel) panel.hidden = true;
  if (container) container.innerHTML = "";
}

// -----------------------------------------------------------------------
// Actions
// -----------------------------------------------------------------------

async function doSearch(query, sort) {
  if (!query) {
    setStatus("Enter a search query.", true);
    return;
  }
  lastResults = null;
  detail = null;
  setStatus("Searching artists, albums, and recordings…");
  const container = document.getElementById("mb-results");
  if (container) container.innerHTML = renderEmpty("Searching…");
  const detailPanel = document.getElementById("mb-detail-panel");
  if (detailPanel) detailPanel.hidden = true;

  try {
    lastResults = await api.searchMusicBrainz(query, LIMIT, sort);
    setStatus("");
  } catch (err) {
    lastResults = { artist: null, albums: [], recordings: [] };
    setStatus(`${err.code || "ERROR"}: ${err.message}`, true);
  }
  renderResultsList();
}

async function showArtistDiscography(artist) {
  detail = { kind: "artist", artist, albums: null, error: null };
  const panel = document.getElementById("mb-detail-panel");
  if (panel) panel.hidden = false;
  const container = document.getElementById("mb-detail");
  if (container) container.innerHTML = renderEmpty("Loading discography…");
  setStatus(`Loading albums for ${artist.name}…`);
  try {
    detail.albums = normalizeList(await api.artistAlbums(artist.mbid, 100));
    setStatus("");
  } catch (err) {
    detail.albums = [];
    detail.error = `${err.code || "ERROR"}: ${err.message}`;
    setStatus("");
  }
  renderDetail();
}

async function showAlbumTracks(album) {
  detail = { kind: "album", album, tracks: null, error: null };
  const panel = document.getElementById("mb-detail-panel");
  if (panel) panel.hidden = false;
  const container = document.getElementById("mb-detail");
  if (container) container.innerHTML = renderEmpty("Loading tracks…");
  try {
    detail.tracks = normalizeList(await api.albumTracks(album.mbid));
  } catch (err) {
    detail.tracks = [];
    detail.error = `${err.code || "ERROR"}: ${err.message}`;
  }
  renderDetail();
}

async function handleDownloadRecording(mbid, btn) {
  if (btn) btn.disabled = true;
  try {
    await api.downloadRecording(mbid);
    showToast("Queued — resolving via Soulseek (will appear in Transfers)");
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleDownloadAlbum(mbid, btn) {
  if (btn) btn.disabled = true;
  try {
    await api.downloadAlbum(mbid);
    showToast("Queued — resolving via Soulseek (will appear in Transfers)");
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// -----------------------------------------------------------------------
// Cover art
// -----------------------------------------------------------------------

// A leading table cell with a small cover thumbnail (or an empty cell when
// there's no cover). A broken/missing cover removes itself on `error`, so a
// release-group with no Cover Art Archive image just renders as an empty cell.
function coverCell(mbid) {
  const td = document.createElement("td");
  td.className = "cover-cell";
  if (!mbid) return td;
  const img = document.createElement("img");
  img.className = "cover-thumb";
  img.src = coverUrl(mbid, 250);
  img.alt = "";
  img.loading = "lazy";
  img.addEventListener("error", () => img.remove());
  img.addEventListener("click", () => openCover(mbid));
  td.appendChild(img);
  return td;
}

// Full-screen lightbox: click the thumbnail to see the cover larger; click
// anywhere (or the ×) to dismiss. Removed from the DOM on error too, so a
// release-group whose 250px thumb resolved but whose 500px view 404s doesn't
// leave an empty overlay behind.
function openCover(mbid) {
  const overlay = document.createElement("div");
  overlay.className = "cover-overlay";
  const img = document.createElement("img");
  img.src = coverUrl(mbid, 500);
  img.alt = "";
  img.addEventListener("error", () => overlay.remove());
  const close = document.createElement("button");
  close.type = "button";
  close.className = "cover-overlay-close";
  close.textContent = "×";
  close.setAttribute("aria-label", "Close");
  overlay.append(img, close);
  overlay.addEventListener("click", () => overlay.remove());
  document.body.appendChild(overlay);
}

// -----------------------------------------------------------------------
// Render — recordings
// -----------------------------------------------------------------------

function renderRecordings(container, recordings) {
  container.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const table = document.createElement("table");
  table.innerHTML = `
    <thead>
      <tr><th class="cover-col"></th><th>Title</th><th>Artist</th><th data-optional>Album</th><th data-optional>Year</th><th></th></tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  for (const r of recordings) {
    const tr = document.createElement("tr");
    tr.appendChild(coverCell(r.cover_mbid));
    tr.insertAdjacentHTML(
      "beforeend",
      `
      <td>${esc(r.title)}</td>
      <td>${esc(r.artist || "—")}</td>
      <td data-optional>${esc(r.album || "—")}</td>
      <td data-optional>${esc(r.year ?? "—")}</td>
      <td></td>
    `,
    );
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-sm";
    btn.textContent = "Download";
    btn.addEventListener("click", () => handleDownloadRecording(r.mbid, btn));
    tr.lastElementChild.appendChild(btn);
    tbody.appendChild(tr);
  }
  wrap.appendChild(table);
  container.appendChild(wrap);
}

// -----------------------------------------------------------------------
// Render — artists
// -----------------------------------------------------------------------

function renderArtists(container, artists) {
  container.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const table = document.createElement("table");
  table.innerHTML = `
    <thead>
      <tr><th>Artist</th><th data-optional>Disambiguation</th><th></th></tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  for (const a of artists) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(a.name)}</td>
      <td data-optional>${esc(a.disambiguation || "—")}</td>
      <td></td>
    `;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-sm";
    btn.textContent = "Discography";
    btn.addEventListener("click", () => showArtistDiscography(a));
    tr.lastElementChild.appendChild(btn);
    tbody.appendChild(tr);
  }
  wrap.appendChild(table);
  container.appendChild(wrap);
}

// -----------------------------------------------------------------------
// Render — albums (shared by results list and artist discography)
// -----------------------------------------------------------------------

function renderAlbumsInto(container, albums, title = null) {
  container.innerHTML = "";
  if (title) {
    const header = document.createElement("div");
    header.className = "panel-toolbar";
    header.innerHTML = `<strong>${esc(title)}</strong>`;
    container.appendChild(header);
  }
  if (!albums.length) {
    container.insertAdjacentHTML("beforeend", renderEmpty("No albums found."));
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const table = document.createElement("table");
  table.innerHTML = `
    <thead>
      <tr><th class="cover-col"></th><th>Title</th><th>Artist</th><th data-optional>Year</th><th></th></tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  for (const album of albums) {
    const tr = document.createElement("tr");
    tr.appendChild(coverCell(album.mbid));
    tr.insertAdjacentHTML(
      "beforeend",
      `
      <td>${esc(album.title)}</td>
      <td>${esc(album.artist || "—")}</td>
      <td data-optional>${esc(album.year ?? "—")}</td>
      <td></td>
    `,
    );
    const actions = tr.lastElementChild;

    const tracksBtn = document.createElement("button");
    tracksBtn.type = "button";
    tracksBtn.className = "btn btn-ghost btn-sm";
    tracksBtn.textContent = "View tracks";
    tracksBtn.addEventListener("click", () => showAlbumTracks(album));
    actions.appendChild(tracksBtn);

    const dlBtn = document.createElement("button");
    dlBtn.type = "button";
    dlBtn.className = "btn btn-ghost btn-sm";
    dlBtn.textContent = "Download album";
    dlBtn.addEventListener("click", () => handleDownloadAlbum(album.mbid, dlBtn));
    actions.appendChild(dlBtn);

    tbody.appendChild(tr);
  }
  wrap.appendChild(table);
  container.appendChild(wrap);
}

// -----------------------------------------------------------------------
// Render — tracks (album detail)
// -----------------------------------------------------------------------

function renderTracksInto(container, album, tracks) {
  container.innerHTML = "";

  const header = document.createElement("div");
  header.className = "panel-toolbar";
  if (album.mbid) {
    const cover = document.createElement("img");
    cover.className = "cover-thumb cover-thumb-lg";
    cover.src = coverUrl(album.mbid, 250);
    cover.alt = "";
    cover.addEventListener("error", () => cover.remove());
    cover.addEventListener("click", () => openCover(album.mbid));
    header.appendChild(cover);
  }
  const label = document.createElement("strong");
  label.textContent = `${album.title}${album.artist ? " — " + album.artist : ""}`;
  const dlAlbumBtn = document.createElement("button");
  dlAlbumBtn.type = "button";
  dlAlbumBtn.className = "btn btn-primary btn-sm";
  dlAlbumBtn.textContent = "Download album";
  dlAlbumBtn.addEventListener("click", () => handleDownloadAlbum(album.mbid, dlAlbumBtn));
  header.append(label, dlAlbumBtn);
  container.appendChild(header);

  if (!tracks.length) {
    container.insertAdjacentHTML("beforeend", renderEmpty("No tracks found."));
    return;
  }

  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const table = document.createElement("table");
  table.innerHTML = `
    <thead>
      <tr><th>#</th><th>Title</th><th data-optional>Artist</th><th></th></tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  tracks.forEach((t, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(i + 1)}</td>
      <td>${esc(t.title)}</td>
      <td data-optional>${esc(t.artist || "—")}</td>
      <td></td>
    `;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-sm";
    btn.textContent = "Download";
    btn.addEventListener("click", () => handleDownloadRecording(t.mbid, btn));
    tr.lastElementChild.appendChild(btn);
    tbody.appendChild(tr);
  });
  wrap.appendChild(table);
  container.appendChild(wrap);
}

// -----------------------------------------------------------------------
// Render — orchestrators
// -----------------------------------------------------------------------

function renderResultsList() {
  const container = document.getElementById("mb-results");
  if (!container) return;
  if (!lastResults) return;
  const { artist, albums, recordings } = lastResults;
  if (!artist && !albums.length && !recordings.length) {
    container.innerHTML = renderEmpty("No matching MusicBrainz results.");
    return;
  }
  container.innerHTML = "";

  if (artist) {
    const section = document.createElement("section");
    section.className = "mb-result-section";
    const heading = document.createElement("h2");
    heading.textContent = "Artist";
    renderArtists(section, [artist]);
    section.prepend(heading);
    container.appendChild(section);
  }
  if (albums.length) {
    const section = document.createElement("section");
    section.className = "mb-result-section";
    renderAlbumsInto(section, albums, "Albums");
    container.appendChild(section);
  }
  if (recordings.length) {
    const section = document.createElement("section");
    section.className = "mb-result-section";
    const heading = document.createElement("h2");
    heading.textContent = "Matching recordings";
    renderRecordings(section, recordings);
    section.prepend(heading);
    container.appendChild(section);
  }
}

function renderDetail() {
  const panel = document.getElementById("mb-detail-panel");
  const container = document.getElementById("mb-detail");
  if (!container) return;
  if (!detail) {
    if (panel) panel.hidden = true;
    return;
  }
  if (panel) panel.hidden = false;
  if (detail.error) {
    container.innerHTML = renderEmpty(detail.error);
    return;
  }
  if (detail.kind === "artist") {
    if (detail.albums === null) return;
    renderAlbumsInto(container, detail.albums, `Discography — ${detail.artist.name}`);
  } else if (detail.kind === "album") {
    if (detail.tracks === null) return;
    renderTracksInto(container, detail.album, detail.tracks);
  }
}

export function renderMusicBrainz() {
  const container = document.getElementById("mb-results");
  if (!container) return;
  if (detail) renderDetail();
  else if (lastResults) renderResultsList();
}

// -----------------------------------------------------------------------
// Init (wire form + back button)
// -----------------------------------------------------------------------

export function initMusicBrainzTab() {
  const form = document.getElementById("mb-search-form");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const query = document.getElementById("mb-query").value.trim();
      const sort = document.getElementById("mb-sort").value;
      doSearch(query, sort);
    });
  }
  const backBtn = document.getElementById("mb-back");
  if (backBtn) backBtn.addEventListener("click", hideDetail);
}
