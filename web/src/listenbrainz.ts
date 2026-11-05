// ListenBrainz tab: Recs (a stored list, a range of it) and Radio (an artist's
// fresh radio), both ending in a pull: a named batch that downloads behind the
// user's own requests into one Navidrome playlist. The sub-tab lives in the
// hash (#/listenbrainz?tab=radio).
import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import { button, filters, paged, pageHead, railSection, row, state, toast, type Tone } from "./ui";

const POLL_MS = 3000;
const MAX_RANK = 1000;

type Pull = Schemas["PullOut"];
type RadioTrack = Schemas["RadioTrack"];

function failed(err: unknown): void {
  toast(err instanceof ApiError ? err.message : "Can't reach Post-Vinyl.", "err");
}

// through Post-Vinyl, from the Cover Art Archive
const coverOf = (release?: string | null) => (release ? `/api/catalog/musicbrainz/covers/release/${release}?size=80` : undefined);

const day = (seconds: number) => new Date(seconds * 1000).toISOString().slice(0, 10);

export function listenbrainzPage(): HTMLElement {
  const sub = new URLSearchParams(location.hash.split("?")[1] ?? "").get("tab") === "radio" ? "radio" : "recs";
  const tabs = filters("Pull from", [["recs", "Recommendations"], ["radio", "Radio"]], sub, (v) => (location.hash = `#/listenbrainz?tab=${v}`));
  return h(
    "div",
    {},
    h("div", { class: "center-main" }, pageHead("ListenBrainz", tabs), sub === "recs" ? recsPane() : radioPane()),
    h("aside", { class: "side-rail rail" }, pullsRail()),
  );
}

function nameInput(placeholder: string): HTMLInputElement {
  return h("input", { class: "input w-full", placeholder, maxlength: "200", "aria-label": "Playlist name" });
}

function tall(b: HTMLButtonElement): HTMLButtonElement {
  b.classList.remove("btn-sm"); // the same height as the fields beside it
  return b;
}

function field(label: string, control: Node, cls = "mb-4 block"): HTMLElement {
  return h("label", { class: cls }, h("span", { class: "mb-1 block text-muted" }, label), control);
}

// ── recs ────────────────────────────────────────────────────────────────

function rangePicker(onChange: (first: number, last: number) => void): HTMLElement {
  const handle = (label: string, value: number) =>
    h("input", { type: "range", class: "range-handle", min: "1", max: String(MAX_RANK), value: String(value), "aria-label": label });
  const from = handle("First song", 1);
  const to = handle("Last song", 100);
  const shown = h("output", { class: "font-mono" });
  const pair = h("div", { class: "range-pair" }, from, to);
  const moved = (keepFrom: boolean) => {
    if (Number(from.value) > Number(to.value)) (keepFrom ? (to.value = from.value) : (from.value = to.value));
    shown.textContent = `${from.value} – ${to.value}`;
    pair.style.setProperty("--from", `${((Number(from.value) - 1) / (MAX_RANK - 1)) * 100}%`);
    pair.style.setProperty("--to", `${((Number(to.value) - 1) / (MAX_RANK - 1)) * 100}%`);
  };
  from.oninput = () => moved(true);
  to.oninput = () => moved(false);
  const done = () => onChange(Number(from.value), Number(to.value));
  from.onchange = done;
  to.onchange = done;
  moved(true);
  queueMicrotask(done);
  return h("div", { class: "mb-4" }, pair, shown);
}

function recsPane(): HTMLElement {
  const lists = h("select", { class: "select w-full", "aria-label": "Recommendations" });
  const preview = h("div");
  let range: [number, number] = [1, 100];
  let token = 0;
  const name = nameInput("recommendations-<today>");

  const show = async () => {
    const mine = ++token;
    if (!lists.value) return preview.replaceChildren(h("p", { class: "text-muted" }, "No recommendations yet. Check for new ones."));
    const [first, last] = range;
    try {
      const tracks = await api<Schemas["LbTrack"][]>("GET", `/api/lb/recs/${lists.value}/tracks?first=${first}&last=${last}`);
      if (mine !== token) return;
      const rows = tracks.map((t) => () => row({ title: t.title ?? "", artist: t.artist ?? "", album: t.album ?? undefined, side: String(t.rank), cover: coverOf(t.release_mbid) }));
      preview.replaceChildren(
        h("p", { class: "mb-2" }, `${tracks.length} songs will be pulled`),
        field("Playlist name", name),
        h("div", { class: "mb-4" }, button("Pull", pull, "primary")),
        paged(rows, ["song", "songs"]),
      );
    } catch (err) {
      failed(err);
    }
  };
  const pull = async () => {
    try {
      const body = { last_updated: Number(lists.value), first_rank: range[0], last_rank: range[1], name: name.value.trim() || null };
      await api("POST", "/api/pulls/recs", body satisfies Schemas["NewRecsPull"]);
      toast("Pull started");
    } catch (err) {
      failed(err);
    }
  };
  const load = async (select?: number) => {
    try {
      const all = await api<Schemas["LbList"][]>("GET", "/api/lb/recs");
      lists.replaceChildren(...all.map((l) => h("option", { value: String(l.last_updated) }, `${day(l.last_updated)} (${l.songs} songs)`)));
      if (select) lists.value = String(select);
      await show();
    } catch (err) {
      failed(err);
    }
  };
  const check = async () => {
    try {
      const r = await api<Schemas["RecCheckOut"]>("POST", "/api/lb/recs/check");
      toast(r.new ? "New recommendations found" : r.list ? "No new recommendations yet" : "ListenBrainz has no recommendations for you yet");
      await load(r.list?.last_updated);
    } catch (err) {
      failed(err);
    }
  };
  lists.onchange = () => void show();
  void load();
  return h(
    "div",
    {},
    h("div", { class: "mb-4 flex gap-2" }, lists, tall(button("Check for new", check))),
    rangePicker((first, last) => ((range = [first, last]), void show())),
    preview,
  );
}

