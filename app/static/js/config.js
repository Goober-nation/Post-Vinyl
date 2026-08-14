// config.js — Config tab: settings form (hot-reload/restart-tiered), secrets
// management, and a compact system status readout.

import * as api from "./api.js";
import { state, render } from "./state.js";
import { esc, fmtUptime, showToast, confirmAction } from "./components.js";
import { buildFieldGrid, fieldId as sharedFieldId, readFieldValue as readSharedFieldValue } from "./field_builder.js";

// Sections not in the backend's HOT_SECTIONS set (app/routes/config.py)
// take effect only after a restart — mirrored here so the form can label
// them before the user saves; the actual `requires_restart` list in the
// POST response is the source of truth for what happened.
const RESTART_SECTIONS = new Set(["paths", "navidrome", "slskd", "musicbrainz"]);

// Hot-reload sections first (take effect immediately via POST /api/config),
// restart-required sections after — reordered + visually separated per
// P6-13 so the distinction is obvious before the user saves.
const SECTIONS = [
  {
    key: "search",
    title: "Search",
    fields: [
      { key: "wait_seconds", label: "Wait seconds", type: "number" },
      { key: "response_threshold", label: "Response threshold", type: "number" },
      { key: "response_cap", label: "Response cap", type: "number", min: "1" },
      { key: "min_wait_seconds", label: "Min wait seconds", type: "number" },
      { key: "pass_ratio_threshold", label: "Pass ratio threshold", type: "number", step: "0.05", min: "0.05", max: "1" },
      { key: "artist_match_min_words", label: "Artist words must match", type: "number", min: "1" },
    ],
  },
  {
    key: "download",
    title: "Download",
    fields: [
      { key: "check_interval", label: "Check interval", type: "number" },
      { key: "max_retries_per_track", label: "Max retries / track", type: "number" },
      { key: "bad_peer_threshold", label: "Bad peer threshold", type: "number" },
      { key: "pending_timeout_minutes", label: "Pending timeout (minutes)", type: "number" },
      { key: "orphan_grace_polls", label: "Orphan grace (polls)", type: "number" },
      { key: "manual_gate_minutes", label: "Manual gate (minutes)", type: "number" },
      { key: "history_clear_interval_minutes", label: "History clear interval (minutes)", type: "number", min: "0" },
    ],
  },
  {
    key: "sync",
    title: "Sync",
    fields: [{ key: "interval_hours", label: "Interval (hours)", type: "number" }],
  },
  {
    key: "logging",
    title: "Logging",
    fields: [
      {
        key: "level",
        label: "Level",
        type: "select",
        options: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
      },
    ],
  },
  {
    key: "musicbrainz",
    title: "MusicBrainz",
    fields: [
      { key: "enabled", label: "Enabled", type: "checkbox" },
      { key: "min_score", label: "Min score", type: "number" },
      { key: "min_request_interval", label: "Min request interval (seconds)", type: "number", step: "0.1" },
      { key: "timeout_seconds", label: "Timeout (seconds)", type: "number" },
      { key: "cache_ttl_seconds", label: "Cache TTL (seconds)", type: "number" },
      { key: "search_official_only", label: "Official releases only", type: "checkbox" },
    ],
  },
  {
    key: "paths",
    title: "Paths",
    // music_dir is intentionally not a field here — it's the Docker mount
    // root; editing it from the frontend would break the container's
    // volumes. It's shown as read-only text instead (see buildSettingsFields).
    // The rest are relative suffixes joined under music_dir server-side.
    fields: [
      { key: "download_dir", label: "Download dir", type: "text" },
      { key: "searches_dir", label: "Searches dir", type: "text" },
      { key: "library_dir", label: "Library dir", type: "text" },
      { key: "discovery_familiar_dir", label: "Comfort Zone folder", type: "text" },
      { key: "discovery_new_releases_dir", label: "Fresh Picks folder", type: "text" },
      { key: "discovery_exploration_dir", label: "Deep Cuts folder", type: "text" },
    ],
  },
  {
    key: "navidrome",
    title: "Navidrome",
    fields: [{ key: "url", label: "URL", type: "text" }],
  },
  {
    key: "slskd",
    title: "slskd",
    fields: [{ key: "url", label: "URL", type: "text" }],
  },
  // No ListenBrainz section: its URL stays a fixed default in config.toml,
  // and it's enabled automatically once a username + token are saved in
  // the Secrets panel below (restart required, same as other secrets).
];

