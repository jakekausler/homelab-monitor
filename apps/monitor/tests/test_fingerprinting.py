"""Tests for kernel.alerts.fingerprinting."""

from __future__ import annotations

from homelab_monitor.kernel.alerts.fingerprinting import (
    compute_fingerprint,
    quarantine_fingerprint,
)
from homelab_monitor.kernel.alerts.types import AlertmanagerV2AlertItem


def _item(
    *,
    fingerprint: str = "",
    labels: dict[str, str] | None = None,
) -> AlertmanagerV2AlertItem:
    return AlertmanagerV2AlertItem(
        status="firing",
        labels=labels if labels is not None else {"alertname": "Foo"},
        startsAt="2026-05-07T00:00:00+00:00",
        fingerprint=fingerprint,
    )


def test_compute_fingerprint_uses_alertmanager_when_present() -> None:
    item = _item(fingerprint="upstream-fp")
    assert compute_fingerprint(item) == "upstream-fp"


def test_compute_fingerprint_falls_back_to_sha256_of_sorted_labels() -> None:
    item = _item(labels={"alertname": "Foo", "severity": "warning"})
    fp = compute_fingerprint(item)
    assert len(fp) == 64  # noqa: PLR2004 -- SHA-256 hex
    assert all(c in "0123456789abcdef" for c in fp)


def test_compute_fingerprint_label_order_independent() -> None:
    a = _item(labels={"alertname": "Foo", "severity": "warning"})
    b = _item(labels={"severity": "warning", "alertname": "Foo"})
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_quarantine_fingerprint_deterministic() -> None:
    fp1 = quarantine_fingerprint("hostmetrics", "consecutive_failures>=5")
    fp2 = quarantine_fingerprint("hostmetrics", "consecutive_failures>=5")
    assert fp1 == fp2


def test_quarantine_fingerprint_different_collectors_different_fps() -> None:
    fp1 = quarantine_fingerprint("hostmetrics", "boom")
    fp2 = quarantine_fingerprint("netdata", "boom")
    fp3 = quarantine_fingerprint("hostmetrics", "different reason")
    assert fp1 != fp2
    assert fp1 != fp3
