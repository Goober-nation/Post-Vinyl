// Downloads tab (#123): the user's downloads (admins: everyone's), polled
// while the tab is open, with live progress; a picked download's attempts in
// the rail with next peer / drop / re-request; the queue's pause for admins;
// the bottom bar shows what is downloading now. What is shown lives in the
// hash (#/downloads?state=…&page=…&everyone=1&id=…).
import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import { button, confirm, filters, kv, pageHead, pager, railSection, row, state, toast, type Tone } from "./ui";

const POLL_MS = 1500;
type Item = Schemas["QueueItem"];
type Me = Schemas["Me"];

const STATES: [string, string][] = [
  ["", "All"],
  ["downloading", "Downloading"],
  ["queued", "Queued"],
  ["failed", "Failed"],
  ["completed", "Done"],
  ["cancelled", "Dropped"],
];
const TONE: Record<string, Tone> = { downloading: "live", queued: "wait", completed: "ok", failed: "err", cancelled: "idle" };
const LABEL: Record<string, string> = { downloading: "Downloading", queued: "Queued", completed: "Done", failed: "Failed", cancelled: "Dropped" };

function params(): URLSearchParams {
  return new URLSearchParams(location.hash.split("?")[1] ?? "");
}
function go(change: Record<string, string>): void {
  const p = params();
  for (const [k, v] of Object.entries(change)) v ? p.set(k, v) : p.delete(k);
  location.hash = `#/downloads?${p}`;
}

export function downloadsPage(me: Me): HTMLElement {
  const p = params();
  // no state in the URL: the server uses the user's own default (settings)
  const stateFilter = p.get("state");
  const page = Math.max(1, Number(p.get("page")) || 1);
  const everyone = me.is_admin && p.get("everyone") === "1";
  const selected = Number(p.get("id")) || 0;

  const list = h("div", { class: "rows" }, h("p", { class: "py-2 text-muted", role: "status" }, "Loading…"));
  const pages = h("div");
  const detail = h("div");
  const bar = h("div");
  // without a state in the URL the server applies the user's default filter;
  // read it so the matching filter shows as chosen
  const stateTabs = h("div", { class: "ml-auto" });
  const drawTabs = (current: string) =>
    stateTabs.replaceChildren(filters("State", STATES, current, (v) => go({ state: v || "all", page: "" })));
  if (stateFilter !== null) drawTabs(stateFilter === "all" ? "" : stateFilter);
  else void api<Record<string, unknown>>("GET", "/api/me/settings").then((s) => drawTabs(String(s["downloads.state"] ?? "")), () => drawTabs(""));
  const head = pageHead(
    "Downloads",
    stateTabs,
    h(
      "label",
      { class: "flex items-center gap-2 text-muted" },
      h("input", {
        type: "checkbox",
        class: "toggle toggle-xs",
        checked: everyone,
        onchange: (e: Event) => (me.is_admin ? go({ everyone: (e.target as HTMLInputElement).checked ? "1" : "", page: "" }) : (toast("Admins only.", "err"), ((e.target as HTMLInputElement).checked = false))),
      }),
      "Everyone's",
    ),
  );
  const el = h(
    "div",
    {},
    h("div", { class: "center-main" }, head, list, pages),
    h("aside", { class: "side-rail rail" }, detail, queueRail(me)),
    bar,
  );

  let last = "";
  let polls = 0;
  const poll = async () => {
    // the open details follow too, less often: they ask the peer its queue place
    if (selected && ++polls % 4 === 0 && el.isConnected) void showDetail(detail, selected);
    if (!el.isConnected && last) return; // the tab was left: stop
    const q = new URLSearchParams({ page: String(page) });
    if (stateFilter !== null) q.set("state", stateFilter === "all" ? "" : stateFilter);
    if (everyone) q.set("everyone", "true");
    try {
      // the bar shows what is downloading whatever the filter, so it asks on its own
      const running = new URLSearchParams({ state: "downloading", page_size: "5" });
      if (everyone) running.set("everyone", "true");
      const [r, now] = await Promise.all([
        api<Schemas["QueuePage"]>("GET", `/api/downloads?${q}`),
        api<Schemas["QueuePage"]>("GET", `/api/downloads?${running}`),
      ]);
      const sig = JSON.stringify([r, now]);
      if (sig !== last) {
        // redraw only on change, so hover and focus survive the polling
        last = sig;
        list.replaceChildren(...(r.items.length ? r.items.map((it) => itemRow(it, it.id === selected, everyone)) : [h("p", { class: "py-2 text-muted" }, "No downloads here.")]));
        pages.replaceChildren(pager(page, Math.max(1, Math.ceil(r.total / r.page_size)), `${r.total} ${r.total === 1 ? "download" : "downloads"}`, (n) => go({ page: String(n) })));
        bar.replaceChildren(activityBar(now));
      }
    } catch (e) {
      list.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl."));
    }
    setTimeout(() => void poll(), POLL_MS);
  };
  void poll();
  // on narrower screens the details sit below the list: bring them into view
  if (selected) void showDetail(detail, selected).then(() => innerWidth < 1280 && detail.scrollIntoView({ behavior: "smooth" }));
  return el;
}

