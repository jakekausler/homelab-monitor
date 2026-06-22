# Parallel Instance Support — Implementation Plan

> **Status:** COMPLETE — Build + Refinement + Finalize done. Both instances (A on 29090, B on 39090) live on rebuilt branch images. Commits: 1ff8aaf (Build), 5b7cf43 (Refinement fixes + docs), 296421f (changelog).
>
> **Refinement findings (live two-instance run on host):**
> - Instance A migrated to new compose (project/containers/volumes/network byte-identical; cadvisor+vector kept via `COMPOSE_PROFILES=host-collectors` added to A's live `.env`; A restarted, healthy). A's `.env.bak-prerestart` backup exists.
> - Instance B stood up at `/storage/programs/homelab-monitor-b` (project `homelab-monitor-b`, monitor on 39090), coexisting with A. No name/port/volume/network collision; A undisturbed throughout.
> - **DEFECT 1 (fixed):** config-init did not chown `/data` + `/storage/backup` → fresh `data_monitor` volume (baked 1000:1000/755 by image) was unwritable by the runtime uid (995) → SQLite "unable to open database file". This is a LATENT bug for ANY fresh instance incl. a fresh A. Fix: extended config-init to `chown ${HM_CRON_HOST_UID}:${HM_CRON_HOST_GID} /data /storage/backup && chmod 755`. **Verified on a truly fresh volume** (removed B's data_monitor, re-upped → config-init chowned to 995:995, monitor healthy, no manual step). Reviewed APPROVED.
> - **DEFECT 2 (fixed):** the monitor compose service never forwarded `HOMELAB_MONITOR_DOCKER_ENABLED` into the container (was comment-only) → the B1 flag was silently dropped. Fix: added `HOMELAB_MONITOR_DOCKER_ENABLED: ${HOMELAB_MONITOR_DOCKER_ENABLED:-true}` to the monitor `environment:`.
> - **DEFECT 3 (process, fixed):** the running images for BOTH A and B PREDATED all parallel-instance code (A `:dev` built 3h before the B1 commit; B pulled `:latest` from 5 weeks ago). Initial B validation was INVALID (reflected old image). Resolution: build B's image LOCALLY from the branch (`GITHUB_REPOSITORY=homelab-monitor-local-b`, `IMAGE_TAG=dev`). Fresh-clone build requires `pnpm install` + `pnpm --filter ui run generate-types` (gitignored `apps/ui/src/api/schema.ts`) + UI build before `docker compose build`.
> - **Re-validation on the real image: ALL PASS** — `DockerConfig(enabled=False)` live in B's container; no docker collector registered; no socket access; docker route serves no data; cron discovery clean (`inserted=0 errors=0`) against empty dirs; B-private mount sources; A healthy/undisturbed. e2e-tester 8/8 PASS.
>
> **A rebuild COMPLETE:** A was rebuilt from the branch (image homelab-monitor-local:dev), restarted, healthy. DockerConfig(enabled=True) confirmed in A's running container. The config-init fix (`chown` to 995:995) was verified applied and `/data` ownership confirmed. A now runs the same code as B (parallel-instance feature complete, both instances on reviewed/finalized branch code).
>
> **Minor open item (deferred):** B still mounts `/var/run/docker.sock` (unused when docker disabled). Could omit for B; no functional impact. Noted for operator docs.
>
> ---
> **Original Build outcome (branch `feat/parallel-instance`):**
>
> **Build outcome (branch `feat/parallel-instance`, uncommitted):**
> - **B1 done:** `DockerConfig`/`load_docker_config()` in `config.py`; `HOMELAB_MONITOR_DOCKER_ENABLED` gates DockerSocketCollector + DockerDiscoverer registration, the image-update loop, the override-loader, AND the whole `ComposeActionRunner` block (its `__init__` requires a non-None socket client, so gating the entire block is the type-safe reading; the docker router already 503s when the runner is absent). New test `test_docker_disabled_skips_registration_and_socket_client`. The 3 auto-degrading consumers stay None-safe.
> - **B2 done:** `HM_INSTANCE` namespaces project/13 container names/network; volumes namespaced via project prefix (no explicit `name:`). Mount **sources** parameterized (`HM_CRON_IPC_SRC`, `HM_CRON_SNAPSHOT_SRC`, `HM_HOST_ETC`, `HM_HOST_PROC`). `cadvisor`+`vector` profile-gated (`host-collectors`). `.env.example` documents all. Monitor container special-cased to `${HM_INSTANCE:-homelab-monitor}` (no `-monitor` suffix) to preserve today's name. **⚠️ Instance A is NOT fully zero-touch:** A's live `deploy/compose/.env` MUST add `COMPOSE_PROFILES=host-collectors` or cadvisor+vector silently stop. This is inherent to Compose profiles (no default-on-but-omittable). **Refinement/operator-doc action item.**
> - **B3 done:** `${HM_DEV_BASE:-/tmp/hm-dev}` in `dev-up.sh` (placed after env-sourcing so `dev.env` wins); build-sources heredoc unquoted to expand it. `dev.env.example` documents it.
> - **B4 PASS:** `make verify` green — ruff, pyright strict (0 errors), pytest 5535 passed @ 100% kernel coverage, vitest 1673 passed, tsc, UI build smoke. (97 pre-existing eslint React-19 warnings, 0 errors.)
>
> **Carry-forward for Refinement:** (1) A's `.env` needs `COMPOSE_PROFILES=host-collectors` added BEFORE/at next A deploy. (2) Run code review (Finalize steps 1-2) before the live two-instance bring-up.
>
> **Post-review fix (container-name backward-compat):** Inspecting the LIVE instance A revealed its containers are named `homelab-<svc>` (prefix `homelab`) while its project/volumes/network use prefix `homelab-monitor`. A single `HM_INSTANCE` var couldn't reproduce both, so the default would have renamed all 12 sidecar containers on restart. **Fix:** added a second var `HM_CONTAINER_PREFIX` (default `homelab`) controlling only the 13 `container_name:` lines; `HM_INSTANCE` (default `homelab-monitor`) still controls project/network/volumes. Default render now reproduces A's exact container names byte-for-byte. **Instance B must set BOTH** `HM_INSTANCE` and `HM_CONTAINER_PREFIX` (to the same value). Second code review APPROVED. (First review missed this because it reasoned about the compose file in isolation, not against the live deploy's actual names.)
>
> **Live A facts (from `docker inspect`):** project `homelab-monitor`, containers `homelab-*`, volumes `homelab-monitor_*`, network `homelab-monitor-net`. A's live `.env` has neither `HM_INSTANCE` nor `COMPOSE_PROFILES` (both rely on defaults / need the profiles line added).
> **Type:** Cross-cutting infra change (NOT an epic/stage — adapted phase-build → phase-refinement → phase-finalize)
> **Goal:** Run a second, fully independent instance ("instance B") of homelab-monitor on the same host, alongside the existing prod instance ("instance A"). Instance B is **dev-only** but needs its own **full prod-like compose stack** to develop against. Instance B does **not** do Docker container monitoring and does **not** participate in host-integration (cron snapshot/apply, systemd units).

---

## ⚠️ Git / branch caveat (read first)

At the time this plan was written, **another agent is doing vulnerability fixes on a non-main branch**. It will switch back to `main` at some point and may **not** commit this document. Consequences:

- This file (`PARALLEL-INSTANCE-PLAN.md`) lives at repo root and is **untracked**. If a branch switch / checkout discards it, **re-create it from this content** (it is self-contained).
- Do **not** assume the current branch. Before starting Build, confirm we are on `main` (or a fresh branch off `main`) with a clean tree: `git status`, `git branch --show-current`.
- All Build work for this plan should happen on a **dedicated branch off `main`** (e.g. `feat/parallel-instance`), created only after the vuln-fix agent has merged/landed and we are cleanly on `main`. Do NOT interleave with the vuln-fix branch.
- Per project convention: **never `git add -A`** — always explicit paths.

---

## Background & Decisions (locked)

Findings from three investigation passes (see "Investigation Summary" at bottom). Decisions confirmed with user:

1. **Instance B does NOT need Docker container monitoring** → add a config flag to disable the Docker plugin entirely (discoverer + socket collector). This sidesteps the docker-socket-stomping problem (both instances share `/var/run/docker.sock` with no instance namespace).
2. **Instance B does NOT participate in host-integration** → cron snapshot/apply, the `homelab-monitor` host user/group, the `/usr/local/sbin` executors, and the 6 systemd units are a **host singleton** and stay owned by instance A only. The app **degrades gracefully** when these are absent (verified), so instance B simply doesn't wire them. `host-setup.sh` does **NOT** need to become instance-aware.
3. **Tests & validation are already isolated** → `make verify`, `make test-fast`, `make integration`, and Playwright run with ephemeral/tmpdir state, distinct compose project (`homelab-monitor-test`), and the test monitor on `127.0.0.1:19090`. Clone B can run the full suite while instance A's prod stack is live, with zero collision. **No test-isolation work required.**

### What this means for scope

| Surface | Action |
| --- | --- |
| Compose stack identity (project name, container names, network, volumes) | **Parameterize** behind one `HM_INSTANCE` var |
| Host-published ports (prod backend, UDM syslog; all dev sidecar ports) | Already env-driven — just need distinct values per instance |
| SQLite DB path / data dirs | Already env-driven (`HOMELAB_MONITOR_DB_URL`) — distinct per instance |
| `/tmp/hm-dev` base path in `dev-up.sh` | **Parameterize** behind a base-dir var |
| Docker plugin | **New disable flag** (`HOMELAB_MONITOR_DOCKER_ENABLED`) |
| Cron snapshot / IPC env vars (in-container *app behavior*) | Point instance B at non-existent paths (graceful no-op) — **no code change** |
| Cron/crontab/etc/proc bind-mount **SOURCES** (host paths in the YAML) | **Parameterize the mount sources** (see B2/C1) — env vars do NOT touch the mount line; the literal host path is mounted regardless |
| `cadvisor` + `vector` docker.sock / rootfs / journal mounts | **Gate behind a compose profile** so instance B can omit them (they ignore `HOMELAB_MONITOR_DOCKER_ENABLED`; that flag only governs the *monitor's* docker plugin) |
| `host-setup.sh`, systemd units, host user/group, `/usr/local/sbin` scripts | **Out of scope** — instance A owns them; instance B doesn't use them |
| Test/integration/Playwright isolation | **Out of scope** — already isolated |

---

## Scope Boundaries (YAGNI)

**IN scope:**
- One `HM_INSTANCE` variable that namespaces the prod compose stack identity.
- One `HOMELAB_MONITOR_DOCKER_ENABLED` flag (default `true`) that skips Docker discoverer + socket collector registration and all docker-socket-dependent wiring.
- Parameterized dev-rig base path so two dev rigs don't collide on `/tmp/hm-dev`.
- Documentation: an operator guide for standing up instance B.

**OUT of scope (explicitly NOT building):**
- Making `host-setup.sh` / systemd units instance-aware. (Host singleton; instance B runs cron-disabled.)
- A docker-socket instance-ownership/label filter that would let BOTH instances monitor containers. (User confirmed B doesn't need container monitoring.)
- Per-instance test isolation. (Already isolated.)
- Any changes to instance A's running stack beyond the backward-compatible default values.

> **Correction (from review):** The host bind-mount **sources** (the literal host paths in the compose `volumes:` lines, e.g. `/var/lib/homelab-monitor/cron-apply:/host-ipc:rw`) are NOT controlled by the `HM_CRON_*` env vars — those env vars only set the *in-container* path the app reads. `docker compose up` mounts the literal host source regardless. If left as literals, instance B would RW-mount instance A's **live** cron-apply IPC directory — violating independence and risking corruption of A. Therefore the mount **sources** must be parameterized (B2/C1), not just the env vars. Likewise `cadvisor`/`vector` mount `docker.sock`/rootfs/journal independently of the monitor's docker flag, so they need a profile gate.

**Backward-compatibility invariant:** With no new env vars set, the prod stack and the app behave **exactly as today** (`HM_INSTANCE` defaults to `homelab-monitor`, `HOMELAB_MONITOR_DOCKER_ENABLED` defaults to `true`, dev base path defaults to `/tmp/hm-dev`). Instance A must require **zero** changes to its `.env`.

---

## Build Phase — Work Items

> All execution (edits, file reads, verification) is delegated to subagents per project convention. Main agent coordinates.

### B1. Docker-plugin disable flag

**Files:**
- `apps/monitor/homelab_monitor/kernel/config.py` — add `DockerConfig` dataclass + `load_docker_config()`, mirroring the `HaRegistryConfig` / `load_ha_registry_config()` pattern (`config.py` ~1643–1707).
- `apps/monitor/homelab_monitor/kernel/api/lifespan.py` — load `docker_config` alongside other config loaders (~line 161), then gate:
  - DockerSocketCollector registration (lifespan.py ~423–442)
  - DockerDiscoverer registration (lifespan.py ~444–466)
  - Early `DockerSocketClient` construction for `ComposeActionRunner` (~860–869)
  - Image-update events loop (~1100–1184) — add `docker_config.enabled` to existing guards
  - Override-loader startup (~1187–1224)

**Approach:** **Skip registration entirely** when disabled (recommended by investigation). `DockerSocketClient.__init__` does not connect at construction, so the post-construction `isinstance(c, DockerSocketCollector/DockerDiscoverer)` wiring blocks become automatic no-ops when the collectors are never registered. The docker router endpoints (`routers/docker.py`) already return 503 defensively when `docker_socket_client` is None — no change needed there.

**Auto-degrading consumers (verified — no extra gating needed, but test):** `ProbeSupervisor` (wired ~lifespan.py:971), `ImageUpdateCollector` (~991), and `LocalBuildUpdateCollector` (~1015) each receive the socket client via `getattr(app.state, "docker_socket_client", None)`. When docker is disabled and the client is never constructed, they receive `None` and degrade gracefully. The new lifespan test must assert these three still register/run cleanly with the flag off (no crash, None client).

**Env var:** `HOMELAB_MONITOR_DOCKER_ENABLED` (default `true`; truthy = `1`/`true`/`yes`, matching `DrainConfig` convention).

**Tests (write in Build, TDD where practical):**
- `test_api_lifespan.py`: new `test_docker_disabled_skips_registration_and_socket_client` — with `HOMELAB_MONITOR_DOCKER_ENABLED=false`, neither collector is registered, `app.state.docker_socket_client is None`, docker router endpoints return 503.
- Confirm existing docker tests (`test_docker_socket_collector.py`, `test_docker_discoverer.py`, `test_docker_override_loader.py`, the 3 docker wiring tests in `test_api_lifespan.py`) still pass with the flag defaulting to `true`.

**Success criteria:** Backend boots clean with the flag off; no docker socket access occurs; 100% kernel coverage gate still passes (new branch needs coverage).

---

### B2. Parameterize prod compose stack identity

**File:** `deploy/compose/docker-compose.yml`

Introduce a single `HM_INSTANCE` variable (default `homelab-monitor`) and use Compose interpolation:
- Top-level `name: ${HM_INSTANCE:-homelab-monitor}` (replaces literal `name: homelab-monitor`).
- Each `container_name: homelab-*` (13 entries) → `container_name: ${HM_INSTANCE:-homelab-monitor}-<svc>` (preserves existing names when unset).
- Network `homelab-monitor-net` → `${HM_INSTANCE:-homelab-monitor}-net`.
- **Volumes — rely on project-name prefixing; do NOT add explicit `name:`.** The 11 volumes have **no explicit `name:`** today, so Compose already prefixes them with the project name (effective names are `homelab-monitor_data_monitor`, etc.). Setting the project name via `HM_INSTANCE` **namespaces the volumes for free**. Adding explicit `name:` lines is redundant *and* would change `docker compose config` output. **Leave the volume keys bare.**

**C1 — Parameterize the host bind-mount SOURCES (required for true isolation):**
The `monitor` service mounts these literal host paths; instance B must NOT share A's live RW paths. Make the sources env-driven with today's literals as defaults (preserves A's behavior when unset):
- `${HM_CRON_IPC_SRC:-/var/lib/homelab-monitor/cron-apply}:/host-ipc:rw` — **RW, the dangerous one.** B points it at an instance-B-private dir.
- `${HM_CRON_SNAPSHOT_SRC:-/var/lib/homelab-monitor/crontab-snapshot}:/host-crontab-snapshot:ro`
- `/etc:/host/etc:ro` and `/proc:/host/proc:ro` — read-only and harmless to share, but make the source env-driven too (`${HM_HOST_ETC:-/etc}`, `${HM_HOST_PROC:-/proc}`) so B can point at empty dirs and avoid mounting A's host `/etc`. (B's app already no-ops on empty cron sources.)
- `/var/run/docker.sock` — B has docker disabled in the app, but the monitor mount is still declared. Leave default (shared, read path only) OR null it for B; document the decision.

**I3 — Gate `cadvisor` and `vector` docker/host mounts behind a profile:**
`cadvisor` (mounts `docker.sock:ro`, `/:rootfs:ro`, `/sys`, `/var/lib/docker`, `/var/run`) and `vector` (mounts `docker.sock:ro`, journal, machine-id) have **no profile** today and always start — independent of the monitor's docker flag. For instance B (no container monitoring), put them behind a compose `profiles:` (e.g. `host-collectors`) that is active by default for A and omitted for B. **Decision to lock in B2:** profile-gate them; B sets `COMPOSE_PROFILES` to exclude `host-collectors`. Document in the Risk Register and operator guide either way.

**Cross-references that must stay consistent:**
- Internal service URLs the app uses (`http://victoriametrics:8428`, `http://alertmanager:9093`, karma, grafana) reference **compose service names**, NOT `container_name` — **verified** (service names stay literal in the YAML; only `container_name`/network/project change). No internal-resolution breakage.
- `config-init` is a `service_completed_successfully` dependency for `monitor`; it `chown`s render volumes to `${HM_CRON_HOST_UID:-1000}:2000`. B must set `HM_CRON_HOST_UID` consistently; **Refinement must verify B's `config-init` completes** (else B's monitor never starts).
- `.env.example` — document `HM_INSTANCE`, the new mount-source vars, `COMPOSE_PROFILES`, and `HOMELAB_MONITOR_DOCKER_ENABLED` with defaults.

**Host-published ports for instance B** (must differ from A's `2xxxx`):
- Instance B prod backend → `HOMELAB_MONITOR_PORT=39090` (already env-driven; no YAML change).
- Instance B UDM syslog → **LOCKED:** the mapping is unconditional and `vector` always starts, so B leaving the default `0.0.0.0:5514` would collide with A and **fail B's `vector` startup**. You cannot conditionally omit a single port line via interpolation. **B's `.env` sets `HM_UDM_SYSLOG_PORT=35514` and `HM_UDM_SYSLOG_BIND_HOST=127.0.0.1`** (B has no real UDM feed; bind loopback). (If `vector` is profile-gated per I3, B omits it entirely and this is moot — but set the distinct port anyway as belt-and-suspenders.)

**Success criteria:**
- With all new vars unset, `docker compose config` renders **the same effective container/network/volume names and the same mount sources as today** (defaults reproduce current behavior — instance A needs zero `.env` changes). Verify A's existing live `homelab-monitor_*` volumes are bound (not orphaned) under the default project name.
- With `HM_INSTANCE=homelab-monitor-b` + B's mount-source vars + `COMPOSE_PROFILES` excluding `host-collectors`, config renders fully namespaced names, B-private mount sources, and no cadvisor/vector.
- `make dev-prod` still works for instance A defaults.

---

### B3. Parameterize dev-rig base path

**File:** `scripts/dev-up.sh`

- Replace hard-coded `/tmp/hm-dev` (used for backup, runbook-transcripts, seed compose, build-contexts, config) with `${HM_DEV_BASE:-/tmp/hm-dev}`.
- `LOG_DIR` is already `${REPO_ROOT}/deploy/dev/logs` (per-checkout) — OK, no change.
- `deploy/dev/dev.env` already drives ports (`HM_DEV_*`) and DB URL — instance B's clone gets its own `dev.env` with distinct `1xxxx`-range ports and a distinct `HM_DEV_BASE`.

**Note:** `make dev` (hybrid) for instance B is **optional** — the user said B needs *prod* to develop against. But parameterizing the base path is cheap and prevents a foot-gun if both dev rigs ever run. Keep it minimal.

**Success criteria:** Two clones can each run `make dev` with distinct `HM_DEV_BASE` and distinct `HM_DEV_*` ports without colliding on `/tmp` or ports. (Spot-check, not exhaustive — prod is the real target.)

---

### B4. Build verification gate

After B1–B3, delegate to verifier + tester:
- `make verify` (full) must pass: ruff + black + pyright + pytest @100% kernel coverage + tsc + vitest + UI build smoke.
- Tee output: `make verify 2>&1 | tee /tmp/parallel-instance-verify-$(date +%s).log`; inspect via read-only subagent.

---

## Refinement Phase — Adapted (the unusual part)

This is **infrastructure/tooling + backend** refinement, validated against **real two-instance reality on this host**. It is NOT a frontend viewport sign-off. The defining feature: we actually stand up instance B as a sibling clone and run instance A + instance B **concurrently**.

> **Review-before-live ordering (workflow C2):** To avoid the most common re-validation churn, run the Finalize **code-reviewer pre-test review FIRST** (and implement its suggestions) *before* the clone/bring-up below. The skills reserve commits for Finalize and require live validation against reviewed code. If review changes land *after* the live bring-up, the two-instance validation is invalidated and must be redone (step 8). Prefer a **local clone** so amended/rebased commits don't require force-push.

### Refinement procedure

1. **Prep a clean, committed Build branch.** *(Cross-phase ordering: run Finalize steps 1–2 — code review + implement suggestions — BEFORE this bring-up, per the Refinement note above, so the live validation runs against reviewed code.)* Ensure `feat/parallel-instance` has **all** Build work committed and the tree is clean (`git status` clean). A local `git clone` copies only committed content; **`.env` files are gitignored and will NOT be cloned** — step 3 creates them fresh. Record the commit hash being validated. *(This commit exists only to enable the clone; the formal commit is in Finalize. If Finalize review amends the code, re-pull/rebuild — step 8.)*

2. **Clone to a sibling folder** (delegate to a subagent — local clone from the working repo):
   ```bash
   git clone /storage/programs/homelab-monitor /storage/programs/homelab-monitor-b
   cd /storage/programs/homelab-monitor-b && git checkout feat/parallel-instance
   ```

3. **Create instance B's env from scratch** (gitignored, not cloned) at `/storage/programs/homelab-monitor-b/deploy/compose/.env`:
   - `HM_INSTANCE=homelab-monitor-b`
   - `HOMELAB_MONITOR_PORT=39090`
   - `HOMELAB_MONITOR_DOCKER_ENABLED=false`
   - `COMPOSE_PROFILES=` (empty / excluding `host-collectors`) so cadvisor + vector are omitted (I3)
   - `HM_UDM_SYSLOG_PORT=35514`, `HM_UDM_SYSLOG_BIND_HOST=127.0.0.1` (I2)
   - `HM_CRON_IPC_SRC`, `HM_CRON_SNAPSHOT_SRC`, `HM_HOST_ETC`, `HM_HOST_PROC` → **instance-B-private host dirs** (e.g. under `/var/lib/homelab-monitor-b/…` or empty tmp dirs) — NOT A's live paths (C1)
   - `HM_CRON_SNAPSHOT_DIR` / `HM_CRON_APPLY_IPC_DIR` (in-container app paths) → non-existent (cron app behavior disabled)
   - `HOMELAB_MONITOR_DB_URL` → instance-B-specific (its own namespaced volume)
   - `HM_CRON_HOST_UID` → set consistently (config-init dependency)
   - `HOMELAB_MONITOR_MASTER_KEY` → **freshly generated for B** (do not reuse A's):
     `python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`
   - `chmod 600` the file.

4. **Bring up instance B's prod stack** alongside A's still-running stack. **Load B's `.env` explicitly and assert the project name before `up`** (I1):
   ```bash
   cd /storage/programs/homelab-monitor-b/deploy/compose
   docker compose --env-file .env -f docker-compose.yml config | grep -E '^name:'   # MUST print: name: homelab-monitor-b
   # If it prints "homelab-monitor", ABORT — .env not loaded, B would stomp A.
   docker compose --env-file .env -f docker-compose.yml up -d 2>&1 | tee /tmp/instance-b-up-$(date +%s).log
   ```
   Verify (read-only subagent on the log + `docker ps` + `docker volume ls`):
   - **`config-init` completed** (`service_completed_successfully`) — else monitor won't start.
   - All instance-B containers up with `homelab-monitor-b-*` names, on `homelab-monitor-b-net`, with `homelab-monitor-b_*` volumes; **no cadvisor/vector** (profile excluded).
   - **No collision** with A's containers/network/volumes; A's live `homelab-monitor_*` volumes untouched.
   - **B's cron-apply mount source is the B-private dir, NOT `/var/lib/homelab-monitor/cron-apply`** (inspect `docker inspect` mounts). This is the C1 correctness check.
   - Instance A's stack **undisturbed** (still healthy).

5. **Scenario 1 — regression re-check (skill-mandated, before live validation).** Run the full suite in the clone against the just-built code: `make verify 2>&1 | tee /tmp/parallel-instance-refine-verify-$(date +%s).log` (read-only subagent inspect). Skill scenario 3 (frontend-layer) is **N/A** — no API-contract/UI change.

6. **Scenarios 2 + 4 — live backend validation. Delegate to `e2e-tester`** (designs + runs the two-instance API checks; STOPs and surfaces on failure, does not self-fix — per `feedback_e2e_tester_no_debug_no_fix`):
   - Health (scenario 2, direct service-layer): `curl -s http://127.0.0.1:39090/<health route>` healthy.
   - **Docker plugin truly off:** B's logs show no docker collector registration; docker API endpoints 503; no `/var/run/docker.sock` access from B.
   - **Cron gracefully disabled:** discovery empty, no errors; cron-apply endpoint clean 503 (not hang/crash).
   - **Sidecars internal-only:** B's VM/VL/Grafana reachable only via B's monitor proxy (`/api/<sidecar>/`), not host-published (except backend 39090).
   - **No shared state (scenario 4, full-flow):** B's SQLite is its own volume; A's data untouched; exercise a representative end-to-end path.

7. **Run `make integration` in the clone while A + B are both live:** `make integration 2>&1 | tee /tmp/parallel-instance-integration-$(date +%s).log`. Confirm the `homelab-monitor-test` stack (port 19090) doesn't collide with A (29090) or B (39090), and that the test stack's own bind mounts don't collide with A's cron-apply RW source. Confirm A stayed healthy throughout.

8. **User parallel-use sign-off.** Hand off: the user uses instance B's prod (`http://127.0.0.1:39090`) **in parallel** with A's existing prod, exercises the parts they intend to develop, and confirms both run independently. This is the human acceptance gate.

9. **Issue loop:** any failure → debugger-lite/debugger → fixer → verifier → **return to step 4** (rebuild B, re-validate). Per the phase-refinement reset rule, **any code change re-invalidates the validation** — including changes that land during Finalize review (re-pull the clone to the amended commit, rebuild, re-run affected steps, and obtain fresh user sign-off before the final commit).

### Refinement gate (adapted from skill)
- [ ] Scenario 1: full suite (`make verify`) re-run in the clone passed **before** instance-B bring-up.
- [ ] `config-init` completed for B; instance B prod stack comes up clean alongside A (no name/net/volume/port collision).
- [ ] B's cron-apply mount **source** is B-private (NOT A's live `/var/lib/homelab-monitor/cron-apply`) — verified via `docker inspect`.
- [ ] cadvisor + vector confirmed **absent** in B (profile excluded).
- [ ] Instance A verified undisturbed throughout (containers healthy, `homelab-monitor_*` volumes intact).
- [ ] Docker plugin confirmed OFF in B (no socket access, 503 on docker endpoints) — via e2e-tester.
- [ ] Cron/host-integration confirmed gracefully disabled in B (empty discovery, clean 503 on apply).
- [ ] `make integration` passes in the clone with A + B live (no test-stack collision).
- [ ] **User parallel-use sign-off obtained** (explicit approval after using B's prod alongside A).

> **Skill adaptation note:** This replaces the frontend Desktop/Mobile viewport gate with a **two-instance-coexistence + user-parallel-use** gate, and maps the backend 4-scenario template (1=regression re-check, 2=direct service-layer, 4=full-flow; 3=frontend-layer N/A). The Documentation-Only operator-guide verification (Finalize) still applies for the new docs.

### Refinement exit gate (adapted — MANDATORY, before Finalize)
1. Mark Refinement complete in this plan doc (substitutes the stage-file update).
2. Record regression items in the changelog/plan-doc (substitutes epic `regression.md`): e.g. "bring up instance B alongside A; confirm no name/net/volume/port collision; confirm B's cron-apply mount source is B-private; confirm docker-503 and cron-503 in B; confirm A undisturbed."
3. Invoke `lessons-learned` skill.
4. Invoke `journal` skill.
Then invoke `phase-finalize`. *(Per the skills, lessons-learned + journal run at BOTH phase boundaries — here AND at Finalize exit — not once.)*

---

## Finalize Phase — Adapted

Standard phase-finalize, adapted: there is **no epic/stage tracking file** and **no `EPIC-XXX.md` table**. This plan doc + the changelog are the tracking surface.

> **Ordering:** Steps 1–2 (pre-test code review + implement suggestions) SHOULD run **before** the Refinement live bring-up (see Refinement note) so the two-instance validation runs against reviewed code. Steps 3+ run after Refinement sign-off.

1. **code-reviewer (Opus)** — pre-test review of B1–B3 diffs (config gating, compose interpolation incl. mount-source params + profile gate, dev-up.sh). Pay attention to: backward-compat (defaults reproduce today's behavior, A needs zero `.env` changes), the docker-disable gate covering ALL points (incl. the 3 auto-degrading consumers), the C1 mount-source parameterization, the I3 cadvisor/vector profile, compose service-name vs container_name correctness.
2. **Implement ALL review suggestions** (fixer/scribe) — all severities mandatory.
3. **Tests:** ensure B1's new test exists; if any code changed in review, **test-writer** fills gaps.
4. **tester** — run full suite in the **primary working tree against the final post-review code** (distinct from the Refinement clone's verify run); tee + read-only inspect.
5. **Second code-reviewer pass** IF **ANY existing implementation file** changed after the first review (the skill's human-judgment test — not limited to YAML/config; any human-decided edit triggers it). Pure automated formatting (prettier/ruff --fix) does not.
6. **Documentation (doc-writer, Opus — this is operator-facing infra):**
   - New `docs/dev/parallel-instance.md` (or extend `docs/dev/local-environment.md`): how to stand up instance B — clone, env config (`HM_INSTANCE`, `HOMELAB_MONITOR_DOCKER_ENABLED=false`, distinct ports/DB/master-key, cron-disabled), bring-up, teardown, and the explicit statement that host-integration (cron/systemd) stays owned by instance A.
   - Update `CLAUDE.md` port map / conventions: add the `3xxxx` instance-B prod port band and the `HM_INSTANCE` convention.
   - Update `.env.example` comments for `HM_INSTANCE` + `HOMELAB_MONITOR_DOCKER_ENABLED`.
   - **Two-pass doc verification** (required — docs reference env vars/commands/paths): **ALL = 100%, no spot-checking/sampling.** Verify every env var name against `config.py`/compose, every command exists, every path is real.
7. **doc-updater** — changelog entry in `changelog/<date>.changelog.md` (include the Refinement regression items captured at the Refinement exit gate).
8. **Implementation commit** (main agent, specific paths only — NO `git add -A`): config.py, lifespan.py, tests, docker-compose.yml, .env.example, dev-up.sh, docs, CLAUDE.md, **and `PARALLEL-INSTANCE-PLAN.md`** (commit the plan doc with the change — it documents real infra and resolves the untracked-loss risk from the git caveat).
9. **doc-updater** — add commit hash to changelog.
10. **Commit changelog** (changelog file only).
11. **doc-updater** — mark this plan doc Finalize complete / Status: Complete.
12. **Commit tracking** — `PARALLEL-INSTANCE-PLAN.md` (specific path) with message `chore: mark parallel-instance plan Complete`.
13. Exit gate: `lessons-learned` skill, then `journal` skill.

### Finalize gate
- [ ] code-reviewer pre-test review done; all suggestions implemented.
- [ ] Full suite green via tester.
- [ ] Second review IF impl changed post-first-review.
- [ ] Operator docs written + two-pass verified (env vars/commands/paths cross-checked against source).
- [ ] CLAUDE.md port-map + `.env.example` updated.
- [ ] Changelog entry + commit hash.
- [ ] Commits use specific paths (never `git add -A`).
- [ ] lessons-learned + journal invoked.

---

## Risk Register

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Compose `container_name` change breaks internal service-name resolution | Med | Service names in YAML stay literal; only `container_name`/net/project namespaced. Volumes namespaced via project-name prefix (no explicit `name:`). Verify effective names unchanged when unset. |
| Docker-disable flag misses a wire point → boot error or socket access | Med | Investigation enumerated all 5 gate points + 3 auto-degrading consumers (ProbeSupervisor/ImageUpdate/LocalBuild, all `getattr(...,None)`-safe). New lifespan test asserts `docker_socket_client is None` and clean boot. |
| **(C1) Instance B RW-mounts A's live cron-apply IPC dir** | **High if unfixed** | The mount **source** is a literal host path in the YAML, NOT controlled by `HM_CRON_*` env vars. B2 parameterizes the source (`HM_CRON_IPC_SRC` etc.); B points at a B-private dir. Refinement verifies via `docker inspect`. **This was the plan's original blind spot — fixed.** |
| **(I3) cadvisor/vector mount host `docker.sock`/rootfs independent of the docker flag** | Med | The `HOMELAB_MONITOR_DOCKER_ENABLED` flag only governs the *monitor's* docker plugin. B2 profile-gates cadvisor+vector; B excludes the `host-collectors` profile. |
| Instance B accidentally disturbs instance A's prod data/cron | Low (after C1 fix) | Namespaced volumes, distinct DB, B-private mount sources, cron app-disabled. Refinement explicitly verifies A undisturbed. |
| (I1) B's `.env` not loaded → project name falls back to `homelab-monitor` → stomps A | Med | Bring-up runs from `deploy/compose` with `--env-file .env` and **asserts `docker compose config` prints `name: homelab-monitor-b`** before `up`. |
| (I2) UDM syslog `0.0.0.0:5514` mapping is unconditional; `vector` always starts → collides | Med | Cannot omit a single port line via interpolation. B sets `HM_UDM_SYSLOG_PORT=35514` + loopback bind; or omits vector via profile (I3). |
| Finalize review changes invalidate the two-instance validation | Med | Run code-review BEFORE the live bring-up; if changes land after, re-pull/rebuild B + re-run affected steps + fresh user sign-off (Refinement step 9). |
| The vuln-fix branch switch discards this untracked plan doc | Med | Doc is self-contained; re-create from this content. Start Build only when cleanly on `main`. Plan doc is committed in Finalize (resolves the loss risk going forward). |

---

## Investigation Summary (provenance)

Three read-only investigation passes established:

- **Conflict surfaces (7 categories):** ports (mostly env-driven), container names (hard-coded literals), network name (hard-coded), volumes (no explicit `name:` → namespaced by project prefix), DB path (env-driven), **bind-mount host paths — cron IPC (RW) / crontab-snapshot / `/etc` / `/proc` are hard-coded SOURCES in the YAML and are a real collision/data-mixing hazard for two RW stacks, NOT a safe no-op** (corrected in review — parameterized in B2), `cadvisor`/`vector` mount docker.sock/rootfs independent of the monitor docker flag, other singletons (master key is env-var-only here — no host-path risk; docker socket; host fixer user + systemd units = instance-A-only).
- **Host-integration coupling:** app degrades gracefully when cron snapshot dir / `/host/etc` / `/host/proc` are absent (`is_dir()` guards + psutil fallback); cron-apply raises a clean `CronApplyUnavailableError` → 503 only when invoked; app is **completely unaware of systemd** (talks only to the IPC dir). Docker plugin is the one hard blocker → solved by disable flag.
- **Test isolation:** `make verify`/`test-fast`/`test-nocov` use tmpdir SQLite + mocked nonexistent docker socket; integration uses `homelab-monitor-test` project + port 19090; Playwright spawns its own Vite server; CRG `.code-review-graph/` is per-checkout. **No isolation work needed.**

Key file references:
- Docker gate points: `kernel/api/lifespan.py` ~423–466, ~860–869, ~1084–1090, ~1100–1184, ~1187–1224.
- Config pattern to mirror: `kernel/config.py` `HaRegistryConfig` / `load_ha_registry_config()` ~1643–1707; env convention `HOMELAB_MONITOR_*_ENABLED`.
- Cron graceful guards: `plugins/discoverers/cron_discoverer.py:231` (`is_dir()`), `kernel/metrics/host_boot_time.py:26–46` (psutil fallback), `kernel/cron/cron_apply_ipc.py:184` (clean 503).
- Compose identity: `deploy/compose/docker-compose.yml` (`name:` line, `container_name:` ×13, network ~527, volumes ~531–541).
- Dev base path: `scripts/dev-up.sh` (`/tmp/hm-dev` literals).
- Test isolation: `apps/monitor/tests/conftest.py` (tmpdir DB, mocked socket), `deploy/compose/docker-compose.test.yml` (project `homelab-monitor-test`, port 19090).
