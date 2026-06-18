"""Tests for ``hm ssh-probe`` CLI subcommands (STAGE-017-004)."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import asyncssh
import pytest

from homelab_monitor.cli._support import build_secrets_repo
from homelab_monitor.cli.main import main
from homelab_monitor.cli.ssh_probe import (
    _RESTRICTION_EXIT_CODE,  # pyright: ignore[reportPrivateUsage]
    _cmd_capture_hostkey,  # pyright: ignore[reportPrivateUsage]
    _cmd_test,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.secrets.master_key import ENV_VAR

# Re-export the loopback SSH server fixtures.
from tests.ssh.conftest import (  # noqa: F401  -- pytest fixture re-export
    SshTestServer,
    ssh_test_server,  # pyright: ignore[reportUnusedImport]
    ssh_test_server_forced_command,  # pyright: ignore[reportUnusedImport]
)

KEY = bytes(range(32))
KEY_B64 = base64.b64encode(KEY).decode("ascii")


@pytest.fixture
def cli_env(db_url_env: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Environment with DB URL + master key set."""
    monkeypatch.setenv(ENV_VAR, KEY_B64)
    return db_url_env


def _get_secret(name: str) -> str | None:
    """Read a secret value back via the real AsyncSecretsRepository (sync wrapper)."""

    async def _run() -> str | None:
        repo = await build_secrets_repo()
        return await repo.get(name)

    return asyncio.run(_run())


async def _astore_secret(name: str, value: str) -> None:
    """Store a secret via the real AsyncSecretsRepository (awaitable; for async test bodies)."""
    repo = await build_secrets_repo()
    await repo.set(name, value, who="test")


