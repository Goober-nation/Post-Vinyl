// The small notes on the left of each tab: how things work, in plain words.
import { h } from "./dom";

const TIPS: Record<string, string[]> = {
  search: [
    "Warning: unfortunately, soulseek (the default download source) does not have every song in the world. I have done what i could to maximize the success rate of backend searches, but misses are still present :(",
    "Fill the cart, then request everything at once to append the cart items to the service-wide slskd download queue",
    "Search respects the settings of catalog priority: If Musicbrainz is prioritized over Deezer and is reachable, search results will come from it, and vise versa",
    "Rankings look weird? try putting Deezer as #1 priority in the settings",
  ],
  listenbrainz: [
    "ListenBrainz makes your recommendations once in a while (no clue as to how often); \"Check for new\" looks for a fresh list",
    "After your token is set, Listenbrainz will take its time to gather your tastes and figure out what you like, so until then I suggest using the radio mode",
    "Pulls run in the background. Anything manually requested via catalog or slskd is prioritized",
    "Searches are limited to 4 a minute, so a big pull takes a long time",
    "Songs you already own go straight into the playlist",
    "Downloads that already started still finish when you cancel a pull",
    "Live versions, remixes and the like are left out of the pulls",
  ],
  slskd: [
    "This searches Soulseek directly, for when you want to control exactly which files are downloaded",
    "Results are sorted by file matches, availability and upload speed",
    "One-letter titles are hard to find on Soulseek, so expect misses. Try searching by album/artist directly"
  ],
  downloads: [
    "For catalog searches, a slow or busy peer gets skipped, and the next one is tried by itself",
    "For catalog searches, a failed song can be requested again, which starts a fresh search",
    "Downloads started in the slskd tab intentionally do not get the automations of the catalog-based ones",
    "Stuck on a peer? Click on the download and skip to the next peer",
    "Pausing the queue stops new searches; running downloads carry on.",
  ],
  files: [
    "After a song downloads it will take a little time for the app to tag it and move it into the library, refresh to check if the file got there",
    "This is everything Navidrome has in your library",
    "Renaming looks up the correct tags first and assigns them upon confirmation, so check them before applying",
    "Deleting removes the song for every user",
  ],
  settings: [
    "For Russian users: Deezer (and only some parts of Listenbrainz) works without a VPN/proxy, put it as first priority for ease of use",
    "Personal settings are for you and apply upon save without restarting the app",
    "Server-wide settings affect everyone and need a restart",
    "Your ListenBrainz token unlocks recommendations and radio",
    "Prioritizing Deezer has no downsides: speeds up search, better rankings",
    "Turning services off may have different effects, make sure you know what you are doing"
  ],
  admin: [
    "You can manage users from here. Each user here is tied to the Navidrome account of the same name",
    "Peers that keep failing get banned for a while. You can clear the bans here",
  ],
};

const YEAR_S = 31_536_000;
const cookieName = (tab: string) => `pv-tips-${tab}`;

// each tab's tips start open; a cookie remembers them being folded
function isOpen(tab: string): boolean {
  return !document.cookie.split("; ").includes(`${cookieName(tab)}=closed`);
}

function remember(tab: string, open: boolean): void {
  document.cookie = `${cookieName(tab)}=${open ? "open" : "closed"}; path=/; max-age=${YEAR_S}; samesite=lax`;
}

export function tipsRail(tab: string): HTMLElement | string {
  const tips = TIPS[tab];
  if (!tips) return "";
  const list = h("ol", {}, ...tips.map((tip) => h("li", {}, tip)));
  const arrow = h("button", { type: "button", class: "icon-btn -my-1 -mr-1 ml-auto text-xl" });
  const show = (open: boolean) => {
    list.hidden = !open;
    arrow.textContent = open ? "▾" : "▸";
    arrow.setAttribute("aria-expanded", String(open));
    arrow.setAttribute("aria-label", open ? "Fold tips" : "Open tips");
  };
  arrow.addEventListener("click", () => {
    const open = list.hidden;
    remember(tab, open);
    show(open);
  });
  show(isOpen(tab));
  return h(
    "aside",
    { class: "tips-rail", "aria-label": "Tips" },
    h("div", { class: "flex items-center" }, h("h2", { class: "rail-title mb-0!" }, "Tips"), arrow),
    list,
  );
}
