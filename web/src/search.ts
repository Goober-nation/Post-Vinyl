// Search tab (#121): one search for songs, albums and artists together
// from one box (user, 2026-10-30), browsing artist → albums → editions → tracks on the
// source that answered, and the download cart. What is shown lives in the
// hash (#/search?q=…), so reload and back work.
import { ApiError, api, type Schemas } from "./api";
import { cart, CART_MAX, type CartEntry } from "./cart";
import { h } from "./dom";
import { button, iconButton, paged, pageHead, railSection, row, state, toast } from "./ui";

type View =
  | { q: string }
  | { browse: "artist" | "album" | "edition"; source: string; id: string; name: string; artist: string };

function readView(): View {
  const p = new URLSearchParams(location.hash.split("?")[1] ?? "");
  const browse = p.get("browse");
  const artist = p.get("artist") ?? "";
  if (browse === "artist" || browse === "album" || browse === "edition")
    return { browse, source: p.get("source") ?? "", id: p.get("id") ?? "", name: p.get("name") ?? "", artist };
  return { q: p.get("q") ?? "" };
}

function go(params: Record<string, string>): void {
  const p = new URLSearchParams(Object.entries(params).filter(([, v]) => v));
  location.hash = `#/search?${p}`;
}

const enc = encodeURIComponent;

export function searchPage(): HTMLElement {
  const view = readView();
  // search and results stay centred on the window; the cart keeps to the
  // window's right edge (below the results on narrower screens)
  const main = "q" in view ? [searchForm(view), ...searchResults(view)] : [browseHead(view), browseResults(view)];
  return h("div", {}, h("div", { class: "center-main" }, ...main), h("aside", { class: "side-rail rail" }, cartRail()));
}

// ── the search ──────────────────────────────────────────────────────────

function searchForm(view: { q: string }): HTMLElement {
  const q = h("input", {
    class: "input input-lg w-full",
    name: "q",
    type: "search",
    placeholder: "Song, album or artist",
    "aria-label": "Song, album or artist",
    required: true,
    value: view.q,
  });
  const form = h(
    "form",
    {
      class: "flex gap-2",
      role: "search",
      onsubmit: (e: Event) => {
        e.preventDefault();
        go({ q: q.value.trim() });
      },
    },
    q,
    h("button", { class: "btn btn-primary btn-lg", type: "submit" }, "Search"),
  );
  // before a search: the title over the bar, both centred, 30% down the window
  return view.q
    ? h("div", { class: "mb-6" }, pageHead("Search"), form)
    : h("div", { class: "pt-[calc(30vh-8rem)] text-center" }, h("h1", { class: "mb-4" }, "Search"), form);
}

/** Three searches at once, each in its own section: a slow or failing source
 *  only holds up its own part. Each pages on its own. */
function searchResults(view: { q: string }): HTMLElement[] {
  if (!view.q) return [];
  // sections with nothing in them drop out; if all three do, one line says so
  const none = h("p", { class: "hidden py-2 text-muted" }, `Nothing found for “${view.q}”.`);
  let empty = 0;
  const gone = () => ++empty === 3 && none.classList.remove("hidden");
  return [
    section("Artists", gone,  async () => {
      const r = await api<Schemas["Results_Artist_"]>("GET", `/api/catalog/artists?name=${enc(view.q)}`);
      return [r.items.map(artistRow), r.source];
    }),
    section("Albums", gone,  async () => {
      const r = await api<Schemas["Results_Album_"]>("GET", `/api/catalog/albums?title=${enc(view.q)}`);
      return [r.items.map(albumRow), r.source];
    }),
    section("Songs", gone,  async () => {
      const r = await api<Schemas["Results_Track_"]>("GET", `/api/catalog/tracks?title=${enc(view.q)}`);
      return [r.items.map((t) => trackRow(t, r.origin_id)), r.source];
    }),
    none,
  ];
}

