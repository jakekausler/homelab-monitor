#!/usr/bin/env bash
set -euo pipefail

# Drain stdin (the runner writes a config + secrets JSON blob and closes the
# pipe). We do not need the contents here, but reading prevents SIGPIPE if
# the plugin is downstream of an unread stdin.
cat >/dev/null

echo '{"type":"metric","name":"homelab_hello_world","kind":"counter","value":1,"labels":{"language":"bash"}}'
echo '{"type":"result","ok":true}'
