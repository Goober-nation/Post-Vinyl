import "./style.css";
import { ApiError, api, whenLoggedOut, type Schemas } from "./api";
import { h } from "./dom";
import { loginPage } from "./login";

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

const root = document.getElementById("app")!;
let me: Schemas["Me"] | null = null;

function currentTab(): Tab {
  const path = location.hash.replace(/^#\/?/, "");
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
    { role: "tablist", class: "tabs tabs-border" },
    ...TABS.map((t) =>
      h(
        "a",
        {
          role: "tab",
          href: `#/${t.path}`,
          class: t === tab ? "tab tab-active" : "tab",
          "aria-selected": String(t === tab),
        },
        t.label,
      ),
    ),
  );
  const logout = h(
    "button",
    {
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
      { class: "navbar bg-base-100 shadow-sm gap-4 px-4 flex-wrap" },
      h("span", { class: "font-bold text-lg" }, "Post-Vinyl"),
      nav,
      h("span", { class: "ml-auto text-sm opacity-70" }, me.username),
      logout,
    ),
    h("main", { class: "p-4", role: "tabpanel" }, page(tab)),
  );
  document.title = `${tab.label} · Post-Vinyl`;
}

function page(tab: Tab): HTMLElement {
  // Admin-only parts are shown to everyone and refuse for non-admins (user,
  // 2026-10-30); the API's 403 stays the real guard.
  if (tab.adminOnly && !me?.is_admin) return adminsOnly();
  return h("p", { class: "opacity-70" }, `${tab.label}: coming in milestone 8.`);
}

export function adminsOnly(): HTMLElement {
  return h("div", { role: "alert", class: "alert alert-warning" }, "Admins only.");
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
