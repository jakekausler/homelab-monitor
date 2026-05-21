# vmagent dynamic scrape targets

Files in this directory are picked up by vmagent via `file_sd_configs: ['/etc/vmagent/targets.d/*.yml']` (declared in `../scrape.yaml`).

## Contract

- **Format:** Prometheus `file_sd` YAML — a top-level YAML list whose entries are objects with `targets: [<host:port>...]` and `labels: {key: value, ...}` keys.
- **Writers:** the monitor process (STAGE-003-005 and later) renders targets here when label-based probe configuration discovers new containers.
- **Reader:** vmagent reloads automatically when files change (`-promscrape.configCheckInterval` default 5s).
- **Ownership:** files in this directory are owned by the writer (the monitor). DO NOT hand-edit; updates will be overwritten on next render.
- **Lifecycle:** the directory is empty in STAGE-003-001 (this stage) — only the static `cadvisor` + `monitor` jobs exist in `../scrape.yaml`. STAGE-003-005 begins writing files here.

## Filename convention

`<plugin>-<purpose>.yml` (e.g., `docker-probes.yml`, `cron-wrappers.yml`).
