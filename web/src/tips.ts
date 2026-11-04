// The small notes on the left of each tab: how things work, in plain words.
import { h } from "./dom";

const TIPS: Record<string, string[]> = {
  search: [
    "Fill the cart, then request everything at once. Songs ",
    "Different editions of an album show up separately, so pick the one with the tracks you want.",
    "Your last search stays here while you look around.",
  ],
  listenbrainz: [
    "ListenBrainz makes your recommendations; \"Check for new\" looks for a fresh list.",
    "Pulls run in the background, behind anything you request yourself.",
    "Searches are limited to 4 a minute, so a big pull takes hours.",
    "Songs you already own go straight into the playlist.",
    "Cancelling leaves downloads that already started to finish.",
    "Live versions, remixes and the like are left out for you.",
  ],
  slskd: [
    "This searches Soulseek directly, for when you want one exact file.",
    "Marks show how each file matches what you typed.",
    "Checked files wait in that peer's queue for as long as it takes.",
    "One-letter titles are hard to find on Soulseek, so expect misses.",
  ],
  downloads: [
    "A slow or busy peer gets skipped, and the next one is tried by itself.",
    "Stuck on a peer? Open the download and try the next one.",
    "A failed song can be requested again, which starts a fresh search.",
    "Pausing the queue stops new searches; running downloads carry on.",
  ],
  files: [
    "This is everything Navidrome has in your library.",
    "Renaming looks up the correct tags first, so check them before applying.",
    "Deleting removes the song for every user.",
  ],
  settings: [
    "Mine is just for you and applies at once.",
    "Server-wide affects everyone and needs a restart.",
    "Your ListenBrainz token unlocks recommendations and radio.",
  ],
  admin: [
    "Users come from Navidrome, so add them there.",
    "Peers that keep failing get banned for a while; you can clear them here.",
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
