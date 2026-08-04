# BabelForge

Public-domain book → **bilingual (original + Korean) EPUB** → **Calibre OPDS** pipeline, with
best-effort push to an XTeink X3/X4 e-ink reader. Served at **books.foundby.kr**.

A standalone Python project, split out of the `OpenClaw` ops repo on 2026-07-05 (it was never
tracked there — this repo's history starts fresh). Read this file first; the user-facing
overview is `README.md`, and the agent runbook is `.agents/skills/book-translator/SKILL.md`.

## Architecture

Single-process Python pipeline, no framework. Every path derives from `__file__`, so the tree
is **relocatable** (no hardcoded absolute paths — keep it that way).

```
request → search (Gutenberg / Standard Ebooks / Anna's / local)
        → mandatory source research (autonomously prefer a verified public-domain
                                     original-language edition over relay translation)
        → fetch source → extract chapters (BeautifulSoup / ebooklib)
        → structured concept sheet (whole-book candidates → web grounding
                                    → adjudication → adversarial cross-check
                                    → enforcement-schema normalization)
        → GLM precision translation (paragraph-aligned, char-budget-capped batches,
                                     concurrent workers, semantic proofread,
                                     HTML-tag + concept distinctions preserved)
        → fail-closed whole-book terminology audit + contextual repair
        → assemble bilingual EPUB (cp-original / cp-translation block-level <p> markers)
        → publish: calibredb add → Calibre Content Server OPDS feed
        → best-effort HTTP push to the XTeink reader (skipped silently if unreachable)
        → LINE notify (via the openclaw CLI)
```

### Key files (SSOT per concern)

| File | Owns |
|---|---|
| `pipeline.py` | Orchestrator — `run()` drives fetch→extract→translate→assemble→publish→push, updating `data/catalog.json`. |
| `config.py` | **All config + paths.** `BASE_DIR` from `__file__`; `DATA_DIR`/`LIBRARY_DIR`/`CACHE_DIR`/`CHECKPOINT_DIR`/`LOGS_DIR`; ZAI/GLM, Calibre, OPDS, device-push settings. |
| `search.py` | Catalog search → JSONL candidates; `openclaw message send` LINE notify. |
| `sources/` | Source adapters (`gutenberg`, `standard_ebooks`, `annas_archive`, `local_file`), dispatched via `sources/__init__.py::get_adapter()`. |
| `extract.py` | Chapter/paragraph extraction. Plain-text Gutenberg sources recover title-case multilingual chapter headings from an explicit indented TOC; headings stay as bilingual units so structural improvements can reuse aligned checkpoints. |
| `translate.py` | **GLM translation engine** — the biggest file. System prompt is **inline** (literary-translator). Batch output is aligned by **numbered `[k]` markers** (`_parse_numbered_slots` — GLM keeps an explicit numbered list far more reliably than a lone `⟦P⟧` delimiter); missing slots are back-filled by single-paragraph calls, so a partial/collapsed batch keeps its good paragraphs. Plus batching, backoff, checkpoint resume, 2-pass. Also: **inline-HTML tag stashing** (`_stash_tags`/`_restore_tags` swap `<i>/<b>/<em>/…` for PUA placeholders so GLM can't mangle them), **glossary** enforcement in the system prompt, and **source-side context** (each batch gets the preceding 3 source paragraphs — parallel-safe, keeps tone/terminology steady across concurrent workers, `config.TRANSLATE_WORKERS`). |
| `precision.py` | **Mandatory precision preflight** — researches original language/title, autonomously replaces a relay catalog source when a verified public-domain original is available, extracts lowercase/multiword and rare lexical-neighbor concepts across the book, gathers web evidence, then runs adjudicator + adversarial critic + enforcement-schema normalization passes. Malformed JSON is repaired autonomously. Writes a structured, request-scoped concept sheet under `data/concepts/`. |
| `quality_audit.py` | **Fail-closed pre-publication audit** — first independently reviews every final pair for semantic errors, omissions, untranslated quotations, and malformed Korean, then checks every strict source concept occurrence for the canonical Korean term and forbidden variants. It repairs issues through GLM and rejects the book if any remain. A forbidden Korean string may be retained only through an explicit paragraph/concept-scoped contextual review when it translates a different source lemma. A matched spelling may skip the canonical term only when an exact, longer source expression proves a non-conceptual idiom (for example `in Wahrheit` = “실은”); an exact standalone concept term is never exemptible. |
| `glossary_builder.py` | Legacy standalone flat-glossary utility. The production pipeline no longer uses this best-effort path; `precision.py` owns mandatory structured terminology research. |
| `mcp_client.py` | Minimal **self-contained MCP (Streamable-HTTP) client** — no Node dependency. Speaks initialize → notifications/initialized → tools/call over urllib against Z.ai MCP servers, authed with the Coding-Plan key. Exposes `web_search(query)`; fails soft (returns `[]`, never raises) so a search hiccup never blocks translation. |
| `assemble.py` | Builds the bilingual EPUB. Emits **block-level `<p class="cp-original">` / `<p class="cp-translation">` only** — never `<span>` (firmware parser contract). |
| `publish.py` | `calibredb add` into `~/Calibre-Library`. |
| `device_push.py` | HTTP POST push to the reader (mDNS/subnet discovery). |
| `auto_push_watcher.py` + `pending_queue.py` | launchd-driven retry queue for device pushes. |
| `catalog.py` | `data/catalog.json` state machine (requested → researching → selected → analyzing → translating → quality_checked → assembling → published/failed). |
| `babelforge_mcp.py` | **Agent-facing MCP server** (FastMCP) — the appliance's front door. Tools: `search_books` / `translate_book` (detached spawn, returns `req_id` immediately) / `retry_request` (same-`req_id` checkpoint resume) / `get_status` / `list_recent`. A thin typed skin over the same functions the CLI uses. Runs streamable-HTTP under launchd `com.local.babelforge-mcp` (`http://127.0.0.1:8770/mcp`, `config.MCP_PORT`); `--http` for the appliance, stdio otherwise. Deploy plist: `deploy/com.local.babelforge-mcp.plist`. |

## Run

```bash
# search candidates (JSONL, one per line)
./venv/bin/python3 search.py "<query>" --no-line
./venv/bin/python3 search.py "annas: <query>" --no-line   # opt-in Anna's Archive

# translate + publish a chosen candidate (save one candidate line to a file)
export ZAI_API_KEY=<GLM Coding Plan key>
./start.sh <candidate.json>          # detached; log → logs/pipeline.<ts>.log

# progress
./venv/bin/python3 -c "import catalog; print(catalog.recent(5))"
```

Full agent runbook (search → pick → translate → deliver): **`.agents/skills/book-translator/SKILL.md`**.

## External dependencies (contracts crossing repo boundaries)

BabelForge is self-contained Python, but leans on four things outside it:

| Dependency | For | Contract |
|---|---|---|
| **`crosspoint-agentdeck`** (sibling checkout `~/github/crosspoint-agentdeck`) | The **bilingual-EPUB format** is defined there — `docs/bilingual-epub.md` is the **SSOT**. `assemble.py`'s skeleton derives from its `scripts/generate_bilingual_test_epub.py`; `build_font.py` reads its `lib/EpdFont/scripts`. | Producer↔consumer. This pipeline is the **producer**; the reader firmware is the consumer. A format change is breaking on both sides — coordinate via that doc. |
| **`openclaw` CLI** (`/opt/homebrew/bin/openclaw`) | LINE notifications (`search.py`, `config.py:OPENCLAW_BIN`). | One `openclaw message send` subprocess call — no code dependency. |
| **Calibre Content Server** (launchd `com.local.calibre-server`, port 8080; cloudflared `com.getlingo.openclaw.cloudflared`) | Hosts the OPDS feed at `books.foundby.kr/opds`. `publish.py` only calls `calibredb add`. The older `books.getlingo.store` route remains as a compatibility alias, but **that domain is due to expire** — generated BabelForge links use `books.foundby.kr`. | External service (KeepAlive launchd). |
| **ZAI / GLM API** (`api.z.ai`, model `glm-5.2`) | Translation engine. Key from `.env` (`ZAI_API_KEY` or `GLM_API_KEY`) — **never hardcode**. | External API. |
| **Z.ai MCP** (`api.z.ai/api/mcp/web_search_prime`, tool `web_search_prime`) | Grounds source provenance and concept decisions in external evidence (`precision.py`, `mcp_client.py`). Same Coding-Plan key; MCP is entitled on it independently of REST. | External MCP (Streamable HTTP). Individual searches fail soft; source-context adjudication and an adversarial cross-check remain mandatory. |

## Conventions & gotchas

- **Secrets:** `.env` holds `GLM_API_KEY` (and optional device host). It is gitignored — never commit it, never print its values. `config.py::_load_dotenv()` populates `os.environ` from this repo's `.env` on import (via `setdefault`, so a real env var wins); the translator then reads the key as `ZAI_API_KEY` or the `GLM_API_KEY` fallback. No key is stored in code.
- **Relocatable:** all paths come from `BASE_DIR = dirname(abspath(__file__))`. Do not introduce hardcoded `~/github/...` self-paths (references to the *sibling* crosspoint repo are the one allowed home-relative exception).
- **venv:** was moved wholesale on the split, so `venv/bin/activate` and console-script shebangs hold the stale path. Invoking `venv/bin/python3` **directly** (as `run_request.sh` and the LaunchAgent do) works. If you need `activate`/console scripts, recreate the venv from `requirements.txt`.
- **launchd:** `auto_push_watcher.py` runs under **`com.local.book-translator-watcher`** (`~/Library/LaunchAgents/…`, `StartInterval` 20s, `--once`). If you move/rename this dir, repoint that plist (6 path occurrences). Logs: `logs/watcher.{out,err}.log`.
- **EPUB format is a contract:** only block-level `<p>` `cp-original`/`cp-translation`; single-file bilingual (ESP32-C3 can't hold two EPUBs in RAM). Don't emit `<span>` markers.
- **Anna's Archive** is opt-in only via the `annas:` prefix; default search never touches it (copyright = user's responsibility).
- **Checkpoint resume never regresses:** a re-queued batch window can mix already-translated paragraphs with blank ones (windows are recomputed each run and can shift, e.g. after a batching change), so if that batch ultimately fails, `_commit_batch` in `translate.py` must never overwrite an already non-blank slot with a blank result. This bit us live — a resumed run's rate-limit failures wiped ~90 previously-good translations before the guard was added. Preserve that invariant if you touch `_commit_batch`.
- **Structural checkpoint remap is exact and audited:** when a better extractor replaces `Untitled` chunks with real chapters, translations may be remapped only by exact in-order source strings at ≥95% coverage. The current semantic and terminology audits always run afterward.
- **Precision is mandatory:** there is no fast/best-effort production mode. Concept research failures and unresolved strict-term audit failures stop publication. Checkpoints are keyed by both source hash and precision-profile hash, so terminology/prompt changes intentionally restart stale work.
- **Strict rules are sense-aware but fail closed:** `avoid_ko` is concept-scoped, not paragraph-global, so `Erkenntniss→인식` may coexist with `Wissen→지식`. The auditor records exact paragraph/concept/string exemptions. A canonical term may be absent only for an independently reviewed, verbatim multiword idiom containing the matched spelling; standalone concept occurrences still fail closed.
- **Structured JSON uses the provider contract:** precision research and the final audit call Z.ai with `response_format=json_object`. Business error `1213` is a request-parameter error (not a length/rate-limit error); never blindly replay the same invalid body. Empty/malformed responses are repaired only with non-empty wrapped input, then regenerated from the original evidence payload.
- **Gitignored:** `venv/`, `.env`, `data/`, `logs/`, `__pycache__/` (see `.gitignore`).

## Multi-agent harness

This repo is worked with Claude Code / Codex / OpenClaw. The book-translator procedure is a
**skill**: canonical copy at `.agents/skills/book-translator/SKILL.md` (Codex auto-discovers
`.agents/skills/`; Claude Code reaches it via the pointer in `.claude/skills/`). The OpenClaw
gateway agent discovers a thin pointer under `~/.openclaw/workspace/skills/book-translator/`
that routes back to this repo's SSOT. **Don't fork the procedure into multiple copies** — edit
the `.agents/skills/` original.
