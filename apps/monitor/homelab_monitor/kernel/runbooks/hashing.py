"""Canonical content hash for a parsed runbook config (STAGE-009-001).

The hash is the content-addressable identity of a runbook's SEMANTIC config:
``SHA256(json_canonical(config.model_dump(mode="json")))``. Because it hashes
the PARSED + normalized model (not raw YAML bytes), two configs that differ only
in YAML formatting, key ordering, or whitespace hash to the same value;
semantically different configs hash differently.

Idiom matches kernel/cron/fingerprint.py: sort_keys=True + separators=(",", ":")
(so a value containing a delimiter cannot collide with a different field) +
ensure_ascii=False (Unicode hashes to its source bytes).
"""

from __future__ import annotations

import hashlib
import json

from homelab_monitor.kernel.runbooks.config import RunbookConfig


def compute_runbook_content_hash(config: RunbookConfig) -> str:
    """Return the 64-char lowercase SHA256 hex of a runbook's canonical config.

    Args:
        config: A validated :class:`RunbookConfig`.

    Returns:
        64-character lowercase hex string. Stable across YAML reformatting;
        sensitive to any semantic change.
    """
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["compute_runbook_content_hash"]
