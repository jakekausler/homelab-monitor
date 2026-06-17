"""Declarative ``ssh_targets`` config model + loader (STAGE-017-002).

Operator-facing YAML lives under the top-level ``ssh_targets:`` key as a list of
target entries. Each entry validates against :class:`SshTargetConfig` (pydantic
v2, ``extra="forbid"``) and projects DOWN to the frozen
:class:`~homelab_monitor.kernel.ssh.params.SshTargetParams` consumed by
:class:`~homelab_monitor.kernel.ssh.client.AsyncSshClientFactory`.

The loader mirrors the calling convention of
``homelab_monitor.kernel.config.load_redact_patterns``: zero-arg, reads the path
from ``HOMELAB_MONITOR_CONFIG`` (default ``/config/homelab-monitor.yaml``), and
fails fast. Public default is EMPTY (no targets shipped).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from homelab_monitor.kernel.ssh.params import SshTargetParams

_DEFAULT_CONFIG_PATH = "/config/homelab-monitor.yaml"
_SSH_TARGETS_KEY = "ssh_targets"
_MIN_HOST_KEY_TOKENS = 2

# Pragmatic OpenSSH public-key-type allow-list. token[0] of a bare pubkey line.
_KNOWN_KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ssh-dss",
        "ssh-ecdsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)


def _validate_bare_host_key(value: str) -> None:
    """Validate ``value`` is a BARE OpenSSH public-key line, raising on invalid input.

    A bare line is ``"<key-type> <base64>[ comment]"`` — token[0] is a known
    key-type. A ``ssh-keyscan``/``known_hosts`` line (leading hostname / ``[host]:port`` /
    comma-list / ``|1|`` hashed token before the key-type) is REJECTED with a clear,
    operator-actionable error.
    """
    tokens = value.split()
    if len(tokens) < _MIN_HOST_KEY_TOKENS:
        msg = (
            "host_key must be a bare OpenSSH public key "
            "('<key-type> <base64>'); "
            f"got too few whitespace-separated tokens: {value!r}"
        )
        raise ValueError(msg)
    key_type = tokens[0]
    if key_type not in _KNOWN_KEY_TYPES:
        msg = (
            f"host_key first token {key_type!r} is not a known SSH key-type; "
            "provide the BARE public key ('ssh-ed25519 AAAA...'), NOT a "
            "ssh-keyscan/known_hosts line (which begins with a hostname)"
        )
        raise ValueError(msg)


class SshTargetConfig(BaseModel):
    """One operator-facing ``ssh_targets`` entry.

    Carries fields consumed by later stages (017-003 probe / 017-005 install / 017-006 concurrency)
    (``forced_command`` / ``script_id`` / ``concurrency_group``) that are NOT part of the narrower
    ``SshTargetParams`` projection.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1)
    account_mode: Literal["appliance", "dedicated-user"]
    key_secret_ref: str | None = None
    host_key: str | None = None
    forced_command: str | None = None
    script_id: str | None = None
    concurrency_group: str | None = None

    @model_validator(mode="after")
    def _check_and_normalize(self) -> SshTargetConfig:
        # Fill key_secret_ref default from id when omitted/empty.
        if not self.key_secret_ref:
            self.key_secret_ref = f"ssh_probe_key_{self.id}"
        # XOR: at most one of forced_command / script_id.
        if self.forced_command is not None and self.script_id is not None:
            msg = (
                f"ssh_targets entry {self.id!r}: at most one of "
                "'forced_command' / 'script_id' may be set, not both"
            )
            raise ValueError(msg)
        # host_key, when present, must be a BARE OpenSSH public key.
        if self.host_key is not None:
            _validate_bare_host_key(self.host_key)  # Raises ValueError on invalid input
        return self

    @property
    def _account_mode_underscore(self) -> Literal["appliance", "dedicated_user"]:
        """``account_mode`` normalized to the SshTargetParams underscore form."""
        return "dedicated_user" if self.account_mode == "dedicated-user" else "appliance"

    def to_params(self) -> SshTargetParams:
        """Project this config entry DOWN to the frozen transport contract."""
        # key_secret_ref is guaranteed non-None after the model_validator.
        key_secret_name = self.key_secret_ref
        assert key_secret_name is not None
        return SshTargetParams(
            host=self.host,
            port=self.port,
            user=self.user,
            key_secret_name=key_secret_name,
            pinned_host_key=self.host_key,
            account_mode=self._account_mode_underscore,
        )


