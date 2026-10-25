---
name: book-translator
description: Find a public-domain book or use a local book and translate it into a portable original/Korean bilingual EPUB, with optional Calibre/OPDS publication. Use for book translation, bilingual book and e-reader delivery requests.
metadata:
  openclaw:
    emoji: "📖"
---

# book-translator

Use BabelForge to produce a standard EPUB readable in ordinary reading apps.
Read the repository's `CLAUDE.md` for implementation context. The file-format contract
is `docs/epub-format.md`; a firmware repository or special device is not required.

## Choose the source and destination

Use the `babelforge` MCP server when available. Otherwise run the repository CLI with
`./venv/bin/python3` directly; do not rely on historically moved venv activation scripts.

- `search_books(query)` searches Gutenberg and Standard Ebooks. Show a short candidate
  list when a title is ambiguous. If the user already selected a specific edition,
  use that choice without asking again.
- A filesystem path selects a local file. `annas:` is the only search opt-in for
  Anna's Archive; do not add it to normal searches.
- For an EPUB-file request use `translate_book(candidate, publish_output=False)`.
  For library/OPDS delivery use `publish_output=True`. The tool default remains True
  for compatibility with existing library workflows, so choose the flag explicitly.
- `notify=False` suppresses all LINE messages. Do not enable external notifications
  unless the user's request or established delivery workflow includes them.

The pipeline may replace a relay translation only when the original-language source
can be matched to the same work, author and volume with eligible rights metadata.
Uncertain matches retain the selected edition.

## Run and follow progress

`translate_book(candidate, publish_output=..., notify=...)` launches a detached worker
and returns `req_id`. Use `get_status(req_id)` to follow the same request; do not create
another request to inspect progress. `list_recent(n)` lists existing work.

CLI fallback, from the repository root:

```bash
./venv/bin/python3 search.py "<title or author>" --no-line
# Save the selected candidate object as candidate.json.
./run_request.sh candidate.json --no-publish --no-notify
# To publish through Calibre/OPDS instead:
./start.sh candidate.json --no-notify
```

`config.py` loads `.env` containing `GLM_API_KEY` or `ZAI_API_KEY`; exported values take
precedence. Never print the key. Local generation does not need Calibre. Publication
needs the configured Calibre Content Server (`localhost:8080` in this installation).

The pipeline researches the source and concepts, preserves source structure, translates
and proofreads aligned units, converges semantic and terminology audits, assembles a
portable EPUB, and validates it before completion/publication. There is no fast mode
that bypasses unresolved accuracy failures.

Output is `data/library/<title>-<identity>.epub`. `completed` means a validated local
file; `published` means public acquisition verification and obsolete cleanup have
also completed. Return the actual artifact
path or acquisition URL, not device-specific button instructions. OPDS feed:
`https://books.foundby.kr/opds`.

## Validation and recovery

Install the official pinned full validator with:

```bash
./venv/bin/python3 scripts/setup_epubcheck.py
./venv/bin/python3 epub_validate.py /path/to/book.epub
```

Java is needed for EPUBCheck. Local structural checks always run. Full checking runs
when the bundled distribution, `epubcheck` command or `EPUBCHECK_JAR` is available.
`BABELFORGE_REQUIRE_EPUBCHECK=1` requires it; do not claim full conformance when the
report says the external checker is unavailable.

Use `retry_request(req_id)` for a failed or stopped worker. It keeps the request,
source selection, saved output mode and notification preference, reuses compatible translation/audit checkpoints
and prevents concurrent workers for the same request. Retry at most once for an unchanged
transient failure; retry again only after evidence that the cause has changed. Inspect
validation/quality errors rather than bypassing them or manually marking the request
successful. Ordinary recovery does not require editing production code or state files.

## Re-review existing editions

For a request to apply current audits to existing bilingual books, use
`review_library.py` rather than sending legacy checkpoints through a new draft
profile. Create a JSON manifest with `review_dir` and `editions`; each edition binds
`id`, `inputs` (baseline EPUB first, then any source-identical superseded versions),
`source_path`, `source_format`, `language`, `title`, `author`, `source_name`,
`source_url`, `rights`, `output` and known `source_research`. Paths are relative to
the repository or explicitly absolute. Keep distinct relay editions separate.

```bash
./venv/bin/python3 review_library.py manifest.json --prepare-only
./venv/bin/python3 review_library.py manifest.json --workers 2
```

