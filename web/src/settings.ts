// Settings tab (#125): every field drawn from GET /api/settings/schema, so the
// page can't drift from the code. "Mine" applies at once; "Install" (admins)
// is written to the config file and applies at the next restart. The
// ListenBrainz token sits in the rail. Install settings are shown to
// everyone and refused for non-admins (user, 2026-10-30).
import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import { button, filters, loadPageSize, pageHead, railSection, setting, slider, state, toast } from "./ui";

type Field = Schemas["SchemaField"];
type Me = Schemas["Me"];
type Values = Record<string, unknown>;
type Sources = { order: string[]; off: string[] };

const NAMES: Record<string, string> = {
  musicbrainz: "MusicBrainz",
  deezer: "Deezer",
  listenbrainz: "ListenBrainz",
  slskd: "slskd",
  postradio: "Postradio",
  yandex: "Yandex",
  "": "All",
  cancelled: "Dropped",
  completed: "Done",
  downloading: "Downloading",
  failed: "Failed",
  queued: "Queued",
};
const nice = (v: string) => NAMES[v] ?? v;
const fail = (e: unknown) => toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");

/** "slskd.peer_ban_days" → "Peer ban days" */
const label = (key: string) => {
  const s = key.split(".").pop()!.replaceAll("_", " ");
  return s[0].toUpperCase() + s.slice(1);
};

export function settingsPage(me: Me): HTMLElement {
  const which = new URLSearchParams(location.hash.split("?")[1] ?? "").get("of") === "install" ? "install" : "mine";
  const body = h("div", {}, h("p", { class: "text-muted", role: "status" }, "Loading…"));
  const head = pageHead(
    "Settings",
    filters("Whose settings", [["mine", "Mine"], ["install", "Install"]], which, (v) => (location.hash = `#/settings?of=${v}`)),
  );
  void draw(body, which, me);
  return h("div", {}, h("div", { class: "center-main" }, head, body), h("aside", { class: "side-rail rail" }, listenBrainzRail()));
}

async function draw(body: HTMLElement, which: "mine" | "install", me: Me): Promise<void> {
  if (which === "install" && !me.is_admin) {
    body.replaceChildren(h("div", { role: "alert", class: "alert alert-warning" }, "Admins only."));
    return;
  }
  try {
    const schema = await api<Schemas["Schema"]>("GET", "/api/settings/schema");
    const fields = which === "mine" ? schema.user : schema.install;
    let values: Values;
    let pending: string[] = [];
    if (which === "mine") values = await api<Values>("GET", "/api/me/settings");
    else ({ values, restart_pending: pending } = await api<Schemas["InstallView"]>("GET", "/api/admin/settings"));
    const changes: Values = {};
    const save = button("Save", async () => {
      save.disabled = true;
      try {
        await api("PUT", which === "mine" ? "/api/me/settings" : "/api/admin/settings", { changes } satisfies Schemas["Changes"]);
        toast(which === "mine" ? "Saved. In use now." : "Saved to the config file. In use after the next restart.");
        if (which === "mine") await loadPageSize();
        void draw(body, which, me);
      } catch (e) {
        fail(e); // 422 names the first value that doesn't fit; nothing is stored
        save.disabled = false;
      }
    }, "primary");
    const reset = button("Undo changes", () => void draw(body, which, me));
    const changed = (key: string, v: unknown) => {
      if (JSON.stringify(v) === JSON.stringify(values[key])) delete changes[key];
      else changes[key] = v;
      const n = Object.keys(changes).length;
      save.textContent = n ? `Save ${n} ${n === 1 ? "change" : "changes"}` : "Save";
      save.disabled = reset.disabled = n === 0;
    };
    save.disabled = reset.disabled = true;

    const sections = new Map<string, HTMLElement[]>();
    for (const f0 of fields) {
      // the downloads page size pages every list now, so it reads as one
      const f = f0.key === "downloads.page_size" ? { ...f0, section: "lists", help: "Rows per page in every list." } : f0;
      const control = controlFor(f, values[f.key], (v) => changed(f.key, v));
      const note = pending.includes(f.key) ? h("div", { class: "mt-1" }, state("Changed, waiting for a restart", "wait")) : "";
      const el = setting(label(f.key), f.help, h("div", {}, control, note), which === "install" && f.applies === "restart");
      sections.set(f.section, [...(sections.get(f.section) ?? []), el]);
    }
    body.replaceChildren(
      which === "install" ? h("p", { class: "mb-4 text-muted" }, "Written to the config file (comments kept, a backup made next to it); the running app picks them up at the next restart.") : "",
      ...[...sections].map(([name, els]) => h("section", { class: "mb-8" }, h("h2", { class: "rail-title" }, label(name)), ...els)),
      h("div", { class: "sticky bottom-0 flex justify-end gap-2 border-t border-line bg-base-100 py-3" }, reset, save),
    );
  } catch (e) {
    body.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl."));
  }
}