def _write_install_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Write a temporary config file and point HOMELAB_MONITOR_CONFIG to it."""
    config_file = tmp_path / "homelab-monitor.yaml"
    config_file.write_text(body, encoding="utf-8")
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))


# --------------------------- keygen ---------------------------


def test_keygen_creates_secret_and_prints_pubkey(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """keygen writes the PEM secret + prints the bare public key; never the private key."""
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "keygen", "udm"])
    captured = capsys.readouterr()
    assert rc == 0

    # Bare public key line printed to stdout.
    assert "ssh-ed25519 " in captured.out

    # Private key PEM NEVER appears in stdout or stderr.
    assert "PRIVATE KEY" not in captured.out
    assert "PRIVATE KEY" not in captured.err

    # Secret was written and IS a PEM private key.
    stored = _get_secret("ssh_probe_key_udm")
    assert stored is not None
    assert "PRIVATE KEY" in stored


def test_keygen_refuses_existing_without_rotate(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A second keygen without --rotate exits 1 and does not overwrite."""
    main(["migrate"])
    capsys.readouterr()
    assert main(["ssh-probe", "keygen", "udm"]) == 0
    first = _get_secret("ssh_probe_key_udm")
    capsys.readouterr()

    rc = main(["ssh-probe", "keygen", "udm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "already exists" in err
    assert "--rotate" in err
    # Unchanged.
    assert _get_secret("ssh_probe_key_udm") == first


def test_keygen_rotate_replaces_existing(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """keygen --rotate on an existing key rotates it (exit 0, new value)."""
    main(["migrate"])
    capsys.readouterr()
    assert main(["ssh-probe", "keygen", "udm"]) == 0
    first = _get_secret("ssh_probe_key_udm")
    capsys.readouterr()

    rc = main(["ssh-probe", "keygen", "udm", "--rotate"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "ssh-ed25519 " in captured.out
    assert "PRIVATE KEY" not in captured.out
    assert "PRIVATE KEY" not in captured.err

    rotated = _get_secret("ssh_probe_key_udm")
    assert rotated is not None
    assert rotated != first  # fresh keypair


def test_keygen_rotate_absent_errors(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """keygen --rotate on a non-existent secret exits 1 with 'omit --rotate'."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe", "keygen", "newtarget", "--rotate"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "omit --rotate" in err


def test_keygen_invalid_target_charset(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An id with disallowed characters exits 1."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe", "keygen", "bad id/with*chars"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid target" in err


def test_keygen_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keygen reports MasterKeyError + exits 1 when no master key is configured."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe", "keygen", "udm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


# ----------------------- capture-hostkey -----------------------


@pytest.mark.asyncio
async def test_capture_hostkey_success_via_host_override(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    ssh_test_server: SshTestServer,  # noqa: F811
) -> None:
    """capture-hostkey against the real loopback server prints the exact host key + fingerprint."""
    main(["migrate"])
    capsys.readouterr()

    rc = await _cmd_capture_hostkey(
        "loopback",
        host_override="127.0.0.1",
        port_override=ssh_test_server.port,
    )
    out = capsys.readouterr().out
    assert rc == 0

    # The printed bare line EXACTLY equals the server's host pubkey line.
    expected = ssh_test_server.host_pubkey_line.strip()
    assert expected in out

    # Fingerprint line present + matches the captured key.
    expected_fp = asyncssh.import_public_key(expected).get_fingerprint()  # pyright: ignore[reportUnknownMemberType]
    assert f"fingerprint: {expected_fp}" in out

    # TOFU warning + paste instruction present.
    assert "TOFU" in out
    assert "host_key" in out


@pytest.mark.asyncio
async def test_capture_hostkey_succeeds_when_key_already_in_known_hosts(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ssh_test_server: SshTestServer,  # noqa: F811
) -> None:
    """capture-hostkey MUST succeed even if the server's key is already in ~/.ssh/known_hosts.

    Regression for the known_hosts suppression bug: omitting known_hosts (or passing any
    falsy value: None, b'', []) makes asyncssh auto-load ~/.ssh/known_hosts; when the server
    key is already trusted there, validate_host_public_key never fires and capture returns None.
    The fix passes known_hosts=asyncssh.import_known_hosts("") (truthy-but-empty) so the
    callback always fires. FAILS against omitted-known_hosts code; PASSES after the fix.
    """
    pub_line = ssh_test_server.host_pubkey_line.strip()
    parts = pub_line.split(" ", 2)
    keytype, b64key = parts[0], parts[1]
    kh_line = f"[127.0.0.1]:{ssh_test_server.port} {keytype} {b64key}"

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir()
    kh_file = ssh_dir / "known_hosts"
    kh_file.write_text(kh_line + "\n", encoding="utf-8")
    kh_file.chmod(0o600)
    monkeypatch.setenv("HOME", str(fake_home))

    main(["migrate"])
    capsys.readouterr()

    rc = await _cmd_capture_hostkey(
        "loopback",
        host_override="127.0.0.1",
        port_override=ssh_test_server.port,
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert pub_line in out


@pytest.mark.asyncio
async def test_capture_hostkey_resolves_from_config(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ssh_test_server: SshTestServer,  # noqa: F811
) -> None:
    """capture-hostkey with NO --host resolves host/port from ssh_targets config."""
    config_file = tmp_path / "homelab-monitor.yaml"
    config_file.write_text(
        "ssh_targets:\n"
        "  - id: loopback\n"
        "    host: 127.0.0.1\n"
        f"    port: {ssh_test_server.port}\n"
        "    user: nobody\n"
        "    account_mode: dedicated-user\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))

    main(["migrate"])
    capsys.readouterr()
    rc = await _cmd_capture_hostkey("loopback", host_override=None, port_override=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert ssh_test_server.host_pubkey_line.strip() in out


def test_capture_hostkey_target_not_configured(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """capture-hostkey for an unknown target (no --host) exits 1."""
    # Point config at a missing file → load_ssh_targets() returns {}.
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "missing.yaml"))
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe", "capture-hostkey", "ghost"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found in ssh_targets" in err


def test_capture_hostkey_connection_refused(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """capture-hostkey against an unused port exits 1 with a clear message."""
    main(["migrate"])
    capsys.readouterr()
    # Port 1 is reserved + unbound on the test host → ConnectionRefusedError.
    rc = main(["ssh-probe", "capture-hostkey", "x", "--host", "127.0.0.1", "--port", "1"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "failed" in err or "could not capture" in err


def test_capture_hostkey_invalid_port(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A --port outside 1-65535 exits 1."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe", "capture-hostkey", "x", "--host", "127.0.0.1", "--port", "70000"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "--port must be between" in err


def test_capture_hostkey_invalid_target_charset(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An id with disallowed characters exits 1 (before any connect)."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe", "capture-hostkey", "bad id*"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid target" in err


def test_capture_hostkey_host_override_default_port(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """capture-hostkey with --host but no --port uses default port 22."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe", "capture-hostkey", "x", "--host", "127.0.0.1", "--port", "1"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "failed" in err or "could not capture" in err


@pytest.mark.asyncio
async def test_capture_hostkey_config_with_port_override(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ssh_test_server: SshTestServer,  # noqa: F811
) -> None:
    """capture-hostkey with config + explicit --port uses the override."""
    config_file = tmp_path / "homelab-monitor.yaml"
    config_file.write_text(
        "ssh_targets:\n"
        "  - id: loopback\n"
        "    host: 127.0.0.1\n"
        "    port: 22\n"
        "    user: nobody\n"
        "    account_mode: dedicated-user\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))

    main(["migrate"])
    capsys.readouterr()
    rc = await _cmd_capture_hostkey(
        "loopback", host_override=None, port_override=ssh_test_server.port
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert ssh_test_server.host_pubkey_line.strip() in out


@pytest.mark.asyncio
async def test_capture_hostkey_connect_succeeds_no_key_captured(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the branch where connect() returns a live conn but the host-key
    callback never fired (no key captured) → exit 1 + clean conn close."""

    class _FakeConn:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def _fake_connect(*args: object, **kwargs: object) -> _FakeConn:
        return _FakeConn()

    monkeypatch.setattr(
        "homelab_monitor.cli.ssh_probe.asyncssh.connect",
        _fake_connect,
    )

    main(["migrate"])
    capsys.readouterr()
    rc = await _cmd_capture_hostkey(
        "myhost",
        host_override="127.0.0.1",
        port_override=9,
    )
    assert rc == 1


# ----------------------- install-instructions ----------------------


def test_install_instructions_appliance(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions appliance: renders the forced-command line + persistence warning."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: udm\n"
        "    host: 192.168.2.1\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        "    forced_command: /usr/bin/show-stuff\n",
    )
    main(["migrate"])
    capsys.readouterr()

    # keygen first
    assert main(["ssh-probe", "keygen", "udm"]) == 0
    capsys.readouterr()

    # install-instructions
    rc = main(["ssh-probe", "install-instructions", "udm"])
    _captured = capsys.readouterr()
    out, err = _captured.out, _captured.err
    assert rc == 0
    assert (
        'command="/usr/bin/show-stuff",no-port-forwarding,no-pty,no-X11-forwarding,no-agent-forwarding'
        in out
    )
    assert "hm-probe-udm" in out
    assert "ssh-ed25519 " in out
    assert "WARNING (UniFi OS firmware persistence)" in out
    assert "PRIVATE KEY" not in out
    assert "PRIVATE KEY" not in err


def test_install_instructions_appliance_no_forced_command(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions with no forced_command renders a placeholder + NOTE."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: udm\n"
        "    host: 192.168.2.1\n"
        "    user: root\n"
        "    account_mode: appliance\n",
    )
    main(["migrate"])
    capsys.readouterr()

    assert main(["ssh-probe", "keygen", "udm"]) == 0
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "udm"])
    _captured = capsys.readouterr()
    out, err = _captured.out, _captured.err
    assert rc == 0
    assert 'command="<CONFIGURE forced_command IN ssh-targets.yaml>"' in out
    assert "NOTE:" in err
    assert "no forced_command" in err


def test_install_instructions_appliance_script_id_rejected(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions for appliance + script_id exits 1."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: udm\n"
        "    host: 192.168.2.1\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        "    script_id: s1\n",
    )
    main(["migrate"])
    capsys.readouterr()

    assert main(["ssh-probe", "keygen", "udm"]) == 0
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "udm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "appliance mode forces a command, not a script" in err


def test_install_instructions_dedicated_user(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions dedicated-user: renders the 5-step recipe (no persistence warning)."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: synology\n"
        "    host: 192.168.2.4\n"
        "    port: 53197\n"
        "    user: monitor\n"
        "    account_mode: dedicated-user\n",
    )
    main(["migrate"])
    capsys.readouterr()

    assert main(["ssh-probe", "keygen", "synology"]) == 0
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "synology"])
    _captured = capsys.readouterr()
    out, err = _captured.out, _captured.err
    assert rc == 0
    assert "sudo useradd -m -s /bin/sh monitor" in out
    assert "cat /proc/uptime" in out
    assert "monitor ALL=(root) NOPASSWD: <ABSOLUTE_PATHS_OF_READ_ONLY_COMMANDS>" in out
    assert (
        'command="/home/monitor/hm-probe.sh",no-port-forwarding,no-pty,no-X11-forwarding,no-agent-forwarding'
        in out
    )
    assert "hm-probe-synology" in out
    assert "ssh-ed25519 " in out
    assert "WARNING (UniFi OS firmware persistence)" not in out
    assert "PRIVATE KEY" not in out
    assert "PRIVATE KEY" not in err


def test_install_instructions_target_not_in_config(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions for a target not in config exits 1."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n  - id: other\n    host: 1.1.1.1\n"
        "    user: x\n    account_mode: appliance\n",
    )
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "ghost"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not in config" in err


def test_install_instructions_no_key_secret(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions without keygen exits 1."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: udm\n"
        "    host: 192.168.2.1\n"
        "    user: root\n"
        "    account_mode: appliance\n",
    )
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "udm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no probe key" in err
    assert "keygen" in err


def test_install_instructions_invalid_target_charset(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """install-instructions with invalid charset exits 1."""
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "bad id*"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid target" in err


def test_install_instructions_malformed_config(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions with malformed config (root as list) exits 1."""
    _write_install_config(tmp_path, monkeypatch, "- a\n- b\n")
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "x"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "config root must be a mapping" in err


def test_install_instructions_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """install-instructions without master key exits 1."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: udm\n"
        "    host: 192.168.2.1\n"
        "    user: root\n"
        "    account_mode: appliance\n",
    )
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "install-instructions", "udm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


# -------------------------------- test ---------------------------------


@pytest.mark.asyncio
async def test_test_pass_restriction_enforced(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ssh_test_server_forced_command: SshTestServer,  # noqa: F811
) -> None:
    """test command PASS path: forced server, marker absent → restriction holds."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: loopback\n"
        f"    host: 127.0.0.1\n"
        f"    port: {ssh_test_server_forced_command.port}\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        "    forced_command: echo HM_FORCED_OK\n"
        f"    host_key: {ssh_test_server_forced_command.host_pubkey_line.strip()}\n",
    )
    main(["migrate"])
    capsys.readouterr()

    await _astore_secret("ssh_probe_key_loopback", ssh_test_server_forced_command.client_key_pem)

    rc = await _cmd_test("loopback")
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS: forced-command restriction enforced" in out
    assert "forced-command output:" in out
    assert "HM_FORCED_OK" in out
    assert "HM_PROBE_RESTRICTION_CHECK_loopback" not in out


@pytest.mark.asyncio
async def test_test_fail_restriction_not_enforced(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ssh_test_server: SshTestServer,  # noqa: F811
) -> None:
    """test command NEGATIVE path: plain server (no forced), marker echoed → restriction broken."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: loopback\n"
        f"    host: 127.0.0.1\n"
        f"    port: {ssh_test_server.port}\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        "    forced_command: echo HM_FORCED_OK\n"
        f"    host_key: {ssh_test_server.host_pubkey_line.strip()}\n",
    )
    main(["migrate"])
    capsys.readouterr()

    await _astore_secret("ssh_probe_key_loopback", ssh_test_server.client_key_pem)

    rc = await _cmd_test("loopback")
    err = capsys.readouterr().err
    assert rc == _RESTRICTION_EXIT_CODE
    assert "FAIL: restriction NOT enforced" in err
    assert 'command="' in err


@pytest.mark.asyncio
async def test_test_no_host_key(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test exits 1 when no host_key in config."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: loopback\n"
        "    host: 127.0.0.1\n"
        "    port: 22\n"
        "    user: root\n"
        "    account_mode: appliance\n",
    )
    main(["migrate"])
    capsys.readouterr()

    rc = await _cmd_test("loopback")
    err = capsys.readouterr().err
    assert rc == 1
    assert "no pinned host key" in err
    assert "capture-hostkey" in err


@pytest.mark.asyncio
async def test_test_no_key_secret(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test exits 1 when no probe key secret."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: loopback\n"
        "    host: 127.0.0.1\n"
        "    port: 22\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        "    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc123 x\n",
    )
    main(["migrate"])
    capsys.readouterr()

    rc = await _cmd_test("loopback")
    err = capsys.readouterr().err
    assert rc == 1
    assert "no probe key" in err
    assert "keygen" in err


def test_test_target_not_in_config(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test exits 1 when target not in config."""
    monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(tmp_path / "missing.yaml"))
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "test", "ghost"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not in config" in err


def test_test_invalid_target_charset(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """test exits 1 with invalid charset."""
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "test", "bad id*"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid target" in err


@pytest.mark.asyncio
async def test_test_host_key_mismatch(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ssh_test_server: SshTestServer,  # noqa: F811
) -> None:
    """test exits 1 + prints CRITICAL/MITM when host key mismatches."""
    # Use a freshly-generated key (different from the server's).
    wrong_host_key = (  # pyright: ignore[reportUnknownMemberType]
        asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()  # pyright: ignore[reportUnknownMemberType]
    )

    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: loopback\n"
        f"    host: 127.0.0.1\n"
        f"    port: {ssh_test_server.port}\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        f"    host_key: {wrong_host_key}\n",
    )
    main(["migrate"])
    capsys.readouterr()

    await _astore_secret("ssh_probe_key_loopback", ssh_test_server.client_key_pem)

    rc = await _cmd_test("loopback")
    err = capsys.readouterr().err
    assert rc == 1
    assert "CRITICAL" in err
    assert "MITM" in err
    assert "HostKeyMismatch" in err


def test_test_connection_refused(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test exits 1 when connection refused (port unbound)."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: loopback\n"
        "    host: 127.0.0.1\n"
        "    port: 1\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        "    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc123 x\n",
    )
    main(["migrate"])
    capsys.readouterr()

    assert main(["ssh-probe", "keygen", "loopback"]) == 0
    capsys.readouterr()

    rc = main(["ssh-probe", "test", "loopback"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err


@pytest.mark.asyncio
async def test_test_malformed_config(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test exits 1 when config root is malformed (not a mapping)."""
    _write_install_config(tmp_path, monkeypatch, "- a\n- b\n")
    main(["migrate"])
    capsys.readouterr()

    rc = await _cmd_test("x")
    err = capsys.readouterr().err
    assert rc == 1
    assert "config root must be a mapping" in err


def test_test_master_key_error(
    db_url_env: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test exits 1 when master key unavailable."""
    _write_install_config(
        tmp_path,
        monkeypatch,
        "ssh_targets:\n"
        "  - id: loopback\n"
        "    host: 127.0.0.1\n"
        "    port: 22\n"
        "    user: root\n"
        "    account_mode: appliance\n"
        "    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc123 x\n",
    )
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        "homelab_monitor.kernel.secrets.master_key.DEFAULT_KEY_FILE",
        "/nonexistent/path/master-key",
    )
    main(["migrate"])
    capsys.readouterr()

    rc = main(["ssh-probe", "test", "loopback"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no master key" in err


# --------------------------- dispatch ---------------------------


def test_no_subcommand_prints_usage(
    cli_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``hm ssh-probe`` with no subcommand exits 2 and prints usage."""
    main(["migrate"])
    capsys.readouterr()
    rc = main(["ssh-probe"])
    err = capsys.readouterr().err
    assert rc == 2  # noqa: PLR2004
    assert "usage" in err.lower()