Preparation records input hashes and an editorial baseline, verifies exact ordered source alignment and
restores omitted TXT passages and logical source paragraph/chapter structure.
Inspect coverage before running model calls; an edition mismatch must be resolved,
not fuzzily aligned. Only restored/resegmented blank slots need drafting. All
existing Korean receives the current source-grounded semantic and terminology
review, including independent approval of repaired wording. The command checkpoints
reviews and writes corrected EPUBs under the review directory after validation;
it never publishes or notifies. Rerun the same manifest to resume, optionally with
`--edition ID`. Diagnose unchanged validation/convergence failures before retrying.
A book is complete only when its `result.json` and validated output exist. After
all editions complete, run `./venv/bin/python3 finish_library_review.py manifest.json`.
This verifies every current exact-text approval, restores explicit indented source
line breaks and unlisted chapter navigation without changing approved Korean,
then runs content comparison and official EPUBCheck again. Quotation-boundary or
legacy abbreviation-boundary flags require source review before delivery. Save any
content repair as an unapproved proposal in the isolated review checkpoint and
rerun that edition; never insert a hand-written approval. It writes a local
reviewed-edition folder and ZIP bundle. It does not retain old EPUB backups;
live originals remain until explicitly requested publication verifies replacements.
If a model repeatedly reverses a source-grounded correction, verify scholarly
lexical/grammatical references and retain them in the isolated book's optional
`source_evidence` map keyed by SHA-256 of the exact source string. Supply facts
and reference locations, not an instruction to approve. Evidence binds only that
passage's audit signature; changed evidence requires fresh independent approval.
Optional manifest `attribution` supplements source metadata. Return the actual
corrected EPUBs and report any incomplete editions explicitly.

## Publish corrected editions and remove superseded copies

Normal publication now uses the same verified revision promotion as reviewed books.
Do not use one-off backup/export/SQL scripts or mark a request published by hand.
There is no old-edition backup: temporary validation/download files are removed
automatically; hashes and stage journals remain under `data/publications/`.

```bash
./venv/bin/python3 publish_reviewed.py manifest.json --check-only
./venv/bin/python3 publish_reviewed.py manifest.json
# Or finish, validate and publish in one explicitly requested operation:
./venv/bin/python3 finish_library_review.py manifest.json --publish
```

Manifest editions may include `publication` with `source`, `source_id`, `request_id`,
`destination` and `title`. Existing catalog source identities and Gutenberg edition
URLs can supply missing identities; ambiguous local editions require explicit fields.
Keep different languages/volumes/relay editions separate. An unidentified legacy
Calibre item requires explicit `legacy_id`, `legacy_path` and `legacy_sha256`; its
actual download must match that local artifact before binding the edition identifier.

Promotion validates the exact approved artifact, replaces the canonical Calibre EPUB
while retaining its ID, follows public OPDS navigation/pagination, downloads both the
advertised and stable acquisitions, compares all source/Korean pairs and requires
official EPUBCheck. Only then does it commit the local library/catalog and delete
known obsolete copies and same-identifier duplicate records. Deletion uses explicit
paths and frozen hashes, never fuzzy titles. Verification failures do not run obsolete
cleanup; an already uploaded canonical EPUB may be the new file. Incomplete cleanup
remains retryable and journaled; resume the same command. Publication owns the
request lock, so concurrent retry workers cannot rewrite its state.

A configured managed read-only server is stopped briefly for local CLI writes and
restarted even after a write failure. Its public write policy is never weakened.
An interrupted publisher records its own server-maintenance marker; the next
publication recovers that service only if the configuration hash still matches.
Unmanaged/remote write failures stop publication rather than bypassing server locks.
Do not claim success merely because a Calibre import succeeded: return the verified
acquisition URLs only after the final publication journal says `completed`.

## Deploy MCP code and verify the full published library

After reviewing and merging code, use the managed deployment command instead of
restarting the LaunchAgent alone. It does not retranslate, approve, publish or
remove books. Use a completed review manifest covering **every published request**;
any number of editions is supported, and Calibre IDs come from the catalog.

```bash
./venv/bin/python3 deploy_mcp.py manifest.json --expected-commit "$(git rev-parse HEAD)"
# Inspect the running service and published books without restarting:
./venv/bin/python3 deploy_mcp.py manifest.json --check-only
```

A real deployment requires a clean checkout and a matching installed MCP
LaunchAgent. `BABELFORGE_MCP_LAUNCH_AGENT` can select its plist. The command holds
deployment/publication locks, refuses active workers and unfinished Calibre
maintenance, and blocks new MCP translation/retry claims while checking. Status
queries remain available. It validates all current exact-text approvals and
retained source/profile/artifact hashes before restarting, then requires a new PID
and real MCP tool responses. Transport readiness is retried within 30 seconds;
stale statuses and failed approvals are never retried into success.

