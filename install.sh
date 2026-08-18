#!/usr/bin/env bash
# governor - one-shot setup. Registers the plugin, wires the status line,
# optionally sets your cap. Restart Claude Code afterwards.
#
#   ./install.sh --dry-run        show the edit
#   ./install.sh --cap 50         install and cap yourself at 50%
#   ./install.sh --uninstall      undo it
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${1:-}" = "--uninstall" ]; then shift; exec python3 "$here/bin/governor" uninstall "$@"; fi
exec python3 "$here/bin/governor" install "$@"
