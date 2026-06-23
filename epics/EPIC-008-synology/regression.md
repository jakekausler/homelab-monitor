# Regression Checklist - EPIC-008: Synology

(Items added per stage during Refinement.)

## STAGE-008-001 — Synology DSM v7 client

- [ ] **Live auth + system info:** The real `SynologyRestClient` (base_url `https://192.168.2.4:5001`, account `homelab-monitor`, secret `synology_dsm_password`) authenticates against the live DSM (`SYNO.API.Auth` v7 login, **NO `session=` param**) and `system_info()` returns a `SynologyResponse` with real data (model `DS3622xs+`, firmware string, uptime, sys_temp) + a `took_seconds` float. (Sending a `session=` param returns DSM error 402 — do NOT add it.)
- [ ] **Session reuse:** A second client call reuses the `_sid` without re-login (only ONE `SYNO.API.Auth method=login` across two calls).
- [ ] **Logout frees the session:** `aclose()` issues `SYNO.API.Auth method=logout` and never raises even if the NAS is unreachable at shutdown.
- [ ] **119 re-auth:** A DSM body error code 119 (session expired, on HTTP 200) triggers exactly ONE re-auth + retry; a second 119 surfaces `SynologyError(reason="auth", status=119)`.
- [ ] **Self-signed TLS:** the dedicated Synology httpx client uses `verify=False` (DSM cert `CN=synology`).
- [ ] **DSM password never logged:** no Synology error message or log line ever contains the password or the `_sid`.
- [ ] **Cron fixtures present:** `apps/monitor/tests/data/cron_fixtures/{system_cron,user_crontab,reboot_only}.example` exist + are git-tracked (the `!apps/monitor/tests/data/` `.gitignore` exception) so the 3 cron-parser tests pass on a clean checkout. (Pre-existing failure fixed in this stage.)

## STAGE-008-002 — Synology integration bundle skeleton

- [ ] **Bundle registers:** `register_all` (called from lifespan after the Unifi registration) registers `synology_placeholder` via `loader.load_all()` with config name `synology_placeholder`, interval 60s, timeout 5s, concurrency_group `default` (NOT `synology`).
- [ ] **Defensive isolation:** a raising `loader.register` for one collector is logged (`synology_integration.collector_register_failed`) and does NOT propagate (other collectors still register).
- [ ] **Placeholder emits:** `SynologyPlaceholderCollector.run()` returns ok=True, metrics_emitted==1, emits `homelab_synology_bundle_loaded=1.0`, and never touches `ctx.synology`.
- [ ] **Placeholder is scaffolding:** `placeholder.py` + the `_SYNOLOGY_COLLECTORS` entry + `test_synology_placeholder_collector.py` are REMOVED in STAGE-008-005 (the first Wave-B collector). After 008-005, `homelab_synology_bundle_loaded` no longer emits.
- [ ] **No `_shared.py` yet:** `integrations/synology/_shared.py` is created by STAGE-008-003 (not 008-002).

## STAGE-008-003: Synology `_shared.py` helpers + cardinality-cap hook

- [ ] **`_shared.py` helper unit suite green:** `make uv ARGS="--directory apps/monitor pytest tests/test_synology_shared.py"` passes (currently 14+ tests, 100% branch coverage of `_shared.py`). Covers `client_unconfigured_result`, `failed_result`, `fetch_or_result` (success + error branches), `as_float` (all branches incl. bool/None/non-finite rejection), `bytes_field`/`percent_field`, `cap_for_synology`, `capped_emitter`.
- [ ] **API-latency metric name is stable:** `_shared.M_API_TOOK_SECONDS == "homelab_synology_api_took_seconds"`; `fetch_or_result` emits it with a single `{api=<endpoint>}` label on every successful fetch. Any Synology collector relying on this gauge must keep the name + label key in sync.
- [ ] **`ok=True`-when-NAS-sad convention preserved:** `fetch_or_result` returns the `SynologyResponse` (NOT a failed `CollectorResult`) whenever the client returns a `SynologyResponse`, regardless of degraded payload fields (e.g. volume status "has_unverified_disk"). Only a `SynologyError` produces `ok=False`. Collectors own the per-field state decision.
- [ ] **Cardinality cap is the default 500 (guardrail, not active limit):** `cap_for_synology("homelab_synology_<any>")` returns 500 (no `homelab_synology_*` entries in `_DEFAULT_CARDINALITY_FAMILIES` — intentional). `capped_emitter(...).emit_family(...)` always writes one `homelab_metric_family_dropped_series` gauge and appends a `SuggestionEvent(severity="warning")` only when series exceed the cap. Verified: 8 obs → 0 dropped; 600 obs → 100 dropped + 1 warning event.
- [ ] **3b live exercise owed by STAGE-008-005:** `_shared.py` helpers are first exercised against the LIVE DSM in STAGE-008-005 (storage collector). Confirm there.
- [ ] **Collector-author contract notes (for STAGE-008-005+):** use `emitted: list[int] = [0]` and accumulate across `fetch_or_result` calls (mutable-box is intentional); call `capped_emitter` once per `run()` tick with the same `events` list returned in the `CollectorResult`; count the always-written drop gauge toward `metrics_emitted` (e.g. `emit_family` return + 1 per family). `bytes_field`/`percent_field` are thin `as_float` aliases — the extension points if a field later needs unit conversion.

