"""Unified search across registered sources with routing for opt-in adapters.

Usage from the agent or run_request.sh:

    python3 search.py "pride and prejudice"            # default sources only
    python3 search.py "annas: machine learning textbook"  # + Anna's Archive
    python3 search.py "/Users/me/book.epub"            # local file ingest

The default source set excludes Anna's Archive (copyright safety for
auto-suggest). The user opts in with the `annas:` prefix.

When run as a script, prints candidates as JSON to stdout (one object per
line) AND notifies LINE via `openclaw message send` so the user can pick
from their phone.
"""
from __future__ import annotations
import json
import os
import sys
import subprocess
from typing import Optional

import config
from sources import Candidate, get_adapter


def route_query(query: str) -> tuple[list[str], str]:
    """Decide which adapters to query and return the cleaned query string."""
    q = query.strip()
    if q.lower().startswith("annas:"):
        return ["gutenberg", "standard_ebooks", "annas_archive"], q[len("annas:"):].strip()
    if q.startswith("/") or q.startswith("~"):
        return ["local"], q
    if q.lower().startswith("local:"):
        return ["local"], q[len("local:"):].strip()
    return list(config.DEFAULT_SOURCES), q


def search_all(query: str, sources: Optional[list[str]] = None,
               limit_per_source: int = 5) -> list[Candidate]:
    """Run each adapter's search(); merge and de-dup by (title, author)."""
    if sources is None:
        sources, query = route_query(query)
    out: list[Candidate] = []
    seen = set()
    for name in sources:
        try:
            mod = get_adapter(name)
            for c in mod.search(query, limit=limit_per_source):
                key = (c.title.lower()[:120], c.author.lower()[:80])
                if key in seen:
                    continue
                seen.add(key)
                out.append(c)
        except Exception as e:
            # Don't let one broken source kill the whole search.
            sys.stderr.write(f"[{name}] search failed: {e}\n")
    return out


def format_for_line(candidates: list[Candidate], query: str) -> str:
    if not candidates:
        return f"🔍 '{query}' — 검색 결과 없음"
    lines = [f"🔍 '{query}' 검색 결과 ({len(candidates)}건)"]
    lines.append("번호를 답장으로 보내면 번역을 시작합니다.")
    lines.append("")
    for i, c in enumerate(candidates[:config.MAX_CANDIDATES], 1):
        badge = {"gutenberg": "GB", "standard_ebooks": "SE",
                 "annas_archive": "AA", "local": "LOCAL"}.get(c.source, c.source)
        rights = f"[{c.rights}]" if c.rights and c.rights != "unknown" else ""
        lines.append(f"{i}. [{badge}] {c.title} / {c.author} {rights}")
    return "\n".join(lines)


def notify_line(message: str) -> bool:
    """Send a LINE message via OpenClaw. Returns True on success.

    launchd agents run with a minimal PATH that doesn't include /opt/homebrew/bin,
    where `node` (and thus `openclaw`) live. We inject the homebrew PATH so the
    `openclaw message send` call works from the watcher daemon too.
    """
    if not os.path.exists(config.OPENCLAW_BIN):
        sys.stderr.write(f"openclaw binary missing: {config.OPENCLAW_BIN}\n")
        return False
    import os as _os
    env = dict(_os.environ)
    # Prepend homebrew paths (idempotent — won't duplicate if already present).
    extra = "/opt/homebrew/bin:/usr/local/bin"
    env["PATH"] = extra + ":" + env.get("PATH", "")
    try:
        subprocess.run(
            [config.OPENCLAW_BIN, "message", "send",
             "--channel", "line", "--target", config.LINE_TARGET,
             "-m", message],
            check=True, timeout=30, env=env,
        )
        return True
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"LINE send failed: {e}\n")
        return False


def main():
    if len(sys.argv) < 2:
        print("usage: search.py '<query>' [--no-line]", file=sys.stderr)
        sys.exit(2)
    query = sys.argv[1]
    send_line = "--no-line" not in sys.argv

    candidates = search_all(query)
    # Emit JSONL to stdout (agent / orchestrator consumes this).
    for c in candidates:
        print(json.dumps(c.to_dict(), ensure_ascii=False))

    if send_line and candidates:
        notify_line(format_for_line(candidates, query))


if __name__ == "__main__":
    main()
