// Shared components (#120). Plain DOM built with h(); classes in style.css.
import { h } from "./dom";

type Child = Node | string | null | false | undefined;

/** Main column plus the right rail (below it on phones). */
export function page(main: Child[], rail: Child[] = []): HTMLElement {
  return h("div", { class: "page" }, h("div", {}, ...main), h("aside", { class: "rail" }, ...rail));
}

export function pageHead(title: string, ...rest: Child[]): HTMLElement {
  return h("div", { class: "page-head" }, h("h1", {}, title), ...rest);
}

export function railSection(title: string, ...children: Child[]): HTMLElement {
  return h("section", {}, h("h2", { class: "rail-title" }, title), ...children);
}

/** Label/value pairs, e.g. queue stats. */
export function kv(pairs: [string, Child][]): HTMLElement {
  return h("dl", { class: "kv" }, ...pairs.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

/** Underlined text filters; `onPick` gets the chosen value. */
export function filters(
  label: string,
  options: [value: string, text: string][],
  current: string,
  onPick: (value: string) => void,
): HTMLElement {
  return h(
    "div",
    { class: "filters", role: "group", "aria-label": label },
    ...options.map(([value, text]) =>
      h("button", { type: "button", "aria-pressed": String(value === current), onclick: () => onPick(value) }, text),
    ),
  );
}

// live = happening now (warm, a turning record); wait = queued (hollow dot);
// accent = picked by the user (in the cart).
export type Tone = "ok" | "wait" | "err" | "live" | "idle" | "accent";
const TONE: Record<Tone, string> = {
  ok: "text-success",
  wait: "wait text-muted",
  err: "text-error",
  live: "live text-live",
  idle: "text-muted",
  accent: "text-accent",
};

/** A state: its mark plus its words, never colour alone. */
export function state(text: string, tone: Tone): HTMLElement {
  return h("span", { class: `state ${TONE[tone]}` }, tone === "live" && h("span", { class: "rec", "aria-hidden": "true" }), text);
}

/** Small outlined label, e.g. a matcher mark. */
export function tag(text: string, tone?: Tone): HTMLElement {
  return h("span", { class: tone ? `tag ${TONE[tone]}` : "tag" }, text);
}

/** The cover's colour, or initials until covers come from Navidrome. */
export function cover(title: string, art?: string): HTMLElement {
  if (art) return h("span", { class: "cover", style: `--art:${art}`, "aria-hidden": "true" });
  const words = title.replace(/[^\p{L}\p{N}\s]/gu, "").split(/\s+/).filter(Boolean);
  const initials = words.map((w) => w[0]).join("").slice(0, 2);
  return h("span", { class: "cover", "aria-hidden": "true" }, initials.toUpperCase());
}

/** Transfer progress as words: "62% · 1.2 MB/s". */
export function progress(fraction: number, speed = ""): HTMLElement {
  const pct = `${Math.round(fraction * 100)}%`;
  return h("span", { class: "font-mono text-muted" }, speed ? `${pct} · ${speed}` : pct);
}

/** A song row, written like a sleeve: side/track before the title, the artist
 *  as a small-caps credit, then the album. */
export function row(opts: {
  title: string;
  artist: string;
  album?: string;
  side?: string;
  art?: string;
  selected?: boolean;
  extra?: Child;
  state?: Child;
  action?: Child;
}): HTMLElement {
  return h(
    "div",
    { class: opts.selected ? "row selected" : "row", style: opts.art ? `--art:${opts.art}` : false },
    cover(opts.title, opts.art),
    h(
      "div",
      { class: "min-w-0" },
      h("div", { class: "title" }, opts.side && h("span", { class: "side" }, opts.side), opts.title),
      h("div", { class: "sub" }, h("span", { class: "credit" }, opts.artist), opts.album && ` · ${opts.album}`),
    ),
    h("div", { class: "row-extra min-w-0" }, opts.extra),
    opts.state ?? h("span"),
    opts.action ?? h("span"),
  );
}

export function iconButton(label: string, glyph: string, onclick: () => void): HTMLElement {
  return h("button", { type: "button", class: "icon-btn", "aria-label": label, title: label, onclick }, glyph);
}

export function button(text: string, onclick: () => void, kind: "primary" | "quiet" | "danger" = "quiet"): HTMLButtonElement {
  const cls = { primary: "btn btn-primary btn-sm", quiet: "btn btn-outline btn-sm border-line", danger: "btn btn-outline btn-sm border-line text-error" };
  return h("button", { type: "button", class: cls[kind], onclick }, text);
}

/** A slider that shows its value next to it, updated while dragging. */
export function slider(label: string, value: number, min: number, max: number, step = 1, onChange?: (value: number) => void): HTMLElement {
  const out = h("output", { class: "w-10 text-right font-mono" }, String(value));
  const input = h("input", {
    type: "range",
    class: "range range-xs range-accent flex-1",
    min: String(min),
    max: String(max),
    step: String(step),
    value: String(value),
    "aria-label": label,
    oninput: () => (out.textContent = input.value),
    onchange: () => onChange?.(Number(input.value)),
  });
  return h("div", { class: "flex items-center gap-3" }, input, out);
}

/** Setting row: label and help on the left, the control on the right. */
export function setting(label: string, help: string, control: Node, restart = false): HTMLElement {
  return h(
    "div",
    { class: "setting" },
    h(
      "div",
      {},
      h("div", {}, label),
      h("div", { class: "text-muted" }, help),
      restart && h("div", { class: "text-xs text-muted italic" }, "Applies at the next restart"),
    ),
    control,
  );
}

/** Ask before something that can't be undone; resolves true on confirm. */
export function confirm(question: string, action: string): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = h(
      "dialog",
      { class: "modal" },
      h(
        "div",
        { class: "modal-box bg-base-200 border border-line" },
        h("p", {}, question),
        h(
          "div",
          { class: "modal-action" },
          button("Cancel", () => dialog.close()),
          button(action, () => ((ok = true), dialog.close()), "primary"),
        ),
      ),
    );
    let ok = false;
    dialog.addEventListener("close", () => (dialog.remove(), resolve(ok)));
    document.body.append(dialog);
    dialog.showModal();
  });
}

const toasts = h("div", { class: "toast toast-end bottom-16 z-20", role: "status", "aria-live": "polite" });
/** Short message in the corner; errors stay until clicked. */
export function toast(text: string, tone: "ok" | "err" = "ok"): void {
  if (!toasts.isConnected) document.body.append(toasts);
  const el = h(
    "div",
    { class: `alert ${tone === "err" ? "alert-error" : "bg-base-300 border-line"} cursor-pointer`, onclick: () => el.remove() },
    text,
  );
  toasts.append(el);
  if (tone === "ok") setTimeout(() => el.remove(), 4000);
}
