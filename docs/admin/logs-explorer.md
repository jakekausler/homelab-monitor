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

## Deep-linking and bookmarking

The search term and time range are encoded in the URL, making any view shareable
and bookmarkable.

| URL parameter | Meaning |
| --- | --- |
| `q` | Search term (URL-encoded). Omitted when the search box is cleared. |
| `since` | Active preset, e.g. `since=24h` |
| `start` / `end` | Custom range bounds as ISO timestamps |

Examples:

```
/logs?q=connection%20refused&since=24h
/logs?start=2026-05-30T00:00:00Z&end=2026-05-31T00:00:00Z
/logs
```

## What's next

This is the first iteration of the Logs Explorer. Advanced LogsQL, stream picker,
saved queries, query history, field inspector, histogram, export, and live tail are
planned in later stages.