function controlFor(f: Field, value: unknown, onChange: (v: unknown) => void): HTMLElement {
  if (!f.editable) return h("div", { class: "font-mono text-muted", title: "Matches the Docker mounts: change it there" }, String(value ?? ""));
  if (f.type === "int") return slider(label(f.key), Number(value), f.min ?? 0, f.max ?? 100, f.step ?? 1, onChange);
  if (f.type === "choice") {
    const sel = h("select", { class: "select select-sm w-full", "aria-label": label(f.key) }, ...f.choices.map((c) => h("option", { value: c, selected: c === value }, nice(c))));
    sel.addEventListener("change", () => onChange(sel.value));
    return sel;
  }
  if (f.type === "sources") return orderList(f.key, (value as Sources).order, f.choices, (value as Sources).off, (order, on) => onChange({ order, off: order.filter((c) => !on.has(c)) }));
  if (f.type === "ordered_list") {
    // the install's list: which are used, in order; unused choices go last
    const used = (value as string[]) ?? [];
    const all = [...used, ...f.choices.filter((c) => !used.includes(c))];
    return orderList(f.key, all, f.choices, f.choices.filter((c) => !used.includes(c)), (order, on) => onChange(order.filter((c) => on.has(c))));
  }
  const input = h("input", { class: "input input-sm w-full", value: String(value ?? ""), "aria-label": label(f.key) });
  input.addEventListener("input", () => onChange(input.value));
  return input;
}

/** Sources in the order they're tried, moved with ↑/↓ (keyboard-friendly, no
 *  dragging), each switched on or off. */
function orderList(key: string, order: string[], choices: string[], off: string[], onChange: (order: string[], on: Set<string>) => void): HTMLElement {
  const items = [...order, ...choices.filter((c) => !order.includes(c))];
  const on = new Set(items.filter((c) => !off.includes(c)));
  const list = h("ol", { class: "flex flex-col gap-1", "aria-label": label(key) });
  const draw = () => {
    list.replaceChildren(
      ...items.map((c, i) => {
        const toggle = h("input", { type: "checkbox", class: "toggle toggle-xs toggle-accent", checked: on.has(c), "aria-label": `Use ${nice(c)}` });
        toggle.addEventListener("change", () => {
          toggle.checked ? on.add(c) : on.delete(c);
          onChange([...items], on);
          draw();
        });
        const move = (d: number) => () => {
          [items[i], items[i + d]] = [items[i + d], items[i]];
          onChange([...items], on);
          draw();
        };
        return h(
          "li",
          { class: `flex h-9 items-center gap-2 rounded-[3px] border border-line bg-base-200 px-2 ${on.has(c) ? "" : "text-muted"}` },
          h("span", { class: "w-4 font-mono text-xs text-muted" }, String(i + 1)),
          h("span", { class: "flex-1" }, nice(c)),
          h("button", { type: "button", class: "icon-btn size-7", "aria-label": `Move ${nice(c)} up`, disabled: i === 0, onclick: move(-1) }, "↑"),
          h("button", { type: "button", class: "icon-btn size-7", "aria-label": `Move ${nice(c)} down`, disabled: i === items.length - 1, onclick: move(1) }, "↓"),
          toggle,
        );
      }),
    );
  };
  draw();
  return list;
}

// ── the rail: the user's ListenBrainz token ─────────────────────────────

function listenBrainzRail(): HTMLElement {
  const holder = h("div");
  const drawRail = (c: Schemas["CredentialsView"] | null) => {
    const input = h("input", { class: "input input-sm w-full", type: "password", autocomplete: "off", placeholder: "Paste your token", "aria-label": "ListenBrainz token" });
    const put = async (token: string | null, done: string) => {
      try {
        const r = await api<Schemas["CredentialsView"]>("PUT", "/api/me/credentials", { listenbrainz_token: token } satisfies Schemas["Credentials"]);
        toast(done);
        drawRail(r);
      } catch (e) {
        // 422 ListenBrainz says the token is wrong; 503 it can't check (the old one stays); 409 it's off
        fail(e);
      }
    };
    const form = h(
      "form",
      {
        class: "mt-3 flex flex-col gap-2",
        onsubmit: (e: Event) => {
          e.preventDefault();
          if (input.value.trim()) void put(input.value.trim(), "Token checked with ListenBrainz and stored.");
        },
      },
      input,
      h("button", { type: "submit", class: "btn btn-sm btn-primary" }, c?.listenbrainz ? "Replace token" : "Save token"),
    );
    holder.replaceChildren(
      railSection(
        "ListenBrainz",
        c === null
          ? h("p", { class: "text-muted" }, "Loading…")
          : c.listenbrainz
            ? state(c.listenbrainz_user ? `Token stored for ${c.listenbrainz_user}` : "Token stored", "ok")
            : h("p", { class: "text-muted" }, "No token. With one, renames and tagging can ask ListenBrainz first."),
        form,
        c?.listenbrainz && h("button", { type: "button", class: "btn btn-ghost btn-xs mt-2 w-full text-muted", onclick: () => void put(null, "Token removed.") }, "Remove token"),
        h("p", { class: "mt-3 text-xs text-muted" }, "Stored encrypted and never shown again; Post-Vinyl checks it with ListenBrainz before keeping it."),
      ),
    );
  };
  drawRail(null);
  void api<Schemas["CredentialsView"]>("GET", "/api/me/credentials").then(drawRail, () => drawRail({ listenbrainz: false }));
  return holder;
}
