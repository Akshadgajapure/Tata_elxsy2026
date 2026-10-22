#!/usr/bin/env bash
# Run from anywhere. Needs root because it enters network namespaces (ip netns exec).
# Binds to 127.0.0.1 only. Override: QOS_WEB_HOST / QOS_WEB_PORT.
set -euo pipefail
cd "$(dirname "$0")/.."
exec sudo -E env "PATH=$PATH" "${PYTHON:-python3}" -m web.app
