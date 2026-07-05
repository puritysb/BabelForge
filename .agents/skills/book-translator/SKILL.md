---
name: book-translator
description: Translate a requested book to Korean and deliver it to the Xteink X3/X4 OPDS browser. Use when the user says "OOO 책 번역해줘", "책 찾아줘", "bilingual book", "translate book to Korean", or references reading on the e-ink device. Searches public-domain catalogs (Project Gutenberg, Standard Ebooks), opt-in Anna's Archive (with `annas:` prefix), or local files; produces a bilingual EN/KO EPUB with cp-original/cp-translation markers; publishes it to the Calibre Content Server so the device's OPDS browser can find and download it.
metadata:
  openclaw:
    emoji: "📖"
---

# book-translator skill

A producer pipeline that turns a book request into a bilingual EPUB on the
Xteink X3/X4. Lives entirely off-device; the device only pulls the result via
its existing OPDS browser.

## What it does

```
request → search (Gutenberg / Standard Ebooks / Anna's / local)
        → fetch source text
        → extract chapters
        → GLM-5.2 paragraph-wise translation (batches, retries)
        → assemble cp-original/cp-translation bilingual EPUB
        → calibredb add → Content Server OPDS feed
        → best-effort HTTP POST push to device (if File Transfer on)
        → LINE notify (push result + OPDS pickup instructions)
```

The user opens the device's OPDS browser (or finds the file already pushed to
`/Books`), picks the new book titled "<Title> (KO bilingual)", opens it, and
long-presses Confirm to cycle Both → Original only → Translation only.

## Files

- Pipeline code: `~/github/BabelForge/`
- Config: `~/github/BabelForge/config.py`
- State machine: `data/catalog.json` (requested → searching → selected →
  translating → assembling → published / failed)
- Calibre library: `~/Calibre-Library/` (served by launchd
  `com.local.calibre-server` on port 8080)
- Public OPDS: `https://books.getlingo.store/opds` (cloudflared tunnel)

## How the agent drives it

**Preferred: the `babelforge` MCP server** (launchd `com.local.babelforge-mcp`,
`http://127.0.0.1:8770/mcp`). If your host has it, drive the appliance with typed
tools instead of shelling out — no paths, no JSONL parsing:

- `search_books(query)` → candidate list (prefix `annas:` to opt in; pass a file
  path for a local file). Show up to 8, let the user pick one.
- `translate_book(candidate)` → returns a `req_id` immediately; the pipeline runs
  in the background (~1-2h). Pass the exact candidate dict the user chose.
- `get_status(req_id)` → progress %, then `published` (epub_path/opds_url) or
  `failed` (error). A failed run is resumable — re-running continues from its
  checkpoint.
- `list_recent(n)` → recent requests.

The CLI below is the **fallback** for hosts without the MCP server (it calls the
same code).

### 1. Search (return candidates to the user)

```bash
cd ~/github/BabelForge
./venv/bin/python3 search.py "<query>" --no-line
```

Prints JSONL candidates (one per line). For Anna's Archive (opt-in,
copyright = user responsibility):

```bash
./venv/bin/python3 search.py "annas: <query>" --no-line
```

For a local file the user owns:

```bash
./venv/bin/python3 search.py "/path/to/book.epub" --no-line
```

Show the user up to 8 candidates and ask them to pick by number.

### 2. Translate + publish the chosen candidate

Take the chosen candidate JSON (one line from step 1), save it to a file, and:

```bash
export ZAI_API_KEY=<GLM Coding Plan key>   # required for GLM-5.2
cd ~/github/BabelForge
./start.sh <candidate.json>
```

`start.sh` runs `run_request.sh` detached (nohup); log lands in
`logs/pipeline.<timestamp>.log`. The pipeline:

1. fetches the source via the adapter named in `candidate.source`
2. extracts chapters (EPUB via ebooklib, txt via Gutenberg-header strip +
   chapter regex)
3. translates each chapter in 20-paragraph batches through GLM-5.2
4. assembles the bilingual EPUB (block-level `<p class="cp-original">` +
   `<p class="cp-translation">` pairs — the firmware contract)
5. `calibredb add` into `~/Calibre-Library/`, tagged
   `bilingual, korean, openclaw`
6. attempts an HTTP POST push of the EPUB to the device's File Transfer server
   (`http://crosspoint.local/upload?path=/Books`). If the device isn't in
   File Transfer mode, the push is silently skipped — the book is still
   available via OPDS pull. Push result is recorded in `catalog.json` under
   `device_push`.
7. sends a LINE completion notice reflecting the delivery path (pushed
   directly, or OPDS pickup instructions)

### 3. Report progress (if asked)

```bash
./venv/bin/python3 -c "import catalog; print(catalog.recent(5))"
```

## Environment requirements

- `ZAI_API_KEY` exported (GLM Coding Plan). Without it, translate.py fails
  fast with an actionable message.
- launchd agents `com.local.calibre-server` and `com.getlingo.openclaw.cloudflared`
  both running (they are KeepAlive=true).

## Device-side setup (one time, done by the user)

1. RIDIBatang font installed at `/SD/.fonts/RIDIBatang/` and selected under
   Settings → Reader → Font Family.
2. Settings → System → OPDS Servers → Add Server:
   - Name: `OpenClaw Library`
   - URL: `https://books.getlingo.store/opds`
   - Username/Password: (none — Content Server runs without auth on the tunnel;
     enable Basic auth later if exposing wider)
3. Settings → Controls → Long-press Menu → cycle to **Bilingual Toggle**
   (firmware must include the bilingual-toggle feature, merged in PR #1).

After a book finishes, the user opens the OPDS browser on the device, searches
"<Title> (KO bilingual)", downloads to SD, opens it, and long-presses Confirm
to switch views.

## Limits and guardrails

- Default auto-suggest never touches Anna's Archive; the `annas:` prefix is
  the only gate. Copyright is the user's responsibility.
- The firmware parser only honors block-level `<p>` cp-original/cp-translation
  classes; the assembler never emits `<span>` markers.
- ESP32-C3 can't hold two EPUBs in RAM — single-file bilingual only.
- 2.4 GHz Wi-Fi only on the device.
- No completion push to the device by design — the user pulls from OPDS.

## Format contract reference

See `~/github/crosspoint-agentdeck/docs/bilingual-epub.md` for the exact
marker rules the firmware parser (`lib/Epub/Epub/parsers/ChapterHtmlSlimParser.cpp`)
enforces, and `docs/bilingual-pipeline.md` for the end-to-end architecture.
