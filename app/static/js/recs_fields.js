// recs_fields.js — Recs settings field schema, used by the Recs tab's own
// settings form (recs.js). Previously also shared with the Config tab's
// "recs" section, but P6.5-3 moved recs settings entirely onto the Recs
// tab (duplication across two forms was a source of drift), so this is
// recs.js's alone now. Kept as its own leaf module (no imports) since
// recs.js/config.js/state.js already form an import cycle.
//
// P6.5-3b: each enabled checkbox controls automatic pulls only. Manual pulls
// are selected separately beside the Pull button and remain available when a
// category's periodic worker is disabled.

export const RECS_CATEGORY_TABLES = [
  {
    key: "comfort_zone",
    title: "Comfort Zone",
    description: "Personalized recommendations from your ListenBrainz model.",
    fields: [
      {
        key: "comfort_zone_enabled",
        label: "Periodic pulls",
        type: "checkbox",
        hint: "Allow the scheduled worker to pull this category.",
      },
      { key: "comfort_zone_count", label: "Tracks per pull", type: "number", min: "0" },
      {
        key: "comfort_zone_interval_days",
        label: "Interval (days)",
        type: "number",
        min: "1",
      },
      { key: "comfort_zone_playlist_name", label: "Playlist", type: "text" },
    ],
  },
  {
    key: "fresh_picks",
    title: "Fresh Picks",
    description: "New releases from the configured rolling release window.",
    fields: [
      {
        key: "fresh_picks_enabled",
        label: "Periodic pulls",
        type: "checkbox",
        hint: "Allow the nightly worker to pull this category.",
      },
      {
        section: "fresh_picks",
        key: "pull_window",
        label: "Release window",
        type: "text",
        hint: "For example, 30d.",
      },
      {
        section: "fresh_picks",
        key: "offset",
        label: "Newest releases to skip",
        type: "number",
        min: "0",
      },
      {
        section: "fresh_picks",
        key: "count",
        label: "Tracks per pull",
        type: "number",
        min: "0",
      },
      {
        section: "fresh_picks",
        key: "search_buffer",
        label: "Search buffer",
        type: "number",
        min: "0",
      },
      { key: "fresh_picks_playlist_name", label: "Playlist", type: "text" },
    ],
  },
  {
    key: "deep_cuts",
    title: "Deep Cuts",
    description: "Tracks served from new ListenBrainz recommendation playlists.",
    fields: [
      {
        key: "deep_cuts_enabled",
        label: "Periodic pulls",
        type: "checkbox",
        hint: "Allow the scheduled worker to pull this category.",
      },
      { key: "deep_cuts_count", label: "Tracks per pull", type: "number", min: "0" },
      {
        key: "deep_cuts_interval_days",
        label: "Interval (days)",
        type: "number",
        min: "1",
      },
      { key: "deep_cuts_playlist_name", label: "Playlist", type: "text" },
    ],
  },
];

export const RECS_GLOBAL_FIELDS = [
  {
    key: "rotation_trash_rating",
    label: "Rotation trash rating",
    type: "number",
    min: "0",
    max: "5",
    hint: "Tracks at or below this rating move to Trash during rotation.",
  },
];

export const RECS_SETTINGS_FIELDS = [
  ...RECS_CATEGORY_TABLES.flatMap((table) => table.fields),
  ...RECS_GLOBAL_FIELDS,
];
