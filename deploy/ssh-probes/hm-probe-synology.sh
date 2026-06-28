#!/bin/sh
# hm-probe-synology.sh - Synology NAS forced-command probe for homelab-monitor.
#
# CANONICAL SOURCE: deploy/ssh-probes/hm-probe-synology.sh in the homelab-monitor repo.
# This repo file is the single source of truth. Operators deploy it to the NAS at
#   /usr/local/bin/hm-probe-synology.sh   (owner root:root, mode 0755)
# and wire it as the forced command in the homelab-probe user's authorized_keys.
# Do NOT hand-edit the copy on the NAS; edit THIS file and redeploy. The CLI command
# `hm ssh-probe install-instructions synology` emits this exact body plus deploy steps.
#
# Emits ===HM_*=== marker sections parsed by the SynologyProbe collector.
# NOTE: no `set -e` - synodisk requires root and will fail as the unprivileged
# homelab-probe user; failed sections are honest-empty and the script still
# reaches ===HM_END=== and exits 0.

echo '===HM_UPTIME==='
uptime

echo '===HM_DF==='
df -P

echo '===HM_SYNODISK_ENUM==='
/usr/syno/bin/synodisk --enum 2>/dev/null || true

for d in a b c d e f g h; do
  echo "===HM_SMART /dev/sd$d==="
  /usr/syno/bin/synodisk --smart_info_get /dev/sd$d 2>/dev/null || true
done

echo '===HM_MDSTAT==='
cat /proc/mdstat

echo '===HM_UPSC==='
/usr/bin/upsc ups 2>/dev/null || true

echo '===HM_HWMON==='
for h in /sys/class/hwmon/hwmon*; do
  [ -d "$h" ] || continue
  nm=$(cat "$h/name" 2>/dev/null)
  for f in "$h"/temp*_input "$h"/temp*_label; do
    [ -e "$f" ] && echo "$h $nm $(basename "$f")=$(cat "$f" 2>/dev/null)"
  done
done

echo '===HM_END==='
exit 0
