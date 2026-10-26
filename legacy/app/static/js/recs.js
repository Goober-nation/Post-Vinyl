// recs.js — Recs tab: summary cards, manual pulls, settings tables, pending table.

import * as api from "./api.js";
import { state, render } from "./state.js";
import { esc, fmtTime, badge, chip, renderEmpty, showToast, pager } from "./components.js";
import { buildFieldTable, readFieldValue, setFieldValue } from "./field_builder.js";
import { RECS_CATEGORY_TABLES, RECS_GLOBAL_FIELDS, RECS_SETTINGS_FIELDS } from "./recs_fields.js";

const PAGE_SIZE = 40;
const MANUAL_PULL_STORAGE_KEY = "musica.recs.manualCategories";
const MANUAL_CATEGORIES = [
  { key: "comfort_zone", inputId: "manual-comfort-zone" },
  { key: "fresh_picks", inputId: "manual-fresh-picks" },
  { key: "deep_cuts", inputId: "manual-deep-cuts" },
];
const STATUS_LABELS = {
  in_library: "In library",
  queued: "Queued",
  downloaded: "Downloaded",
  search_failed: "Search failed",
  queue_failed: "Queue failed",
  cancelled: "Cancelled",
  error: "Error",
};

// -----------------------------------------------------------------------
// Data refresh
// -----------------------------------------------------------------------

export async function refreshRecsStatus({ resync = false } = {}) {
  try {
    const status = await api.getRecsStatus();
    state.recs.status = status;
    state.recs.pullRunning = status.running || state.recs.pullRunning;
    if (resync) {
      // Force resetSettingsForm() to re-run on next render — used when an
      // external change (e.g. the Config tab's recs section) just saved,
      // so this tab's form reflects it without a page reload.
      const form = document.getElementById("rec-settings-form");
      if (form) form.dataset.initialized = "";
    }
  } catch {
    // Leave prior status; RecPuller worker may not be attached (503).
  }
  render();
}

export async function refreshRecsPending() {
  const { filter, page } = state.recs.pending;
  try {
    const result = await api.getRecsPending(
      filter === "all" ? undefined : filter,
      PAGE_SIZE,
      (page - 1) * PAGE_SIZE
    );
    state.recs.pending.total = result.total;
    state.recs.pending.items = result.items;
  } catch {
    // Keep prior pending list on transient failure.
  }
  render();
}

// -----------------------------------------------------------------------
// Actions
// -----------------------------------------------------------------------

async function handlePullNow(btn) {
  const categories = [...(state.recs.manualCategories || [])];
  if (!categories.length) {
    showToast("Select at least one category for the manual pull", "error");
    return;
  }
  btn.disabled = true;
  try {
    await api.pullRecs(categories);
    state.recs.pullRunning = true;
    state.recs.pullStage = "started";
    render();
  } catch (err) {
    if (err.code === "REC_PULL_IN_PROGRESS") {
      showToast("A pull is already running");
    } else {
      showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
    }
  } finally {
    btn.disabled = false;
  }
}

