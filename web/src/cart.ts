// The download cart lives in the browser (user, 2026-10-30) and goes out in
// one batch call. Kept in localStorage so a reload doesn't empty it.
import type { Schemas } from "./api";

export type CartEntry = Schemas["CartItem"] & { title: string; artist: string; error?: string };

const KEY = "pv-cart";
export const CART_MAX = 100; // the batch call's limit
const listeners = new Set<() => void>();
let items: CartEntry[] = load();

function load(): CartEntry[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(parsed) ? (parsed as CartEntry[]) : [];
  } catch {
    return [];
  }
}

function save(next: CartEntry[]): void {
  items = next;
  try {
    localStorage.setItem(KEY, JSON.stringify(items));
  } catch {
    // storage blocked: the cart lasts until the page closes
  }
  listeners.forEach((fn) => fn());
}

const same = (a: { source: string; track_id: string }, b: { source: string; track_id: string }) =>
  a.source === b.source && a.track_id === b.track_id;

export const cart = {
  items: (): readonly CartEntry[] => items,
  has: (t: { source: string; track_id: string }) => items.some((i) => same(i, t)),
  add(entry: CartEntry): boolean {
    if (this.has(entry) || items.length >= CART_MAX) return false;
    save([...items, entry]);
    return true;
  },
  remove: (t: { source: string; track_id: string }) => save(items.filter((i) => !same(i, t))),
  replace: (next: CartEntry[]) => save(next),
  /** Calls `fn` on every change while `el` is on the page; stops once it's gone. */
  watch(el: Element, fn: () => void): void {
    let shown = false;
    const listener = () => {
      if (el.isConnected) shown = true;
      else if (shown) return void listeners.delete(listener);
      fn();
    };
    listeners.add(listener);
  },
};
