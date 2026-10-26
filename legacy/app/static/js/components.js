// components.js — shared UI primitives. Every dynamic value rendered through
// these helpers is escaped; no raw string concatenation of user/server data
// into innerHTML anywhere in this app.

export function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let val = bytes;
  let i = -1;
  do {
    val /= 1024;
    i++;
  } while (val >= 1024 && i < units.length - 1);
  return `${val.toFixed(val >= 10 ? 0 : 1)} ${units[i]}`;
}

export function fmtDur(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.round(seconds);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

export function fmtSpeed(bytesPerSec) {
  if (bytesPerSec === null || bytesPerSec === undefined) return "—";
  if (bytesPerSec < 1024) return `${bytesPerSec} B/s`;
  const kb = bytesPerSec / 1024;
  if (kb < 1024) return `${kb.toFixed(0)} KB/s`;
  return `${(kb / 1024).toFixed(1)} MB/s`;
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

export function fmtUptime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function renderSpinner() {
  return `<span class="spinner" aria-hidden="true"></span>`;
}

export function renderEmpty(text) {
  return `<div class="empty">${esc(text)}</div>`;
}

export function renderError(error) {
  if (!error) return "";
  const code = esc(error.code || "ERROR");
  const message = esc(error.message || "Something went wrong");
  const details =
    error.details && Object.keys(error.details).length
      ? `<div class="error-details">${esc(JSON.stringify(error.details, null, 2))}</div>`
      : "";
  return `<div class="error-panel"><span class="error-code">${code}</span> — ${message}${details}</div>`;
}

export function badge(state) {
  const cls = esc(String(state || "").toLowerCase());
  return `<span class="badge ${cls}">${esc(state)}</span>`;
}

export function chip(label, active, count, onclick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "chip" + (active ? " active" : "");
  btn.textContent = count === undefined || count === null ? label : `${label} (${count})`;
  if (onclick) btn.addEventListener("click", onclick);
  return btn;
}

/**
 * pager({page, pages, onPage}) -> HTMLElement with Prev/Next + "page X of Y".
 */
export function pager({ page, pages, onPage }) {
  const el = document.createElement("div");
  el.className = "pager";

  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "btn btn-ghost btn-sm";
  prev.textContent = "Prev";
  prev.disabled = page <= 1;
  prev.addEventListener("click", () => onPage(page - 1));

  const next = document.createElement("button");
  next.type = "button";
  next.className = "btn btn-ghost btn-sm";
  next.textContent = "Next";
  next.disabled = page >= pages;
  next.addEventListener("click", () => onPage(page + 1));

  const label = document.createElement("span");
  label.textContent = pages <= 1 ? "" : `Page ${page} of ${pages}`;

  el.append(prev, label, next);
  return el;
}

// -----------------------------------------------------------------------
// Toasts
// -----------------------------------------------------------------------

const MAX_TOASTS = 4;

export function showToast(text, variant = "info", durationMs = 5000) {
  const container = document.getElementById("toasts");
  if (!container) return;

  while (container.children.length >= MAX_TOASTS) {
    container.removeChild(container.firstChild);
  }

  const toast = document.createElement("div");
  toast.className = `toast${variant === "error" ? " error" : ""}`;
  toast.textContent = text; // textContent — no HTML injection possible

  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";

  const dismiss = () => {
    toast.classList.add("fade-out");
    setTimeout(() => toast.remove(), 260);
  };
  close.addEventListener("click", dismiss);
  toast.appendChild(close);

  container.appendChild(toast);
  setTimeout(dismiss, durationMs);
}

// -----------------------------------------------------------------------
// Health dot / popover
// -----------------------------------------------------------------------

export function healthDot(status) {
  const cls =
    status === "up" ? "up" : status === "disabled" || status === "unknown" ? "disabled" : "down";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `dot ${cls}`;
  btn.title = `${status}`;
  return btn;
}

export function buildHealthPopover(services, version, uptime) {
  const rows = Object.entries(services || {})
    .map(
      ([name, s]) => `
        <dt>${esc(name)}</dt>
        <dd>${esc(s.status === "unknown" ? "not checked" : s.status)} · ${s.latency_ms !== null && s.latency_ms !== undefined ? esc(s.latency_ms) + "ms" : "—"}${s.error ? " · " + esc(s.error) : ""}</dd>
      `
    )
    .join("");
  const slskdDown = services && services.slskd && services.slskd.status === "down";
  const reconnectBtn = slskdDown
    ? `<button type="button" class="btn btn-sm" id="slskd-reconnect-btn">Reconnect slskd</button>`
    : "";
  return `
    <div class="health-popover" id="health-popover">
      <h3>Service health</h3>
      <dl>${rows}</dl>
      ${reconnectBtn}
      <div class="meta-line">v${esc(version)} · uptime ${esc(fmtUptime(uptime))}</div>
    </div>
  `;
}

export function confirmAction(message) {
  return window.confirm(message);
}
