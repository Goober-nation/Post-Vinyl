// transfers.js — Transfers tab: chips, live table, cancel/retry, clear finished.

import * as api from "./api.js";
import { state, render, ensureTransfersPoll } from "./state.js";
import { esc, fmtSize, fmtSpeed, fmtTime, badge, chip, renderEmpty, confirmAction } from "./components.js";

const FINISHED_STATES = new Set(["completed", "failed", "cancelled"]);
const ACTIVE_STATES = new Set(["queued", "downloading"]);

// -----------------------------------------------------------------------
// Data refresh
// -----------------------------------------------------------------------

export async function refreshTransfers() {
  try {
    const list = await api.listTransfers();
    state.transfers.list = list;
  } catch {
    // Keep prior list on transient failure; avoid spamming errors.
  }
  ensureTransfersPoll();
  render();
}

// -----------------------------------------------------------------------
// Render
// -----------------------------------------------------------------------

function countsByFilter() {
  const list = state.transfers.list;
  return {
    all: list.length,
    active: list.filter((t) => ACTIVE_STATES.has(t.state)).length,
    completed: list.filter((t) => t.state === "completed").length,
    failed: list.filter((t) => t.state === "failed").length,
    cancelled: list.filter((t) => t.state === "cancelled").length,
  };
}

function matchesFilter(t, filter) {
  if (filter === "all") return true;
  if (filter === "active") return ACTIVE_STATES.has(t.state);
  return t.state === filter;
}

function renderChips() {
  const container = document.getElementById("transfer-chips");
  if (!container) return;
  container.innerHTML = "";
  const counts = countsByFilter();
  const labels = [
    ["all", "All"],
    ["active", "Active"],
    ["completed", "Completed"],
    ["failed", "Failed"],
    ["cancelled", "Cancelled"],
  ];
  for (const [key, label] of labels) {
    container.appendChild(
      chip(label, state.transfers.filter === key, counts[key], () => {
        state.transfers.filter = key;
        renderTransfers();
      })
    );
  }
}

async function handleCancel(id, btn) {
  btn.disabled = true;
  try {
    await api.cancelTransfer(id);
  } finally {
    await refreshTransfers();
  }
}

async function handleRetry(id, btn) {
  btn.disabled = true;
  const statusLine = document.getElementById("transfers-status");
  try {
    await api.retryTransfer(id);
    if (statusLine) statusLine.textContent = "retry queued";
  } catch (err) {
    if (statusLine) statusLine.textContent = `${err.code || "ERROR"}: ${err.message}`;
  } finally {
    await refreshTransfers();
  }
}

function renderTable() {
  const container = document.getElementById("transfers");
  if (!container) return;

  let list = state.transfers.list.filter((t) => matchesFilter(t, state.transfers.filter));
  list = [...list].sort((a, b) => (b.started_at || "").localeCompare(a.started_at || ""));

  if (!list.length) {
    container.innerHTML = renderEmpty("No transfers.");
    return;
  }

  container.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const table = document.createElement("table");
  table.innerHTML = `
    <thead>
      <tr>
        <th>User</th><th>File</th><th>Size</th><th>State</th><th>Progress</th>
        <th data-optional>Speed</th><th data-optional>Started</th><th>Actions</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");

  for (const t of list) {
    const progress =
      t.state === "completed" || t.state === "importing"
        ? 100
        : Math.max(0, Math.min(100, Math.round(t.progress || 0)));
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(t.username)}</td>
      <td class="truncate" title="${esc(t.filename)} (${esc(t.transfer_id)})">${esc(t.filename)}</td>
      <td>${esc(fmtSize(t.size))}</td>
      <td>${badge(t.state)}</td>
      <td>
        <span class="pbar"><span class="pbar-fill${t.state === "completed" || t.state === "importing" ? " done" : ""}" style="width:${progress}%"></span></span>
        ${progress}%
      </td>
      <td data-optional>${esc(fmtSpeed(t.speed))}</td>
      <td data-optional>${esc(fmtTime(t.started_at))}</td>
      <td></td>
    `;
    const actionsCell = tr.lastElementChild;
    if (ACTIVE_STATES.has(t.state)) {
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn btn-ghost btn-sm";
      cancelBtn.textContent = "Cancel";
      cancelBtn.addEventListener("click", () => handleCancel(t.transfer_id, cancelBtn));
      actionsCell.appendChild(cancelBtn);
    }
    if (t.state === "failed" || t.state === "cancelled") {
      const retryBtn = document.createElement("button");
      retryBtn.type = "button";
      retryBtn.className = "btn btn-ghost btn-sm";
      retryBtn.textContent = "Retry";
      retryBtn.addEventListener("click", () => handleRetry(t.transfer_id, retryBtn));
      actionsCell.appendChild(retryBtn);
    }
    tbody.appendChild(tr);
  }

  wrap.appendChild(table);
  container.appendChild(wrap);
}

export function renderTransfers() {
  renderChips();
  renderTable();
}

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------

async function handleDeleteFinished(btn, statusLine) {
  const n = state.transfers.list.filter((t) => FINISHED_STATES.has(t.state)).length;
  if (!n) {
    if (statusLine) statusLine.textContent = "No finished transfers to delete";
    return;
  }
  if (!confirmAction(`Permanently delete ${n} finished transfer${n === 1 ? "" : "s"}? This cannot be undone.`)) {
    return;
  }
  btn.disabled = true;
  try {
    const result = await api.deleteFinishedTransfers();
    if (statusLine) statusLine.textContent = `Deleted ${result.deleted_count} finished transfers`;
  } catch (err) {
    if (statusLine) statusLine.textContent = `${err.code || "ERROR"}: ${err.message}`;
  } finally {
    btn.disabled = false;
    await refreshTransfers();
  }
}

export function initTransfersTab() {
  const clearBtn = document.getElementById("btn-clear-finished");
  const refreshBtn = document.getElementById("btn-refresh-transfers");
  const statusLine = document.getElementById("transfers-status");

  if (clearBtn) {
    clearBtn.addEventListener("click", () => handleDeleteFinished(clearBtn, statusLine));
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => refreshTransfers());
  }
}
