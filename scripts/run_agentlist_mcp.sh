#!/usr/bin/env bash
set -euo pipefail

# Run from any directory when the repository is checked out locally. The app
# remains the only holder of wallet keys; this process only speaks MCP to it.
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
exec python -m agentslist_mcp "$@"
