// slskd tab (#122): a typed Soulseek search, polled until slskd says it is
// complete, then every file grouped by peer with the matcher's marks; checked
// files are queued as exactly those files (one request + song each). The
// search shown lives in the hash (#/slskd?id=<request id>).
import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import { button, filters, iconButton, pageHead, railSection, state, tag, toast } from "./ui";

const POLL_MS = 1500; // polling while the tab is open (user, 2026-10-30)
const PEERS_SHOWN = 40; // a popular search answers hundreds of peers; more on demand

type Pick = Schemas["PickedFile"];

export function slskdPage(): HTMLElement {
  const id = Number(new URLSearchParams(location.hash.split("?")[1] ?? "").get("id")) || 0;
  const picks: Pick[] = [];
  const picked = h("div");
  const rail = h("aside", { class: "side-rail rail" }, picked, recentRail());
  const drawRail = () => picked.replaceChildren(picksRail(id, picks, drawRail));
  const main = h("div", { class: "center-main" }, searchForm(id), id ? results(id, picks, drawRail) : "");
  drawRail();
  return h("div", {}, main, rail);
}

// ── the search ──────────────────────────────────────────────────────────

function searchForm(id: number): HTMLElement {
  const q = h("input", {
    class: "input input-lg w-full",
    name: "q",
    type: "search",
    placeholder: "Artist and title, as you'd type it in Soulseek",
    "aria-label": "Soulseek search",
    required: true,
  });
  const go = h("button", { class: "btn btn-primary btn-lg", type: "submit" }, "Search");
  const form = h(
    "form",
    {
      class: "flex gap-2",
      role: "search",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        go.disabled = true;
        try {
          const r = await api<Schemas["RequestOut"]>("POST", "/api/slskd/searches", { query: q.value.trim() } satisfies Schemas["NewSearch"]);
          location.hash = `#/slskd?id=${r.id}`;
        } catch (err) {
          toast(err instanceof ApiError ? err.message : "Can't reach Post-Vinyl.", "err");
          go.disabled = false;
        }
      },
    },
    q,
    go,
  );
  // like Search: before a search the title sits over the bar, 30% down
  return id
    ? h("div", { class: "mb-6" }, pageHead("slskd"), form)
    : h("div", { class: "pt-[calc(30vh-8rem)] text-center" }, h("h1", { class: "mb-4" }, "slskd"), form);
}

// ── results: polled until complete ──────────────────────────────────────

function results(id: number, picks: Pick[], changed: () => void): HTMLElement {
  const status = h("div", { class: "mb-3 flex flex-wrap items-baseline gap-x-4 gap-y-1", role: "status" });
  const body = h("div");
  const el = h("section", {}, status, body);
  let showAll = false;
  let latest: Schemas["SearchView"] | undefined;

  const draw = () => {
    if (!latest) return;
    const peers = [...latest.peers].sort((a, b) => Number(b.has_free_slot) - Number(a.has_free_slot) || b.upload_speed - a.upload_speed);
    const hidden = peers.reduce((n, p) => n + p.files.filter((f) => !pickable(f)).length, 0);
    const shown = peers.map((p) => ({ ...p, files: showAll ? p.files : p.files.filter(pickable) })).filter((p) => p.files.length);
    status.replaceChildren(
      state("Complete", "ok"),
      // counts of what is listed, so they change with the Usable / All files switch
      h("span", { class: "text-muted" }, `${shown.length} peers · ${shown.reduce((n, p) => n + p.files.length, 0)} files`),
      hidden === 0
        ? ""
        : filters("Which files", [["matches", "Usable"], ["all", `All files (${hidden} marked)`]], showAll ? "all" : "matches", (v) => {
          showAll = v === "all";
          draw();
        }),
    );
    if (!shown.length) {
      body.replaceChildren(h("p", { class: "py-2 text-muted" }, latest.file_count ? "Only marked files: see All files." : "No peer answered."));
      return;
    }
    const list = h("div", {}, ...shown.slice(0, PEERS_SHOWN).map((p) => peerBlock(p, picks, changed)));
    const more =
      shown.length > PEERS_SHOWN &&
      h("button", { type: "button", class: "btn btn-ghost btn-sm mt-2 text-muted", onclick: () => (list.append(...shown.slice(PEERS_SHOWN).map((p) => peerBlock(p, picks, changed))), more && more.remove()) }, `Show all ${shown.length} peers`);
    body.replaceChildren(list, more || "");
  };

  const poll = async () => {
    if (latest && !el.isConnected) return; // the tab was left: stop
    try {
      const all = await api<Schemas["SearchView"][]>("GET", `/api/slskd/searches/${id}`);
      latest = all[0]; // newest first
      const box = document.querySelector<HTMLInputElement>('input[name="q"]');
      if (latest && box && !box.value) box.value = latest.query; // the search shown, ready to tweak
      if (!latest) {
        status.replaceChildren(state("Waiting to search", "wait"));
      } else if (!latest.is_complete) {
        status.replaceChildren(state("Searching", "live"), h("span", { class: "font-mono text-muted" }, `${latest.response_count} peers · ${latest.file_count} files so far`));
      } else {
        draw();
        return;
      }
      setTimeout(() => void poll(), POLL_MS);
    } catch (e) {
      status.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl."));
    }
  };
  status.replaceChildren(state("Loading", "idle"));
  void poll();
  return el;
}

// excluded versions, locked files and non-audio can't be picked
const pickable = (f: Schemas["FileView"]) => !f.label || f.label === "correct";

