// The one render helper: build an element with attributes and children.
type Child = Node | string | null | false | undefined;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | boolean | EventListener> = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (typeof value === "function") el.addEventListener(key.replace(/^on/, ""), value);
    else if (value === true) el.setAttribute(key, "");
    else if (value !== false) el.setAttribute(key, value);
  }
  for (const child of children) if (child) el.append(child);
  return el;
}