const SECRET_FIELDS = [
  { key: "navidrome_username", label: "Navidrome username", type: "text" },
  { key: "navidrome_password", label: "Navidrome password", type: "password" },
  { key: "slskd_api_key", label: "slskd API key", type: "password" },
  { key: "listenbrainz_token", label: "ListenBrainz token", type: "password" },
  { key: "listenbrainz_username", label: "ListenBrainz username", type: "text" },
];

function sectionPrefix(sectionKey) {
  return `cfg-${sectionKey}`;
}

function fieldId(sectionKey, fieldKey) {
  return sharedFieldId(sectionPrefix(sectionKey), fieldKey);
}

// -----------------------------------------------------------------------
// Data refresh
// -----------------------------------------------------------------------

export async function refreshConfig({ resync = false } = {}) {
  try {
    const config = await api.getConfig();
    state.config.data = config;
    state.config.error = null;
    if (resync) {
      // Force populateSettingsForm() to re-run on next render — used when
      // an external change (e.g. the Recs tab's own settings form) just
      // saved, so this tab's form reflects it without a page reload.
      const form = document.getElementById("config-settings-form");
      if (form) form.dataset.populated = "";
    }
  } catch (err) {
    state.config.error = err;
  }
  render();
}

// -----------------------------------------------------------------------
// Settings form
// -----------------------------------------------------------------------

function populateSettingsForm() {
  const config = state.config.data;
  if (!config) return;
  const musicDirNote = document.getElementById("config-music-dir-note");
  if (musicDirNote) {
    const musicDir = config.paths && config.paths.music_dir;
    musicDirNote.textContent = musicDir
      ? `Music dir (Docker mount, not editable here): ${musicDir}`
      : "";
  }
  renderResolvedCategoryPaths(config);
  for (const section of SECTIONS) {
    const sectionData = config[section.key] || {};
    for (const field of section.fields) {
      const el = document.getElementById(fieldId(section.key, field.key));
      if (!el) continue;
      const value = sectionData[field.key];
      if (field.type === "checkbox") {
        el.checked = Boolean(value);
      } else {
        el.value = value === null || value === undefined ? "" : value;
      }
    }
  }
}

function renderResolvedCategoryPaths(config) {
  const note = document.getElementById("config-category-paths-note");
  if (!note) return;
  const paths = config.paths || {};
  const rows = [
    ["Comfort Zone", paths.discovery_familiar_path],
    ["Fresh Picks", paths.discovery_new_releases_path],
    ["Deep Cuts", paths.discovery_exploration_path],
  ]
    .filter(([, path]) => path)
    .map(([label, path]) => `<div><span>${esc(label)}</span><code>${esc(path)}</code></div>`)
    .join("");
  note.innerHTML = rows
    ? `<strong>Resolved category folders</strong>${rows}`
    : "";
}

function valuesEqual(a, b) {
  if (typeof a === "number" || typeof b === "number") return Number(a) === Number(b);
  return String(a ?? "") === String(b ?? "");
}

