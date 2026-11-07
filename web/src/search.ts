// Search tab (#121): one search for songs, albums and artists together
// from one box (user, 2026-10-30), browsing artist → album on the source that
// answered, and the download cart. What is shown lives in the hash
// (#/search?q=…), so reload and back work; the last search stays for the
// browser session, its results unrefetched (user, 2026-11-05).
import { ApiError, api, type Schemas } from "./api";
import { cart, CART_MAX, type CartEntry } from "./cart";
import { h } from "./dom";
import { button, cover, iconButton, paged, pageHead, railSection, row, state, toast } from "./ui";

type Browse = { browse: "artist" | "albums" | "album"; source: string; id: string; name: string; artist: string; cover: string; info: string };
type View = { q: string } | Browse;
const LAST_SEARCH = "pv-last-search";

function remember(q: string): void {
  try {
    sessionStorage.setItem(LAST_SEARCH, q);
  } catch {
    // no storage: the Search tab just opens empty
  }
}

function lastSearch(): string {
  try {
    return sessionStorage.getItem(LAST_SEARCH) ?? "";
  } catch {
    return "";
  }
}

function readView(): View {
  const p = new URLSearchParams(location.hash.split("?")[1] ?? "");
  const browse = p.get("browse");
  if (browse === "artist" || browse === "albums" || browse === "album") {
    const text = (k: string) => p.get(k) ?? "";
    return { browse, source: text("source"), id: text("id"), name: text("name"), artist: text("artist"), cover: text("cover"), info: text("info") };
  }
  return { q: p.get("q") ?? lastSearch() };
}

// answers kept for the page's life, so Back shows them without searching again
const answers = new Map<string, Promise<unknown>>();

function fetched<T>(path: string): Promise<T> {
  let answer = answers.get(path);
  if (!answer) {
    answer = api<T>("GET", path);
    answers.set(path, answer);
    answer.catch(() => answers.delete(path));
  }
  return answer as Promise<T>;
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
  const main = "q" in view ? [searchForm(view), ...searchResults(view)] : browsePage(view);
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
  remember(view.q);
  // sections with nothing in them drop out; if all three do, one line says so
  const none = h("p", { class: "hidden py-2 text-muted" }, `Nothing found for “${view.q}”.`);
  let empty = 0;
  const gone = () => ++empty === 3 && none.classList.remove("hidden");
  return [
    section("Artists", gone,  async () => {
      const r = await fetched<Schemas["Results_Artist_"]>(`/api/catalog/artists?name=${enc(view.q)}`);
      return [r.items.map(artistTile), r.source];
    }, (tiles) => carousel(tiles, 3, "grid grid-cols-3 gap-4")),
    section("Albums", gone,  async () => {
      const r = await fetched<Schemas["Results_Album_"]>(`/api/catalog/albums?title=${enc(view.q)}`);
      return [r.items.map(albumTile), r.source];
    }, (tiles) => carousel(tiles, 4, "grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2")),
    section("Songs", gone,  async () => {
      const r = await fetched<Schemas["Results_Track_"]>(`/api/catalog/tracks?title=${enc(view.q)}`);
      return [r.items.map((t) => trackRow(t, r.origin_id)), r.source];
    }),
    none,
  ];
}

function section(
  title: string,
  onEmpty: (() => void) | null,
  load: () => Promise<[HTMLElement[], string | null]>,
  show: (rows: HTMLElement[]) => HTMLElement = (rows) => paged(rows, ["result", "results"]),
): HTMLElement {
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
      body.replaceChildren(show(rows));
    },
    // 409 a source is off, 502 a source failed, 503 none answered: shown as said
    (e) => body.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.")),
  );
  const el = h("section", { class: "mb-8" }, head, body);
  return el;
}

// ── browsing ────────────────────────────────────────────────────────────

function back(): HTMLElement {
  return h("button", { type: "button", class: "btn btn-ghost btn-sm -ml-2 mb-4", onclick: () => history.back() }, "← Back");
}

function browsePage(view: Browse): HTMLElement[] {
  if (view.browse === "album") return albumPage(view);
  return view.browse === "albums" ? allAlbumsPage(view) : artistPage(view);
}

