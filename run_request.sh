#!/usr/bin/env bash
# Bilingual book translator — full pipeline for one chosen candidate.
#
# Usage:
#   ./run_request.sh <candidate.json> [req_id]   # full pipeline + LINE notify
#   ./run_request.sh -                           # candidate JSON on stdin
#
# Reads a Candidate object (as emitted by search.py JSONL output) and runs
# fetch → extract → translate (GLM-5.2) → assemble (cp-* EPUB) → publish
# (calibredb add → Content Server OPDS).
#
# Requires: $ZAI_API_KEY exported in the environment.
set -euo pipefail

BT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$BT_DIR/venv/bin/python3"
export PYTHONPATH="$BT_DIR"
cd "$BT_DIR"

if [[ -z "${ZAI_API_KEY:-}" && -z "${GLM_API_KEY:-}" ]]; then
  echo "ERROR: neither ZAI_API_KEY nor GLM_API_KEY is set." >&2
  echo "  Put GLM_API_KEY=... in this repo's .env (preferred — config.py" >&2
  echo "  auto-loads it) or export ZAI_API_KEY/GLM_API_KEY in the environment." >&2
  exit 3
fi

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <candidate.json> [req_id]" >&2
  exit 2
fi

exec "$PYTHON" pipeline.py "$@"