// ── radio ───────────────────────────────────────────────────────────────

function radioPane(): HTMLElement {
  const artist = h("input", { class: "input input-lg w-full flex-none md:w-auto md:flex-1", type: "search", placeholder: "Artist", "aria-label": "Artist", required: true });
  const level = h("select", { class: "select select-lg w-auto min-w-0 flex-1 md:flex-none", "aria-label": "Level" }, ...["easy", "medium", "hard"].map((l) => h("option", { value: l }, l[0].toUpperCase() + l.slice(1))));
  const preview = h("div");
  const name = nameInput("radio-<artist>-<today>");

  const pick = (tracks: RadioTrack[]) => {
    const list = h("div");
    const number = (label: string, value: number) =>
      h("input", { class: "input w-24", type: "number", min: "1", max: String(tracks.length), value: String(value), "aria-label": label, oninput: draw });
    const from = number("Start", 1);
    const to = number("End", tracks.length);
    const chosen = () => tracks.slice(Math.max(Number(from.value), 1) - 1, Number(to.value));
    function draw(): void {
      const songs = chosen();
      list.replaceChildren(h("p", { class: "mb-2" }, `${songs.length} songs will be pulled`), paged(songs.map((t) => () => row({ title: t.title, artist: t.artist, album: t.album ?? undefined, cover: coverOf(t.release_mbid) })), ["song", "songs"]));
    }
    const pull = async () => {
      try {
        const body = { artist: artist.value.trim(), level: level.value as Schemas["NewRadioPull"]["level"], tracks: chosen(), name: name.value.trim() || null };
        await api("POST", "/api/pulls/radio", body);
        toast("Pull started");
      } catch (err) {
        failed(err);
      }
    };
    draw();
    preview.replaceChildren(
      h("div", { class: "mb-4 flex items-end gap-2" }, field("Start", from, "block"), field("End", to, "block"), tall(button("Reroll", () => void roll()))),
      field("Playlist name", name),
      h("div", { class: "mb-4" }, button("Pull", pull, "primary")),
      list,
    );
  };
  const roll = async () => {
    try {
      const q = new URLSearchParams({ artist: artist.value.trim(), level: level.value });
      const tracks = await api<RadioTrack[]>("GET", `/api/lb/radio?${q}`);
      if (tracks.length) pick(tracks);
      else preview.replaceChildren(h("p", { class: "text-muted" }, "No songs came back. Reroll or try another level."));
    } catch (err) {
      failed(err);
    }
  };
  const go = h("button", { class: "btn btn-primary btn-lg", type: "submit" }, "Get radio");
  const form = h(
    "form",
    { class: "mb-6 flex flex-wrap gap-2 md:flex-nowrap", onsubmit: (e: Event) => (e.preventDefault(), void roll()) },
    artist,
    level,
    go,
  );
  return h("div", {}, form, preview);
}

// ── pulls ───────────────────────────────────────────────────────────────

const TONES: Record<string, Tone> = { running: "live", done: "ok", cancelled: "idle" };

function pullLine(p: Pull, reload: () => void): HTMLElement {
  const cancel = async () => {
    try {
      await api("POST", `/api/pulls/${p.id}/cancel`);
      reload();
    } catch (err) {
      failed(err);
    }
  };
  return h(
    "div",
    { class: "mb-3" },
    h("div", { class: "title" }, p.name),
    p.lb_last_updated != null && h("div", { class: "text-muted" }, `Playlist of ${day(p.lb_last_updated)}`),
    p.first_rank != null && h("div", { class: "text-muted" }, `Pulled from №${p.first_rank} to №${p.last_rank}`),
    h("div", { class: "text-muted" }, `${p.in_playlist} of ${p.total} in playlist · ${p.queued} queued · ${p.failed} failed`),
    h("div", { class: "flex items-center gap-2" }, state(p.state, TONES[p.state]), p.state === "running" ? button("Cancel", cancel, "danger") : ""),
  );
}

function pullsRail(): HTMLElement {
  const list = h("div");
  const load = async () => {
    try {
      const pulls = await api<Pull[]>("GET", "/api/pulls");
      list.replaceChildren(...(pulls.length ? pulls.map((p) => pullLine(p, () => void load())) : [h("p", { class: "text-muted" }, "No pulls yet.")]));
    } catch (err) {
      clearInterval(timer);
      failed(err);
    }
  };
  const timer = window.setInterval(() => (list.isConnected ? void load() : clearInterval(timer)), POLL_MS);
  void load();
  return railSection("Pulls", list);
}
