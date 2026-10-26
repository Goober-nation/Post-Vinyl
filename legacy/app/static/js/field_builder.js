// field_builder.js — shared settings-form field builder. One source of
// truth for turning a field-schema array into a `.form-grid` of labeled
// inputs, used by both the Config tab's per-section fields (config.js) and
// the Recs tab's own settings form (recs.js) — P6-13.

import { esc } from "./components.js";

export function fieldId(idPrefix, key) {
  return `${idPrefix}-${key}`;
}

/**
 * Build a `.form-grid` div of labeled inputs for `fields`, each ID'd as
 * `${idPrefix}-${field.key}`. Field shape:
 *   { key, label, type: "text"|"number"|"checkbox"|"select", options? }
 * Caller is responsible for clearing/placing the returned element.
 */
export function buildFieldGrid(fields, idPrefix) {
  const grid = document.createElement("div");
  grid.className = "form-grid";

  for (const field of fields) {
    const label = document.createElement("label");
    const id = fieldId(idPrefix, field.key);
    if (field.type === "checkbox") {
      label.className = "checkbox-label";
      label.innerHTML = `<input type="checkbox" id="${id}"> ${esc(field.label)}`;
    } else if (field.type === "select") {
      const opts = field.options.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("");
      label.innerHTML = `${esc(field.label)}<select id="${id}">${opts}</select>`;
    } else {
      // `step` matters for number inputs: without it the browser assumes
      // step=1 and rejects any fractional value on submit, which silently
      // made float settings (search.pass_ratio_threshold) uneditable.
      const attrs = [
        `type="${field.type}"`,
        `id="${id}"`,
        field.step ? `step="${esc(field.step)}"` : "",
        field.min !== undefined ? `min="${esc(field.min)}"` : "",
        field.max !== undefined ? `max="${esc(field.max)}"` : "",
      ]
        .filter(Boolean)
        .join(" ");
      label.innerHTML = `${esc(field.label)}<input ${attrs}>`;
    }
    grid.appendChild(label);
  }

  return grid;
}

/** Build a compact two-column settings table from the same field schema. */
export function buildFieldTable(fields, idPrefix) {
  const table = document.createElement("table");
  table.className = "settings-table";
  const tbody = document.createElement("tbody");

  for (const field of fields) {
    const row = document.createElement("tr");
    const heading = document.createElement("th");
    heading.scope = "row";
    heading.textContent = field.label;
    row.appendChild(heading);

    const cell = document.createElement("td");
    const id = fieldId(idPrefix, field.key);
    if (field.type === "checkbox") {
      const label = document.createElement("label");
      label.className = "table-checkbox";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = id;
      label.append(input, document.createTextNode(" On"));
      cell.appendChild(label);
    } else if (field.type === "select") {
      const select = document.createElement("select");
      select.id = id;
      for (const option of field.options) {
        const optionEl = document.createElement("option");
        optionEl.value = option;
        optionEl.textContent = option;
        select.appendChild(optionEl);
      }
      cell.appendChild(select);
    } else {
      const input = document.createElement("input");
      input.type = field.type;
      input.id = id;
      if (field.step !== undefined) input.step = field.step;
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      cell.appendChild(input);
    }
    if (field.hint) {
      const hint = document.createElement("span");
      hint.className = "field-hint";
      hint.textContent = field.hint;
      cell.appendChild(hint);
    }
    row.appendChild(cell);
    tbody.appendChild(row);
  }

  table.appendChild(tbody);
  return table;
}

/** Read a field's current value from the DOM, typed per field.type. */
export function readFieldValue(field, idPrefix) {
  const el = document.getElementById(fieldId(idPrefix, field.key));
  if (!el) return undefined;
  if (field.type === "checkbox") return el.checked;
  if (field.type === "number") {
    if (el.value === "") return undefined;
    const n = Number(el.value);
    return Number.isNaN(n) ? undefined : n;
  }
  return el.value;
}

/** Populate a field's DOM input from a value, typed per field.type. */
export function setFieldValue(field, idPrefix, value) {
  const el = document.getElementById(fieldId(idPrefix, field.key));
  if (!el) return;
  if (field.type === "checkbox") {
    el.checked = Boolean(value);
  } else {
    el.value = value === null || value === undefined ? "" : value;
  }
}
