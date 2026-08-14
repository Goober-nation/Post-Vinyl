// state.js — entry module. Global state, render orchestration, SSE client.
//
// Import note: this module and the tab modules (search.js/transfers.js/recs.js)
// import from each other (state.js needs their render/init functions; they need
// `state`/`setState` from here). This is a standard, safe ES-module circular
// import: the functions are only ever *called* inside render()/init handlers,
// which run after all modules have finished evaluating, so every binding is
// live by the time it's used.

import * as api from "./api.js";
import { healthDot, buildHealthPopover, showToast, esc, confirmAction } from "./components.js";
import { renderSearch, initSearchTab, refreshRecents } from "./search.js";
import { renderTransfers, initTransfersTab, refreshTransfers } from "./transfers.js";
import { renderRecs, initRecsTab, refreshRecsStatus, refreshRecsPending } from "./recs.js";
import { renderConfig, initConfigTab, refreshConfig } from "./config.js";
import { renderMusicBrainz, initMusicBrainzTab } from "./musicbrainz.js";
import { initSetup } from "./setup.js";

export const state = {
  tab: "search",
  searches: {
    list: [],
    selectedId: null,
    results: [],
    resultsLoading: false,
    page: 1,
    active: false,
  },
  transfers: {
    list: [],
    filter: "all",
    pollTimer: null,
    hadActive: false,
  },
  recs: {
    status: null,
    pending: { total: 0, items: [], filter: "all", page: 1 },
    pullRunning: false,
    pullStage: null,
    manualCategories: [],
  },
  system: { services: {}, version: "", uptime: 0, restartAvailable: false },
  config: { data: null, error: null },
  ui: { searchTimestamps: [], toastCount: 0 },
};

export function setState(updater) {
  const patch = typeof updater === "function" ? updater(state) : updater;
  Object.assign(state, patch);
  render();
}

export function render() {
  renderHeader();
  renderSearch();
  renderTransfers();
  renderRecs();
  renderConfig();
  renderMusicBrainz();
}

// -----------------------------------------------------------------------
// Header: tab switching + health dots/popover
// -----------------------------------------------------------------------

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((btn) => {
    const active = btn.dataset.tab === tab;
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `panel-${tab}`;
  });
}

function initTabBar() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
}

let popoverOpen = false;

function closePopover() {
  const existing = document.getElementById("health-popover");
  if (existing) existing.remove();
  popoverOpen = false;
  document.removeEventListener("click", onOutsideClick, true);
  document.removeEventListener("keydown", onEscKey, true);
}

function onOutsideClick(e) {
  const popover = document.getElementById("health-popover");
  const dotsContainer = document.getElementById("health-dots");
  if (popover && !popover.contains(e.target) && !dotsContainer.contains(e.target)) {
    closePopover();
  }
}

function onEscKey(e) {
  if (e.key === "Escape") closePopover();
}

function togglePopover() {
  if (popoverOpen) {
    closePopover();
    return;
  }
  const container = document.getElementById("health-dots");
  if (!container) return;
  container.insertAdjacentHTML(
    "beforeend",
    buildHealthPopover(state.system.services, state.system.version, state.system.uptime)
  );
  popoverOpen = true;
  setTimeout(() => {
    document.addEventListener("click", onOutsideClick, true);
    document.addEventListener("keydown", onEscKey, true);
  }, 0);
}

function renderHeader() {
  const container = document.getElementById("health-dots");
  if (!container) return;

  const wasOpen = popoverOpen;
  container.innerHTML = "";

  const names = ["slskd", "navidrome", "listenbrainz"];
  for (const name of names) {
    const svc = state.system.services[name];
    const status = svc ? svc.status : "down";
    const dot = healthDot(status);
    dot.title = `${name}: ${status}`;
    dot.addEventListener("click", (e) => {
      e.stopPropagation();
      togglePopover();
    });
    container.appendChild(dot);
  }

  if (wasOpen) {
    popoverOpen = false;
    container.insertAdjacentHTML(
      "beforeend",
      buildHealthPopover(state.system.services, state.system.version, state.system.uptime)
    );
    popoverOpen = true;
  }
}

export async function reconnectSlskd(btn) {
  if (btn) btn.disabled = true;
  try {
    const result = await api.reconnectSlskd();
    showToast(result.success ? "Reconnect requested" : "Reconnect request failed", result.success ? "info" : "error");
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
    refreshSystemStatus();
  }
}

export async function checkListenBrainz(btn) {
  if (btn) btn.disabled = true;
  try {
    const result = await api.checkListenBrainz();
    showToast(
      result.status === "up" ? "ListenBrainz reachable" : `ListenBrainz ${result.status}${result.error ? `: ${result.error}` : ""}`,
      result.status === "up" ? "info" : "error"
    );
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
    refreshSystemStatus();
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("#slskd-reconnect-btn, #config-slskd-reconnect-btn");
  if (btn) reconnectSlskd(btn);
  const lbBtn = e.target.closest("#config-listenbrainz-check-btn");
  if (lbBtn) checkListenBrainz(lbBtn);
});

