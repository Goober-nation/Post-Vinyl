import { ApiError, api, type Schemas } from "./api";
import { h } from "./dom";

export function loginPage(onLogin: (me: Schemas["Me"]) => void): HTMLElement {
  const error = h("p", { class: "text-error text-sm", role: "alert" });
  const button = h("button", { class: "btn btn-primary w-full", type: "submit" }, "Log in");
  const field = (label: string, name: string, type: string, autocomplete: string) =>
    h(
      "label",
      { class: "floating-label w-full" },
      h("span", {}, label),
      h("input", { class: "input w-full", name, type, autocomplete, required: true, placeholder: label }),
    );

  const form = h(
    "form",
    {
      class: "card-body gap-4",
      onsubmit: async (event: Event) => {
        event.preventDefault();
        const data = new FormData(form);
        error.textContent = "";
        button.disabled = true;
        try {
          onLogin(
            await api<Schemas["Me"]>("POST", "/api/auth/login", {
              username: data.get("username"),
              password: data.get("password"),
            } satisfies Record<keyof Schemas["LoginBody"], unknown>),
          );
        } catch (e) {
          error.textContent = loginError(e);
        } finally {
          button.disabled = false;
        }
      },
    },
    h("h1", { class: "card-title text-2xl" }, "Post-Vinyl"),
    h("p", { class: "text-sm opacity-70" }, "Log in with your Navidrome account."),
    field("Username", "username", "text", "username"),
    field("Password", "password", "password", "current-password"),
    error,
    button,
  );
  return h(
    "main",
    { class: "min-h-screen grid place-items-center p-4" },
    h("div", { class: "card bg-base-100 w-full max-w-sm shadow" }, form),
  );
}

function loginError(e: unknown): string {
  if (!(e instanceof ApiError)) return "Can't reach Post-Vinyl.";
  if (e.status === 401) return "Wrong username or password.";
  if (e.status === 429)
    return `Too many failed logins. Try again in ${Math.ceil(Number(e.retryAfter ?? 60) / 60)} min.`;
  if (e.status === 503) return "Navidrome isn't reachable, so logins can't be checked.";
  return e.message;
}