function section(title: string, onEmpty: (() => void) | null, load: () => Promise<[HTMLElement[], string | null]>): HTMLElement {
  const body = h("div", {}, h("p", { class: "py-2 text-muted", role: "status" }, "Searching…"));
  const head = h("h2", { class: "rail-title flex items-baseline gap-3" }, title);
  void load().then(
    ([rows, source]) => {
      if (!rows.length) {
        if (onEmpty) return void (el.remove(), onEmpty());
        body.replaceChildren(h("p", { class: "py-2 text-muted" }, "Nothing found."));
        return;
      }
      if (source) head.append(h("span", { class: "text-xs normal-case" }, `from ${SOURCE_NAMES[source] ?? source}`));
      body.replaceChildren(paged(rows, ["result", "results"]));
    },
    // 409 a source is off, 502 a source failed, 503 none answered: shown as said
    (e) => body.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.")),
  );
  const el = h("section", { class: "mb-8" }, head, body);
  return el;
}

// ── browsing ────────────────────────────────────────────────────────────

function browseHead(view: Exclude<View, { q: string }>): HTMLElement {
  const back = h("button", { type: "button", class: "btn btn-ghost btn-sm -ml-2", onclick: () => history.back() }, "← Back");
  const sub = view.browse === "artist" ? "Albums" : view.browse === "album" ? `${view.artist} · Editions` : view.artist;
  return h("div", { class: "mb-4" }, back, pageHead(view.name, h("span", { class: "text-muted" }, sub)));
}

function browseResults(view: Exclude<View, { q: string }>): HTMLElement {
  const base = `/api/catalog/${enc(view.source)}`;
  const label = { artist: "Albums", album: "Editions", edition: "Tracks" }[view.browse];
  return section(label, null, async () => {
    if (view.browse === "artist") return [(await api<Schemas["Album"][]>("GET", `${base}/artists/${enc(view.id)}/albums`)).map(albumRow), view.source];
    if (view.browse === "album")
      return [(await api<Schemas["Edition"][]>("GET", `${base}/albums/${enc(view.id)}/editions`)).map((e) => editionRow(e, view.artist)), view.source];
    return [(await api<Schemas["Track"][]>("GET", `${base}/editions/${enc(view.id)}/tracks`)).map((t) => trackRow(t, null, view.name)), view.source];
  });
}

const SOURCE_NAMES: Record<string, string> = { musicbrainz: "MusicBrainz", deezer: "Deezer", listenbrainz: "ListenBrainz" };

