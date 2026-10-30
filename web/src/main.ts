import "./style.css";
import { ApiError, api, whenLoggedOut, type Schemas } from "./api";
import { h } from "./dom";
import { loginPage } from "./login";
import { searchPage } from "./search";
import { slskdPage } from "./slskd";
import { downloadsPage } from "./downloads";
import logoUrl from "./logo.svg";
import { page } from "./ui";

// Hash routes (#/downloads…): a reload stays on the same tab; the session
// cookie keeps the login (user, 2026-10-30).
type Tab = { path: string; label: string; adminOnly?: boolean };
const TABS: Tab[] = [
  { path: "search", label: "Search" },
  { path: "slskd", label: "slskd" },
  { path: "downloads", label: "Downloads" },
  { path: "files", label: "Files" },
  { path: "settings", label: "Settings" },
  { path: "admin", label: "Admin", adminOnly: true },
];
// The component sheet, in `npm run dev` only.
const KIT: Tab = { path: "kit", label: "Kit" };

const root = document.getElementById("app")!;
let me: Schemas["Me"] | null = null;

function currentTab(): Tab {
  const path = location.hash.replace(/^#\/?/, "").split("?")[0];
  if (import.meta.env.DEV && path === KIT.path) return KIT;
  return TABS.find((t) => t.path === path) ?? TABS[0];
}

function render(): void {
  if (!me) {
    root.replaceChildren(loginPage((user) => ((me = user), render())));
    return;
  }
  const tab = currentTab();
  const nav = h(
    "nav",
    { class: "navtabs", "aria-label": "Main" },
    ...TABS.map((t) => h("a", { href: `#/${t.path}`, "aria-current": t === tab ? "page" : false }, t.label)),
  );
  const logout = h(
    "button",
    {
      type: "button",
      class: "btn btn-ghost btn-sm",
      onclick: async () => {
        await api("POST", "/api/auth/logout").catch(() => {});
        me = null;
        render();
      },
    },
    "Log out",
  );
  root.replaceChildren(
    h(
      "header",
      { class: "topbar" },
      h("span", { class: "flex items-center gap-2 font-display text-lg whitespace-nowrap md:gap-2.5 md:text-[22px]" }, logo(), "Post-Vinyl"),
      nav,
      h(
        "div",
        { class: "ml-auto flex items-center gap-3" },
        h("span", { class: "hidden text-muted sm:inline" }, me.username),
        logout,
      ),
    ),
    h("main", {}, content(tab)),
  );
  nav.querySelector('[aria-current="page"]')?.scrollIntoView({ inline: "nearest", block: "nearest" });
  document.title = `${tab.label} · Post-Vinyl`;
}

function content(tab: Tab): HTMLElement {
  if (import.meta.env.DEV && tab === KIT) {
    // loaded on demand, so the production build leaves the kit out
    const spot = h("div");
    void import("./kit").then((kit) => spot.replaceWith(kit.kitPage()));
    return spot;
  }
  // Admin-only parts are shown to everyone and refuse for non-admins (user,
  // 2026-10-30); the API's 403 stays the real guard.
  if (tab.adminOnly && !me?.is_admin) return page([adminsOnly()]);
  if (tab.path === "search") return searchPage();
  if (tab.path === "slskd") return slskdPage();
  if (tab.path === "downloads" && me) return downloadsPage(me);
  return page([h("p", { class: "text-muted" }, `${tab.label}: coming in milestone 8.`)]);
}

export function adminsOnly(): HTMLElement {
  return h("div", { role: "alert", class: "alert alert-warning" }, "Admins only.");
}

function logo(): HTMLImageElement {
  return h("img", { src: logoUrl, alt: "", class: "size-7 md:size-9" });
}

whenLoggedOut(() => {
  me = null;
  render();
});
window.addEventListener("hashchange", render);

api<Schemas["Me"]>("GET", "/api/auth/me")
  .then((user) => (me = user))
  .catch((e) => {
    if (!(e instanceof ApiError)) throw e;
  })
  .finally(render);