async function handleSaveSettings(form) {
  const statusLine = document.getElementById("config-settings-status");
  const config = state.config.data;
  if (!config) return;

  const payload = {};
  for (const section of SECTIONS) {
    const sectionData = config[section.key] || {};
    const changed = {};
    for (const field of section.fields) {
      const value = readSharedFieldValue(field, sectionPrefix(section.key));
      if (value === undefined) continue;
      if (!valuesEqual(value, sectionData[field.key])) {
        changed[field.key] = value;
      }
    }
    if (Object.keys(changed).length) payload[section.key] = changed;
  }

  if (!Object.keys(payload).length) {
    if (statusLine) statusLine.textContent = "No changes to save";
    return;
  }

  const btn = form.querySelector("#btn-config-save");
  if (btn) btn.disabled = true;
  try {
    const result = await api.updateConfig(payload);
    state.config.data = result.config;
    populateSettingsForm();
    if (result.requires_restart && result.requires_restart.length) {
      showToast(`Saved. Restart required for: ${result.requires_restart.join(", ")}`, "info");
    } else {
      showToast("Settings saved");
    }
    if (statusLine) statusLine.textContent = "";
  } catch (err) {
    if (statusLine) statusLine.textContent = `${err.code || "ERROR"}: ${err.message}`;
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// -----------------------------------------------------------------------
// Secrets form
// -----------------------------------------------------------------------

async function handleSaveSecrets(form) {
  const statusLine = document.getElementById("config-secrets-status");
  const payload = {};
  for (const field of SECRET_FIELDS) {
    const el = document.getElementById(`secret-${field.key}`);
    if (!el) continue;
    const value = el.value.trim();
    if (value) payload[field.key] = value;
  }

  if (!Object.keys(payload).length) {
    if (statusLine) statusLine.textContent = "No secrets entered";
    return;
  }

  const btn = form.querySelector("#btn-config-secrets-save");
  if (btn) btn.disabled = true;
  try {
    const result = await api.updateSecrets(payload);
    form.reset();
    showToast(`Updated: ${result.updated.join(", ")} — restart required to take effect`, "info");
    if (statusLine) statusLine.textContent = "";
  } catch (err) {
    if (statusLine) statusLine.textContent = `${err.code || "ERROR"}: ${err.message}`;
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// -----------------------------------------------------------------------
// Render
// -----------------------------------------------------------------------

function renderSystemStatus() {
  const container = document.getElementById("config-system-status");
  if (!container) return;

  const { services, version, uptime, restartAvailable } = state.system;
  const rows = Object.entries(services || {})
    .map(
      ([name, s]) => `
        <tr>
          <td>${esc(name)}</td>
          <td>${badgeText(s.status)}</td>
          <td>${esc(s.error || "—")}</td>
        </tr>
      `
    )
    .join("");

  const slskdDown = services && services.slskd && services.slskd.status === "down";
  const reconnectBtn = slskdDown
    ? `<button type="button" class="btn btn-sm" id="config-slskd-reconnect-btn">Reconnect slskd</button>`
    : "";

  const lbStatus = services && services.listenbrainz && services.listenbrainz.status;
  const lbCheckBtn =
    lbStatus && lbStatus !== "disabled"
      ? `<button type="button" class="btn btn-sm" id="config-listenbrainz-check-btn">Check ListenBrainz connection</button>`
      : "";

  container.innerHTML = `
    <div class="table-scroll">
      <table>
        <thead><tr><th>Service</th><th>Status</th><th data-optional>Error</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="3">No data yet</td></tr>`}</tbody>
      </table>
    </div>
    ${reconnectBtn}
    ${lbCheckBtn}
    <div class="meta-line">Musica v${esc(version || "—")} · uptime ${esc(fmtUptime(uptime))}</div>
    <div class="danger-zone">
      <div class="danger-zone-label">Break glass</div>
      <div class="row">
        <button type="button" class="btn btn-sm btn-danger" id="config-stop-slskd-btn">Stop all slskd activity</button>
        <button type="button" class="btn btn-sm btn-danger" id="config-abort-recs-btn">Stop all rec activity</button>
      </div>
      <div class="hint">Stop slskd activity cancels every search and transfer, any origin. Stop rec activity aborts an in-progress pull and cancels queued rec downloads.</div>
    </div>
    <div class="danger-zone">
      <div class="danger-zone-label">Maintenance</div>
      <div class="row">
        <button type="button" class="btn btn-sm" id="config-sync-now-btn">Sync now</button>
        <button type="button" class="btn btn-sm" id="config-unblock-peers-btn">Unblock all peers</button>
        <button type="button" class="btn btn-sm" id="config-rerun-setup-btn">Re-run setup</button>
        ${
          restartAvailable
            ? `<button type="button" class="btn btn-sm btn-danger" id="config-restart-app-btn">Restart app</button>`
            : ""
        }
      </div>
      <div class="hint">Sync now sends pending favorites and trash feedback, applies star ratings, and permanently removes files in Navidrome's Trash playlist. Unblock all peers clears the ban list (e.g. after a burst of connection-caused failures banned peers that weren't actually at fault) — peers still misbehaving just get re-banned on their next real failure.</div>
      ${
        restartAvailable
          ? `<div class="hint">Restart app applies restart-required settings and changed .env secrets, with a few seconds of downtime while Docker relaunches it.</div>`
          : `<div class="hint">To apply restart-required settings, stop the app (Ctrl+C in its terminal) and run <code>python -m app.main</code> again.</div>`
      }
    </div>
  `;
}

function badgeText(status) {
  const cls =
    status === "up" ? "completed" : status === "disabled" ? "" : status === "unknown" ? "queued" : "failed";
  const label = status === "unknown" ? "not checked" : status;
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

export function renderConfig() {
  renderSystemStatus();

  const form = document.getElementById("config-settings-form");
  if (form && state.config.data && !form.dataset.populated) {
    populateSettingsForm();
    form.dataset.populated = "true";
  }

  const errEl = document.getElementById("config-settings-status");
  if (errEl && state.config.error) {
    errEl.textContent = `${state.config.error.code || "ERROR"}: ${state.config.error.message}`;
  }
}

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------

function buildSettingsFields() {
  const container = document.getElementById("config-settings-fields");
  if (!container) return;
  container.innerHTML = "";

  // Two islands, each carrying a single label for the whole group instead
  // of a tag repeated on every section — P6-13 follow-up.
  const hotIsland = document.createElement("div");
  hotIsland.className = "config-island config-island-hot";
  hotIsland.innerHTML = `<div class="config-island-label">Applies on save</div>`;

  const restartIsland = document.createElement("div");
  restartIsland.className = "config-island config-island-restart";
  restartIsland.innerHTML = `<div class="config-island-label">Restart required</div>`;

  for (const section of SECTIONS) {
    const isRestart = RESTART_SECTIONS.has(section.key);

    const fieldset = document.createElement("fieldset");
    fieldset.className = "config-section";
    const legend = document.createElement("legend");
    legend.textContent = section.title;
    fieldset.appendChild(legend);
    if (section.key === "paths") {
      const musicDirNote = document.createElement("div");
      musicDirNote.className = "hint";
      musicDirNote.id = "config-music-dir-note";
      fieldset.appendChild(musicDirNote);
    }
    fieldset.appendChild(buildFieldGrid(section.fields, sectionPrefix(section.key)));
    if (section.key === "paths") {
      const categoryPathsNote = document.createElement("div");
      categoryPathsNote.className = "resolved-paths";
      categoryPathsNote.id = "config-category-paths-note";
      fieldset.appendChild(categoryPathsNote);
    }

    (isRestart ? restartIsland : hotIsland).appendChild(fieldset);
  }

  container.appendChild(hotIsland);
  container.appendChild(restartIsland);
}

function buildSecretsFields() {
  const container = document.getElementById("config-secrets-fields");
  if (!container) return;
  container.innerHTML = "";

  const grid = document.createElement("div");
  grid.className = "form-grid";
  for (const field of SECRET_FIELDS) {
    const label = document.createElement("label");
    label.innerHTML = `${esc(field.label)}<input type="${field.type}" id="secret-${field.key}" autocomplete="off" placeholder="Leave blank to keep unchanged">`;
    grid.appendChild(label);
  }
  container.appendChild(grid);
}

export function initConfigTab() {
  buildSettingsFields();
  buildSecretsFields();

  const settingsForm = document.getElementById("config-settings-form");
  if (settingsForm) {
    settingsForm.addEventListener("submit", (e) => {
      e.preventDefault();
      handleSaveSettings(settingsForm);
    });
  }

  const secretsForm = document.getElementById("config-secrets-form");
  if (secretsForm) {
    secretsForm.addEventListener("submit", (e) => {
      e.preventDefault();
      handleSaveSecrets(secretsForm);
    });
  }
}