def _load_ssh_target_configs_internal() -> dict[str, SshTargetConfig]:
    """Read + validate ``ssh_targets`` from the active config file into raw configs.

    Shared parsing core for :func:`load_ssh_targets` (projected params) and
    :func:`load_ssh_target_configs` (un-projected configs). Reads
    ``HOMELAB_MONITOR_CONFIG`` (default ``/config/homelab-monitor.yaml``).

    Fallback cascade (mirrors ``load_redact_patterns``):
      * config file missing            -> ``{}``
      * ``ssh_targets`` key absent      -> ``{}``
      * ``ssh_targets`` null            -> ``{}``
      * ``ssh_targets: []``             -> ``{}``

    Raises:
        ValueError: malformed structure (root not a mapping, ``ssh_targets`` not
            a list, list entry not a mapping, duplicate ``id``, or an entry that
            fails field/model validation).
        yaml.YAMLError: malformed YAML (propagated).
    """
    config_path = Path(os.environ.get("HOMELAB_MONITOR_CONFIG", _DEFAULT_CONFIG_PATH))
    if not config_path.is_file():
        return {}

    with config_path.open(encoding="utf-8") as f:
        raw_obj: object = yaml.safe_load(f) or {}
    if not isinstance(raw_obj, dict):
        msg = f"config root must be a mapping, got {type(raw_obj).__name__}"
        raise ValueError(msg)

    raw = cast(dict[str, Any], raw_obj)
    if _SSH_TARGETS_KEY not in raw:
        return {}
    targets_obj: object = raw.get(_SSH_TARGETS_KEY)
    if targets_obj is None:
        return {}
    if not isinstance(targets_obj, list):
        msg = f"{_SSH_TARGETS_KEY} must be a list, got {type(targets_obj).__name__}"
        raise ValueError(msg)

    targets_list = cast(list[object], targets_obj)
    result: dict[str, SshTargetConfig] = {}
    for idx, entry_obj in enumerate(targets_list):
        if not isinstance(entry_obj, dict):
            msg = f"{_SSH_TARGETS_KEY}[{idx}] must be a mapping, got {type(entry_obj).__name__}"
            raise ValueError(msg)
        entry = cast(dict[str, Any], entry_obj)
        try:
            cfg = SshTargetConfig.model_validate(entry)
        except ValidationError as exc:
            msg = f"{_SSH_TARGETS_KEY}[{idx}] is invalid: {exc}"
            raise ValueError(msg) from exc
        if cfg.id in result:
            msg = f"{_SSH_TARGETS_KEY} has duplicate id {cfg.id!r}"
            raise ValueError(msg)
        result[cfg.id] = cfg
    return result


def load_ssh_targets() -> dict[str, SshTargetParams]:
    """Load + validate ``ssh_targets`` and project each entry to ``SshTargetParams``.

    See :func:`_load_ssh_target_configs_internal` for the file-read + fallback
    cascade + validation contract. This loader returns the narrower transport
    projection consumed by :class:`AsyncSshClientFactory`.
    """
    return {tid: cfg.to_params() for tid, cfg in _load_ssh_target_configs_internal().items()}


def load_ssh_target_configs() -> dict[str, SshTargetConfig]:
    """Load + validate ``ssh_targets`` returning the UN-projected ``SshTargetConfig`` objects.

    Same file-read + fallback cascade + validation as :func:`load_ssh_targets`,
    but preserves the config-only fields (``forced_command`` / ``script_id`` /
    ``account_mode`` / ``concurrency_group``) that ``to_params()`` drops.
    Consumed by ``hm ssh-probe install-instructions`` + ``test`` (STAGE-017-005).
    """
    return _load_ssh_target_configs_internal()
