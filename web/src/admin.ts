// Admin tab (#125): the users (Navidrome owns the accounts; no secrets here)
// and the peers with a bad record, banned or not, which can be cleared.
import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import { button, confirm, modalForm, pageHead, paged, state, toast } from "./ui";

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
    h("section", { class: "mb-10" }, h("h2", { class: "rail-title" }, "Users"), h("p", { class: "mb-3 text-xs text-muted" }, "Adds the account to Navidrome too. Anyone made in Navidrome directly appears here after their first login."), newUserForm(users), users),
    h("section", {}, h("h2", { class: "rail-title" }, "Peers with a bad record"), peers),
  );
}

function newUserForm(users: HTMLElement): HTMLElement {
  const input = (label: string, name: string, type: string, autocomplete: string) =>
    h("input", { class: "input input-sm w-full", name, type, autocomplete, required: true, placeholder: label, "aria-label": label });
  const add = h("button", { class: "btn btn-primary btn-sm", type: "submit" }, "Add user");
  const form = h(
    "form",
    {
      class: "mt-3 flex max-w-sm flex-col gap-2",
      onsubmit: async (event: Event) => {
        event.preventDefault();
        const data = new FormData(form);
        if (data.get("password") !== data.get("confirm")) {
          toast("The passwords don't match.", "err");
          return;
        }
        add.disabled = true;
        try {
          const user = await api<Schemas["UserView"]>("POST", "/api/admin/users", {
            username: data.get("username"),
            password: data.get("password"),
            is_admin: data.get("is_admin") === "on",
          } satisfies Record<keyof Schemas["NewUser"], unknown>);
          toast(`Added ${user.username}.`);
          form.reset();
          panel.removeAttribute("open");
          void loadUsers(users);
        } catch (e) {
          fail(e);
        } finally {
          add.disabled = false;
        }
      },
    },
    input("Username", "username", "text", "off"),
    input("Password", "password", "password", "new-password"),
    input("Repeat password", "confirm", "password", "new-password"),
    h(
      "div",
      { class: "mt-1 flex items-center justify-between gap-2" },
      h("label", { class: "flex items-center gap-2 text-sm" }, h("input", { class: "checkbox checkbox-sm", name: "is_admin", type: "checkbox" }), "Admin"),
      add,
    ),
  );
  const panel = h("details", { class: "fold-panel" }, h("summary", {}, h("span", { class: "rail-title mb-0! text-lg" }, "Add user")), form);
  return panel;
}

async function loadUsers(el: HTMLElement): Promise<void> {
  try {
    const [list, me] = await Promise.all([api<Schemas["UserView"][]>("GET", "/api/admin/users"), api<Schemas["Me"]>("GET", "/api/auth/me")]);
    const remove = async (u: Schemas["UserView"]) => {
      if (!(await confirm(`Delete ${u.username}? Their Navidrome account goes too. Their music stays.`, "Delete"))) return;
      try {
        await api("DELETE", `/api/admin/users/${u.id}`);
        toast(`Deleted ${u.username}.`);
        void loadUsers(el);
      } catch (e) {
        fail(e);
      }
    };
    const changePassword = async (u: Schemas["UserView"]) => {
      const values = await modalForm(`New password for ${u.username}`, "Change password", [
        { label: "New password", name: "password", type: "password", autocomplete: "new-password" },
        { label: "Repeat password", name: "confirm", type: "password", autocomplete: "new-password" },
      ]);
      if (!values) return;
      if (values.password !== values.confirm) {
        toast("The passwords don't match.", "err");
        return;
      }
      try {
        await api("PUT", `/api/admin/users/${u.id}/password`, { password: values.password } satisfies Schemas["NewPassword"]);
        toast(`Changed ${u.username}'s password.`);
        void loadUsers(el);
      } catch (e) {
        fail(e);
      }
    };
    el.replaceChildren(paged(
      list.map((u) =>
        h(
          "div",
          { class: "row user-row" },
          h("span", { class: "cover", "aria-hidden": "true" }, u.username.slice(0, 2).toUpperCase()),
          h("div", { class: "min-w-0" }, h("div", { class: "title" }, u.username), h("div", { class: "sub" }, [`since ${day(u.created_at)}`, u.is_admin && "Admin", u.has_listenbrainz_token && "ListenBrainz token"].filter(Boolean).join(" · "))),
          u.status === "active" ? state("Active", "ok") : state(u.status === "needs_reauth" ? "Needs to log in again" : u.status, "wait"),
          h(
            "div",
            { class: "flex justify-end gap-2" },
            button("Password", () => void changePassword(u)),
            u.username === me.username ? "" : button("Delete", () => void remove(u), "danger"),
          ),
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
