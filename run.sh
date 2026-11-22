#!/usr/bin/env bash
# Start the local web UI.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m libgen_service serve --open "$@"
