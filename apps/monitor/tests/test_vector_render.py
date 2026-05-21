"""Tests for kernel/cron/render.py — csv_to_toml_array, render_config, render_on_boot.

STAGE-003-002: Vector container-log ingestion fix + VECTOR_DOCKER_EXCLUDE opt-out.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest import mock

import pytest
import structlog
from structlog.testing import capture_logs

from homelab_monitor.kernel.cron.render import (
    VectorRenderContext,
    csv_to_toml_array,
    render_config,
    render_on_boot,
)

EXPECTED_CONFIG_MODE = 0o640
EXPECTED_ALL_INVALID_WARNINGS = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# csv_to_toml_array
# ---------------------------------------------------------------------------


def test_csv_empty_string_returns_empty_array() -> None:
    """Empty string → '[]'."""
    assert csv_to_toml_array("", _make_log()) == "[]"


def test_csv_whitespace_only_returns_empty_array() -> None:
    """Whitespace-only string → '[]'."""
    assert csv_to_toml_array("   ", _make_log()) == "[]"


def test_csv_single_entry() -> None:
    """Single valid name → '[\"name\"]'."""
    assert csv_to_toml_array("mycontainer", _make_log()) == '["mycontainer"]'


def test_csv_multiple_entries() -> None:
    """Three entries → correct TOML array."""
    assert csv_to_toml_array("a,b,c", _make_log()) == '["a", "b", "c"]'


def test_csv_whitespace_padded_entries_are_trimmed() -> None:
    """Entries with surrounding spaces are trimmed."""
    assert csv_to_toml_array(" a , b ", _make_log()) == '["a", "b"]'


def test_csv_trailing_comma_ignored() -> None:
    """Trailing comma produces no empty entry."""
    assert csv_to_toml_array("a,b,", _make_log()) == '["a", "b"]'


def test_csv_empty_between_commas_ignored() -> None:
    """Doubled commas produce no empty entry."""
    assert csv_to_toml_array("a,,b", _make_log()) == '["a", "b"]'


def test_csv_entry_with_quote_skipped_others_retained() -> None:
    """Entry containing double-quote → skipped; WARNING logged; others kept."""
    with capture_logs() as logs:
        result = csv_to_toml_array('good,bad"name,ok', _make_log())
    assert result == '["good", "ok"]'
    warnings = [r for r in logs if r.get("log_level") == "warning"]
    assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}: {logs}"
    assert warnings[0].get("event") == "vector_docker_exclude.invalid_entry"
    assert warnings[0].get("entry") == 'bad"name'


def test_csv_entry_with_backslash_skipped() -> None:
    """Entry containing backslash → skipped; WARNING logged; others kept."""
    with capture_logs() as logs:
        result = csv_to_toml_array("good,bad\\name,ok", _make_log())
    assert result == '["good", "ok"]'
    warnings = [r for r in logs if r.get("log_level") == "warning"]
    assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}: {logs}"
    assert warnings[0].get("event") == "vector_docker_exclude.invalid_entry"
    assert warnings[0].get("entry") == "bad\\name"


def test_csv_all_invalid_entries_returns_empty_array() -> None:
    """When all entries are invalid → '[]'; both WARNINGs emitted."""
    with capture_logs() as logs:
        result = csv_to_toml_array('bad"one,bad\\two', _make_log())
    assert result == "[]"
    warnings = [r for r in logs if r.get("log_level") == "warning"]
    assert len(warnings) == EXPECTED_ALL_INVALID_WARNINGS, (
        f"expected {EXPECTED_ALL_INVALID_WARNINGS} WARNINGs, got {len(warnings)}: {logs}"
    )
    assert warnings[0].get("event") == "vector_docker_exclude.invalid_entry"
    assert warnings[0].get("entry") == 'bad"one'
    assert warnings[1].get("event") == "vector_docker_exclude.invalid_entry"
    assert warnings[1].get("entry") == "bad\\two"


# ---------------------------------------------------------------------------
# render_config
# ---------------------------------------------------------------------------


def _write_template(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "vector.toml.template"
    p.write_text(content, encoding="utf-8")
    return p


def test_render_config_substitutes_both_placeholders(tmp_path: Path) -> None:
    """Both ${CRON_EVENTS_INGEST_TOKEN} and ${VECTOR_DOCKER_EXCLUDE} are replaced."""
    template_content = (
        'token = "${CRON_EVENTS_INGEST_TOKEN}"\nexclude_containers = ${VECTOR_DOCKER_EXCLUDE}\n'
    )
    tmpl = _write_template(tmp_path, template_content)
    out = tmp_path / "vector.toml"
    ctx = VectorRenderContext(cron_events_token="tok-abc", docker_exclude_csv="")
    render_config(template_path=tmpl, output_path=out, context=ctx, log=_make_log())
    rendered = out.read_text()
    assert 'token = "tok-abc"' in rendered
    assert "exclude_containers = []" in rendered


def test_render_config_empty_docker_exclude_renders_empty_array(tmp_path: Path) -> None:
    """Empty docker_exclude_csv → exclude_containers = []"""
    tmpl = _write_template(tmp_path, "exclude_containers = ${VECTOR_DOCKER_EXCLUDE}\n")
    out = tmp_path / "vector.toml"
    ctx = VectorRenderContext(cron_events_token="t", docker_exclude_csv="")
    render_config(template_path=tmpl, output_path=out, context=ctx, log=_make_log())
    assert "exclude_containers = []" in out.read_text()


def test_render_config_multiple_containers_excluded(tmp_path: Path) -> None:
    """CSV with two containers → exclude_containers = ["a","b"]"""
    tmpl = _write_template(tmp_path, "exclude_containers = ${VECTOR_DOCKER_EXCLUDE}\n")
    out = tmp_path / "vector.toml"
    ctx = VectorRenderContext(cron_events_token="t", docker_exclude_csv="a,b")
    render_config(template_path=tmpl, output_path=out, context=ctx, log=_make_log())
    assert 'exclude_containers = ["a", "b"]' in out.read_text()


def test_render_config_atomic_write(tmp_path: Path) -> None:
    """Output file exists after render_config (atomic write succeeded)."""
    tmpl = _write_template(
        tmp_path,
        "token = ${CRON_EVENTS_INGEST_TOKEN}\nexclude = ${VECTOR_DOCKER_EXCLUDE}\n",
    )
    out = tmp_path / "vector.toml"
    ctx = VectorRenderContext(cron_events_token="t", docker_exclude_csv="")
    render_config(template_path=tmpl, output_path=out, context=ctx, log=_make_log())
    assert out.exists()


def test_render_config_mode_0640(tmp_path: Path) -> None:
    """Rendered file has mode 0o640 (group-readable, world-unreadable)."""
    tmpl = _write_template(tmp_path, "${CRON_EVENTS_INGEST_TOKEN} ${VECTOR_DOCKER_EXCLUDE}")
    out = tmp_path / "vector.toml"
    ctx = VectorRenderContext(cron_events_token="t", docker_exclude_csv="")
    render_config(template_path=tmpl, output_path=out, context=ctx, log=_make_log())
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == EXPECTED_CONFIG_MODE


def test_render_config_raises_on_missing_template(tmp_path: Path) -> None:
    """FileNotFoundError is re-raised when template is absent."""
    out = tmp_path / "vector.toml"
    ctx = VectorRenderContext(cron_events_token="t", docker_exclude_csv="")
    with pytest.raises(FileNotFoundError):
        render_config(
            template_path=tmp_path / "nonexistent.template",
            output_path=out,
            context=ctx,
            log=_make_log(),
        )


# ---------------------------------------------------------------------------
# render_on_boot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_on_boot_reads_vector_docker_exclude_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_on_boot reads VECTOR_DOCKER_EXCLUDE from env and passes it to context."""
    monkeypatch.setenv("VECTOR_DOCKER_EXCLUDE", "mycontainer")

    tmpl = _write_template(
        tmp_path,
        'token = "${CRON_EVENTS_INGEST_TOKEN}"\nexclude = ${VECTOR_DOCKER_EXCLUDE}\n',
    )
    out = tmp_path / "vector.toml"

    with (
        mock.patch(
            "homelab_monitor.kernel.cron.render.ensure_cron_events_token",
            return_value="the-token",
        ),
        mock.patch(
            "homelab_monitor.kernel.cron.render.VectorRenderContext",
            wraps=VectorRenderContext,
        ) as ctx_spy,
    ):
        result = await render_on_boot(
            auth_repo=mock.AsyncMock(),
            secrets_repo=mock.AsyncMock(),
            template_path=tmpl,
            output_path=out,
            log=_make_log(),
        )

    assert result == "the-token"
    # Verify VectorRenderContext was built with the env var value
    ctx_spy.assert_called_once_with(
        cron_events_token="the-token",
        docker_exclude_csv="mycontainer",
    )


@pytest.mark.asyncio
async def test_render_on_boot_default_env_uses_empty_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When VECTOR_DOCKER_EXCLUDE is unset, docker_exclude_csv defaults to ''."""
    monkeypatch.delenv("VECTOR_DOCKER_EXCLUDE", raising=False)

    tmpl = _write_template(
        tmp_path,
        '"${CRON_EVENTS_INGEST_TOKEN}" ${VECTOR_DOCKER_EXCLUDE}',
    )
    out = tmp_path / "vector.toml"

    with (
        mock.patch(
            "homelab_monitor.kernel.cron.render.ensure_cron_events_token",
            return_value="tok",
        ),
        mock.patch(
            "homelab_monitor.kernel.cron.render.VectorRenderContext",
            wraps=VectorRenderContext,
        ) as ctx_spy,
    ):
        await render_on_boot(
            auth_repo=mock.AsyncMock(),
            secrets_repo=mock.AsyncMock(),
            template_path=tmpl,
            output_path=out,
            log=_make_log(),
        )

    ctx_spy.assert_called_once_with(
        cron_events_token="tok",
        docker_exclude_csv="",
    )