// "Break glass" kill switches — stop everything hitting slskd (any origin),
// and separately stop the rec pipeline (LB pull + its own slskd traffic).
// Both live in Config's System panel and are mirrored next to the activity
// they affect (Transfers tab / Recs tab).

export async function stopSlskdActivity(btn) {
  if (
    !confirmAction(
      "Stop all in-progress searches and cancel every queued/downloading transfer (any origin, including rec downloads)? This cannot be undone."
    )
  ) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const result = await api.stopSlskdActivity();
    showToast(
      `Stopped: ${result.cancelled_searches} search(es), ${result.cancelled_transfers} transfer(s) cancelled` +
        (result.failed_transfers ? ` — ${result.failed_transfers} transfer(s) failed to cancel` : "")
    );
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
    await Promise.all([refreshRecents(), refreshTransfers()]);
  }
}

export async function abortRecsActivity(btn) {
  if (
    !confirmAction(
      "Stop the recommendation pull (if one is running) and cancel every queued rec download? This cannot be undone."
    )
  ) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const result = await api.abortRecs();
    showToast(
      `Recs stopped: ${result.aborted_pull ? "pull aborted, " : ""}${result.cancelled_recs} queued rec(s) cancelled` +
        (result.failed_transfers ? ` — ${result.failed_transfers} transfer(s) failed to cancel` : "")
    );
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
    await Promise.all([refreshRecsStatus(), refreshRecsPending(), refreshTransfers()]);
  }
}

document.addEventListener("click", (e) => {
  const stopBtn = e.target.closest("#config-stop-slskd-btn, #transfers-stop-slskd-btn");
  if (stopBtn) stopSlskdActivity(stopBtn);
  const abortBtn = e.target.closest("#config-abort-recs-btn, #recs-abort-btn");
  if (abortBtn) abortRecsActivity(abortBtn);
  const syncBtn = e.target.closest("#config-sync-now-btn");
  if (syncBtn) syncNow(syncBtn);
  const unblockBtn = e.target.closest("#config-unblock-peers-btn");
  if (unblockBtn) unblockAllPeers(unblockBtn);
  const restartBtn = e.target.closest("#config-restart-app-btn");
  if (restartBtn) restartAppNow(restartBtn);
});

// Restarts the container (Docker's `restart: unless-stopped` policy brings
// it back up — see POST /api/system/restart) so restart-required settings
// (Server, Paths, Navidrome, slskd) and changed .env secrets take effect.
export async function restartAppNow(btn) {
  if (
    !confirmAction(
      "Restart the app? It applies restart-required settings and changed secrets, with a few seconds of downtime while it comes back up."
    )
  ) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    await api.restartApp();
    showToast("Restarting — the page will refresh when the app is back");
    await waitForAppRestart();
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
    if (btn) btn.disabled = false;
  }
}

export async function unblockAllPeers(btn) {
  if (
    !confirmAction(
      "Unblock every banned peer and reset their failure counts? Peers that were genuinely misbehaving will just get re-banned on their next real failure."
    )
  ) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const result = await api.unblockAllPeers();
    showToast(`Unblocked ${result.unblocked} peer(s)`);
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function syncNow(btn) {
  if (
    !confirmAction(
      "Run sync now? This sends pending favorites/trash feedback and permanently deletes files in Navidrome's Trash playlist."
    )
  ) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const result = await api.syncNow();
    const love = result.love_sync || {};
    const trash = result.trash_purge || {};
    showToast(
      `Sync complete: ${love.synced || 0} love(s) sent, ${trash.trashed || 0} trash item(s) purged`
    );
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
    refreshSystemStatus();
  }
}

async function waitForAppRestart() {
  // Let the old process receive SIGTERM before probing; otherwise the first
  // successful ping can come from the process that is about to exit.
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      await api.getSystemPing();
      window.location.reload();
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  throw new Error("The app did not become available after restart");
}

async function refreshSystemStatus() {
  try {
    const status = await api.getSystemStatus();
    state.system.services = status.services;
    state.system.version = status.version;
    state.system.uptime = status.uptime_seconds;
    state.system.restartAvailable = status.restart_available;
  } catch {
    // Treat as down/grey silently — do not block other tabs.
    state.system.services = {};
  }
  renderHeader();
  renderConfig();
}

// -----------------------------------------------------------------------
// SSE client
// -----------------------------------------------------------------------

let eventSource = null;

function upsertTransfer(payload) {
  const list = state.transfers.list;
  const idx = list.findIndex((t) => t.transfer_id === payload.transfer_id);
  if (idx >= 0) {
    list[idx] = { ...list[idx], ...payload };
  } else {
    list.unshift(payload);
  }
}