function peerBlock(p: Schemas["PeerView"], picks: Pick[], changed: () => void): HTMLElement {
  return h(
    "div",
    { class: "peer" },
    h(
      "div",
      { class: "peer-head" },
      h("span", { class: "font-mono text-base-content" }, p.username),
      p.has_free_slot ? state("free slot", "ok") : h("span", {}, `queue ${p.queue_length}`),
      h("span", { class: "font-mono" }, speed(p.upload_speed)),
    ),
    ...p.files.map((f) => fileRow(p.username, f, picks, changed)),
  );
}

function fileRow(username: string, f: Schemas["FileView"], picks: Pick[], changed: () => void): HTMLElement {
  const ok = pickable(f);
  const at = () => picks.findIndex((x) => x.username === username && x.filename === f.filename);
  const box = h("input", {
    type: "checkbox",
    class: "checkbox checkbox-sm checkbox-accent",
    disabled: !ok,
    checked: at() >= 0,
    "aria-label": `${f.filename.split(/[\\/]/).pop()} from ${username}`,
  });
  box.addEventListener("change", () => {
    if (box.checked) picks.push({ username, filename: f.filename });
    else picks.splice(at(), 1);
    changed();
  });
  const parts = f.filename.split(/[\\/]/);
  const shortPath = parts.slice(-2).join("/"); // album folder + file; the full peer path on hover
  return h(
    "label",
    { class: ok ? "file" : "file text-muted" },
    box,
    h("span", { class: "path", title: f.filename }, shortPath),
    h("span", { class: "hidden md:block" }, format(f)),
    h("span", { class: "hidden font-mono md:block" }, `${(f.size / 1e6).toFixed(1)} MB${f.length ? ` · ${length(f.length)}` : ""}`),
    f.label ? tag(markText(f.label), f.label === "correct" ? "ok" : undefined) : h("span"),
  );
}

const markText = (label: string) => (label.startsWith("excluded:") ? label.slice(9) : label.replace("_", " "));
const length = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const speed = (bps: number) => (bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} MB/s` : `${Math.round(bps / 1e3)} KB/s`);

function format(f: Schemas["FileView"]): string {
  const ext = (f.filename.match(/\.(\w+)$/)?.[1] ?? "").toUpperCase();
  if (f.bit_depth && f.sample_rate) return `${ext} ${f.bit_depth}/${Math.round(f.sample_rate / 1000)}`;
  return f.bitrate ? `${ext} ${f.bitrate}` : ext;
}

// ── the rail: picked files, recent searches ─────────────────────────────

function picksRail(id: number, picks: Pick[], changed: () => void): HTMLElement {
  if (!id || !picks.length) return h("div");
  const send = button(picks.length === 1 ? "Queue 1 file" : `Queue ${picks.length} files`, async () => {
    send.disabled = true;
    try {
      const made = await api<Schemas["RequestOut"][]>("POST", "/api/slskd/downloads", { request_id: id, files: picks } satisfies Schemas["Picks"]);
      toast(`Queued ${made.length} ${made.length === 1 ? "file" : "files"}.`);
      picks.splice(0);
      document.querySelectorAll<HTMLInputElement>(".file input:checked").forEach((b) => (b.checked = false));
      changed();
    } catch (e) {
      // 409 the queue is paused, 404 a file isn't in this search's results
      toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");
      send.disabled = false;
    }
  }, "primary");
  send.classList.add("mt-2", "w-full");
  // phones: the rail sits below the results, so a bar keeps the picks in reach
  const holder = h("div");
  const bar = h(
    "div",
    { class: "activity xl:hidden" },
    h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => holder.scrollIntoView({ behavior: "smooth" }) }, `To download · ${picks.length}`),
    h("span", { class: "flex-1" }),
    button(picks.length === 1 ? "Queue 1 file" : `Queue ${picks.length} files`, () => send.click(), "primary"),
  );
  holder.append(bar);
  holder.append(railSection(
    `To download · ${picks.length}`,
    ...picks.map((p, i) =>
      h(
        "div",
        { class: "mb-2 flex items-start gap-2" },
        h("div", { class: "min-w-0 flex-1" }, h("div", { class: "title", title: p.filename }, p.filename.split(/[\\/]/).pop() ?? p.filename), h("div", { class: "sub font-mono" }, p.username)),
        iconButton(`Remove ${p.filename}`, "×", () => {
          picks.splice(i, 1);
          document.querySelectorAll<HTMLInputElement>(".file input:checked").forEach((b) => {
            const label = b.closest(".file")?.querySelector(".path")?.getAttribute("title");
            if (label === p.filename) b.checked = false;
          });
          changed();
        }),
      ),
    ),
    send,
  ));
  return holder;
}

function recentRail(): HTMLElement {
  const list = h("div", { class: "flex flex-col gap-1" });
  void api<Schemas["TypedSearchOut"][]>("GET", "/api/slskd/searches?limit=12").then(
    (recent) =>
      list.replaceChildren(
        ...recent.map((t) =>
          h(
            "a",
            { href: `#/slskd?id=${t.request.id}`, class: "flex items-baseline justify-between gap-2 truncate text-muted hover:text-base-content" },
            h("span", { class: "truncate" }, t.request.query ?? `#${t.request.id}`),
            t.latest?.is_complete && h("span", { class: "font-mono text-xs", title: "files found" }, String(t.latest.file_count)),
          ),
        ),
      ),
    () => list.replaceChildren(),
  );
  return railSection("Recent searches", list);
}
