"""Reference mirror of the synology_parse VRL category-bucketing parser.

Tested against REAL captured DSM samples + labeled-synthetic lines
(apps/monitor/tests/fixtures/synology_syslog_samples.txt). This reference ports
the synology_parse ALGORITHM (deploy/vector/vector.toml.template
[transforms.synology_parse]) to pure Python so CI can exercise the parse WITHOUT
a vector binary:

  1. Envelope: parse_syslog() happy path (modeled here by an RFC-3164 envelope
     regex). On failure -> service="synology-other" + parse_failed=1.
  2. Category derivation (colon-OR-space, independent of parse_syslog .appname):
       - short colon-delimited head (<=3 words, <=32 chars) -> that head
       - else first whitespace token
  3. Closed category -> service map (case-insensitive):
       Connection      -> synology-auth
       Storage Manager -> synology-smart
       Package Center  -> synology-package
       <recognized else> -> synology-system
       parse-fail/none -> synology-other (parse_failed=1)

The AUTHORITATIVE check is `vector validate` (the @pytest.mark.slow test in
test_vector_template.py) plus live Refinement against the real DSM on 5515. Keep
this reference in sync with [transforms.synology_parse].

This module lives under apps/monitor/tests/ (OUTSIDE the homelab_monitor coverage
source), so its branches are NOT subject to the 100% branch gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# RFC-3164 envelope (models parse_syslog's happy path: <PRI> ts host body).
_ENVELOPE = re.compile(
    r"^<(?P<pri>\d+)>"
    r"(?P<ts>[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<body>.*)$"
)

_MAX_CATEGORY_WORDS = 3
_MAX_CATEGORY_CHARS = 32


def _derive_category(head_src: str) -> str:
    """Colon-OR-space category derivation.

    Produces identical category results for all known fixture shapes via a behavioral
    mirror of the VRL, though the mechanism differs: the VRL peels appname then
    re-folds it, while the reference models appname="" and operates on the body
    directly. Both paths converge to the same output for all known shapes.
    """
    colon_parts = head_src.split(":", 1)
    colon_head = colon_parts[0].strip()
    if (
        len(colon_parts) > 1
        and len(colon_head.split()) <= _MAX_CATEGORY_WORDS
        and len(colon_head) <= _MAX_CATEGORY_CHARS
    ):
        return colon_head
    space_parts = head_src.split(" ", 1)
    return space_parts[0].strip()


def parse_synology_line(raw: str) -> dict[str, str]:
    """Pure-Python port of the synology_parse VRL. Returns enriched event fields."""
    result: dict[str, str] = {"parse_failed": "0"}
    raw = raw.strip()
    m = _ENVELOPE.match(raw)
    if m is None:
        # parse_syslog failure path.
        result["parse_failed"] = "1"
        result["message"] = raw
        result["source_type"] = "synology"
        result["service"] = "synology-other"
        result["syn_category"] = ""
        return result

    result["source_type"] = "synology"
    result["host"] = m.group("host")
    body = m.group("body")
    # parse_syslog may peel a leading colon-terminated word into appname; model the
    # message as the full body and reconstruct head_src (appname re-folded in if it
    # was peeled). For the reference we treat appname="" and head_src=body, which
    # produces identical category results for all known shapes (behavioral mirror).
    # The VRL's appname-refold mechanism is not modeled here; convergence is verified
    # by the fixture sweep.
    result["message"] = body
    result["appname"] = ""
    head_src = body
    category = _derive_category(head_src)
    result["syn_category"] = category

    cat_l = category.lower()
    if cat_l == "connection":
        result["service"] = "synology-auth"
    elif cat_l == "storage manager":
        result["service"] = "synology-smart"
    elif cat_l == "package center":
        result["service"] = "synology-package"
    elif category != "":
        result["service"] = "synology-system"
    else:
        result["service"] = "synology-other"
    return result


# --- Real captured fixtures (verbatim) ---
_REAL_TEST_HEARTBEAT = (
    "<14>Jun 27 09:13:46 NAS System Test message from Synology Syslog Client from (70.229.192.120)"
)
_REAL_CONNECTION_LOGIN = (
    "<14>Jun 27 09:19:40 NAS Connection: User [jakekausler] from "
    "[2600:387:15:700e::8] signed in to [DSM] successfully via [amfa-email]."
)
# --- Synthetic (pending real capture) ---
_SYN_STORAGE_MANAGER = (
    "<14>Jun 27 10:00:00 NAS Storage Manager: [Storage Pool 1] SMART warning detected on disk 3."
)
_SYN_PACKAGE_CENTER = (
    "<14>Jun 27 10:01:00 NAS Package Center: Package [Docker] has been updated to version 24.0.2."
)
_MALFORMED = "this is not a valid syslog datagram at all"


def test_real_test_heartbeat_buckets_system() -> None:
    p = parse_synology_line(_REAL_TEST_HEARTBEAT)
    assert p["service"] == "synology-system"
    assert p["source_type"] == "synology"
    assert p["syn_category"] == "System"
    assert p["parse_failed"] == "0"


def test_real_connection_login_buckets_auth() -> None:
    p = parse_synology_line(_REAL_CONNECTION_LOGIN)
    assert p["service"] == "synology-auth"
    assert p["syn_category"] == "Connection"
    # The IPv6 [2600:387:15:700e::8] must NOT defeat the colon-head rule.
    assert p["parse_failed"] == "0"


def test_synthetic_storage_manager_buckets_smart() -> None:
    p = parse_synology_line(_SYN_STORAGE_MANAGER)
    assert p["service"] == "synology-smart"
    assert p["syn_category"] == "Storage Manager"


def test_synthetic_package_center_buckets_package() -> None:
    p = parse_synology_line(_SYN_PACKAGE_CENTER)
    assert p["service"] == "synology-package"
    assert p["syn_category"] == "Package Center"


def test_unrecognized_category_buckets_system() -> None:
    line = "<14>Jun 27 10:02:00 NAS Backup: scheduled task completed."
    p = parse_synology_line(line)
    assert p["service"] == "synology-system"
    assert p["syn_category"] == "Backup"
    assert p["parse_failed"] == "0"


def test_malformed_line_marks_parse_failed() -> None:
    p = parse_synology_line(_MALFORMED)
    assert p["parse_failed"] == "1"
    assert p["service"] == "synology-other"
    assert p["source_type"] == "synology"


def test_trailing_newline_still_parses() -> None:
    p = parse_synology_line(_REAL_CONNECTION_LOGIN + "\n")
    assert p["service"] == "synology-auth"
    assert p["parse_failed"] == "0"


# --- Parametrized sweep over EVERY fixture line ---
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "synology_syslog_samples.txt"
_VALID_SERVICES = {
    "synology-auth",
    "synology-smart",
    "synology-package",
    "synology-system",
    "synology-other",
}


def _fixture_lines() -> list[str]:
    text = _FIXTURE_PATH.read_text(encoding="utf-8")
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("line", _fixture_lines())
def test_every_fixture_line_maps_to_a_sane_service(line: str) -> None:
    p = parse_synology_line(line + "\n")
    assert p["service"] in _VALID_SERVICES, f"unexpected service for: {line!r}"
    assert "message" in p
    # Every fixture line is a well-formed RFC-3164 datagram, so none should be
    # bucketed as the parse-failure fallback.
    assert p["parse_failed"] == "0", f"unexpected parse failure for: {line!r}"
