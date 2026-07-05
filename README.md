# BabelForge

Public-domain book → **bilingual (original + Korean) EPUB** → **Calibre OPDS** pipeline, with
best-effort push to an XTeink e-ink reader. Serves at **books.getlingo.store**.

Split out of the `OpenClaw` ops repo on 2026-07-05 as a standalone project. It was never
tracked in OpenClaw's git history — this repo's history starts fresh.

## Pipeline

```
request → search (Gutenberg / Standard Ebooks / Anna's / local)
        → fetch → extract chapters (BeautifulSoup)
        → build auto-glossary (recurring names/terms → canonical Korean,
                               web-search-grounded via Z.ai MCP)
        → GLM translation (paragraph-aligned, batched, 2-pass draft+proofread,
                            HTML-tag preserving, glossary + source-context aware)
        → assemble bilingual EPUB (cp-original / cp-translation markers)
        → publish: calibredb add → Calibre Content Server OPDS feed
        → best-effort HTTP push to the XTeink e-ink reader
        → LINE notify (via the openclaw CLI)
```

Orchestrator: `pipeline.py`. Config/paths: `config.py` (all paths derived from `__file__`, so
the tree is relocatable). Entry points: `run_request.sh` (full pipeline), `start.sh` (detached),
`search.py` (candidate search only).

## Agent interface (MCP)

BabelForge is a self-hosted appliance; agents drive it through the **`babelforge` MCP server**
(`babelforge_mcp.py`) rather than the CLI. It runs streamable-HTTP under launchd
(`com.local.babelforge-mcp`, `http://127.0.0.1:8770/mcp`) and exposes four tools:
`search_books`, `translate_book` (returns a `req_id`; the pipeline runs detached), `get_status`,
`list_recent`. Deploy: `cp deploy/com.local.babelforge-mcp.plist ~/Library/LaunchAgents/ &&
launchctl load -w ~/Library/LaunchAgents/com.local.babelforge-mcp.plist`. Register the URL in
your MCP client (repo `.mcp.json` for Claude Code; `openclaw mcp add`). The CLI below remains a
fallback.

## External dependencies (not vendored here)

BabelForge is a self-contained Python project, but it leans on four things outside it:

| Dependency | What for | Coupling |
|---|---|---|
| **`crosspoint-agentdeck`** repo (sibling checkout at `~/github/crosspoint-agentdeck`) | The **bilingual-EPUB format is defined there** — `docs/bilingual-epub.md` is the SSOT; `assemble.py`'s skeleton derives from its `scripts/generate_bilingual_test_epub.py`, and `build_font.py` reads its `lib/EpdFont/scripts`. | Producer↔consumer format contract. The reader firmware is the consumer; this pipeline is the producer. Changes are breaking on both sides — see that repo's `docs/bilingual-epub.md`. |
| **`openclaw` CLI** (`/opt/homebrew/bin/openclaw`) | LINE notifications (`search.py`, `config.py:OPENCLAW_BIN`). | A single `openclaw message send` subprocess call. No code dependency. |
| **Calibre Content Server** (launchd `com.local.calibre-server`, port 8080) | Hosts the OPDS feed at `books.getlingo.store/opds` (cloudflared → localhost:8080). `publish.py` only calls `calibredb add`. | External service. |
| **ZAI / GLM API** (`api.z.ai`) | Translation engine. Key from `.env` (`ZAI_API_KEY` / `GLM_API_KEY`) — never committed. | External API. |

## Automation

`auto_push_watcher.py` (retry queue for device pushes) runs under the LaunchAgent
**`com.local.book-translator-watcher`** (`StartInterval` 20s, `--once` each fire). The plist
lives at `~/Library/LaunchAgents/com.local.book-translator-watcher.plist` and was repointed to
this path on the 2026-07-05 split. Logs: `logs/watcher.{out,err}.log`.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill ZAI_API_KEY
```

> **Note:** `venv/` was moved wholesale from the old location on the split, so its
> `bin/activate` and console-script shebangs still hold the old path. Invoking
> `venv/bin/python3` directly (as `run_request.sh` and the LaunchAgent do) works fine; if you
> need `activate` or the console scripts, recreate the venv from `requirements.txt`.

`venv/`, `.env`, `data/`, `logs/`, and `__pycache__/` are gitignored.

## Usage

The flow is two steps: **search** for a book, then **translate** the candidate you pick.

```bash
# 1. Search — prints candidates as JSONL, one book per line.
./venv/bin/python3 search.py "pride and prejudice" --no-line
./venv/bin/python3 search.py "annas: dostoevsky" --no-line   # opt-in Anna's Archive

# 2. Save the ONE candidate line you want to a file, then run the pipeline.
#    start.sh runs it detached (returns immediately; log under logs/).
echo '<paste the chosen candidate JSON line here>' > candidate.json
export ZAI_API_KEY=<your GLM Coding Plan key>   # or put GLM_API_KEY=... in .env
./start.sh candidate.json

#    …or run in the foreground (blocks until done, streams logs to the terminal):
./run_request.sh candidate.json
```

The pipeline runs fetch → extract → auto-glossary → translate → assemble → publish → device push,
updating `data/catalog.json` at each step. A novel takes ~1–2h (8 concurrent GLM workers);
translation is checkpointed, so a killed run resumes where it left off on rerun.

```bash
# Progress: last few requests and their status.
./venv/bin/python3 -c "import catalog; print(catalog.recent(5))"

# Live log of the most recent detached run.
tail -f logs/pipeline.*.log
```

On success the bilingual EPUB is added to Calibre and served at the OPDS feed
(`books.getlingo.store/opds`); the reader can also pull it, or it is pushed to the device
directly if reachable. The full agent runbook lives in
`.agents/skills/book-translator/SKILL.md`.