// ── the list ────────────────────────────────────────────────────────────

const mb = (b: number) => `${(b / 1e6).toFixed(1)} MB`;
const speed = (bps: number) => (bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} MB/s` : `${Math.round(bps / 1e3)} KB/s`);

function progressText(it: Item): string {
  const p = it.progress;
  if (!p) return "";
  if (p.place_in_queue != null) return p.place_in_queue === 0 ? "next in the peer's queue" : `#${p.place_in_queue} in the peer's queue`;
  if (!p.size) return "";
  if (!p.bytes_transferred && !p.average_speed) return "waiting to start"; // queued or initializing at slskd
  return `${Math.round((100 * p.bytes_transferred) / p.size)}% · ${speed(p.average_speed)}`;
}

function itemRow(it: Item, selected: boolean, everyone: boolean): HTMLElement {
  const title = it.title ?? it.current?.filename.split(/[\\/]/).pop() ?? `Request ${it.request_id}`;
  const r = row({
    title,
    artist: everyone ? `${it.artist ?? "Unknown artist"} · for ${it.username}` : (it.artist ?? "Unknown artist"),
    selected,
    extra: h("span", { class: "sub font-mono" }, progressText(it) || (it.attempts > 1 ? `${it.attempts} peers tried` : "")),
    state: state(LABEL[it.state] ?? it.state, TONE[it.state] ?? "idle"),
    action: h("button", { type: "button", class: "icon-btn", "aria-label": `Details of ${title}`, onclick: () => go({ id: String(it.id) }) }, "›"),
  });
  r.classList.add("cursor-pointer");
  r.addEventListener("click", (e) => {
    if (!(e.target as Element).closest("button")) go({ id: String(it.id) });
  });
  return r;
}


// ── the bottom bar: what is downloading now ─────────────────────────────

function activityBar(running: Schemas["QueuePage"]): HTMLElement | string {
  const now = running.items[0];
  if (!now) return "";
  const p = now.progress;
  const pct = p && p.size ? Math.round((100 * p.bytes_transferred) / p.size) : 0;
  return h(
    "div",
    { class: "activity", "aria-label": "Downloading now" },
    h("span", { class: "state live text-live" }, h("span", { class: "rec", "aria-hidden": "true" })),
    h("span", { class: "min-w-0 truncate" }, h("b", { class: "font-medium" }, now.title ?? "…"), h("span", { class: "text-muted" }, ` · ${now.artist ?? ""}`)),
    h("div", { class: "hidden h-[3px] max-w-90 flex-1 overflow-hidden rounded bg-base-300 md:block", role: "progressbar", "aria-valuenow": String(pct) }, h("span", { class: "block h-full bg-live", style: `width:${pct}%` })),
    h("span", { class: "font-mono text-muted" }, `${pct}%`),
    h("span", { class: "ml-auto hidden text-muted sm:inline" }, `${running.total} downloading`),
  );
}

// ── the rail: one download's attempts, the queue ────────────────────────

