"""drain3 log-signature engine (STAGE-004-025).

Wraps the (untyped) drain3 TemplateMiner per "model" (a service bucket or a cron
fingerprint) and tracks, for each model, the set of mined log templates and the
unix-ms timestamp each template was first seen.

D-TEMPLATE-HASH-STABLE semantic
-------------------------------
A template's identity is `sha256(template_str)`. This hash is *stable* in the sense
that it is deterministic across process restarts for an UNCHANGED template string:
restoring a model from its persisted snapshot reproduces the same templates and thus
the same hashes. drain3 generalises templates in place as new variants arrive (a
literal token becomes the `<*>` wildcard), so when a template string changes, its hash
changes too — the generalised template is intentionally a NEW signature. We never use
drain3's per-instance `cluster_id` for identity; it is an unstable counter.

Timestamps are unix-ms INTEGER throughout. `add_line` is async ONLY because lazy model
load (`get_model`) hits the DB; the clustering itself is synchronous.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from drain3 import (
    TemplateMiner,  # pyright: ignore[reportMissingTypeStubs]  -- drain3 has no type stubs
)
from drain3.template_miner_config import (
    TemplateMinerConfig,  # pyright: ignore[reportMissingTypeStubs]  -- drain3 has no type stubs
)

from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.logs.drain_persistence import (
    DrainPersistence,
    _BufferingHandler,
)
from homelab_monitor.kernel.logs.models import LogLine


class _DrainCluster(Protocol):
    """Minimal structural type for drain3 LogCluster (drain3 ships no type stubs)."""

    cluster_id: int
    size: int

    def get_template(self) -> str: ...


# drain3 still calls save_state synchronously on every cluster/template CHANGE
# inside add_log_message; this interval only suppresses the additional TIME-BASED
# periodic save. The _BufferingHandler absorbs all save_state calls as in-memory
# writes (no I/O); real DB I/O happens exclusively in the async snapshot().
_CRON_FP_LEN = 16  # hex chars of the cron-command fingerprint
_DRAIN_SIM_TH = 0.4
_DRAIN_DEPTH = 4
_SNAPSHOT_INTERVAL_MINUTES = 1440


@dataclass(frozen=True, slots=True)
class SignatureEvent:
    """The outcome of feeding one LogLine to the engine."""

    model_key: str
    template_id: int
    template_hash: str
    template_str: str
    is_new: bool
    cluster_size: int
    first_seen_ts: int
    last_seen_ts: int


@dataclass(frozen=True, slots=True)
class SignatureTemplate:
    """One mined template within a model, for enumeration.

    Note: `last_seen_ts` is the MODEL's last-processed timestamp, shared across all
    templates of the model — drain3 has no per-cluster last-seen timestamp.
    """

    model_key: str
    template_id: int
    template_hash: str
    template_str: str
    size: int
    first_seen_ts: int
    last_seen_ts: int


ModelKeyFn = Callable[[LogLine], str]
ConfigFactory = Callable[[], TemplateMinerConfig]


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _parse_iso_ms(ts: str) -> int:
    """Parse an ISO-8601 timestamp (with 'Z', offset, or naive) to unix-ms.

    Naive timestamps are treated as UTC. An empty or unparseable string falls back
    to the current time so a malformed line never crashes ingestion.
    """
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return _now_ms()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def default_model_key(line: LogLine) -> str:
    """Bucket a LogLine into a model key.

    - No service -> "_unknown".
    - service == "hmrun" (cron runner) -> "cron:<16-hex>" where the hex is
      sha256(canonical_log_key(command))[:16]; "cron:unknown" when no usable command
      (a shared catch-all bucket for multiple command-less hmrun cron jobs).
    - any other service -> the service name itself.
    """
    svc = line.service
    if svc is None:
        return "_unknown"
    if svc == "hmrun":
        cmd = line.fields.get("command")
        if isinstance(cmd, str) and cmd:
            fp = _sha256_hex(canonical_log_key(cmd))[:_CRON_FP_LEN]
        else:
            fp = "unknown"
        return f"cron:{fp}"
    return svc


def _default_config() -> TemplateMinerConfig:
    config = TemplateMinerConfig()
    config.snapshot_interval_minutes = _SNAPSHOT_INTERVAL_MINUTES
    config.snapshot_compress_state = True
    config.drain_sim_th = _DRAIN_SIM_TH
    config.drain_depth = _DRAIN_DEPTH
    return config


@dataclass(slots=True)
class _Model:
    """In-memory per-model engine state."""

    miner: TemplateMiner
    handler: _BufferingHandler
    first_seen: dict[str, int]
    line_count: int
    last_processed_ts: int | None


class DrainEngine:
    """Per-model drain3 wrapper with lazy load + on-demand snapshot persistence."""

    def __init__(
        self,
        persistence: DrainPersistence,
        model_key_fn: ModelKeyFn = default_model_key,
        config_factory: ConfigFactory = _default_config,
    ) -> None:
        self._persistence = persistence
        self._model_key_fn = model_key_fn
        self._config_factory = config_factory
        self._models: dict[str, _Model] = {}
        self._load_lock = asyncio.Lock()

    async def get_model(self, key: str) -> _Model:
        cached = self._models.get(key)
        if cached is not None:
            return cached
        async with self._load_lock:
            cached = self._models.get(key)  # re-check after acquiring the lock
            if cached is not None:
                return cached
            stored = await self._persistence.load_state_for(key)
            handler = _BufferingHandler(loaded=stored.snapshot)
            config = self._config_factory()
            # drain3's load_state runs in __init__ and raises a heterogeneous set
            # (base64/zlib/jsonpickle) on a corrupt snapshot BLOB. Degrade to a
            # fresh model rather than permanently bricking this key.
            try:
                miner = TemplateMiner(persistence_handler=handler, config=config)
            except Exception:
                handler = _BufferingHandler(loaded=None)
                miner = TemplateMiner(persistence_handler=handler, config=config)
            model = _Model(
                miner=miner,
                handler=handler,
                first_seen=dict(stored.first_seen_map),
                line_count=0,
                last_processed_ts=None,
            )
            # TODO(STAGE-026): self._models is never evicted; bounded by
            # service+cron-key cardinality. Revisit if cron fingerprint
            # cardinality proves unbounded.
            self._models[key] = model
            return model

    async def add_line(self, line: LogLine) -> SignatureEvent:
        key = self._model_key_fn(line)
        ts_ms = _parse_iso_ms(line.timestamp)
        model = await self.get_model(key)

        result: dict[str, Any] = model.miner.add_log_message(line.message)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  -- drain3 untyped
        template_str: str = str(result["template_mined"])
        template_id: int = int(result["cluster_id"])
        cluster_size: int = int(result["cluster_size"])
        template_hash = _sha256_hex(template_str)

        is_new = template_hash not in model.first_seen
        if is_new:
            # TODO(STAGE-026/027): first_seen grows one entry per distinct
            # template_hash forever; generalization churn leaves stale hashes.
            # Consider pruning hashes with no live cluster (intersect against
            # {sha256(c.get_template()) for c in miner.drain.clusters}) at
            # snapshot().
            model.first_seen[template_hash] = ts_ms
        first_seen_ts = model.first_seen[template_hash]

        model.line_count += 1
        model.last_processed_ts = ts_ms

        return SignatureEvent(
            model_key=key,
            template_id=template_id,
            template_hash=template_hash,
            template_str=template_str,
            is_new=is_new,
            cluster_size=cluster_size,
            first_seen_ts=first_seen_ts,
            last_seen_ts=ts_ms,
        )

    async def snapshot(self) -> None:
        """Persist every loaded model's current drain3 state to the DB.

        For each loaded model we force drain3 to serialise its live state via
        `miner.save_state("manual")` (always produces current bytes regardless of
        change_type — see drain3 TemplateMiner.save_state), then read those bytes
        from the buffering handler and upsert the row.
        """
        now = _now_ms()
        for key, model in self._models.items():
            model.miner.save_state("manual")  # pyright: ignore[reportUnknownMemberType]  -- drain3 untyped
            snapshot_bytes = model.handler.pending
            # drain3's save_state("manual") unconditionally serializes, so
            # pending is always set after the force-save above; this guard is
            # defensive only.
            if snapshot_bytes is None:  # pragma: no cover
                continue
            template_count = sum(1 for _ in model.miner.drain.clusters)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType]  -- drain3 untyped
            await self._persistence.persist(
                model_key=key,
                snapshot=snapshot_bytes,
                line_count=model.line_count,
                template_count=template_count,
                last_processed_ts=model.last_processed_ts,
                first_seen_map_json=_dump_first_seen(model.first_seen),
                updated_at=now,
            )

    def templates(self, key: str) -> list[SignatureTemplate]:
        """Enumerate the mined templates for a loaded model (empty if not loaded)."""
        model = self._models.get(key)
        if model is None:
            return []
        out: list[SignatureTemplate] = []
        for _raw in model.miner.drain.clusters:  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  -- drain3 untyped
            cluster = cast(_DrainCluster, _raw)
            template_str: str = str(cluster.get_template())
            template_id: int = int(cluster.cluster_id)
            size: int = int(cluster.size)
            template_hash = _sha256_hex(template_str)
            first_seen_ts = model.first_seen.get(template_hash, model.last_processed_ts or 0)
            last_seen_ts = model.last_processed_ts or first_seen_ts
            out.append(
                SignatureTemplate(
                    model_key=key,
                    template_id=template_id,
                    template_hash=template_hash,
                    template_str=template_str,
                    size=size,
                    first_seen_ts=first_seen_ts,
                    last_seen_ts=last_seen_ts,
                )
            )
        return out


def _dump_first_seen(first_seen: dict[str, int]) -> str:
    return json.dumps(first_seen)


__all__ = [
    "DrainEngine",
    "ModelKeyFn",
    "SignatureEvent",
    "SignatureTemplate",
    "default_model_key",
]
