"""Unit tests for STAGE-009-015 egress enforcement primitives.

Covers ``AutoFixOrchestrator._configure_egress_for_exec`` and the module-level
``_is_valid_hostname`` helper in isolation from ``_exec_claude`` — no runbook
YAML, no alert, just an orchestrator instance + fake docker client +
``ResolvedGrants``. Reuses ``_FakeDockerClient`` / ``_make_orchestrator`` from
``tests.test_autofix_orchestrator`` (module-level, importable — mirrors the
convention already established by
``tests/kernel/autofix/test_autofix_intent_gateway.py``).

Every test points ``egress_allowlist_path`` at a ``tmp_path`` file — never
``/data/proxy/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.autofix.approvals_repository import (
    RunbookRunApprovalsRepository,
)
from homelab_monitor.kernel.autofix.orchestrator import (
    AutoFixOrchestrator,
    _is_valid_hostname,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.autofix.runs_repository import RunbookRunsRepository
from homelab_monitor.kernel.autofix.types import EgressConfigurationError, ResolvedGrants
from homelab_monitor.kernel.config import FixerRunnerConfig
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.docker.socket_client import (
    DockerExecTimeoutError,
    DockerSocketConnectionError,
    ExecResult,
)
from homelab_monitor.kernel.runbooks.repository import RunbookRepo
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository
from tests.test_autofix_orchestrator import (
    _FakeDockerClient,  # pyright: ignore[reportPrivateUsage]
)

_BASELINE_HOSTNAMES = (
    "api.anthropic.com",
    "console.anthropic.com",
    "install.claude.ai",
    "statsig.anthropic.com",
)
_SORTED_BASELINE = sorted(_BASELINE_HOSTNAMES)


def _grants(egress: tuple[str, ...] = ()) -> ResolvedGrants:
    return ResolvedGrants(
        docker_container=None,
        docker_allowed_actions=(),
        ssh_target_id=None,
        egress=egress,
    )


def _make_orch_with_config(  # noqa: PLR0913
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    docker: _FakeDockerClient,
    *,
    allowlist_path: Path,
    egress_baseline_hostnames: tuple[str, ...] = _BASELINE_HOSTNAMES,
    egress_proxy_container: str = "test-fixer-egress-proxy",
    egress_reconfigure_timeout_seconds: float = 2.0,
) -> AutoFixOrchestrator:
    """Build an AutoFixOrchestrator whose FixerRunnerConfig points the egress
    allow-list at a tmp_path file. Mirrors _make_orchestrator's shape but
    injects a config with the egress_* fields overridden (that helper doesn't
    expose those as kwargs)."""
    log = structlog.get_logger()
    config = FixerRunnerConfig(
        container="test-fixer",
        transcript_dir="/tmp/transcripts-egress-unit-test",
        exec_log_dir="/tmp/exec-logs-egress-unit-test",
        fixer_user="homelab-fixer",
        exec_timeout_seconds=60.0,
        egress_proxy_container=egress_proxy_container,
        egress_allowlist_path=str(allowlist_path),
        egress_baseline_hostnames=egress_baseline_hostnames,
        egress_reconfigure_timeout_seconds=egress_reconfigure_timeout_seconds,
    )
    return AutoFixOrchestrator(
        runbook_repo=RunbookRepo(repo),
        alert_repo=AlertRepository(repo),
        app_settings_repo=AppSettingsRepository(repo),
        secrets_repo=secrets_repo,
        docker_client=docker,  # type: ignore[arg-type]
        db=repo,
        runs_repo=RunbookRunsRepository(repo),
        approvals_repo=RunbookRunApprovalsRepository(repo),
        config=config,
        log=log,
    )


# ---------------------------------------------------------------------------
# _configure_egress_for_exec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_egress_writes_baseline_only(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    await orch._configure_egress_for_exec(grants=_grants())  # pyright: ignore[reportPrivateUsage]

    content = allowlist_path.read_text(encoding="utf-8")
    assert content == "".join(f"{h}\n" for h in _SORTED_BASELINE)
    assert len(docker.calls) == 1
    call = docker.calls[0]
    assert call.cmd == ["squid", "-k", "reconfigure"]
    assert call.container_id == "test-fixer-egress-proxy"
    assert call.timeout_seconds == 2.0  # noqa: PLR2004


@pytest.mark.asyncio
async def test_configure_egress_writes_baseline_plus_grants(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    await orch._configure_egress_for_exec(  # pyright: ignore[reportPrivateUsage]
        grants=_grants(("registry-1.docker.io",))
    )

    content = allowlist_path.read_text(encoding="utf-8")
    expected = sorted((*_BASELINE_HOSTNAMES, "registry-1.docker.io"))
    lines = content.splitlines()
    assert len(lines) == 5  # noqa: PLR2004
    assert lines == expected
    assert "registry-1.docker.io" in lines


@pytest.mark.asyncio
async def test_configure_egress_dedups_baseline_and_grants(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    await orch._configure_egress_for_exec(  # pyright: ignore[reportPrivateUsage]
        grants=_grants(("api.anthropic.com",))
    )

    lines = allowlist_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # noqa: PLR2004
    assert lines == _SORTED_BASELINE


@pytest.mark.asyncio
async def test_configure_egress_calls_reconfigure_with_correct_args(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orch_with_config(
        repo,
        secrets_repo,
        docker,
        allowlist_path=allowlist_path,
        egress_proxy_container="my-proxy",
        egress_reconfigure_timeout_seconds=5.0,
    )

    await orch._configure_egress_for_exec(grants=_grants())  # pyright: ignore[reportPrivateUsage]

    assert len(docker.calls) == 1
    call = docker.calls[0]
    assert call.container_id == "my-proxy"
    assert call.cmd == ["squid", "-k", "reconfigure"]
    assert call.timeout_seconds == 5.0  # noqa: PLR2004
    assert call.user is None
    assert call.env is None


@pytest.mark.asyncio
async def test_configure_egress_raises_on_write_failure(
    repo: SqliteRepository,
    secrets_repo: AsyncSecretsRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    def _raising_open(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _raising_open)

    with pytest.raises(EgressConfigurationError) as exc_info:
        await orch._configure_egress_for_exec(grants=_grants())  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.reason == "allowlist_write_failed"
    assert "disk full" in exc_info.value.detail
    assert len(docker.calls) == 0


@pytest.mark.asyncio
async def test_configure_egress_raises_on_reconfigure_nonzero(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(
        squid_result=ExecResult(exit_code=1, stdout="", stderr="parse error at line 5")
    )
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    with pytest.raises(EgressConfigurationError) as exc_info:
        await orch._configure_egress_for_exec(grants=_grants())  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.reason == "reconfigure_nonzero"
    assert "exit_code=1" in exc_info.value.detail
    assert "parse error at line 5" in exc_info.value.detail


@pytest.mark.asyncio
async def test_configure_egress_raises_on_reconfigure_timeout(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_raises=DockerExecTimeoutError("timeout"))
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    with pytest.raises(EgressConfigurationError) as exc_info:
        await orch._configure_egress_for_exec(grants=_grants())  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.reason == "reconfigure_timeout"


@pytest.mark.asyncio
async def test_configure_egress_raises_on_reconfigure_docker_error(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_raises=DockerSocketConnectionError("socket refused"))
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    with pytest.raises(EgressConfigurationError) as exc_info:
        await orch._configure_egress_for_exec(grants=_grants())  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.reason == "reconfigure_docker_error"


@pytest.mark.asyncio
async def test_configure_egress_raises_on_invalid_hostname_in_grants(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orch_with_config(repo, secrets_repo, docker, allowlist_path=allowlist_path)

    bad_hostname = "evil host\nname"
    with pytest.raises(EgressConfigurationError) as exc_info:
        await orch._configure_egress_for_exec(  # pyright: ignore[reportPrivateUsage]
            grants=_grants((bad_hostname,))
        )

    assert exc_info.value.reason == "invalid_hostname"
    # STAGE-009-015: production code repr()s the hostname to prevent log
    # injection, so the newline is escaped in the detail message.
    assert repr(bad_hostname) in exc_info.value.detail
    assert not allowlist_path.exists()
    assert len(docker.calls) == 0


@pytest.mark.asyncio
async def test_configure_egress_raises_on_invalid_hostname_in_baseline(
    repo: SqliteRepository, secrets_repo: AsyncSecretsRepository, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    docker = _FakeDockerClient(squid_result=ExecResult(exit_code=0, stdout="", stderr=""))
    orch = _make_orch_with_config(
        repo,
        secrets_repo,
        docker,
        allowlist_path=allowlist_path,
        egress_baseline_hostnames=("bad_underscore.example.com",),
    )

    with pytest.raises(EgressConfigurationError) as exc_info:
        await orch._configure_egress_for_exec(grants=_grants())  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.reason == "invalid_hostname"


# STAGE-009-015: atomic-write tests removed — write is now inode-preserving
# truncate (open + write mode), no tempfile. Bind-mounts in the proxy
# container track the original inode; os.replace would create a new inode
# and the container would see stale data. The write happens synchronously
# before squid -k reconfigure is called, so a torn write is safe.


# ---------------------------------------------------------------------------
# _is_valid_hostname
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("api.anthropic.com", True),
        ("a", True),
        ("a.b.c", True),
        ("host-with-dash.example.co.uk", True),
        ("", False),
        ("a" * 254, False),
        ("-leading-hyphen.example.com", False),
        ("trailing-hyphen-.example.com", False),
        ("under_score.example.com", False),
        ("space in name.com", False),
        ("has\nnewline.com", False),
    ],
)
def test_is_valid_hostname_positive_and_negative(hostname: str, expected: bool) -> None:
    assert _is_valid_hostname(hostname) is expected
