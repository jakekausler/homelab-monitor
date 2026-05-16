"""Canonical log-match key for B-mode cron log-scrape (STAGE-002-008).

The same physical cron command is observed in two places with different
surface forms:

- On disk (the cron-discoverer): the raw crontab line. The discoverer stores
  the *scrubbed* command in ``crons.command`` and the canonical key in
  ``crons.log_match_key``.
- In the log line (Vector → /api/internal/cron-events): vanilla cron logs
  ``CRON[pid]: (user) CMD (command)`` — the command is wrapped in one layer of
  parentheses and carries no secret redaction.

``canonical_log_key`` is the single function both sides apply so the two
surface forms converge on the same string. Matching is then a plain equality
join on ``(crons.host, crons.log_match_key)``.

Known limitation: cron ``%`` substitution. A crontab line containing an
unescaped ``%`` has the text after the first ``%`` fed to the command as stdin,
so the *logged* command differs from the *disk* command. ``canonical_log_key``
cannot reconcile that; such crons will not match. Documented in
``docs/architecture/cron-logscrape.md``.
"""

from __future__ import annotations

from homelab_monitor.kernel.cron.secrets import scrub_secrets

_MIN_PARENS_LEN = 2  # "()"


def canonical_log_key(command: str) -> str:
    """Return the canonical match key for a cron command string.

    Steps: scrub secrets -> collapse whitespace -> strip one wrapping ``(...)``.
    Idempotent on already-scrubbed input (``scrub_secrets`` is idempotent).
    """
    scrubbed = scrub_secrets(command)
    collapsed = " ".join(scrubbed.split())
    if len(collapsed) >= _MIN_PARENS_LEN and collapsed.startswith("(") and collapsed.endswith(")"):
        inner = collapsed[1:-1]
        # Only strip if the parens are a matched pair enclosing the WHOLE
        # string (not e.g. "(a) && (b)"). Verify depth never hits 0 early.
        depth = 0
        balanced = True
        for ch in inner:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    balanced = False
                    break
        if balanced and depth == 0:
            collapsed = " ".join(inner.split())
    return collapsed


__all__ = ["canonical_log_key"]
