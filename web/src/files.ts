// Files tab (#124): the whole library as Navidrome indexes it, searchable and
// paged; a picked song in the rail with rename (look the new names up →
// apply the suggested tags, or strip them when nothing matches) and delete.
// Rename and delete are admin-only: shown to everyone, refused for others
// (user, 2026-10-30). What is shown lives in the hash (#/files?q=…&page=…).
import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import { button, confirm, kv, pageHead, pager, railSection, row, state, tag, toast } from "./ui";

type File = Schemas["LibraryFile"];
type Me = Schemas["Me"];
const PAGE_SIZE = 50;
const REFRESH_MS = 30_000;

function params(): URLSearchParams {
  return new URLSearchParams(location.hash.split("?")[1] ?? "");
}
function go(change: Record<string, string>): void {
  const p = params();
  for (const [k, v] of Object.entries(change)) v ? p.set(k, v) : p.delete(k);
  location.hash = `#/files?${p}`;
}
const fail = (e: unknown) => toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");

export function filesPage(me: Me): HTMLElement {
  const p = params();
  const q = p.get("q") ?? "";
  const page = Math.max(1, Number(p.get("page")) || 1);
  const list = h("div", { class: "rows" }, h("p", { class: "py-2 text-muted", role: "status" }, "Loading…"));
  const pages = h("div");
  const rail = h("aside", { class: "side-rail rail" }, railSection("Selected", h("p", { class: "text-muted" }, "Pick a song to rename or delete it.")));

  const box = h("input", { class: "input w-full", type: "search", name: "q", placeholder: "Title, album or artist", "aria-label": "Search the library", value: q });
  const form = h(
    "form",
    {
      class: "mb-6 flex gap-2",
      role: "search",
      onsubmit: (e: Event) => {
        e.preventDefault();
        go({ q: box.value.trim(), page: "" });
      },
    },
    box,
    h("button", { class: "btn btn-primary", type: "submit" }, "Search"),
  );

  let last = "";
  const load = async (force = false) => {
    try {
      const r = await api<Schemas["LibraryPage"]>("GET", `/api/library/files?${new URLSearchParams({ q, page: String(page), page_size: String(PAGE_SIZE) })}`);
      const sig = JSON.stringify(r);
      if (sig === last && !force) return; // unchanged: keep the list as it is
      last = sig;
      list.replaceChildren(
        ...(r.items.length
          ? r.items.map((f) =>
              fileRow(f, () => {
                list.querySelectorAll(".row.selected").forEach((x) => x.classList.remove("selected"));
                showFile(rail, f, me, load);
                // on narrower screens the rail sits below the list: bring it into view
                if (innerWidth < 1280) rail.scrollIntoView({ behavior: "smooth" });
              }),
            )
          : [h("p", { class: "py-2 text-muted" }, q ? `Nothing in the library matches “${q}”.` : "The library is empty.")]),
      );
      pages.replaceChildren(pager(page, Math.max(1, Math.ceil(r.total / r.page_size)), `${r.total} ${r.total === 1 ? "song" : "songs"}`, (n) => go({ page: String(n) })));
    } catch (e) {
      list.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl."));
    }
  };
  void load();
  // new downloads land in the library on their own: look again now and then
  // while the tab is open and visible, and on demand
  const tick = () => {
    if (!list.isConnected) return;
    if (document.visibilityState === "visible") void load();
    setTimeout(tick, REFRESH_MS);
  };
  setTimeout(tick, REFRESH_MS);
  const refresh = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => void load(true).then(() => toast("Library refreshed.")) }, "↻ Refresh");
  return h("div", {}, h("div", { class: "center-main" }, pageHead("Files", h("span", { class: "flex-1" }), refresh), form, list, pages), rail);
}

const length = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

function fileRow(f: File, pick: () => void): HTMLElement {
  const r = row({
    title: f.title || "(no title)",
    artist: f.artist || "Unknown artist",
    album: f.album || undefined,
    extra: h("span", { class: "sub font-mono" }, `${length(f.duration)} · ${f.suffix.toUpperCase()}`),
    // Post-Vinyl's own downloads carry their file state; a missing file is flagged
    state: f.missing ? state("Missing", "err") : f.file_id ? tag("Post-Vinyl") : h("span"),
    action: h("button", { type: "button", class: "icon-btn", "aria-label": `Open ${f.title}` }, "›"),
  });
  r.classList.add("cursor-pointer");
  r.addEventListener("click", () => {
    pick();
    r.classList.add("selected");
  });
  return r;
}

// ── the rail: one song, rename, delete ──────────────────────────────────

function showFile(rail: HTMLElement, f: File, me: Me, reload: () => Promise<void>): void {
  const adminOnly = () => (me.is_admin ? false : (toast("Admins only.", "err"), true));
  const del = button("Delete", async () => {
    if (adminOnly()) return;
    if (!(await confirm(`Delete “${f.title}” from the library? It goes for every user; a later request downloads it again.`, "Delete"))) return;
    try {
      await api("DELETE", `/api/library/files/${encodeURIComponent(f.id)}`);
      toast("Deleted. Navidrome rescans.");
      rail.replaceChildren(railSection("Selected", h("p", { class: "text-muted" }, "Pick a song to rename or delete it.")));
      void reload();
    } catch (e) {
      fail(e);
    }
  }, "danger");
  rail.replaceChildren(
    railSection(
      "Selected",
      h("div", { class: "title" }, f.title),
      h("div", { class: "sub" }, [f.artist, f.album].filter(Boolean).join(" · ")),
      h(
        "div",
        { class: "mt-3" },
        kv([
          ["Length", length(f.duration)],
          ["Format", f.suffix.toUpperCase()],
          ["Downloaded by", f.file_id ? "Post-Vinyl" : "—"],
          ...(f.file_state ? [["File state", f.file_state] as [string, string]] : []),
        ]),
      ),
      h("div", { class: "mt-4" }, del),
    ),
    renameForm(rail, f, me, adminOnly, reload),
  );
}

