# Frontend

`web/`: TypeScript with no UI framework, Tailwind CSS v4 and daisyUI 5, built with Vite. A build step
was needed for Tailwind anyway, and types catch API drift. daisyUI is CSS-only, so it works on plain
DOM.

The Dockerfile's Node stage builds `web/dist`, and FastAPI serves it on the same origin as the API,
so the session cookie and CSRF check work with no CORS. Neither Node nor `node_modules` ships.
`index.html` is rechecked on every load, and hashed `/assets/` files cache as usual.

## Commands

From `web/`:

```bash
npm ci
```

```bash
npm run build
```

Dev server with live reload, proxying `/api`. `PV_API` must equal the app's `PV_PUBLIC_ORIGIN`:

```bash
PV_API=http://localhost:8000 npm run dev
```

After any API change, regenerate the types against an app run with `PV_DEV=1`:

```bash
PV_API=http://localhost:8000 npm run gen:api
```

`src/api-types.ts` is generated and committed, so a renamed field is a compile error. `npm run build`
runs `tsc --noEmit`. TypeScript stays on 5.x until openapi-typescript supports a newer one.

## Structure

| File | Job |
|---|---|
| `main.ts` | Shell, hash routes (`#/downloads`), header |
| `api.ts` | Calls, errors, logged-out handling |
| `dom.ts`, `ui.ts` | `h()` element helper, shared components |
| `search.ts`, `cart.ts` | Search tab, browser-kept cart |
| `slskd.ts`, `listenbrainz.ts`, `downloads.ts`, `files.ts`, `settings.ts`, `admin.ts` | One per tab |
| `login.ts`, `services.ts`, `tips.ts` | Login page, service lights, tips panels |
| `kit.ts` | Component sheet at `#/kit`, in `npm run dev` only |

## Behaviour rules

- **Live updates by polling**, only while a tab is open: downloads and slskd about 1.5 s, pulls 3 s,
  toasts 5 s, not found 10 s, service lights and files 30 s.
- A reload keeps the tab; the session cookie keeps the login.
- The cart lives in `localStorage`, the last searches in `sessionStorage`, folded tips in a cookie
  per tab. Nothing there is needed for the app to work.
- Admin-only parts are shown to everyone and refuse with "Admins only".
- Each section of a page loads and fails on its own.
- Settings screens are drawn from the schema, not hand-written per field.

## Design

Dark only. Layout: a top bar with tabs, a main list with a rail, and a fixed bottom bar for download
activity, as text such as "62% · 1.2 MB/s".

| Token | Value |
|---|---|
| Accent (warm yellow, one variable) | `#e8c468` |
| Backgrounds | `#101114`, `#17191d`, `#202227` |
| Lines | `#2b2e34` |
| Text, muted | `#f7f7f7`, `#9a9a9a` |

- Flat surfaces, hairline borders, 3–6 px corners; no gradients, glass, glow or oversized cards.
- **Fonts**: IBM Plex Sans and Mono, bundled, so no request leaves the origin. Forta for the wordmark
  and page titles.
- **A state is a dot plus words**, never colour alone. Queued is a hollow dot.
- What is happening now shows a small turning record, still under reduced motion.
- Logo `src/logo.svg`; tab icon `src/favicon.svg`, its own simpler file, also rendered to
  `public/favicon.ico` and `favicon-32.png`. iOS and Android icons in `public/` use the big logo,
  padded on the dark background, and `site.webmanifest` lists them. Devices fetch these with no
  session, so each one is in `ICON_FILES` in `app/middleware.py`.
- Theme in `src/style.css`, components in `src/ui.ts`.
- Below `md`, shelves scroll sideways in one row.
