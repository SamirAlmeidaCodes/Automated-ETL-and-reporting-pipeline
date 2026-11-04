#!/usr/bin/env bash
# Check the pinned requirements for known vulnerabilities.
# Installs pip-audit into the active interpreter when it is missing.
# safety check -r requirements.txt is an equivalent second opinion.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if ! python -m pip_audit --version >/dev/null 2>&1; then
  python -m pip install --disable-pip-version-check "pip-audit==2.10.1"
fi

python -m pip_audit -r requirements.txt -r requirements-dev.txt