const artistBase = (view: Browse) => `/api/catalog/${enc(view.source)}/artists/${enc(view.id)}`;

function artistPage(view: Browse): HTMLElement[] {
  const base = artistBase(view);
  const head = h(
    "div",
    { class: "browse-head" },
    cover(view.name, coverOf(view.source, view.cover, 300)),
    h("div", { class: "min-w-0" }, h("div", { class: "text-muted" }, "Artist"), h("h1", {}, view.name)),
  );
  head.firstElementChild?.classList.add("round");
  const all = () => go({ ...view, browse: "albums" });
  const albums = section("Albums", null, async () => [(await fetched<Schemas["Album"][]>(`${base}/albums`)).slice(0, 5).map(albumSleeve), null], (tiles) => h("div", { class: "sleeves" }, ...tiles));
  albums.querySelector("h2")?.append(h("button", { type: "button", class: "ml-auto normal-case text-base-content", onclick: all }, "View all ›"));
  const popular = section("Popular tracks", () => {}, async () => [(await fetched<Schemas["Track"][]>(`${base}/top-tracks`)).map(popularRow), null], (rows) => h("div", { class: "popular" }, ...rows));
  const similar = section("Similar artists", () => {}, async () => [(await fetched<Schemas["Artist"][]>(`${base}/similar`)).slice(0, 5).map(artistTile), null], (tiles) => h("div", { class: "sleeves round-tiles" }, ...tiles));
  return [back(), head, albums, popular, similar];
}

function allAlbumsPage(view: Browse): HTMLElement[] {
  return [
    back(),
    pageHead(`Albums by ${view.name}`),
    section("Newest first", null, async () => [(await fetched<Schemas["Album"][]>(`${artistBase(view)}/albums`)).map(albumSleeve), null], (tiles) => h("div", { class: "sleeves wrap" }, ...tiles)),
  ];
}

function albumPage(view: Browse): HTMLElement[] {
  const head = h(
    "div",
    { class: "browse-head" },
    cover(view.name, coverOf(view.source, view.cover, 300)),
    h("div", { class: "min-w-0 flex-1" }, h("div", { class: "text-muted" }, "Album"), h("h1", {}, view.name), h("div", { class: "text-muted" }, meta(view.artist, view.info))),
    albumToggle(view.name, view.source, view.id, true),
  );
  const tracks = section("Tracks", null, async () => [(await fetched<Schemas["Track"][]>(editionPath(view.source, view.id))).map((t) => trackRow(t, null, true)), null], (rows) => {
    const list = paged(rows, ["song", "songs"], "rows", Math.max(rows.length, 1));
    list.classList.add("tracklist");
    return list;
  });
  return [back(), head, tracks];
}

const SOURCE_NAMES: Record<string, string> = { musicbrainz: "MusicBrainz", deezer: "Deezer", listenbrainz: "ListenBrainz" };

