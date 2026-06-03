"""Integration test: nested-JSON log line -> vector flatten -> VictoriaLogs -> fields bag.

STAGE-004-017. Plant a JSON object with KNOWN nested structure via noisy-logger;
assert the flattened dotted-path keys surface under the LogLine `fields` bag
(json.context.user_id, json.context.request.path, json.context.request.latency_ms),
stringified.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from .helpers.rig import Rig
from .helpers.rig_health import require_rig_components

VECTOR_LATENCY_BUDGET_S = 30.0


@pytest.mark.integration
@pytest.mark.slow
def test_nested_json_line_is_flattened_into_fields() -> None:
    """Plant a nested-JSON line; poll /api/logs/query until flattened keys appear."""
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-json-{uuid.uuid4().hex}"
    payload = {
        "marker": marker,
        "context": {
            "user_id": 42,
            "request": {"path": "/x", "latency_ms": 1200},
        },
    }
    line = json.dumps(payload, separators=(",", ":"))

    with Rig.boot() as rig:
        # noisy-logger emits the raw line to stdout; vector's docker_logs source reads
        # it as .message (the full JSON string), then json_flatten re-parses + flattens.
        rig.plant_log_via_noisy_logger(line)

        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matched: dict[str, Any] | None = None
        last_resp_text = ""
        while time.time() < deadline:
            now = datetime.now(UTC)
            start = (now - timedelta(minutes=1)).isoformat()
            end = now.isoformat()
            resp = rig.get(
                "/api/logs/query",
                params={"expr": f'"{marker}"', "start": start, "end": end, "limit": "50"},
            )
            if resp.status_code == 200:  # noqa: PLR2004
                last_resp_text = resp.text
                for ln in resp.json().get("lines", []):
                    fields = ln.get("fields", {})
                    if fields.get("json.marker") == marker:
                        matched = ln
                        break
            if matched is not None:
                break
            time.sleep(2.0)

        assert matched is not None, (
            f"nested-JSON marker {marker!r} did not surface with flattened fields within "
            f"{VECTOR_LATENCY_BUDGET_S}s. Last response body: {last_resp_text[:500]}"
        )
        fields = cast(dict[str, Any], matched["fields"])
        # flatten() stringifies values into the VL fields bag (str(v) at ingest).
        assert fields.get("json.context.user_id") == "42", (
            f"expected json.context.user_id == '42', got {fields.get('json.context.user_id')!r}; "
            f"fields keys: {sorted(fields.keys())}"
        )
        assert fields.get("json.context.request.path") == "/x", (
            f"expected json.context.request.path == '/x', got "
            f"{fields.get('json.context.request.path')!r}"
        )
        assert fields.get("json.context.request.latency_ms") == "1200", (
            f"expected json.context.request.latency_ms == '1200', got "
            f"{fields.get('json.context.request.latency_ms')!r}"
        )


@pytest.mark.integration
@pytest.mark.slow
def test_json_flatten_field_cap_sets_truncated_flag() -> None:
    """Plant a JSON line with >100 leaf keys; assert json._truncated == 'true'.

    VRL for_each key order is nondeterministic, so we cannot rely on json.marker
    surviving truncation.  We match the line by the marker substring in the top-level
    message (the entire JSON string is the log message), then assert the cap flag.
    """
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-cap-trunc-{uuid.uuid4().hex}"
    # Build a payload with the marker embedded plus 120 flat keys (k0..k119).
    # Total leaf count = 121 > field cap of 100 → truncated flag must fire.
    payload: dict[str, Any] = {"marker": marker}
    for i in range(120):
        payload[f"k{i}"] = i
    line = json.dumps(payload, separators=(",", ":"))

    with Rig.boot() as rig:
        rig.plant_log_via_noisy_logger(line)

        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matched: dict[str, Any] | None = None
        last_resp_text = ""
        while time.time() < deadline:
            now = datetime.now(UTC)
            start = (now - timedelta(minutes=1)).isoformat()
            end = now.isoformat()
            resp = rig.get(
                "/api/logs/query",
                params={"expr": f'"{marker}"', "start": start, "end": end, "limit": "50"},
            )
            if resp.status_code == 200:  # noqa: PLR2004
                last_resp_text = resp.text
                for ln in resp.json().get("lines", []):
                    # Match by the top-level message (marker is in the JSON string),
                    # NOT by json.marker field (may have been truncated away).
                    msg_field = ln.get("message", "")
                    if marker in msg_field:
                        matched = ln
                        break
            if matched is not None:
                break
            time.sleep(2.0)

    assert matched is not None, (
        f"field-cap marker {marker!r} did not surface in logs within "
        f"{VECTOR_LATENCY_BUDGET_S}s. Last response body: {last_resp_text[:500]}"
    )
    fields = cast(dict[str, Any], matched["fields"])
    assert fields.get("json._truncated") == "true", (
        f"expected json._truncated == 'true' for a {len(payload)}-key payload "
        f"(field cap = 100), got {fields.get('json._truncated')!r}; "
        f"fields keys: {sorted(fields.keys())}"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_json_flatten_depth_cap_drops_deep_keys() -> None:
    """Plant a JSON line with a key nested 10 levels deep; assert it is absent.

    The fixture depth cap is 8 (dotted-segment count).  A key at depth <= 8 must
    be kept; a key at depth > 8 must be silently dropped.
    """
    require_rig_components("monitor", "victorialogs", "noisy-logger")

    marker = f"rig-cap-depth-{uuid.uuid4().hex}"
    # shallow key: depth 1 (segment count = 1) → kept
    # nested key a.b.c.d.e.f.g.h.i.j: depth 10 (segment count = 10) → dropped
    payload: dict[str, Any] = {
        "marker": marker,
        "shallow": "yes",
        "a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": "deep"}}}}}}}}},
    }
    line = json.dumps(payload, separators=(",", ":"))

    with Rig.boot() as rig:
        rig.plant_log_via_noisy_logger(line)

        deadline = time.time() + VECTOR_LATENCY_BUDGET_S
        matched: dict[str, Any] | None = None
        last_resp_text = ""
        while time.time() < deadline:
            now = datetime.now(UTC)
            start = (now - timedelta(minutes=1)).isoformat()
            end = now.isoformat()
            resp = rig.get(
                "/api/logs/query",
                params={"expr": f'"{marker}"', "start": start, "end": end, "limit": "50"},
            )
            if resp.status_code == 200:  # noqa: PLR2004
                last_resp_text = resp.text
                for ln in resp.json().get("lines", []):
                    msg_field = ln.get("message", "")
                    if marker in msg_field:
                        matched = ln
                        break
            if matched is not None:
                break
            time.sleep(2.0)

    assert matched is not None, (
        f"depth-cap marker {marker!r} did not surface in logs within "
        f"{VECTOR_LATENCY_BUDGET_S}s. Last response body: {last_resp_text[:500]}"
    )
    fields = cast(dict[str, Any], matched["fields"])
    # Shallow key (depth 1) must survive
    assert fields.get("json.shallow") == "yes", (
        f"expected json.shallow == 'yes' (depth 1, kept), "
        f"got {fields.get('json.shallow')!r}; fields keys: {sorted(fields.keys())}"
    )
    # Deep key (depth 10 > cap 8) must be absent
    assert "json.a.b.c.d.e.f.g.h.i.j" not in fields, (
        f"json.a.b.c.d.e.f.g.h.i.j (depth 10) should have been dropped by the depth cap (8), "
        f"but it is present with value {fields.get('json.a.b.c.d.e.f.g.h.i.j')!r}; "
        f"fields keys: {sorted(fields.keys())}"
    )