function renameForm(rail: HTMLElement, f: File, me: Me, adminOnly: () => boolean, reload: () => Promise<void>): HTMLElement {
  const field = (label: string, name: string, value = "", type = "text") =>
    h(
      "label",
      { class: "mb-2 block" },
      h("span", { class: "mb-1 block text-xs text-muted" }, label),
      h("input", { class: "input input-sm w-full", name, type, value, ...(type === "number" ? { min: "1" } : {}) }),
    );
  const out = h("div", { class: "mt-3" });
  const form = h(
    "form",
    {
      onsubmit: async (e: Event) => {
        e.preventDefault();
        if (adminOnly()) return;
        const d = new FormData(form);
        const num = (k: string) => (d.get(k) ? Number(d.get(k)) : null);
        const body: Schemas["RenameBody"] = {
          title: String(d.get("title")).trim(),
          artist: String(d.get("artist")).trim(),
          album_artist: String(d.get("album_artist") ?? "").trim(),
          album: String(d.get("album") ?? "").trim(),
          year: String(d.get("year") ?? "").trim(),
          track: num("track"),
          disc: num("disc"),
        };
        out.replaceChildren(h("p", { class: "text-muted", role: "status" }, "Looking the names up…"));
        try {
          const s = await api<Schemas["SuggestionOut"]>("POST", `/api/library/files/${encodeURIComponent(f.id)}/rename`, body);
          out.replaceChildren(s.match ? suggestion(rail, f, me, s, reload) : noMatch(rail, f, me, body, reload));
        } catch (err) {
          out.replaceChildren();
          fail(err);
        }
      },
    },
    field("Title", "title", f.title),
    field("Artist", "artist", f.artist),
    field("Album artist", "album_artist"),
    field("Album", "album", f.album),
    h("div", { class: "grid grid-cols-3 gap-2" }, field("Year", "year"), field("Track", "track", "", "number"), field("Disc", "disc", "", "number")),
    h("button", { type: "submit", class: "btn btn-sm btn-outline border-line mt-2 w-full" }, "Look up the new names"),
  );
  form.querySelectorAll("input").forEach((i) => ["title", "artist"].includes(i.name) && (i.required = true));
  return railSection("Rename", h("p", { class: "mb-3 text-xs text-muted" }, "New names can mean a different release: Post-Vinyl looks them up and suggests the full tag set."), form, out);
}

/** A release matched: show its tags, apply them on confirm. */
function suggestion(rail: HTMLElement, f: File, me: Me, s: Schemas["SuggestionOut"], reload: () => Promise<void>): HTMLElement {
  const tags = Object.entries(s.tags ?? {}).filter(([, v]) => v !== null && v !== "");
  const pairs = (list: [string, unknown][]) => kv(list.map(([k, v]) => [k.replaceAll("_", " "), String(Array.isArray(v) ? v.join(", ") : v)]));
  // the readable tags up front; the long MusicBrainz ids folded away
  const ids = tags.filter(([k]) => k.startsWith("musicbrainz"));
  const named = tags.filter(([k]) => !k.startsWith("musicbrainz"));
  const apply = button("Apply these tags", async () => {
    try {
      const r = await api<Schemas["Moved"]>("POST", `/api/library/files/${encodeURIComponent(f.id)}/apply`, {
        recording_id: s.recording_id ?? "",
        release_id: s.release_id ?? "",
      } satisfies Schemas["Apply"]);
      toast("Tags written; the file moved where they put it.");
      await reload();
      // the song's id follows its path, so it is a new one now
      if (r.file) showFile(rail, r.file, me, reload);
    } catch (e) {
      fail(e);
    }
  }, "primary");
  apply.classList.add("mt-3", "w-full");
  return h(
    "div",
    {},
    h("div", { class: "mb-2" }, state(`Matched via ${s.via === "listenbrainz" ? "ListenBrainz" : "MusicBrainz"}`, "ok")),
    pairs(named),
    ids.length > 0 &&
      h("details", { class: "mt-2 text-xs" }, h("summary", { class: "cursor-pointer text-muted" }, `MusicBrainz ids (${ids.length})`), h("div", { class: "mt-2 font-mono break-all" }, pairs(ids.map(([k, v]) => [k.replace("musicbrainz_", ""), v])))),
    h("p", { class: "mt-2 text-xs text-muted" }, s.has_cover ? "With its cover art." : "No cover art for this release."),
    apply,
  );
}

/** Nothing matched: the file can still move by the new names, untagged. */
function noMatch(rail: HTMLElement, f: File, me: Me, body: Schemas["RenameBody"], reload: () => Promise<void>): HTMLElement {
  const strip = button("Strip tags and move", async () => {
    if (!(await confirm(`Take every tag off “${f.title}” and move it to ${body.artist}/${body.title}?`, "Strip and move"))) return;
    try {
      const r = await api<Schemas["Moved"]>("POST", `/api/library/files/${encodeURIComponent(f.id)}/strip`, { title: body.title, artist: body.artist } satisfies Schemas["Strip"]);
      toast("Tags stripped; the file moved.");
      await reload();
      if (r.file) showFile(rail, r.file, me, reload);
    } catch (e) {
      fail(e);
    }
  }, "danger");
  strip.classList.add("mt-3", "w-full");
  return h(
    "div",
    {},
    h("div", { class: "mb-2" }, state("No release matches these names", "wait")),
    h("p", { class: "text-xs text-muted" }, `You can strip the tags instead: the file moves to ${body.artist}/${body.title}, untagged.`),
    strip,
  );
}
