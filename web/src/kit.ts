// Every shared component on one page (#/kit, `npm run dev` only), to check
// them in both themes without the backend's data.
import { h } from "./dom";
import {
  button,
  confirm,
  filters,
  iconButton,
  kv,
  page,
  pageHead,
  progress,
  railSection,
  row,
  setting,
  slider,
  state,
  tag,
  toast,
} from "./ui";

export function kitPage(): HTMLElement {
  const rows = h(
    "div",
    { class: "rows" },
    row({ title: "Teardrop", artist: "Massive Attack", album: "Mezzanine", side: "A3", art: "#8a3b2e", selected: true, extra: progress(0.62, "1.2 MB/s"), state: state("Downloading", "live"), action: iconButton("More", "⋯", () => {}) }),
    row({ title: "Archangel", artist: "Burial", album: "Untrue", side: "A2", art: "#3d4a5c", state: state("Queued at peer · #4", "wait"), action: iconButton("More", "⋯", () => {}) }),
    row({ title: "Xtal", artist: "Aphex Twin", album: "Selected Ambient Works 85–92", side: "A1", art: "#6b6f78", extra: h("span", { class: "sub font-mono" }, "FLAC"), state: state("Done", "ok") }),
    row({ title: "Svefn-g-englar", artist: "Sigur Rós", album: "Ágætis byrjun", side: "A2", art: "#9a8a6a", state: state("Failed · 5 peers tried", "err") }),
    row({ title: "Windowlicker", artist: "Aphex Twin", state: state("Searching", "idle") }),
    row({ title: "Roads", artist: "Portishead", album: "Dummy", state: state("In cart", "accent") }),
  );
  const file = (path: string, fmt: string, size: string, mark: Node, disabled = false) =>
    h(
      "label",
      { class: disabled ? "file text-muted" : "file" },
      h("input", { type: "checkbox", class: "checkbox checkbox-sm checkbox-accent", disabled }),
      h("span", { class: "path" }, path),
      h("span", { class: "hidden md:block" }, fmt),
      h("span", { class: "hidden font-mono md:block" }, size),
      mark,
    );
  return page(
    [
      pageHead("Kit", h("span", { class: "cat" }, "PV-0001 – 0142"), filters("State", [["all", "All"], ["run", "Downloading"], ["failed", "Failed"]], "all", () => {})),
      rows,
      h(
        "div",
        { class: "peer mt-6" },
        h("div", { class: "peer-head" }, h("span", { class: "font-mono text-base-content" }, "nightdrive_93"), "queue 2", h("span", { class: "font-mono" }, "880 KB/s")),
        file("BoC/MHTRTC/Roygbiv.mp3", "320", "5.5 MB", tag("correct", "ok")),
        file("BoC/live/Roygbiv (live 1998).mp3", "192", "6.0 MB", tag("excluded: live"), true),
      ),
      setting("Results per search", "How many songs a catalog search shows.", slider("Results per search", 25, 5, 100)),
      setting("Searches per minute", "Higher gets the Soulseek account throttled.", h("input", { class: "input input-sm w-full", type: "number", value: "3", "aria-label": "Searches per minute" }), true),
      h(
        "div",
        { class: "mt-6 flex flex-wrap gap-2" },
        button("Toast", () => toast("Queued 2 files.")),
        button("Error toast", () => toast("slskd isn't reachable.", "err")),
        button("Confirm", async () => toast((await confirm("Delete this file for every user?", "Delete")) ? "Deleted." : "Kept.")),
        button("Drop", () => {}, "danger"),
        button("Request 3 songs", () => {}, "primary"),
      ),
    ],
    [
      railSection("Selected", h("div", { class: "cat" }, "PV-0142"), h("div", { class: "title" }, "Teardrop"), h("div", { class: "sub" }, "Massive Attack · Mezzanine")),
      railSection("Queue", kv([["Running", "3"], ["Waiting", "11"], ["Speed", h("span", { class: "font-mono" }, "1.8 MB/s")]]), h("div", { class: "mt-4" }, pauseButton())),
    ],
  );
}

// The queue's pause/resume as the downloads tab will have it: one button that
// flips, with a toast saying what happened.
function pauseButton(): HTMLButtonElement {
  let paused = false;
  const btn = button("Pause queue", () => {
    paused = !paused;
    btn.textContent = paused ? "Resume queue" : "Pause queue";
    toast(paused ? "Queue paused. Running downloads carry on." : "Queue resumed.");
  });
  return btn;
}
