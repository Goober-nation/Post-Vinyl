// Header status: lights for Navidrome and slskd; the dropdown pings every service.
import { api, type Schemas } from "./api";
import { h } from "./dom";
import { button } from "./ui";

type Ping = Schemas["ServicePing"];
const POLL_MS = 30_000;

const light = () => h("span", { class: "service-light", "aria-hidden": "true" });
const words = (p: Ping) => (p.state === "ok" ? `${p.ms} ms` : p.state === "off" ? "Turned off" : "Not reachable");

export function servicesMenu(): HTMLElement {
  const lights = h("span", { class: "flex items-center gap-1.5" });
  const list = h("div", { class: "flex flex-col gap-2" });
  const summary = h("summary", { class: "services-button", "aria-label": "Services" }, lights, h("span", { "aria-hidden": "true" }, "▾"));
  const menu = h("details", { class: "services-menu" }, summary, h("div", { class: "services-panel" }, h("h2", { class: "rail-title" }, "Services"), list));
  document.addEventListener("click", (e) => {
    if (!menu.contains(e.target as Node)) menu.removeAttribute("open");
  });

  const rows = new Map<string, { dots: HTMLElement[]; text: HTMLElement; main: boolean; label: string }>();
  const show = (p: Ping) => {
    const row = rows.get(p.name);
    if (!row) return;
    row.dots.forEach((d) => (d.dataset.state = p.state));
    row.text.textContent = words(p);
    if (row.main) summary.title = [...rows.values()].filter((r) => r.main).map((r) => `${r.label}: ${r.text.textContent}`).join(", ");
  };
  const ping = async (name: string) => {
    try {
      show(await api<Ping>("GET", `/api/system/services/${name}`));
    } catch {
      show({ name, state: "down", ms: null });
    }
  };

  void api<Schemas["ServiceInfo"][]>("GET", "/api/system/services").then((services) => {
    for (const s of services) {
      const dot = light();
      const text = h("span", { class: "ml-auto text-xs text-muted" }, s.main ? "" : "Not checked");
      const dots = [dot];
      if (s.main) {
        const top = light();
        lights.append(top);
        dots.push(top);
      }
      rows.set(s.name, { dots, text, main: s.main, label: s.label });
      list.append(h("div", { class: "flex items-center gap-2" }, dot, h("span", {}, s.label), text, button("Ping", () => void ping(s.name))));
    }
    list.append(button("Ping all", () => services.forEach((s) => void ping(s.name))));
    const main = services.filter((s) => s.main);
    const tick = () => {
      if (!menu.isConnected) return clearInterval(timer);
      main.forEach((s) => void ping(s.name));
    };
    const timer = window.setInterval(tick, POLL_MS);
    main.forEach((s) => void ping(s.name));
  });
  return menu;
}
