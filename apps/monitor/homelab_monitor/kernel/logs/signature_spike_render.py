"""Render a vmalert metrics rule for log-signature count-spike anomaly detection.

STAGE-004-036 (Anomaly Type B — "signature count spike vs baseline").

This module is a PURE, string-in/string-out renderer: it substitutes
``__UPPER_SNAKE__`` sentinel tokens in an embedded vmalert rule template with
caller-supplied per-signature parameters and returns the rendered rule YAML.
No DB, no filesystem IO. REUSED by STAGE-042 (rule persistence renders
per-signature instances on user demand via STAGE-044).

Why ``__TOKEN__`` + ``str.replace`` (matches kernel/cron/render.py precedent):
vmalert rules embed Go-template substitutions ``{{ $labels.x }}`` / ``{{ $value }}``
that use ``$``. ``string.Template`` (``$placeholder``) would collide with those,
so we use ``__UPPER_SNAKE__`` sentinels and replace ONLY those — the ``{{ }}``
Go-template directives survive untouched into the rendered output.

This stage ships only the TEMPLATE + this renderer. The rendered rule CANNOT
fire until STAGE-042 writes a concrete ``.yaml`` instance into the vmalert rule
dir (vmalert globs ``*.yaml``; the ``.tmpl`` artifact is never loaded). Suppression
is STRUCTURAL: a suppressed signature has no rendered instance (STAGE-042/044 own
not-rendering / deleting instances), so there is no rule to fire — hence no
suppression clause in this template.
"""

from __future__ import annotations

import re
from typing import Final

#: Validation: a vmalert duration like ``5m`` / ``300s`` / ``7d`` / ``2h``.
_DURATION_RE: Final[re.Pattern[str]] = re.compile(r"^\d+[smhd]$")

#: Validation: alertname-safe service_key fragment (alphanumerics + underscore).
_ALERTNAME_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_]")

#: Validation: characters that cannot appear inside a single-line double-quoted
#: PromQL label value — a quote/backslash breaks out of the matcher, and any ASCII
#: control char (newline, CR, tab, ...) breaks out of the matcher AND corrupts the
#: rendered YAML. Reject all of them (the values land inside ``"..."`` in PromQL
#: that itself sits inside the rendered YAML rule).
_FORBIDDEN_IN_PROMQL_VALUE: Final[re.Pattern[str]] = re.compile(r'["\\]|[\x00-\x1f\x7f]')

#: Cold-start cutoff in SECONDS (7 days). Locked Design default
#: (D-COLD-START-1H-FALLBACK). Hard-coded — not user-tunable in this stage.
_COLD_START_CUTOFF_SECONDS: Final[int] = 604800

#: Length of the template_hash prefix used in the alert slug.
_HASH_SLUG_LEN: Final[int] = 8

#: The embedded vmalert metrics rule template. Sentinels: __ALERT_SLUG__,
#: __SERVICE_KEY__, __TEMPLATE_HASH__, __MULTIPLIER__, __WINDOW__, __MIN_BASELINE__.
#: MUST stay byte-identical to deploy/vmalert/metrics/signature_spike.yml.tmpl
#: (a test asserts equality). The {{ ... }} directives are vmalert Go-template and
#: MUST NOT be substituted by this renderer.
_TEMPLATE: Final[
    str
] = """# Rendered per-signature by STAGE-004-042 (persistence) on user demand via
# STAGE-004-044 (UI). This is a TEMPLATE (.tmpl) — vmalert globs only *.yaml in
# this dir, so this file is never loaded live. Each rendered instance is one
# concrete signature ({service_key, template_hash}); the `and on(...)` 1:1 match
# is correct because the instance pins both labels.
#
# Suppression is STRUCTURAL, not a PromQL clause: a suppressed signature has no
# rendered instance (STAGE-042/044 own not-rendering / deleting instances for
# suppressed signatures), so there is nothing to fire. Do NOT add an
# `unless ... homelab_log_signature_suppressed` clause here — that metric does
# not exist (STAGE-035 folded suppression into a collector decision).
#
# Metric facts (from drain_consumer._emit_cycle_metrics):
#   homelab_log_signature_count{service_key,template_hash,severity} — per-cycle
#     line-count GAUGE (gappy: zero-line cycles emit no sample). We sum_over_time
#     it (NOT increase() — it is a gauge, not a counter).
#   homelab_log_signature_first_seen_ts{service_key,template_hash} — first-seen
#     timestamp in NANOSECONDS (hence /1e9 to compare against an age in seconds).
groups:
  - name: log_anomaly_signature_spike
    interval: 30s
    rules:
      - alert: __ALERT_SLUG__
        expr: |
          (
            sum_over_time(homelab_log_signature_count{service_key="__SERVICE_KEY__",template_hash="__TEMPLATE_HASH__"}[__WINDOW__])
              > __MULTIPLIER__ * max(
                  avg_over_time(sum_over_time(homelab_log_signature_count{service_key="__SERVICE_KEY__",template_hash="__TEMPLATE_HASH__"}[__WINDOW__])[7d:__WINDOW__]),
                  __MIN_BASELINE__
                )
          )
          and on (service_key, template_hash)
            (time() - homelab_log_signature_first_seen_ts{service_key="__SERVICE_KEY__",template_hash="__TEMPLATE_HASH__"} / 1e9 >= 604800)
          or
          (
            sum_over_time(homelab_log_signature_count{service_key="__SERVICE_KEY__",template_hash="__TEMPLATE_HASH__"}[__WINDOW__])
              > __MULTIPLIER__ * max(
                  avg_over_time(sum_over_time(homelab_log_signature_count{service_key="__SERVICE_KEY__",template_hash="__TEMPLATE_HASH__"}[__WINDOW__])[1h:__WINDOW__]),
                  __MIN_BASELINE__
                )
          )
          and on (service_key, template_hash)
            (time() - homelab_log_signature_first_seen_ts{service_key="__SERVICE_KEY__",template_hash="__TEMPLATE_HASH__"} / 1e9 < 604800)
        for: 1m
        labels:
          severity: warning
          source_tool: vmalert-metrics
          category: log-anomaly
          anomaly_kind: signature_spike
          target_kind: log_signature
          template_hash: "__TEMPLATE_HASH__"
          service_key: "__SERVICE_KEY__"
        annotations:
          summary: "Log signature spike in {{ $labels.service_key }}"
          description: |
            Signature {{ $labels.template_hash }} in {{ $labels.service_key }}
            fired {{ $value }} times in window __WINDOW__, exceeding __MULTIPLIER__x
            its rolling baseline (floor __MIN_BASELINE__).

            Signature catalog: /logs/signatures#{{ $labels.template_hash }}/{{ $labels.service_key }}
"""


