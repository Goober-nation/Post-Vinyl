// Admin tab (#125): the users (Navidrome owns the accounts; no secrets here)
// and the peers with a bad record, banned or not, which can be cleared.
import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import { button, confirm, pageHead, paged, state, toast } from "./ui";

const fail = (e: unknown) => toast(e instanceof ApiError ? e.message : "Can't reach Post-Vinyl.", "err");
const day = (s: number) => new Date(s * 1000).toLocaleDateString();

export function adminPage(): HTMLElement {
  const users = h("div", {}, h("p", { class: "py-2 text-muted" }, "Loading…"));
  const peers = h("div", {}, h("p", { class: "py-2 text-muted" }, "Loading…"));
  void loadUsers(users);
  void loadPeers(peers);
  return h(
    "div",
    { class: "center-main" },
    pageHead("Admin"),
    h("section", { class: "mb-10" }, h("h2", { class: "rail-title" }, "Users"), h("p", { class: "mb-3 text-xs text-muted" }, "Accounts are made in Navidrome; a user appears here after their first login."), users),
    h("section", {}, h("h2", { class: "rail-title" }, "Peers with a bad record"), peers),
  );
}

async function loadUsers(el: HTMLElement): Promise<void> {
  try {
    const list = await api<Schemas["UserView"][]>("GET", "/api/admin/users");
    el.replaceChildren(paged(
      list.map((u) =>
        h(
          "div",
          { class: "row" },
          h("span", { class: "cover", "aria-hidden": "true" }, u.username.slice(0, 2).toUpperCase()),
          h("div", { class: "min-w-0" }, h("div", { class: "title" }, u.username), h("div", { class: "sub" }, `since ${day(u.created_at)}`)),
          h("div", { class: "row-extra sub" }, [u.is_admin && "Admin", u.has_listenbrainz_token && "ListenBrainz token"].filter(Boolean).join(" · ")),
          u.status === "active" ? state("Active", "ok") : state(u.status === "needs_reauth" ? "Needs to log in again" : u.status, "wait"),
          h("span"),
        ),
      ),
      ["user", "users"],
    ));
  } catch (e) {
    el.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl."));
  }
}

async function loadPeers(el: HTMLElement): Promise<void> {
  try {
    const list = await api<Schemas["SlskdPeer"][]>("GET", "/api/admin/peers");
    if (!list.length) {
      el.replaceChildren(h("p", { class: "py-2 text-muted" }, "No peer has a bad record."));
      return;
    }
    const clear = async (path: string, ask: string, done: (n: number) => string) => {
      if (!(await confirm(ask, "Clear"))) return;
      try {
        const r = await api<Schemas["Cleared"]>("DELETE", path);
        toast(done(r.cleared));
        void loadPeers(el);
      } catch (e) {
        fail(e);
      }
    };
    const banned = list.filter((p) => p.blocked_at).length;
    el.replaceChildren(
      h(
        "div",
        { class: "mb-3 flex items-center gap-3" },
        h("span", { class: "text-muted" }, `${list.length} ${list.length === 1 ? "peer" : "peers"}, ${banned} banned`),
        h("span", { class: "flex-1" }),
        button("Clear all", () => void clear("/api/admin/peers", `Clear the record of all ${list.length} peers? Banned ones can be picked again.`, (n) => `Cleared ${n} ${n === 1 ? "peer" : "peers"}.`), "danger"),
      ),
      paged(
        list.map((p) =>
          h(
            "div",
            { class: "row" },
            h("span", { class: "cover", "aria-hidden": "true" }, p.username.slice(0, 2).toUpperCase()),
            h("div", { class: "min-w-0" }, h("div", { class: "title font-mono" }, p.username), h("div", { class: "sub" }, `${p.failure_count} failed ${p.failure_count === 1 ? "download" : "downloads"}`)),
            h("div", { class: "row-extra sub" }, p.blocked_at ? `banned ${day(p.blocked_at)}` : ""),
            p.blocked_at ? state("Banned", "err") : state("Watched", "wait"),
            h(
              "button",
              {
                type: "button",
                class: "icon-btn",
                "aria-label": `Clear ${p.username}`,
                title: "Clear this peer's record",
                onclick: () => void clear(`/api/admin/peers/${encodeURIComponent(p.username)}`, `Clear ${p.username}'s record?`, () => `Cleared ${p.username}.`),
              },
              "×",
            ),
          ),
        ),
        ["peer", "peers"],
      ),
    );
  } catch (e) {
    el.replaceChildren(h("div", { role: "alert", class: "alert alert-error" }, e instanceof ApiError ? e.message : "Can't reach Post-Vinyl."));
  }
}
