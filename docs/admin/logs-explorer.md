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

## Deep-linking and bookmarking

The search term and time range are encoded in the URL, making any view shareable
and bookmarkable.

| URL parameter | Meaning |
| --- | --- |
| `q` | Plain-mode search term (URL-encoded). Omitted when the box is cleared. |
| `logsql` | Advanced-mode LogsQL expression (URL-encoded). |
| `since` | Active preset, e.g. `since=24h` |
| `start` / `end` | Custom range bounds as ISO timestamps |

`q` and `logsql` are mutually exclusive. A URL containing `logsql` opens the
explorer directly in advanced mode.

Examples:

```
/logs?q=connection%20refused&since=24h
/logs?logsql=service%3Afoo&since=24h
/logs?logsql=service%3Ahome-assistant%20AND%20severity%3Aerror%20%7C%20stats%20count()&since=1h
/logs?start=2026-05-30T00:00:00Z&end=2026-05-31T00:00:00Z
/logs
```

## What's next

Stream picker, saved queries, query history, field inspector, histogram, export,
and live tail are planned in later stages.
