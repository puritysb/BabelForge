#!/usr/bin/env bash
# Background launcher for the book translator pipeline.
# Mirrors yt_dubber/start.sh — kicks off run_request.sh with nohup so the
# OpenClaw agent can return immediately and let the pipeline run detached.
set -euo pipefail

BT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$BT_DIR/logs/pipeline.$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$BT_DIR/logs"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <candidate.json>" >&2
  exit 2
fi

nohup "$BT_DIR/run_request.sh" "$@" >"$LOG" 2>&1 &
echo "started pipeline pid=$! log=$LOG"
