// api.js — thin REST client for the Musica backend. Pure fetch, no deps.

/**
 * Low-level fetch wrapper. Parses JSON, throws {code, message, details} on
 * a non-ok response (from body.error if present, else a synthetic HTTP_<status>).
 */
export async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const res = await fetch(path, { ...opts, headers });

  let body = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!res.ok) {
    if (body && body.error) {
      const err = new Error(body.error.message || `HTTP ${res.status}`);
      err.code = body.error.code;
      err.details = body.error.details;
      throw err;
    }
    const err = new Error(`HTTP ${res.status}`);
    err.code = `HTTP_${res.status}`;
    err.details = {};
    throw err;
  }

  return body;
}

function qs(params) {
  const entries = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null);
  if (entries.length === 0) return "";
  return "?" + entries.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
}

// -----------------------------------------------------------------------
// Search
// -----------------------------------------------------------------------

export function listSearches() {
  return api("/api/searches");
}

export function getSearch(id) {
  return api(`/api/searches/${encodeURIComponent(id)}`);
}

export function createSearch(query, artist) {
  return api("/api/search", {
    method: "POST",
    body: JSON.stringify({ query, artist: artist || null }),
  });
}

export function cancelSearch(id) {
  return api(`/api/searches/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export function getSearchProgress(id) {
  return api(`/api/searches/${encodeURIComponent(id)}/progress`);
}

// -----------------------------------------------------------------------
// Downloads / Transfers
// -----------------------------------------------------------------------

export function listTransfers() {
  return api("/api/transfers");
}

export function queueDownload(username, files, searchId) {
  return api("/api/queue", {
    method: "POST",
    body: JSON.stringify({ username, files, search_id: searchId || null }),
  });
}

export function retryTransfer(id) {
  return api(`/api/queue/retry/${encodeURIComponent(id)}`, { method: "POST" });
}

export function cancelTransfer(id) {
  return api(`/api/transfers/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function deleteFinishedTransfers() {
  return api("/api/transfers?state=finished", { method: "DELETE" });
}

// -----------------------------------------------------------------------
// System
// -----------------------------------------------------------------------

export function getSystemStatus() {
  return api("/api/system/status");
}

export function getSystemPing() {
  return api("/api/system/ping");
}

export function reconnectSlskd() {
  return api("/api/system/slskd/reconnect", { method: "POST" });
}

export function checkListenBrainz() {
  return api("/api/system/listenbrainz/check", { method: "POST" });
}

export function stopSlskdActivity() {
  return api("/api/system/stop-slskd-activity", { method: "POST" });
}

export function restartApp() {
  return api("/api/system/restart", { method: "POST" });
}

export function syncNow() {
  return api("/api/system/sync", { method: "POST" });
}

export function unblockAllPeers() {
  return api("/api/system/peers/unblock", { method: "POST" });
}

// -----------------------------------------------------------------------
// Recs
// -----------------------------------------------------------------------

export function getRecsStatus() {
  return api("/api/recs/status");
}

export async function pullRecs(categories) {
  // 202 {started:true} or 409 REC_PULL_IN_PROGRESS — both come back through
  // api()'s normal ok/not-ok handling (409 throws with err.code set).
  const body = categories === undefined ? {} : { categories };
  return api("/api/recs/pull", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getRecsPending(status, limit, offset) {
  return api(`/api/recs/pending${qs({ status, limit, offset })}`);
}

export function cancelQueuedRecs() {
  return api("/api/recs/pending/cancel-queued", { method: "POST" });
}

export function abortRecs() {
  return api("/api/recs/abort", { method: "POST" });
}

export function updateRecsSettings(partial) {
  return api("/api/recs/settings", {
    method: "POST",
    body: JSON.stringify(partial),
  });
}

// -----------------------------------------------------------------------
// MusicBrainz
// -----------------------------------------------------------------------

export function searchMusicBrainz(query, limit = 20, sort = "relevance") {
  return api(`/api/musicbrainz/search${qs({ query, limit, sort })}`);
}

export function searchRecordings(title, artist, limit = 20, sort = "relevance") {
  return api(`/api/musicbrainz/search/recordings${qs({ title, artist, limit, sort })}`);
}

export function searchAlbums(title, artist, limit = 20, sort = "relevance") {
  return api(`/api/musicbrainz/search/albums${qs({ title, artist, limit, sort })}`);
}

export function searchArtists(name, limit = 20) {
  return api(`/api/musicbrainz/search/artists${qs({ name, limit })}`);
}

export function artistAlbums(mbid, limit = 100) {
  return api(`/api/musicbrainz/artists/${encodeURIComponent(mbid)}/albums${qs({ limit })}`);
}

export function albumTracks(mbid) {
  return api(`/api/musicbrainz/albums/${encodeURIComponent(mbid)}/tracks`);
}

export function downloadRecording(mbid) {
  return api(`/api/musicbrainz/recordings/${encodeURIComponent(mbid)}/download`, { method: "POST" });
}

export function downloadAlbum(mbid) {
  return api(`/api/musicbrainz/albums/${encodeURIComponent(mbid)}/download`, { method: "POST" });
}

// -----------------------------------------------------------------------
// Config
// -----------------------------------------------------------------------

export function getConfig() {
  return api("/api/config");
}

export function updateConfig(payload) {
  return api("/api/config", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateSecrets(payload) {
  return api("/api/config/secrets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// -----------------------------------------------------------------------
// Setup wizard
// -----------------------------------------------------------------------

export function getSetupStatus() {
  return api("/api/setup/status");
}

export function setupNavidrome(username, password) {
  return api("/api/setup/navidrome", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function setupSlskd(username, password) {
  return api("/api/setup/slskd", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function checkSetupSlskd() {
  return api("/api/setup/slskd/check");
}

export function completeSetup() {
  return api("/api/setup/complete", { method: "POST" });
}

export function dismissTutorial() {
  return api("/api/setup/tutorial/dismiss", { method: "POST" });
}

export function rerunSetup() {
  return api("/api/setup/rerun", { method: "POST" });
}
