#! /bin/bash
# OSS ATEN iKVM viewer — opens server picker from servers.toml (no --host).
# Console profile lives in [defaults] of servers.toml; CLI flags still override.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m aten_kvm "$@"