Both advertised and stable OPDS EPUB downloads must match every approved
original/Korean pair and pass official EPUBCheck. The complete Calibre managed
inventory and OPDS pagination must contain the expected canonical editions once,
with no deleted-record acquisitions. The command also checks canonical EPUBs,
summary-bound delivery copies and ZIP contents. Extra EPUB/ZIP files in the managed
library, same-identity catalog aliases, and any remaining paths in the review
folder's optional `old-deliverables.json` block success. Keep sources and editorial
records outside the managed output library; unrelated Calibre books are preserved.

Checks run again after downloads to catch concurrent changes. Every invocation
writes a fresh JSON report under `data/deployments/`; `--report` can select a new
path, but an existing report is never overwritten. A failure exits nonzero and
records `completed=false` and its stage. A successful restart alone is not a
successful deployment. Failed post-restart validation leaves the service running
and the library intact for diagnosis; fix the cause, then rerun with a fresh report.
Check-only mode may inspect an uncommitted checkout but never claims to deploy it.

## Install or reinstall OpenClaw monitoring

The canonical monitor is `openclaw_monitor.py` in this repository. Do not copy its
implementation into the OpenClaw workspace. First obtain a successful complete
`deploy_mcp.py --check-only` report, then prepare a local configuration:

```bash
./venv/bin/python3 install_openclaw_monitor.py manifest.json \
  --baseline-report data/deployments/successful-check.json \
  --line-target APPROVED_PERSONAL_LINE_ID
# Review the plan, then repeat with --apply to write settings and reconcile the job.
# On migration, --job-id EXISTING_MONITOR_ID asserts the intended job identity.
```

The LINE destination must already be explicitly approved by the user. No normal,
setup-completion or recovery messages are sent. The installer defaults to a preview,
requires a complete baseline report tied to the unchanged manifest, actually probes
a compatible Java 17+, and rejects an invalid explicit `--java-home`. Discovery can
skip a broken inherited Java environment; use `--java-home /path/to/jdk` to pin a
known compatible runtime. `--openclaw /path/to/openclaw` overrides CLI discovery.
No API keys or personal destination IDs belong in tracked files.

`--apply` writes private mode-0600 settings under `data/openclaw-monitor/` and
creates or updates one OpenClaw command job at 09:00 Asia/Seoul. Its stable
declaration is `babelforge-mcp-opds-daily-v1`. Existing IDs are reused by stable declaration; a matching name alone never
authorizes adoption. Legacy jobs without a declaration require explicit --job-id.
Ambiguous or unrelated jobs and active runs block installation. New jobs are created disabled
until the explicit no-delivery/no-native-failure-alert policy is applied. The job
runs the repository venv and monitor directly, with an explicit Java environment
and the installer-resolved OpenClaw/Node/Bun search path. Manual monitor invocations
use that same saved search path for notifications.
Scheduler/no-output limits are 300 seconds longer than the checker's timeout,
leaving time to terminate Java descendants, record the failure and send an alert.
Reinstalling leaves incident state and history intact.

```bash
./venv/bin/python3 openclaw_monitor.py
# Or select local settings with --config /path/to/config.json.
# Use the returned job ID for a real scheduler test:
openclaw cron run JOB_ID --wait --wait-timeout 25m
openclaw cron runs JOB_ID --limit 1 --json
```

A healthy run prints `NO_REPLY`, records its fresh report/log and clears the prior
incident silently. A first anomaly sends only predefined diagnostic facts,
affected edition identifiers and local report/log paths to the approved LINE
recipient. Book text, raw errors and credentials stay local. Repeated identical
failures are suppressed; a changed cause or recurrence after a healthy check is
new. Corrupt incident JSON does not suppress the first alert. A pending/ambiguous
send is recorded as unknown and is not automatically replayed, to avoid duplicate
pushes. Inspect `data/openclaw-monitor/runtime/incident.json` and delivery history
before deliberately clearing such a state. Overlapping monitor invocations exit
75 without launching another checker or notifier.

The wrapper does not restart services, reapprove/retranslate/republish or delete
books. Native OpenClaw delivery/failure alerts must stay disabled because this
wrapper owns anomaly delivery. Failure returns nonzero; healthy completion requires
all local/public standard and content checks, matching current approvals, inventory
and the installation baseline. Following a new approved revision or review-version
change, explicitly supply a new successful report and reinstall; never automatically
accept a changed manifest or hashes merely to silence an alert. After moving the
checkout or replacing the JDK, regenerate a valid report for that checkout and
rerun the installer to update paths. Settings/installation receipts and reports are
local ignored data; source code, procedure and offline regression tests stay in Git.

## Optional legacy device delivery

Direct device push is off by default. Only use `BABELFORGE_DEVICE_PUSH=1` when the user
requests that integration. The watcher then retries queued files. Its failure does not
undo EPUB generation or OPDS publication. The core output needs no CrossPoint firmware,
font package, network discovery or bilingual-toggle feature. Do not activate device
integration merely because the user asks for an EPUB.