async function handleCancelQueued(btn) {
  const total = ((state.recs.status && state.recs.status.status_counts) || {}).queued || 0;
  if (total === 0) {
    showToast("No queued recommendations to cancel");
    return;
  }
  if (!confirm(`Cancel all ${total} queued recommendation download(s)?`)) return;

  btn.disabled = true;
  try {
    const result = await api.cancelQueuedRecs();
    showToast(
      `Cancelled ${result.cancelled_recs} recommendation(s)` +
        (result.cancelled_transfers ? `, ${result.cancelled_transfers} transfer(s) stopped` : "")
    );
    await Promise.all([refreshRecsStatus(), refreshRecsPending()]);
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function handleSaveSettings(form) {
  const statusLine = document.getElementById("rec-settings-status");
  // Section-shaped payload: most fields belong to [recs]; a field with a
  // `section` marker (the Fresh Picks count) routes to its own section.
  const payload = {};
  for (const field of RECS_SETTINGS_FIELDS) {
    const value = readFieldValue(field, "rec");
    if (value !== undefined && !(typeof value === "number" && Number.isNaN(value))) {
      const section = field.section || "recs";
      (payload[section] ||= {})[field.key] = value;
    }
  }
  try {
    await api.updateRecsSettings(payload);
    if (statusLine) statusLine.textContent = "Settings saved";
    await refreshRecsStatus();
  } catch (err) {
    if (statusLine) statusLine.textContent = `${err.code || "ERROR"}: ${err.message}`;
  }
}

function resetSettingsForm() {
  const status = state.recs.status;
  if (!status) return;
  const values = {
    comfort_zone_enabled: status.comfort_zone_enabled,
    fresh_picks_enabled: status.fresh_picks_enabled,
    deep_cuts_enabled: status.deep_cuts_enabled,
    comfort_zone_interval_days: status.comfort_zone_interval_days,
    deep_cuts_interval_days: status.deep_cuts_interval_days,
    comfort_zone_count: status.counts.comfort_zone_count,
    pull_window: status.fresh_picks.pull_window,
    offset: status.fresh_picks.offset,
    count: status.fresh_picks.count,
    search_buffer: status.fresh_picks.search_buffer,
    deep_cuts_count: status.counts.deep_cuts_count,
    comfort_zone_playlist_name: status.comfort_zone_playlist_name,
    fresh_picks_playlist_name: status.fresh_picks_playlist_name,
    deep_cuts_playlist_name: status.deep_cuts_playlist_name,
    rotation_trash_rating: status.rotation_trash_rating,
  };
  for (const field of RECS_SETTINGS_FIELDS) {
    setFieldValue(field, "rec", values[field.key]);
  }
}

// -----------------------------------------------------------------------
// Render
// -----------------------------------------------------------------------

function renderBanner() {
  const el = document.getElementById("rec-banner");
  if (!el) return;
  const status = state.recs.status;
  if (!status) {
    el.hidden = true;
    return;
  }

  if (!status.listenbrainz_enabled) {
    el.hidden = false;
    el.className = "banner warn";
    el.innerHTML = `<p>ListenBrainz is disabled: enter your username and token in Config &rarr; Secrets, then restart the server.</p>`;
    return;
  }

  const warnings = Object.entries(status.category_warnings || {});
  if (warnings.length) {
    el.hidden = false;
    el.className = "banner warn";
    el.innerHTML = warnings
      .map(([category, message]) => `<p><strong>${esc(category)}:</strong> ${esc(message)}</p>`)
      .join("");
    return;
  }

  // P6.5-3b: no single master switch any more — each category has its own
  // enabled flag (Settings below), so there's nothing generic to bannerize
  // here beyond the listenbrainz gate above.
  el.hidden = true;
}

function renderSummary() {
  const container = document.getElementById("rec-summary");
  const pullArea = document.getElementById("rec-pull");
  if (!container) return;

  const status = state.recs.status;
  if (!status) {
    renderManualPullControls(null);
    container.innerHTML = renderEmpty("Loading…");
    return;
  }

  container.innerHTML = "";
  const cards = [
    { label: "Comfort Zone", value: status.comfort_zone_enabled ? "On" : "Off" },
    { label: "Fresh Picks", value: status.fresh_picks_enabled ? "On" : "Off" },
    { label: "Deep Cuts", value: status.deep_cuts_enabled ? "On" : "Off" },
    { label: "Last pull", value: status.last_pull_at ? fmtTime(status.last_pull_at) : "never" },
    { label: "Next pull", value: status.next_pull_at ? fmtTime(status.next_pull_at) : "—" },
  ];
  for (const c of cards) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<div class="card-label">${esc(c.label)}</div><div class="card-value">${esc(c.value)}</div>`;
    container.appendChild(card);
  }
  for (const [key, label] of Object.entries(STATUS_LABELS)) {
    const count = (status.status_counts || {})[key] || 0;
    const card = document.createElement("div");
    card.className = "card";
    if (count === 0) card.style.opacity = "0.55";
    card.innerHTML = `<div class="card-label">${esc(label)}</div><div class="card-value">${esc(count)}</div>`;
    container.appendChild(card);
  }

  renderManualPullControls(status);
  if (pullArea) {
    if (state.recs.pullRunning) {
      const stageText =
        state.recs.pullStage === "pulling"
          ? `Pulling from ${(status.counts ? Object.keys(status.counts).length : 3)} sources…`
          : state.recs.pullStage
            ? `Classifying ${state.recs.pullStage.match(/\d+/)?.[0] || ""} tracks…`
            : "Pull in progress…";
      pullArea.textContent = stageText;
    } else {
      pullArea.textContent = "";
    }
  }
}

function readStoredManualCategories() {
  if (typeof localStorage === "undefined") return [];
  try {
    const value = JSON.parse(localStorage.getItem(MANUAL_PULL_STORAGE_KEY) || "null");
    if (!Array.isArray(value)) return [];
    const valid = new Set(MANUAL_CATEGORIES.map(({ key }) => key));
    return [...new Set(value.filter((key) => valid.has(key)))];
  } catch {
    return [];
  }
}

function storeManualCategories(categories) {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(MANUAL_PULL_STORAGE_KEY, JSON.stringify(categories));
  } catch {
    // Private browsing or a blocked storage policy should not disable pulls.
  }
}

function renderManualPullControls(status) {
  const selected = new Set(state.recs.manualCategories || []);
  const disabled = !status || !status.listenbrainz_enabled || state.recs.pullRunning;
  for (const category of MANUAL_CATEGORIES) {
    const input = document.getElementById(category.inputId);
    if (!input) continue;
    input.checked = selected.has(category.key);
    input.disabled = disabled;
    const label = input.closest(".category-toggle");
    if (label) label.classList.toggle("active", input.checked);
  }

  const pullBtn = document.getElementById("btn-pull");
  if (!pullBtn) return;
  pullBtn.disabled = disabled || selected.size === 0;
  pullBtn.textContent = selected.size ? `Pull selected (${selected.size})` : "Select categories";
  pullBtn.onclick = () => handlePullNow(pullBtn);
}

function renderSettingsForm() {
  const details = document.getElementById("rec-settings");
  const form = document.getElementById("rec-settings-form");
  if (!details || !form) return;

  const status = state.recs.status;
  // Gated on listenbrainz, not recs.enabled — these settings (counts,
  // playlist name, interval) are still used by manual pulls even while
  // the automatic loop is off.
  const disabled = !status || !status.listenbrainz_enabled;

  Array.from(form.elements).forEach((el) => {
    if (el.id === "btn-rec-reset") return;
    el.disabled = disabled;
  });

  if (status && !form.dataset.initialized) {
    resetSettingsForm();
    form.dataset.initialized = "true";
  }
}

function buildRecsSettingsTables() {
  const container = document.getElementById("rec-settings-fields");
  if (!container) return;
  container.innerHTML = "";

  const tables = document.createElement("div");
  tables.className = "recs-settings-tables";
  for (const category of RECS_CATEGORY_TABLES) {
    const section = document.createElement("section");
    section.className = "recs-category-settings";
    section.innerHTML = `<h3>${esc(category.title)}</h3><p>${esc(category.description)}</p>`;
    section.appendChild(buildFieldTable(category.fields, "rec"));
    tables.appendChild(section);
  }
  container.appendChild(tables);

  const global = document.createElement("section");
  global.className = "recs-global-settings";
  global.innerHTML = "<h3>Rotation</h3>";
  global.appendChild(buildFieldTable(RECS_GLOBAL_FIELDS, "rec"));
  container.appendChild(global);
}

function renderPendingChips() {
  const container = document.getElementById("rec-pending-chips");
  const totalEl = document.getElementById("rec-pending-total");
  if (!container) return;
  container.innerHTML = "";

  const statusCounts = (state.recs.status && state.recs.status.status_counts) || {};
  const all = Object.values(statusCounts).reduce((a, b) => a + b, 0);

  container.appendChild(
    chip("All", state.recs.pending.filter === "all", all, () => {
      state.recs.pending.filter = "all";
      state.recs.pending.page = 1;
      refreshRecsPending();
    })
  );
  for (const [key, label] of Object.entries(STATUS_LABELS)) {
    const count = statusCounts[key] || 0;
    container.appendChild(
      chip(label, state.recs.pending.filter === key, count, () => {
        state.recs.pending.filter = key;
        state.recs.pending.page = 1;
        refreshRecsPending();
      })
    );
  }

  if (totalEl) totalEl.textContent = `${state.recs.pending.total} total`;

  const cancelBtn = document.getElementById("btn-cancel-queued-recs");
  if (cancelBtn) {
    const queuedCount = statusCounts.queued || 0;
    cancelBtn.disabled = queuedCount === 0;
    cancelBtn.textContent = queuedCount > 0 ? `Cancel all queued (${queuedCount})` : "Cancel all queued";
  }
}

function renderPendingTable() {
  const container = document.getElementById("rec-pending");
  if (!container) return;

  const items = state.recs.pending.items;
  if (!items.length) {
    container.innerHTML = renderEmpty("No recommendations yet — run a pull.");
    return;
  }

  container.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const table = document.createElement("table");
  table.innerHTML = `
    <thead>
      <tr>
        <th>Artist</th><th>Track</th>
        <th data-optional>Source</th><th>Status</th><th data-optional>When</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  for (const r of items) {
    const when = r.processed_at || r.created_at;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(r.artist)}</td>
      <td>${esc(r.track)}</td>
      <td data-optional>${esc(r.source)}</td>
      <td>${badge(r.status)}</td>
      <td data-optional>${esc(fmtTime(typeof when === "number" ? new Date(when * 1000).toISOString() : when))}</td>
    `;
    tbody.appendChild(tr);
  }
  wrap.appendChild(table);
  container.appendChild(wrap);

  const totalPages = Math.max(1, Math.ceil(state.recs.pending.total / PAGE_SIZE));
  if (totalPages > 1) {
    container.appendChild(
      pager({
        page: state.recs.pending.page,
        pages: totalPages,
        onPage: (p) => {
          state.recs.pending.page = p;
          refreshRecsPending();
        },
      })
    );
  }
}

export function renderRecs() {
  renderBanner();
  renderSummary();
  renderSettingsForm();
  renderPendingChips();
  renderPendingTable();
}

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------

export function initRecsTab() {
  buildRecsSettingsTables();
  state.recs.manualCategories = readStoredManualCategories();

  for (const category of MANUAL_CATEGORIES) {
    const input = document.getElementById(category.inputId);
    if (!input) continue;
    input.addEventListener("change", () => {
      const selected = new Set(state.recs.manualCategories || []);
      if (input.checked) selected.add(category.key);
      else selected.delete(category.key);
      state.recs.manualCategories = MANUAL_CATEGORIES
        .map(({ key }) => key)
        .filter((key) => selected.has(key));
      storeManualCategories(state.recs.manualCategories);
      renderRecs();
    });
  }

  const form = document.getElementById("rec-settings-form");
  const resetBtn = document.getElementById("btn-rec-reset");
  const cancelQueuedBtn = document.getElementById("btn-cancel-queued-recs");

  if (cancelQueuedBtn) {
    cancelQueuedBtn.addEventListener("click", () => handleCancelQueued(cancelQueuedBtn));
  }

  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      handleSaveSettings(form);
    });
  }
  if (resetBtn) {
    resetBtn.addEventListener("click", () => resetSettingsForm());
  }
}
