# pihole-restart-loop — runbook intent

Responds to the `PiholeCrashLoop` alert: the Pi-hole DNS resolver container
has been caught in a container-restart-loop failure mode. The desired
outcome is a single clean restart of the affected container to break the
loop, with full audit and human approval before any real action runs.

## Allowed actions

- `docker restart pihole-unbound` — the sole permitted docker action.
- Reading its logs (`docker logs pihole-unbound --tail 100`) for diagnosis
  before deciding whether to restart.

## Forbidden actions

- Touching any container other than `pihole-unbound`.
- Any docker action beyond `restart` (no `stop`, `start`, `recreate`, `rm`,
  `exec`, `pull`, etc.).
- Any host-level command outside docker (no editing config files, no
  restarting the host, etc.).
- Any network-level change.