async function showDetail(el: HTMLElement, id: number): Promise<void> {
  try {
    const d = await api<Schemas["DownloadDetail"]>("GET", `/api/downloads/${id}`);
    const it = d.item;
    const close = h("button", { type: "button", class: "btn btn-ghost btn-xs -mr-2 ml-auto", "aria-label": "Close details", onclick: () => go({ id: "" }) }, "×");
    const act = (label: string, fn: () => Promise<unknown>, done: string, kind: "primary" | "quiet" | "danger" = "quiet") =>
      button(label, async () => {
        try {
          await fn();
          toast(done);
          void showDetail(el, id);
        } catch (e) {
          toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");
        }
      }, kind);
    const running = it.state === "downloading" || it.state === "queued";
    const ended = it.state === "failed" || it.state === "cancelled";
    el.replaceChildren(
      railSection(
        "Selected",
        h("div", { class: "flex items-start" }, h("div", { class: "min-w-0" }, h("div", { class: "cat" }, `PV-${String(it.request_id).padStart(4, "0")}`), h("div", { class: "title" }, it.title ?? "…"), h("div", { class: "sub" }, it.artist ?? "")), close),
        h("div", { class: "my-3" }, state(LABEL[it.state] ?? it.state, TONE[it.state] ?? "idle")),
        it.current && kv([["Peer", h("span", { class: "font-mono" }, it.current.username)], ["Attempt", `${it.attempts}`], ["Progress", progressText(it) || "—"], ...(it.progress?.size ? [["Size", mb(it.progress.size)] as [string, string]] : [])]),
        h(
          "div",
          { class: "mt-4 flex flex-wrap gap-2" },
          running && act("Next peer", () => api("POST", `/api/downloads/${id}/next-peer`), "Trying the next peer."),
          running &&
            button("Drop", async () => {
              if (!(await confirm(`Drop “${it.title ?? "this download"}”? The running transfer is cancelled.`, "Drop"))) return;
              try {
                await api("POST", `/api/requests/${it.request_id}/drop`);
                toast("Dropped.");
                void showDetail(el, id);
              } catch (e) {
                toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");
              }
            }, "danger"),
          ended && act("Request again", () => api("POST", `/api/requests/${it.request_id}/re-request`), "Requested again.", "primary"),
        ),
      ),
      railSection(
        `Attempts · ${d.attempts.length}`,
        ...d.attempts.map((a) =>
          h(
            "div",
            { class: "mb-2" },
            h("div", { class: "flex items-baseline gap-2" }, h("span", { class: "font-mono" }, a.username), h("span", { class: a.outcome ? "text-muted" : "text-live" }, a.outcome ? a.outcome.replace(/([A-Z])/g, " $1").replaceAll("_", " ").toLowerCase() : "running")),
            h("div", { class: "sub font-mono", title: a.filename }, a.filename.split(/[\\/]/).slice(-2).join("/")),
          ),
        ),
      ),
    );
  } catch (e) {
    el.replaceChildren(h("div", { role: "alert", class: "alert alert-error mb-4" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl."));
  }
}

function queueRail(me: Me): HTMLElement {
  const holder = h("div");
  const draw = (paused: boolean | null) => {
    const btn = button(paused ? "Resume queue" : "Pause queue", async () => {
      // shown to everyone, refused for non-admins (user, 2026-10-30)
      if (!me.is_admin) return toast("Admins only.", "err");
      try {
        const r = await api<Schemas["QueueState"]>("POST", `/api/admin/queue/${paused ? "resume" : "pause"}`);
        toast(r.paused ? "Queue paused. Running downloads carry on." : "Queue resumed. Waiting requests start within a minute.");
        draw(r.paused);
      } catch (e) {
        toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");
      }
    });
    holder.replaceChildren(
      railSection("Queue", paused === null ? "" : h("div", { class: "mb-3" }, paused ? state("Paused", "wait") : state("Running", "ok")), btn),
    );
  };
  draw(null);
  if (me.is_admin) void api<Schemas["QueueState"]>("GET", "/api/admin/queue").then((r) => draw(r.paused), () => {});
  return holder;
}
