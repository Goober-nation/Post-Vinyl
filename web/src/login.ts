import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";
import logoUrl from "./logo.svg";

export function loginPage(onLogin: (me: Schemas["Me"]) => void): HTMLElement {
  const error = h("p", { class: "text-error text-sm", role: "alert" });
  const button = h("button", { class: "btn btn-primary w-full", type: "submit" }, "Log in");
  const intro = h("p", { class: "text-muted" }, "Log in with your Navidrome account.");
  let firstAdmin = false;
  const field = (label: string, name: string, type: string, autocomplete: string) =>
    h(
      "label",
      { class: "floating-label w-full" },
      h("span", {}, label),
      h("input", { class: "input w-full", name, type, autocomplete, required: true, placeholder: label }),
    );

  const confirm = field("Repeat password", "confirm", "password", "new-password");
  confirm.hidden = true;
  const showFirstAdmin = (on: boolean) => {
    firstAdmin = on;
    confirm.hidden = !on;
    confirm.querySelector("input")!.required = on;
    button.textContent = on ? "Create admin" : "Log in";
    intro.textContent = on
      ? "Navidrome has no users yet. Create its first admin to start."
      : "Log in with your Navidrome account.";
  };

  const form = h(
    "form",
    {
      class: "card-body gap-4",
      onsubmit: async (event: Event) => {
        event.preventDefault();
        const data = new FormData(form);
        error.textContent = "";
        if (firstAdmin && data.get("password") !== data.get("confirm")) {
          error.textContent = "The passwords don't match.";
          return;
        }
        button.disabled = true;
        try {
          onLogin(
            await api<Schemas["Me"]>("POST", firstAdmin ? "/api/auth/first-admin" : "/api/auth/login", {
              username: data.get("username"),
              password: data.get("password"),
            } satisfies Record<keyof Schemas["LoginBody"], unknown>),
          );
        } catch (e) {
          error.textContent = loginError(e);
          if (e instanceof ApiError && e.status === 409) showFirstAdmin(false);
        } finally {
          button.disabled = false;
        }
      },
    },
    h("h1", { class: "flex items-center gap-3" }, h("img", { src: logoUrl, alt: "", class: "size-12" }), "Post-Vinyl"),
    intro,
    field("Username", "username", "text", "username"),
    field("Password", "password", "password", "current-password"),
    confirm,
    error,
    button,
  );
  void api<Schemas["FirstAdminStatus"]>("GET", "/api/auth/first-admin")
    .then((status) => showFirstAdmin(status.needed))
    .catch(() => {}); // the login attempt reports an unreachable Navidrome
  return h(
    "main",
    { class: "min-h-screen grid place-items-center p-4" },
    h("div", { class: "card bg-base-200 border border-line w-full max-w-sm" }, form),
  );
}

function loginError(e: unknown): string {
  if (!(e instanceof ApiError)) return "Can't reach Post-Vinyl.";
  if (e.status === 409) return "An admin already exists. Log in instead.";
  if (e.status === 401) return "Wrong username or password.";
  if (e.status === 429)
    return `Too many failed logins. Try again in ${Math.ceil(Number(e.retryAfter ?? 60) / 60)} min.`;
  if (e.status === 503) return "Navidrome isn't reachable, so logins can't be checked.";
  return e.message;
}