function connectSSE() {
  eventSource = new EventSource("/api/events?types=search,transfer,rec,mb,system");

  eventSource.addEventListener("search.started", () => {
    refreshRecents();
  });
  eventSource.addEventListener("search.completed", () => {
    refreshRecents();
  });
  eventSource.addEventListener("search.cancelled", () => {
    refreshRecents();
  });

  eventSource.addEventListener("transfer.started", (e) => {
    const data = JSON.parse(e.data);
    upsertTransfer({ transfer_id: data.transfer_id, username: data.username, filename: data.filename, size: data.size, state: "downloading" });
    renderTransfers();
  });
  eventSource.addEventListener("transfer.progress", (e) => {
    const data = JSON.parse(e.data);
    upsertTransfer({ transfer_id: data.transfer_id, progress: data.progress, speed: data.speed, state: "downloading" });
    renderTransfers();
  });
  eventSource.addEventListener("transfer.completed", (e) => {
    const data = JSON.parse(e.data);
    upsertTransfer({ transfer_id: data.transfer_id, filename: data.filename, state: "completed", progress: 100 });
    if (!state.recs.pullRunning) {
      showToast(`Downloaded: ${data.filename}`);
    }
    refreshTransfers();
    refreshRecsStatus();
    refreshRecsPending();
  });
  eventSource.addEventListener("transfer.failed", (e) => {
    const data = JSON.parse(e.data);
    upsertTransfer({ transfer_id: data.transfer_id, state: "failed" });
    if (!state.recs.pullRunning) {
      showToast(`Download failed: ${data.error}${data.will_retry ? " — will retry" : ""}`, "error");
    }
    refreshTransfers();
  });

  eventSource.addEventListener("rec.pull_started", () => {
    state.recs.pullRunning = true;
    state.recs.pullStage = "pulling";
    renderRecs();
  });
  eventSource.addEventListener("rec.classifying", (e) => {
    const data = JSON.parse(e.data);
    state.recs.pullStage = `classifying (${data.total} tracks)`;
    renderRecs();
  });
  eventSource.addEventListener("rec.pull_completed", (e) => {
    const data = JSON.parse(e.data);
    state.recs.pullRunning = false;
    state.recs.pullStage = null;
    showToast(
      `Pull done: ${data.in_library} in library, ${data.queued} queued, ${(data.failures || []).length} failed`
    );
    refreshRecsStatus();
    refreshRecsPending();
  });
  eventSource.addEventListener("rec.warning", (e) => {
    const data = JSON.parse(e.data);
    showToast(`${data.category}: ${data.message}`, "info");
    refreshRecsStatus();
  });

  eventSource.addEventListener("mb.resolve_started", (e) => {
    const data = JSON.parse(e.data);
    const n = data.count ?? data.total ?? data.track_count;
    showToast(n != null ? `Resolving ${n} track(s)…` : "Resolving tracks…");
  });
  eventSource.addEventListener("mb.track_queued", (e) => {
    const data = JSON.parse(e.data);
    showToast(`Queued: ${data.title || "track"}${data.artist ? ` — ${data.artist}` : ""}`);
  });
  eventSource.addEventListener("mb.track_failed", (e) => {
    const data = JSON.parse(e.data);
    showToast(`Failed: ${data.title || "track"}`, "error");
  });
  eventSource.addEventListener("mb.resolve_completed", (e) => {
    const data = JSON.parse(e.data);
    showToast(`Resolve done: ${data.queued ?? 0} queued, ${data.failed ?? 0} failed`);
    refreshTransfers();
  });

  eventSource.addEventListener("system.config_reloaded", (e) => {
    const data = JSON.parse(e.data);
    // Any hot-reload save (Config tab or Recs tab) can affect the other
    // tab's own settings form — resync both so neither needs a reload
    // to reflect a change made elsewhere (P6-13).
    refreshConfig({ resync: true });
    if ((data.changed_keys || []).some((k) => k.startsWith("recs."))) {
      refreshRecsStatus({ resync: true });
    }
  });

  eventSource.onopen = () => {
    state.system.ssedown = false;
    refreshRecents();
    refreshTransfers();
    refreshRecsStatus();
    refreshRecsPending();
    refreshSystemStatus();
  };

  eventSource.onerror = () => {
    state.system.ssedown = true;
    // EventSource auto-reconnects natively — poll fallbacks (transfers 3s
    // while active, system status 30s) already cover the gap.
  };
}

// -----------------------------------------------------------------------
// Poll fallbacks
// -----------------------------------------------------------------------

let transfersPollTimer = null;

export function ensureTransfersPoll() {
  const hasActive = state.transfers.list.some(
    (t) => t.state === "queued" || t.state === "downloading"
  );
  if (hasActive && !transfersPollTimer) {
    transfersPollTimer = setInterval(() => {
      refreshTransfers();
    }, 3000);
  } else if (!hasActive && transfersPollTimer) {
    clearInterval(transfersPollTimer);
    transfersPollTimer = null;
  }
}

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------

async function init() {
  initTabBar();
  initSearchTab();
  initTransfersTab();
  initRecsTab();
  initConfigTab();
  initMusicBrainzTab();

  await Promise.allSettled([
    refreshRecents(),
    refreshTransfers(),
    refreshRecsStatus(),
    refreshRecsPending(),
    refreshSystemStatus(),
    refreshConfig(),
  ]);

  render();
  connectSSE();
  setInterval(refreshSystemStatus, 30000);
  initSetup();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

// Re-export esc for convenience (some tab modules import it from here in
// earlier drafts of this codebase's placeholder — kept for consistency).
export { esc };