const year = (date: string) => date.slice(0, 4);
const length = (s: number | null) => (s == null ? "" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
const meta = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(" · ");

function trackRow(t: Schemas["Track"], originId: number | null, album?: string): HTMLElement {
  const name = t.disambiguation ? `${t.title} (${t.disambiguation})` : t.title;
  const entry: CartEntry = { source: t.source, track_id: t.id, origin_id: originId, title: name, artist: t.artist };
  const inCart = () => cart.has(entry);
  const mark = h("span");
  const toggle = h("button", { type: "button", class: "icon-btn text-lg" });
  const sync = () => {
    mark.replaceChildren(inCart() ? state("In cart", "accent") : "");
    toggle.textContent = inCart() ? "−" : "+";
    toggle.setAttribute("aria-label", `${inCart() ? "Remove" : "Add"} ${name} ${inCart() ? "from" : "to"} the cart`);
  };
  toggle.addEventListener("click", () => {
    if (inCart()) cart.remove(entry);
    else if (!cart.add(entry)) toast(`The cart holds at most ${CART_MAX} songs.`, "err");
  });
  sync();
  cart.watch(toggle, sync);
  const side = t.position ? `${t.disc && t.disc > 1 ? `${t.disc}-` : ""}${t.position}` : undefined;
  return row({
    title: name,
    artist: t.artist,
    album,
    side,
    extra: h("span", { class: "sub font-mono" }, meta(length(t.duration_s), !album && year(t.first_release_date))),
    state: mark,
    action: toggle,
  });
}

function link(label: string, params: Record<string, string>): HTMLElement {
  return h("button", { type: "button", class: "icon-btn", "aria-label": label, title: label, onclick: () => go(params) }, "›");
}

/** A row that opens something: the whole row is the target; the › button
 *  stays the keyboard and screen-reader way in. */
function opens(el: HTMLElement, params: Record<string, string>): HTMLElement {
  el.classList.add("cursor-pointer");
  el.addEventListener("click", (e) => {
    if (!(e.target as Element).closest("button")) go(params);
  });
  return el;
}

function albumRow(a: Schemas["Album"]): HTMLElement {
  const types = [a.primary_type, ...a.secondary_types].filter(Boolean).join(", ");
  const to = { browse: "album", source: a.source, id: a.id, name: a.title, artist: a.artist };
  return opens(row({
    title: a.title,
    artist: a.artist,
    extra: h("span", { class: "sub" }, meta(year(a.first_release_date), types)),
    action: link(`Editions of ${a.title}`, to),
  }), to);
}

function artistRow(a: Schemas["Artist"]): HTMLElement {
  const to = { browse: "artist", source: a.source, id: a.id, name: a.name, artist: a.name };
  return opens(row({
    title: a.name,
    artist: a.disambiguation || " ",
    action: link(`Albums by ${a.name}`, to),
  }), to);
}

function editionRow(e: Schemas["Edition"], artist: string): HTMLElement {
  const to = { browse: "edition", source: e.source, id: e.id, name: e.title, artist };
  return opens(row({
    title: e.title,
    artist: meta(year(e.date), e.country, e.formats.join(" + ")) || artist,
    extra: h("span", { class: "sub" }, meta(`${e.track_count} tracks`, e.status)),
    action: link(`Tracks of the ${meta(year(e.date), e.country)} edition`, to),
  }), to);
}

// ── the cart ────────────────────────────────────────────────────────────

function cartRail(): HTMLElement {
  const section = h("div");
  const draw = () => {
    const items = cart.items();
    const list = items.map((i) =>
      h(
        "div",
        { class: "mb-2 flex items-start gap-2" },
        h(
          "div",
          { class: "min-w-0 flex-1" },
          h("div", { class: "title" }, i.title),
          h("div", { class: "sub" }, i.artist),
          i.error && h("div", { class: "text-xs text-error" }, i.error),
        ),
        iconButton(`Remove ${i.title}`, "×", () => cart.remove(i)),
      ),
    );
    const send = button(items.length === 1 ? "Request 1 song" : `Request ${items.length} songs`, () => void request(send), "primary");
    send.classList.add("w-full", "mt-2");
    // phones: the rail sits below the results, so a bar keeps the cart in reach
    const bar =
      items.length > 0 &&
      h(
        "div",
        { class: "activity xl:hidden" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => section.scrollIntoView({ behavior: "smooth" }) }, `Cart · ${items.length}`),
        h("span", { class: "flex-1" }),
        button(items.length === 1 ? "Request 1 song" : `Request ${items.length} songs`, () => void request(send), "primary"),
      );
    section.replaceChildren(
      bar || "",
      railSection(
        `Cart · ${items.length}`,
        list.length ? paged(list, ["song", "songs"], "") : "",
        items.length > 0 && send,
        items.length > 0 && h("button", { type: "button", class: "btn btn-ghost btn-xs mt-2 w-full text-muted", onclick: () => cart.replace([]) }, "Empty cart"),
      ),
    );
  };
  draw();
  cart.watch(section, draw);
  return section;
}

// The batch answers a status and the source's own words per item; the words
// alone can be a bare id, so the status leads.
function failure(r: Schemas["CartResult"] | undefined): string {
  if (!r) return "No answer for this one.";
  if (r.status === 404) return "Not in the catalog any more.";
  if (r.status === 409) return r.error ?? "Not possible right now.";
  return r.error ? `Failed: ${r.error}` : `Failed (${r.status}).`;
}

async function request(btn: HTMLButtonElement): Promise<void> {
  const items = cart.items();
  btn.disabled = true;
  try {
    const results = await api<Schemas["CartResult"][]>("POST", "/api/requests/batch", {
      items: items.map(({ source, track_id, origin_id }) => ({ source, track_id, origin_id })),
    } satisfies Schemas["Cart"]);
    // one result per item: the ones that went through leave the cart, the rest stay with their reason
    const failed = items
      .map((item) => ({ item, r: results.find((r) => r.source === item.source && r.track_id === item.track_id) }))
      .filter(({ r }) => !r || r.status >= 300)
      .map(({ item, r }) => ({ ...item, error: failure(r) }));
    const sent = items.length - failed.length;
    cart.replace(failed);
    if (sent) toast(`Requested ${sent} ${sent === 1 ? "song" : "songs"}.`);
    if (failed.length) toast(`${failed.length} couldn't be requested; they stay in the cart.`, "err");
  } catch (e) {
    toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");
    btn.disabled = false;
  }
}