def _sanitize_service_key_for_alertname(service_key: str) -> str:
    """Map service_key to an alertname-safe fragment (alphanumerics + underscore).

    vmalert alertnames must be valid Prometheus label values used as identifiers;
    non-[A-Za-z0-9_] chars are replaced with ``_`` so e.g. ``svc/foo-1`` becomes
    ``svc_foo_1``. The raw service_key is still inserted (quoted) into the PromQL
    label matchers and the labels block — only the SLUG is sanitized.
    """
    return _ALERTNAME_SAFE_RE.sub("_", service_key)


def render_signature_spike_rule(
    *,
    template_hash: str,
    service_key: str,
    multiplier: int = 5,
    window: str = "5m",
    min_baseline: int = 10,
) -> str:
    """Render the signature-count-spike vmalert metrics rule for one signature.

    Args:
        template_hash: Drain template hash (hex). Inserted into PromQL label
            matchers and the ``template_hash`` label. First 8 chars form the slug.
        service_key: Service/model key. Inserted (quoted) into PromQL matchers and
            the ``service_key`` label; sanitized for the alert slug.
        multiplier: Spike threshold multiple of the baseline. Must be >= 1.
        window: Aggregation window (vmalert duration, e.g. ``5m``). Must match
            ``^\\d+[smhd]$``.
        min_baseline: Minimum-baseline floor (gap-robustness). Must be >= 1.

    Returns:
        The rendered vmalert rule YAML (a string).

    Raises:
        ValueError: if ``template_hash`` or ``service_key`` contains ``"``,
            ``\\``, or any ASCII control character (they are inserted into PromQL
            ``"..."`` quotes inside the rendered YAML); if ``multiplier`` < 1; if
            ``min_baseline`` < 1; if ``window`` is not a valid simple duration.
    """
    for field_name, value in (("template_hash", template_hash), ("service_key", service_key)):
        if _FORBIDDEN_IN_PROMQL_VALUE.search(value):
            msg = (
                f"{field_name} must not contain a quote, backslash, or control character: {value!r}"
            )
            raise ValueError(msg)
    if multiplier < 1:
        msg = f"multiplier must be >= 1, got {multiplier}"
        raise ValueError(msg)
    if min_baseline < 1:
        msg = f"min_baseline must be >= 1, got {min_baseline}"
        raise ValueError(msg)
    if not _DURATION_RE.match(window):
        msg = f"window must match {_DURATION_RE.pattern!r}, got {window!r}"
        raise ValueError(msg)

    slug = f"SignatureSpike_{_sanitize_service_key_for_alertname(service_key)}_{template_hash[:_HASH_SLUG_LEN]}"

    rendered = _TEMPLATE
    rendered = rendered.replace("__ALERT_SLUG__", slug)
    rendered = rendered.replace("__SERVICE_KEY__", service_key)
    rendered = rendered.replace("__TEMPLATE_HASH__", template_hash)
    rendered = rendered.replace("__MULTIPLIER__", str(multiplier))
    rendered = rendered.replace("__WINDOW__", window)
    rendered = rendered.replace("__MIN_BASELINE__", str(min_baseline))
    return rendered


__all__ = ["render_signature_spike_rule"]
