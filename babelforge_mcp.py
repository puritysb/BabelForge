"""BabelForge MCP server — the appliance's typed, agent-facing interface.

This is the single front door for agents (Claude Code, Codex, the OpenClaw
gateway). Instead of teaching each agent to shell out to search.py / start.sh
and parse JSONL, it exposes five tools over MCP:

    search_books(query)      → candidate list  (pick one)
    translate_book(candidate)→ req_id, returns immediately (runs detached)
    retry_request(req_id)    → same req_id, resumes failed work
    get_status(req_id?)      → progress / final state
    list_recent(limit)       → recent requests

Long translations run *detached* (a separate `pipeline.py` process with its own
session), so they survive this server restarting or a client disconnecting —
exactly matching the existing catalog.json state machine. The tools are a thin
typed skin over the same functions the CLI uses; no pipeline logic lives here.

Run:
    ./venv/bin/python3 babelforge_mcp.py            # stdio (dev; client spawns it)
    ./venv/bin/python3 babelforge_mcp.py --http     # streamable-HTTP appliance
                                                    # (launchd: com.local.babelforge-mcp)
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile

from mcp.server.fastmcp import FastMCP

import config
import catalog
import search as search_mod

mcp = FastMCP("babelforge")


def _candidate_to_dict(c) -> dict:
    return dataclasses.asdict(c) if dataclasses.is_dataclass(c) else dict(c)


def _spawn_pipeline(candidate: dict, req_id: str, notify: bool) -> dict:
    """Persist input and launch one detached pipeline process for ``req_id``."""
    config.ensure_dirs()
    fd, cand_path = tempfile.mkstemp(prefix=f"cand_{req_id}_", suffix=".json",
                                     dir=config.CACHE_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(candidate, f, ensure_ascii=False)

    os.makedirs(config.LOGS_DIR, exist_ok=True)
    log_path = os.path.join(config.LOGS_DIR, f"pipeline.{req_id}.log")
    argv = [config.VENV_PYTHON, "pipeline.py", cand_path, req_id]
    if not notify:
        argv.append("--no-notify")
    with open(log_path, "a", encoding="utf-8") as logf:
        process = subprocess.Popen(
            argv, cwd=config.BASE_DIR, env=os.environ.copy(),
            stdout=logf, stderr=logf, start_new_session=True,
            close_fds=True,
        )
    catalog.update(req_id, selected=candidate, worker_pid=process.pid,
                   error=None)
    return {
        "req_id": req_id,
        "status": "translating",
        "title": candidate.get("title", "") or "(untitled)",
        "log": log_path,
        "worker_pid": process.pid,
        "note": "Running in background — poll get_status(req_id).",
    }


@mcp.tool()
def search_books(query: str, limit_per_source: int = 5) -> list[dict]:
    """Search public-domain catalogs for a book and return candidate editions.

    Default sources are Project Gutenberg + Standard Ebooks. Prefix the query
    with 'annas:' to opt into Anna's Archive (copyright is the user's
    responsibility), or pass a local file path to ingest a file you own.

    Returns a list of candidate dicts including source language. Show them to
    the user and let them pick one. The mandatory precision preflight may
    autonomously replace a relay translation with a verified public-domain
    original-language edition before translating.
    """
    cands = search_mod.search_all(query, limit_per_source=limit_per_source)
    return [_candidate_to_dict(c) for c in cands]


@mcp.tool()
def translate_book(candidate: dict, notify: bool = True) -> dict:
    """Start translating + publishing a candidate returned by search_books().

    Returns IMMEDIATELY with a req_id; the full precision pipeline (source and
    original-edition research → concept-sheet web grounding + adversarial
    cross-check → semantic translation/proofread → fail-closed whole-book audit
    → EPUB → Calibre/OPDS → device push) runs detached. Poll get_status(req_id)
    for progress. Translation is checkpointed against both source text and the
    precision profile, so stale terminology decisions are never reused.
    """
    title = candidate.get("title", "") or "(untitled)"
    req_id = catalog.new_request(title)
    return _spawn_pipeline(candidate, req_id, notify)


@mcp.tool()
def retry_request(req_id: str, notify: bool = True) -> dict:
    """Resume one failed translation using the same request and checkpoints.

    This is the approval-free recovery path for agents. It deliberately accepts
    only a terminal failed request so an agent cannot accidentally start two
    writers for the same checkpoint. Source resolution, concept sheets, and
    translated batches already persisted under ``req_id`` are reused.
    """
    record = catalog.get(req_id)
    if not record:
        return {"error": f"no request with id {req_id!r}"}
    if record.get("status") != "failed":
        return {
            "error": "only failed requests can be retried",
            "req_id": req_id,
            "status": record.get("status"),
        }
    candidate = record.get("selected")
    if not isinstance(candidate, dict):
        return {
            "error": "failed request has no persisted candidate",
            "req_id": req_id,
        }
    catalog.update(
        req_id,
        status="requested",
        error=None,
        progress=None,
        concept_sheet_path=None,
        concepts=None,
        precision_profile=None,
        quality_report=None,
        retry_count=int(record.get("retry_count") or 0) + 1,
    )
    result = _spawn_pipeline(candidate, req_id, notify)
    result["status"] = "retrying"
    return result


@mcp.tool()
def get_status(req_id: str | None = None) -> dict:
    """Get the status/progress of a translation request.

    Pass a req_id from translate_book(); omit it to get the most recent request.
    Returns the catalog record (status, progress %, epub_path/opds_url when
    published, error when failed).
    """
    if req_id:
        rec = catalog.get(req_id)
        return rec or {"error": f"no request with id {req_id!r}"}
    recent = catalog.recent(1)
    return recent[0] if recent else {"error": "no requests yet"}


@mcp.tool()
def list_recent(limit: int = 5) -> list[dict]:
    """List recent translation requests and their status (newest first)."""
    return catalog.recent(limit)


def main() -> None:
    if "--http" in sys.argv:
        mcp.settings.host = config.MCP_HOST
        mcp.settings.port = config.MCP_PORT
        sys.stderr.write(
            f"[babelforge-mcp] streamable-http on "
            f"http://{config.MCP_HOST}:{config.MCP_PORT}/mcp\n")
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
