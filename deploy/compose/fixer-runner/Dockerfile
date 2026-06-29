# syntax=docker/dockerfile:1.7
# fixer-runner image (EPIC-009 / STAGE-009-003): the dedicated, low-privilege
# runtime that hosts the `homelab-fixer` identity and the `claude` CLI used by
# the auto-fix subsystem.
#
# BUILD CONTEXT: deploy/compose/fixer-runner (this directory). Paths below are
# relative to THAT directory (NOT the repo root) — contrast the monitor image,
# which builds from the repo root.
#
# Non-negotiables baked in here:
#   #3 identity : homelab-fixer is created in-image from build-arg UID/GID
#                 (default 1002:1002), is NEVER root at runtime, has NO docker
#                 group and NO sudoers. The image ends with USER homelab-fixer.
#   #7 kill sw  : PID 1 is `tail -f /dev/null` (signal-respecting idle keepalive).
#                 claude is NEVER run at boot. `docker kill` terminates the
#                 container + any in-flight `docker exec claude`.
#
# claude install switch:
#   ARG CLAUDE_BINARY_SOURCE=real  -> fetch + install the pinned native binary
#                                     (NO Node, auto-update DISABLED).
#   ARG CLAUDE_BINARY_SOURCE=fake  -> COPY the in-repo test/fake-claude script to
#                                     the same PATH location. Used by CI + the
#                                     integration test (no real key, no network).

FROM debian:12-slim

# --- Identity build args (#3). Overridable from compose build args
#     (HM_FIXER_UID / HM_FIXER_GID). Defaults match .env.example / host-setup.sh. ---
ARG FIXER_UID=1002
ARG FIXER_GID=1002

# --- claude version pin + install-source switch ---
ARG CLAUDE_VERSION=stable
ARG CLAUDE_BINARY_SOURCE=real

# Disable claude auto-update unconditionally (defense in depth; the binary is
# version-pinned and the runtime user cannot write to /usr/local/bin anyway).
# DISABLE_AUTOUPDATER is the official documented var (code.claude.com/docs/en/setup).
ENV DISABLE_AUTOUPDATER=1

# Minimal runtime deps. ca-certificates + curl are needed for the real install
# (TLS fetch) and are harmless under fake. No Node, no build toolchain.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid "${FIXER_GID}" homelab-fixer \
 && useradd --system --uid "${FIXER_UID}" --gid homelab-fixer \
      --create-home --home-dir /home/homelab-fixer homelab-fixer

# --- claude install (isolated to this block). The real branch fetches the
#     pinned native binary to /usr/local/bin/claude; the fake branch copies the
#     test harness to the SAME path. Either way `claude` is on PATH at
#     /usr/local/bin/claude, root-owned + non-writable by homelab-fixer. ---
COPY test/fake-claude /tmp/fake-claude
RUN if [ "${CLAUDE_BINARY_SOURCE}" = "fake" ]; then \
        install -m 0755 /tmp/fake-claude /usr/local/bin/claude ; \
    else \
        # INSTALL_CMD (verified against official Claude Code docs, June 2026):
        # The official native installer downloads the pinned `claude` native
        # binary (NO Node) into the running user's ~/.local/bin. We run it as
        # root with an explicit HOME so the binary lands at a known path, then
        # relocate it to /usr/local/bin/claude (root-owned, non-writable by
        # homelab-fixer, on PATH for all users). `bash -s <version>` pins the
        # version (CLAUDE_VERSION; `latest` selects newest). Auto-update is
        # disabled via DISABLE_AUTOUPDATER (set ENV above + here for the install).
        # Source: https://code.claude.com/docs/en/setup
        # CLAUDE_VERSION accepts a concrete version (e.g. 2.1.89), or a channel
        # (stable|latest). Default is the documented `stable` channel; pin a concrete
        # version for fully reproducible prod builds + a clear audit trail of what ran.
        export HOME=/root \
        && curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
        && mv /root/.local/bin/claude /usr/local/bin/claude ; \
    fi \
 && rm -f /tmp/fake-claude \
 && chmod 0755 /usr/local/bin/claude \
 # test -x is a deliberate presence/exec-bit check, NOT a run of the binary: the
 # same RUN serves both real|fake sources, and the fake (set -eu, writes to the
 # transcript dir) would FAIL if executed at build time (that dir doesn't exist
 # at this layer). Do NOT replace test -x with `claude --version`.
 && test -x /usr/local/bin/claude

# --- Baked public-floor CLAUDE.md (#B). Lives in the fixer's home dir AND the
#     WORKDIR so `claude` discovers it from cwd and/or $HOME. The host-specific
#     TARGET overlay mounts read-only at /data/policy/CLAUDE.host.md at runtime
#     (STAGE-009-008 + overrides repo); see the CLAUDE.md body. ---
COPY CLAUDE.md /home/homelab-fixer/CLAUDE.md
RUN chown homelab-fixer:homelab-fixer /home/homelab-fixer/CLAUDE.md

# Pre-create the transcript + policy mount points so a fresh bind-mount inherits
# sane ownership if the host path is empty. The real transcript dir is a host
# bind-mount with POSIX default ACLs granting FIXER_UID rwX (host-setup.sh §3.9).
RUN mkdir -p /data/runbook-transcripts /data/policy \
 && chown -R homelab-fixer:homelab-fixer /data

# #3: drop to the baked low-priv identity. NEVER root at runtime.
USER homelab-fixer
WORKDIR /home/homelab-fixer

# #7: PID 1 is a signal-respecting idle keepalive. claude is NEVER launched at
# boot — the orchestrator (STAGE-009-005) `docker exec`s it on demand. tail -f
# /dev/null forwards SIGTERM/SIGKILL so `docker kill` stops the container (and,
# by extension, kills any in-flight `docker exec claude`).
CMD ["tail", "-f", "/dev/null"]