## STAGE-008-004: SSH `homelab-probe` dedicated-user target config (EPIC-017 framework)

- [ ] **ssh-probe CLI dedicated-user tests green:** `make uv ARGS="--directory apps/monitor pytest tests/test_cli_ssh_probe.py"` passes incl. `test_install_instructions_dedicated_user_with_script_id`, `test_test_dedicated_user_restriction_enforced`, `test_test_dedicated_user_restriction_broken`.
- [ ] **install-instructions renders the hardened forced-command line:** for a `dedicated-user` target with `user: homelab-probe`, `hm ssh-probe install-instructions <target>` renders `command="/home/homelab-probe/hm-probe.sh",no-port-forwarding,no-pty,no-X11-forwarding,no-agent-forwarding <pubkey> hm-probe-<target-id>` and an ADVISORY (skippable) sudoers step. NO private key is ever printed.
- [ ] **Docs subsection accurate:** the `docs/ssh-probe-setup.md` Synology subsection's CLI commands, config fields, secret name `ssh_probe_key_synology`, script path `/home/homelab-probe/hm-probe.sh`, and no-sudoers claim stay consistent with `cli/ssh_probe.py` + `kernel/ssh/config.py` + EPIC-008 Amendment 3.
- [ ] **authorized_keys comment is target-id-derived, NOT script_id:** the rendered comment is `hm-probe-<target-id>` (`hm-probe-synology`); `script_id` (`synology_probe`) does NOT appear in install-instructions output (it's for runtime probe dispatch). Don't expect script_id in the setup recipe.
- [ ] **ssh-probe CLI needs a migrated DB + master key:** `keygen`/`install-instructions`/`test` call `build_secrets_repo()`; on a fresh instance the backend must have started once (auto-migrate) before the ssh-probe CLI secret store is usable. (Operator gotcha.)
- [ ] **LIVE provisioning + `homelab-probe` DSM user creation owed by STAGE-008-014 (MAIN instance A):** the real keygen→capture-hostkey→install→`test` against the live Synology, and creation of the low-priv `homelab-probe` DSM user, are deferred to STAGE-008-014 and MUST be done in the main instance A (NOT instance B). Confirm there.

## STAGE-008-005: Synology storage collector (volumes + disks) + placeholder removal

- [ ] **Storage collector unit suite green:** `make uv ARGS="--directory apps/monitor pytest tests/test_synology_storage_collector.py"` passes (13 tests, 100% branch coverage of `storage.py`). The `SynologyPlaceholderCollector` + its test are GONE; `_SYNOLOGY_COLLECTORS` registers `SynologyStorageCollector`; `test_synology_integration_bundle.py` asserts the storage collector registers (name `synology_storage`, 300s interval, 30s timeout).
- [ ] **Live 3b emits real metrics:** running `SynologyStorageCollector().run(ctx)` against the live NAS (secret `synology_dsm_password` in B's store) yields `ok=True`, ~92 metrics, 0 errors — per-volume `homelab_synology_volume_*` (used/total/used_percent/status×2/fs_type + writable/encrypted/locked/inode_full) + per-disk `homelab_synology_disk_*` (temp{disk,model}/smart_status/status/unc_count/remain_life/sb_days_left/size_bytes/slot) for 8 disks.
- [ ] **Real load_info key contract (do not regress):** parse reads `volumes[].id`/`.status`/`.space_status.status`/`.size.used`/`.size.total`/`.fs_type`/`.is_writable`/`.is_encrypted`/`.is_locked`/`.is_inode_full`; `disks[].id`/`.model`/`.temp`/`.smart_status`/`.status`/`.unc`/`.remain_life.value`/`.sb_days_left`/`.size_total`/`.slot_id`. used_percent is DERIVED (no field). `remain_life` is an OBJECT (`.value`, -1 on HDDs emitted literally). Booleans parsed via `_bool_to_gauge` (NOT as_float, which rejects bools).
- [ ] **volume_status is a state-set (2 obs live):** emits `homelab_synology_volume_status{volume,status}=1` for BOTH the top-level `status` and the nested `space_status.status`. Don't expect a single observation.
- [ ] **Cardinality cap guardrail:** all 17 families cap-routed; `homelab_metric_family_dropped_series{family}` emitted (0.0 live — 8 disks/1 vol far under the default-500 cap).
- [ ] **No pool/RAID metrics here:** `storagePools[]` is STAGE-008-006's slice; 005 emits only volumes + disks from the shared `load_info` response.

## STAGE-008-006 — Synology Pool & RAID collector (`pool.py`)

- [ ] **Run the live 3b exercise:** build a real `SynologyRestClient` against `https://192.168.2.4:5001` (account `homelab-monitor`, secret `synology_dsm_password` from B's store) and call `await SynologyPoolCollector().run(ctx)` with a `MemoryRetainingMetricsWriter`. Expect `ok=True`, ~34 metrics, zero errors.
- [ ] **Verify the `homelab_synology_pool_status` family emits ≥2 series** (top-level `status` + nested `space_status.status`), `homelab_synology_pool_unverified_disk=1.0` while the live pool is unverified, `homelab_synology_raid_status{raid="/dev/md2"}=1.0` with normal/designed disk count 8/8, and `homelab_synology_pool_progress_percent=-1.0` when idle.
- [ ] **Unit:** `make uv ARGS="--directory apps/monitor pytest tests/test_synology_pool_collector.py"` passes with 100% branch coverage on `pool.py`; `tests/test_synology_integration_bundle.py` asserts `SynologyPoolCollector` registers (name `synology_pool`, interval 300, timeout 30).

## STAGE-008-007 — Synology System collector (`system.py`) + navigator extraction to `_shared.py`

- [ ] **Run the live 3b exercise:** build a real `SynologyRestClient` against `https://192.168.2.4:5001` (account `homelab-monitor`, secret `synology_dsm_password` from B's store) and call `await SynologySystemCollector().run(ctx)`. Expect `ok=True`, ~17 metrics, zero errors, and exactly 3 `homelab_synology_api_took_seconds` entries (system_info + fanspeed + need_reboot).
- [ ] **Verify `homelab_synology_info` emits with labels {model="DS3622xs+", serial, firmware="DSM 7.3.2-86009", cpu_series="D-1531"}, `homelab_synology_system_uptime_seconds` is a sane positive number (parsed from the "HHH:MM:S" up_time string), `homelab_synology_sys_temp_celsius` ~50, `homelab_synology_sys_temp_warning=0`, `homelab_synology_fan_status{state="cool_fan"}=1.0` / `{state="all_disk_temp_fail"}=0.0` / `{state=<mode>}=1.0`, `homelab_synology_need_reboot=0`.
- [ ] **Multi-fetch degradation:** if a SUPPLEMENTARY fetch (fanspeed/need_reboot) errors, the collector must stay `ok=True` with the error recorded in `result.errors` and the system_info families still emitted; only a PRIMARY (system_info) failure yields `ok=False`.
- [ ] **Navigator-extraction regression guard:** `as_dict`/`as_list_of_dicts`/`nested`/`bool_to_gauge` live in `_shared.py` (NOT re-defined locally in storage.py/pool.py/system.py). `SynologyStorageCollector` and `SynologyPoolCollector` must still emit live (storage ~92 metrics/19 families, pool ~34 metrics/18 families) — a regression here means the extraction broke a call site.
- [ ] **Unit:** `make uv ARGS="--directory apps/monitor pytest tests/test_synology_system_collector.py"` passes; `test_synology_integration_bundle.py` asserts `SynologySystemCollector` registers (name `synology_system`, interval 60, timeout 30); storage + pool + shared collector tests all still pass at 100% branch.

## STAGE-008-008 — Synology Utilization collector (`utilization.py`)

- [ ] **Run the live 3b exercise:** build a real `SynologyRestClient` against `https://192.168.2.4:5001` (account `homelab-monitor`, secret `synology_dsm_password` from B's store — retrieve with `-e HOMELAB_MONITOR_REVEAL=1` on the docker exec) and call `await SynologyUtilizationCollector().run(ctx)`. Expect `ok=True`, ~110 metrics, zero errors, exactly 1 `homelab_synology_api_took_seconds` entry (single fetch).
- [ ] **Verify the UNIT conversions:** `homelab_synology_cpu_load1` is a FLOAT load-avg (≈0.x, NOT the raw ×100 DSM int); `homelab_synology_mem_total_bytes` is ~16e9 bytes (KB×1024, NOT ~16e6 KB); `homelab_synology_nfs_total_max_latency_seconds` is in seconds (ms×0.001).
- [ ] **Verify the AGGREGATE labels:** `homelab_synology_disk_read_bytes_per_second` has 9 series (8 drives sda..sdh + `{device="total"}`); `homelab_synology_net_rx_bytes_per_second` has `{iface="total"}` + per-NIC; `homelab_synology_vol_io_read_bytes_per_second` has `{volume="total"}` + `{volume="volume1"}` (display_name preferred over `dm-1`).
- [ ] **Scope:** smb/lun/swap-io are deliberately NOT emitted (YAGNI scope-out). All 28 data families (6 cpu + 6 mem + 5 disk + 2 net + 3 vol + 6 nfs) must emit.
- [ ] **Unit:** `make uv ARGS="--directory apps/monitor pytest tests/test_synology_utilization_collector.py"` passes at 100% branch coverage on `utilization.py`; `test_synology_integration_bundle.py` asserts `SynologyUtilizationCollector` registers (name `synology_utilization`, interval 60, timeout 30).