// a result's cover through Post-Vinyl, from the source that answered (#127)
const coverOf = (source: string, coverId: string, size = 80) => (coverId ? `/api/catalog/${enc(source)}/covers/${coverId}?size=${size}` : undefined);
const year = (date: string) => date.slice(0, 4);
const length = (s: number | null) => (s == null ? "" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
const meta = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(" · ");

function entryOf(t: Schemas["Track"], originId: number | null): CartEntry {
  const title = t.disambiguation ? `${t.title} (${t.disambiguation})` : t.title;
  return { source: t.source, track_id: t.id, origin_id: originId, title, artist: t.artist };
}

/** The + / − button, with a mark that shows the song is in the cart. */
function cartToggle(entry: CartEntry): [HTMLElement, HTMLElement] {
  const inCart = () => cart.has(entry);
  const mark = h("span");
  const toggle = h("button", { type: "button", class: "icon-btn text-lg" });
  const sync = () => {
    mark.replaceChildren(inCart() ? state("In cart", "accent") : "");
    toggle.textContent = inCart() ? "−" : "+";
    toggle.setAttribute("aria-label", `${inCart() ? "Remove" : "Add"} ${entry.title} ${inCart() ? "from" : "to"} the cart`);
  };
  toggle.addEventListener("click", () => {
    if (inCart()) cart.remove(entry);
    else if (!cart.add(entry)) toast(`The cart holds at most ${CART_MAX} songs.`, "err");
  });
  sync();
  cart.watch(toggle, sync);
  return [mark, toggle];
}

const editionPath = (source: string, id: string) => `/api/catalog/${enc(source)}/editions/${enc(id)}/tracks`;

// an album's songs once known: tiles don't ask for each album's tracks up
// front, so after a reload a tile shows + until it's pressed or opened
const albumSongs = new Map<string, CartEntry[]>();

async function loadAlbum(path: string): Promise<CartEntry[]> {
  const songs = (await fetched<Schemas["Track"][]>(path)).map((t) => entryOf(t, null));
  albumSongs.set(path, songs);
  return songs;
}

/** Adds an album's songs, or removes them once all are in the cart. */
function albumToggle(title: string, source: string, id: string, labelled = false): HTMLButtonElement {
  const path = editionPath(source, id);
  const allIn = () => {
    const songs = albumSongs.get(path);
    return !!songs?.length && songs.every((s) => cart.has(s));
  };
  const toggle = h("button", { type: "button", class: labelled ? "btn btn-outline btn-sm flex-none border-line" : "icon-btn text-lg" });
  const sync = () => {
    const all = allIn();
    const glyph = all ? "−" : "+";
    toggle.textContent = labelled ? `${glyph} ${all ? "Remove all" : "Add all"}` : glyph;
    const label = `${all ? "Remove" : "Add"} all of ${title} ${all ? "from" : "to"} the cart`;
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  };
  toggle.addEventListener("click", () => {
    const remove = allIn(); // decided on what the button showed when pressed
    toggle.disabled = true;
    void loadAlbum(path)
      .then((songs) => {
        if (remove) return cart.removeAll(songs);
        if (!songs.length) return toast("This album has no songs to add.", "err");
        const added = cart.addAll(songs);
        if (!songs.every((s) => cart.has(s))) toast(`The cart holds at most ${CART_MAX} songs: added ${added}.`, "err");
      })
      .catch((e) => toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err"))
      .finally(() => {
        toggle.disabled = false;
        sync();
      });
  });
  sync();
  cart.watch(toggle, sync);
  // the album page loads these tracks anyway, so it can show − straight away
  if (labelled) void loadAlbum(path).then(sync, () => {});
  return toggle;
}

function trackRow(t: Schemas["Track"], originId: number | null, onAlbumPage = false): HTMLElement {
  const entry = entryOf(t, originId);
  const [mark, toggle] = cartToggle(entry);
  const side = t.position ? `${t.disc && t.disc > 1 ? `${t.disc}-` : ""}${t.position}` : undefined;
  return row({
    title: entry.title,
    cover: coverOf(t.source, t.cover_id ?? ""),
    artist: t.artist,
    album: onAlbumPage ? undefined : t.album || undefined,
    side,
    extra: h("span", { class: "sub font-mono" }, meta(length(t.duration_s), !onAlbumPage && year(t.first_release_date))),
    state: mark,
    action: toggle,
  });
}

function popularRow(t: Schemas["Track"]): HTMLElement {
  const [, toggle] = cartToggle(entryOf(t, null));
  return h(
    "div",
    { class: "flex min-w-0 items-center gap-3 py-1" },
    cover(t.title, coverOf(t.source, t.cover_id ?? "")),
    h("div", { class: "min-w-0 flex-1" }, h("div", { class: "title" }, t.title), h("div", { class: "sub" }, t.artist)),
    toggle,
  );
}

/** A few results at a time in a grid, with round ‹ › buttons beside it to
 *  page through the rest (the search page's shelves, user, 2026-10-31). */
function carousel(items: HTMLElement[], perPage: number, cls: string): HTMLElement {
  const grid = h("div", { class: `${cls} min-w-0 flex-1` }, ...items);
  const pages = Math.ceil(items.length / perPage);
  const step = (glyph: string, label: string) => h("button", { type: "button", class: "shelf-btn", "aria-label": label }, glyph);
  const prev = step("‹", "Previous results");
  const next = step("›", "More results");
  let page = 0;
  const show = () => {
    // hidden, not removed: album tiles keep following the cart while off the page
    items.forEach((el, n) => el.classList.toggle("hidden", Math.floor(n / perPage) !== page));
    prev.toggleAttribute("disabled", page === 0);
    next.toggleAttribute("disabled", page >= pages - 1);
  };
  prev.addEventListener("click", () => (page--, show()));
  next.addEventListener("click", () => (page++, show()));
  show();
  // a single page keeps the buttons' room, so the shelves line up
  if (pages <= 1) [prev, next].forEach((b) => b.classList.add("invisible"));
  return h("div", { class: "flex items-center gap-1 md:gap-3" }, prev, grid, next);
}

/** An artist as a sleeve: the big square picture, the name under it. */
function artistTile(a: Schemas["Artist"]): HTMLElement {
  const to = { browse: "artist", source: a.source, id: a.id, name: a.name, artist: a.name, cover: a.cover_id ?? "" };
  return h(
    "button",
    { type: "button", class: "tile min-w-0 text-left", title: a.disambiguation || a.name, "aria-label": `Open ${a.name}`, onclick: () => go(to) },
    cover(a.name, coverOf(a.source, a.cover_id ?? "", 300)),
    h("div", { class: "title mt-2" }, a.name),
    a.disambiguation && h("div", { class: "sub text-xs" }, a.disambiguation),
  );
}

const albumInfo = (a: Schemas["Album"]) => meta(year(a.first_release_date), a.primary_type, !!a.track_count && `${a.track_count} tracks`);
const openAlbum = (a: Schemas["Album"]) => () => go({ browse: "album", source: a.source, id: a.id, name: a.title, artist: a.artist, cover: a.cover_id ?? "", info: albumInfo(a) });

/** An album: a small cover with the title and "by" credit beside it. */
function albumTile(a: Schemas["Album"]): HTMLElement {
  return albumCard(
    a,
    h(
      "button",
      { type: "button", class: "tile tile-row min-w-0 text-left", "aria-label": `Open ${a.title} by ${a.artist}`, onclick: openAlbum(a) },
      cover(a.title, coverOf(a.source, a.cover_id ?? "", 300)),
      h(
        "div",
        { class: "min-w-0" },
        h("div", { class: "title" }, a.title),
        h("div", { class: "sub" }, h("span", { class: "credit" }, a.artist)),
        h("div", { class: "sub text-xs" }, albumInfo(a)),
      ),
    ),
  );
}

/** An album on an artist's page: the big cover with its details under it. */
function albumSleeve(a: Schemas["Album"]): HTMLElement {
  return albumCard(
    a,
    h(
      "button",
      { type: "button", class: "tile min-w-0 text-left", "aria-label": `Open ${a.title}`, onclick: openAlbum(a) },
      cover(a.title, coverOf(a.source, a.cover_id ?? "", 300)),
      h("div", { class: "title mt-2" }, a.title),
      h("div", { class: "sub" }, h("span", { class: "credit" }, a.artist)),
      h("div", { class: "sub text-xs" }, albumInfo(a)),
    ),
  );
}

// a tile is a button, so the + sits beside it, not inside it
const albumCard = (a: Schemas["Album"], tile: HTMLElement) => h("div", { class: "album-card" }, tile, albumToggle(a.title, a.source, a.id));

// ── the cart ────────────────────────────────────────────────────────────

const REDOWNLOAD = "pv-redownload";

function redownloadOn(): boolean {
  try {
    return localStorage.getItem(REDOWNLOAD) === "1";
  } catch {
    return false;
  }
}

const cartKey = (i: CartEntry) => `${i.source}:${i.track_id}`;

function cartRail(): HTMLElement {
  const section = h("div");
  let owned = new Set<string>(); // cart songs the library has already
  let asked = "";
  let redownload = redownloadOn();
  const checkOwned = async () => {
    const items = cart.items();
    const sig = items.map(cartKey).join();
    if (sig === asked) return;
    asked = sig;
    if (!items.length) return;
    try {
      const body = { items: items.map(({ title, artist }) => ({ title, artist })) } satisfies Schemas["OwnedCheck"];
      const have = await api<boolean[]>("POST", "/api/library/files/owned", body);
      if (sig !== asked) return; // the cart moved on
      owned = new Set(items.filter((_, n) => have[n]).map(cartKey));
      draw();
    } catch {
      asked = ""; // unknown stays unmarked; the next change asks again
    }
  };
  const draw = () => {
    const items = cart.items();
    const wanted = redownload ? [...items] : items.filter((i) => !owned.has(cartKey(i)));
    const skipped = items.length - wanted.length;
    const list = items.map((i) =>
      h(
        "div",
        { class: "mb-2 flex items-start gap-2" },
        h(
          "div",
          { class: "min-w-0 flex-1" },
          h("div", { class: "title" }, i.title),
          h("div", { class: "sub" }, i.artist),
          owned.has(cartKey(i)) && h("div", { class: "text-xs text-accent" }, redownload ? "In your library · downloads again" : "In your library · skipped"),
          i.error && h("div", { class: "text-xs text-error" }, i.error),
        ),
        iconButton(`Remove ${i.title}`, "×", () => cart.remove(i)),
      ),
    );
    const label = wanted.length === 1 ? "Request 1 song" : `Request ${wanted.length} songs`;
    const go = () => void request(send, wanted, skipped);
    const send = button(label, go, "primary");
    send.classList.add("w-full", "mt-2");
    send.disabled = sending || !wanted.length;
    const toggle = h(
      "label",
      { class: "mt-3 flex items-center gap-2 text-muted" },
      h("input", {
        type: "checkbox",
        class: "toggle toggle-xs",
        checked: redownload,
        onchange: (e: Event) => {
          redownload = (e.target as HTMLInputElement).checked;
          try {
            localStorage.setItem(REDOWNLOAD, redownload ? "1" : "");
          } catch {
            // storage blocked: the choice lasts until the page closes
          }
          draw();
        },
      }),
      "Re-download songs I already have",
    );
    // phones: the rail sits below the results, so a bar keeps the cart in reach
    const barSend = button(label, go, "primary");
    barSend.disabled = send.disabled;
    const bar =
      items.length > 0 &&
      h(
        "div",
        { class: "activity xl:hidden" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => section.scrollIntoView({ behavior: "smooth" }) }, `Cart · ${items.length}`),
        h("span", { class: "flex-1" }),
        barSend,
      );
    section.replaceChildren(
      bar || "",
      railSection(
        `Cart · ${items.length}`,
        list.length ? paged(list, ["song", "songs"], "") : "",
        items.length > 0 && toggle,
        items.length > 0 && send,
        items.length > 0 && h("button", { type: "button", class: "btn btn-ghost btn-xs mt-2 w-full text-muted", onclick: () => cart.replace([]) }, "Empty cart"),
      ),
    );
    void checkOwned();
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

// Module-wide: a redraw builds fresh buttons, and the phone bar has its own, so
// disabling the clicked button alone doesn't stop a second send.
let sending = false;

async function request(btn: HTMLButtonElement, items: CartEntry[], skipped: number): Promise<void> {
  if (sending || !items.length) return;
  sending = true;
  btn.disabled = true;
  try {
    const results = await api<Schemas["CartResult"][]>("POST", "/api/requests/batch", {
      items: items.map(({ source, track_id, origin_id }) => ({ source, track_id, origin_id })),
    } satisfies Schemas["Cart"]);
    sending = false;
    // one result per item: the ones that went through leave the cart, the rest stay with their reason
    const failed = items
      .map((item) => ({ item, r: results.find((r) => r.source === item.source && r.track_id === item.track_id) }))
      .filter(({ r }) => !r || r.status >= 300)
      .map(({ item, r }) => ({ ...item, error: failure(r) }));
    const sent = items.length - failed.length;
    cart.replace(failed);
    if (sent) toast(`Requested ${sent} ${sent === 1 ? "song" : "songs"}.`);
    if (skipped) toast(`Skipped ${skipped} already in your library.`);
    if (failed.length) toast(`${failed.length} couldn't be requested; they stay in the cart.`, "err");
  } catch (e) {
    sending = false;
    toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");
    cart.replace([...cart.items()]); // redraws, buttons enabled again
  }
}
