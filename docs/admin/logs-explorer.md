# Logs Explorer

The Logs Explorer is a top-level screen at `/logs` (sidebar entry: **Logs**) for
searching log lines across all sources stored in VictoriaLogs.

## Search

Type a term into the search box and press **Enter** or click **Search**. Leaving
the box empty matches all lines in the selected time range.

### Matching semantics

Search is **whole-word phrase matching** against the log message, using VictoriaLogs
tokenization. Substrings do not match:

- `conn` will **not** find lines containing `connection`
- `connection refused` will find lines containing that exact phrase

Multi-word input is treated as a phrase, not individual keywords. Under the hood
the term is sent as a LogsQL `_msg:"<term>"` filter. Direct LogsQL editing is
planned for a later stage.

## Time range

The Logs Explorer uses the shared time-range control — six presets (**5m**, **15m**,
**1h**, **6h**, **24h**, **7d**) plus a custom start/end picker. The default range
is the last **1 hour**.

See [Log Time Ranges](log-time-ranges.md) for full details on presets, custom
ranges, validation rules, and the UTC toggle.

## Results

Results are paginated. The oldest visible lines appear at the top; the newest at
the bottom. Click **Load older** to page back in time.

The header bar provides two toggles:

| Toggle | Effect |
| --- | --- |
| **UTC** | Switch all row timestamps between local time and UTC |
| **Wrap** | Toggle word-wrap on long log lines |

## States

| State | Message shown |
| --- | --- |
| No matches | "No matches in the selected range…" |
| VictoriaLogs unavailable | "Logs backend (VictoriaLogs) is unavailable…" |

## Advanced LogsQL mode

The controls row includes an **Advanced (LogsQL)** toggle. Enabling it replaces the
plain-text search box with a LogsQL editor.

### Writing LogsQL queries

In advanced mode your input is sent **directly to VictoriaLogs as-is** — it is not
wrapped in a `_msg:"…"` filter the way plain search is. This means you can use the
full LogsQL grammar: field filters, pipe stages, and aggregations.

Example:

```
service:home-assistant AND severity:error | stats count()
```

The editor provides basic syntax highlighting for:

- Keywords: `AND`, `OR`, `NOT`, `stats`, `count`, `by`
- Common field names: `service`, `host`, `severity`, `_msg`, `_time`
- Quoted strings, numbers, and durations
- Comparison operators

This is syntax highlighting only — there is no autocomplete or error-checking in
this version. For the full LogsQL grammar see the VictoriaLogs LogsQL documentation.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| **Enter** | Run the query |
| **Shift+Enter** | Insert a newline (multi-line queries) |

The **Search** button also runs the query.

### Switching modes

Toggling between plain and advanced mode **preserves each mode's text independently**.
Switching to advanced and back keeps your plain-text term; switching to plain and
back keeps your LogsQL expression.

### Mobile

On narrow viewports the LogsQL editor is rendered as a plain textarea (no syntax
highlighting) for tap-friendliness. **Enter** still submits the query.

## Services sidebar

The left **Services** panel lists every distinct `service` value present in the
current time window, organized into collapsible sections by **source type**. It
gives you a menu of available log sources without needing to know LogsQL.

### Source types

`source_type` is assigned at log ingest based on where the log came from: docker
containers → `docker`, systemd/journald units → `systemd`, cron jobs (CRON/crond
or the hmrun wrapper) → `cron`, anything else → `unknown`.

### Section layout

Services are grouped into sections in this order: **docker**, **cron**,
**systemd**, then **unknown** last. Sections only appear when at least one service
of that type is present in the current window.

Each section header shows:

- A **collapse/expand** toggle (chevron button). All sections start expanded;
  collapse state is per-session and resets on page reload.
- The **source type label** and an **aggregate line count** for that section.
- A **select-all / select-none** checkbox. The checkbox shows an indeterminate
  (mixed) state when only some of the section's services are selected. Clicking it
  selects all when any are unselected, or deselects all when all are selected.

Within a section, services are sorted by line count (descending).

A service that produces logs under more than one source type appears in **each
relevant section** — once under `docker` and once under `systemd`, for example —
each entry showing the count for that source type only.

### Selecting services

Click a service row to filter results to that service **under its source type**.
Selecting the `docker` entry for a service named `nginx` filters to its docker
logs only; its `systemd` logs (if any) are unaffected unless that entry is also
selected.

The selected identity appears as a chip above the search box (showing
`type:service`); click its **×** to deselect it.

You can select multiple services. The selections are OR'd together, then AND'd
with whatever your current search or LogsQL query matches. For example, selecting
the `docker` entry for `home-assistant` and the `cron` entry for `hmrun` shows
lines from either identity, restricted to your current search term.

The service filter works in **both plain search mode and advanced LogsQL mode**.
It wraps on top of whatever your query produces and never modifies the text you
typed.

### Count semantics

The counts and the service list reflect the **selected time window only**. They
refresh when you change the time range and are **not** affected by your current
search text or which services are already selected — the sidebar always shows what
exists in the window. This is intentional: it is a picker of available sources,
not a live result count.

If more than 100 distinct service+source-type combinations exist in the window,
only the top 100 by count are listed and a **Showing top results** notice appears
below the list.

### Mobile

On narrow screens the sidebar is hidden. A **Services** button appears above the
log list and opens the picker as an overlay dialog. Selected-service chips still
appear above the search box.

## Deep-linking and bookmarking

The search term, time range, and selected services are encoded in the URL, making
any view shareable and bookmarkable.

| URL parameter | Meaning |
| --- | --- |
| `q` | Plain-mode search term (URL-encoded). Omitted when the box is cleared. |
| `logsql` | Advanced-mode LogsQL expression (URL-encoded). |
| `since` | Active preset, e.g. `since=24h` |
| `start` / `end` | Custom range bounds as ISO timestamps |
| `services` | Comma-separated selected identities in `<source_type>:<service>` form, e.g. `services=docker:nginx,cron:hmrun` |

`q` and `logsql` are mutually exclusive. A URL containing `logsql` opens the
explorer directly in advanced mode.

Examples:

```
/logs?q=connection%20refused&since=24h
/logs?logsql=service%3Afoo&since=24h
/logs?logsql=service%3Ahome-assistant%20AND%20severity%3Aerror%20%7C%20stats%20count()&since=1h
/logs?start=2026-05-30T00:00:00Z&end=2026-05-31T00:00:00Z
/logs?q=error&since=6h&services=docker:home-assistant,docker:pi-hole
/logs
```

## What's next

Saved queries, query history, field inspector, histogram, export, and live tail
are planned in later stages.
