# Regression Checklist - EPIC-005A: Alert backfill for EPICs 001-004

(Items added per stage during Refinement.)

## STAGE-005A-001 — Host machine threshold + anomaly rules

- `deploy/vmalert/metrics/host_anomalies.yaml` loads on the live vmalert with all 11 rules `health=ok` (check `docker exec homelab-vmalert-metrics sh -c "wget -qO- http://127.0.0.1:8880/api/v1/rules"`). No false-fire at a healthy baseline (check `/api/v1/alerts` — no `target_kind=host` alert firing/pending).
- Memory-ratio rules must use `/ ignoring(type)` — a bare `homelab_host_memory_bytes{type="used"} / homelab_host_memory_bytes{type="total"}` matches nothing and the rule silently never fires. Applies to HostCriticalMemory, HostMemoryPressure, HostMemoryAnomalous (host_anomalies.yaml) AND HostHighMemory (host.yaml). Regression check: query the division on the live VM and confirm it returns a value (not empty).
- `homelab_host_disk_io_bytes_total` / `homelab_host_net_bytes_total` must read SANE absolute values (kernel-truth ~TB/GB, not PB/EB) and `rate()` in MB/s (not TB/s). Regression check: `rate(homelab_host_disk_io_bytes_total[5m])` per-series should be plausible disk throughput. If inflated, the host collector regressed to passing psutil cumulative values through `write_counter` (.inc) instead of `write_counter_absolute` (Gauge set).
- Rule-test mirror `__tests__/host_anomalies.tests.yaml`: run `docker run --rm --entrypoint promtool -v $(pwd)/deploy/vmalert/metrics:/rules prom/prometheus:v2.47.0 test rules /rules/__tests__/host_anomalies.tests.yaml` — the 9 promtool-validatable rules must pass; the 2 MetricsQL-only rules (HostDiskIOAnomalous, HostNetThroughputAnomalous, which use a `rate(...)[w:s]` subquery) show `got:[]` under promtool by design (validated via live-MetricsQL replay instead). The load rules (HostHighLoad/HostCriticalLoad) use `scalar(count(...))` and ARE promtool-validatable.
