# BabelForge

Public-domain books → **portable bilingual EPUB 3 (original + Korean)** → optional
Calibre/OPDS publication. The output works in ordinary EPUB readers without custom
firmware, a language toggle, a device font package, or a sibling repository.

This is the architecture and implementation SSOT. The operational procedure is
[`.agents/skills/book-translator/SKILL.md`](.agents/skills/book-translator/SKILL.md),
the user overview is `README.md`, and the format contract is `docs/epub-format.md`.

## Architecture

```
request → source/edition research → verified original-language candidate if safe
        → content-validated atomic download → structure-preserving extraction
        → researched concept sheet → aligned translation + proofreading
        → semantic + occurrence-aware terminology audits until stable
        → EPUB assembly → local structural checks + EPUBCheck when installed
        → completed local EPUB OR Calibre/OPDS publication
        → optional device delivery and notification
```

The pipeline is one Python process per request, with parallel translation batches.
Request workers have exclusive process locks; catalog and delivery queue updates
use locked read-modify-write transactions and unique temporary files. State files
remain JSON-compatible with existing installations. Corrupt state fails visibly
instead of silently resetting the catalog or queue.

## Components

| File | Responsibility |
|---|---|
| `pipeline.py` | Worker ownership, stage orchestration, source/artifact hashes, validation gate, optional publication and isolated auxiliary delivery. `run(..., publish_output=False)` generates a local EPUB. |
| `config.py` | Environment-backed settings and all repository paths derived from `__file__`. |
| `sources/` | Gutenberg, Standard Ebooks, opt-in Anna's Archive and local adapters. `sources/download.py` validates content types/signatures, keys caches by URL and commits content atomically. |
| `extract.py` | EPUB spine order, linked footnotes, semantic block units, assets and XHTML body templates; TXT front matter and logical chapter preservation. |
| `epub_markup.py` | Shared inline XHTML sanitization, visible-text extraction and exact ordered tag placeholders. |
| `precision.py` | Original-title/language research, conservative same-work/author/volume/rights selection, concept drafting, web grounding, adversarial review and normalization. |
| `translate.py` | Numbered batch alignment, output budgets, retries, inline tags, source context, checkpoints and audit resume. |
| `quality_audit.py` | Shared fidelity criteria, grounded correction proposals, independent exact-text approval, partial-row recovery, audit ledger, contextual terminology and bounded convergence. |
| `review_library.py` | Manifest-bound existing-edition review; exact source coverage recovery, retained Korean, source/term audits and resumable local EPUB rebuilds. |
| `finish_library_review.py` | Final approval/quotation checks, source verse and TOC restoration, metadata, EPUBCheck and local reviewed-library ZIP delivery. |
| `assemble.py` | Portable EPUB 3, both languages, structured source templates, inline emphasis, images, links, TOC/NCX, provenance and atomic validated output. |
| `epub_validate.py` | ZIP/XML/manifest/spine/link/anchor/pair checks and optional full EPUBCheck. |
| `scripts/setup_epubcheck.py` | Installs a pinned official EPUBCheck release after verifying its SHA-256. Requires Java at runtime. |
| `publish.py` | Stable edition identifiers; replaces an existing EPUB format without deleting its Calibre record, otherwise adds a separate edition. |
| `publication.py` | Serialized revision promotion, temporary staging, public OPDS acquisition/content/EPUBCheck verification, catalog commit and identity/hash-bound obsolete deletion. No old-edition backups. |
| `publish_reviewed.py` | Publishes a completed review manifest only after exact final approval and artifact checks; supports a read-only preflight. |
| `state_store.py`, `catalog.py` | POSIX process locks, atomic state transactions, conditional retry claims and crashed-worker recovery. |
| `babelforge_mcp.py` | Typed search, detached translation, retry and status tools. Local streamable HTTP on `127.0.0.1:8770/mcp` by default. |
| `device_push.py`, `pending_queue.py`, `auto_push_watcher.py` | Optional legacy device integration. Disabled by default. |
| `search.py` | Search routing and optional OpenClaw LINE notifications. |

`glossary_builder.py`, `fill_filtered.py`, `build_font.py` and `deliver.py` are
legacy utilities, not requirements of the standard EPUB path. The old font and
explicit-device tools may still reference `crosspoint-agentdeck`; generation does not.

## Invariants

- **Secrets:** `.env` is ignored by Git. `config.py` loads `ZAI_API_KEY` or
  `GLM_API_KEY` without overriding exported environment variables. Never print keys.
- **Paths:** repository paths derive from `BASE_DIR`; do not hardcode the checkout.
- **Name research:** retain short CJK lexical/name forms during normalization;
  statistical candidate-length filters are not lexical-validity rules. Cluster
  retained-source forms independently from cross-script original/romanized metadata.
  Primary pronunciation annotations outrank an earlier model's guessed reading.
- **Source identity:** same author alone never proves same work. Automatic original
  replacement requires matching work/author/volume and an eligible public-domain
  source. Uncertain matches retain the selected edition.
- **Source conservation:** logical chapters and short utterances are not boilerplate.
  Download cache payloads are content-addressed, so a later fetch cannot replace
  bytes a running request is already reading. Legacy caches remain readable.
  EPUB templates bind indexed translation slots to inline XHTML units; images,
  tables, verse and linked footnotes retain their structure. Unavailable remote
  media in standalone HTML fails before translation instead of disappearing.
- **Portable format:** the local contract is `docs/epub-format.md`, based on EPUB 3.
  `bf-original` / `bf-translation` are CSS hooks, not parser requirements. Do not
  reintroduce device-driven chapter caps or special-font requirements.
- **Defaults:** original paragraph boundaries are preserved. Optional sentence
  units use `BABELFORGE_SENTENCE_SPLIT=1` within the retained source containers.
  Long blocks use smaller translation units within their original containers to
  respect model output budgets without deleting or relocating source text.
- **Checkpoint monotonicity:** batch failures must not replace a nonblank translated
  slot with a blank. Preserve this guard in `_commit_batch`.
- **Profile compatibility:** prompt/model/terminology changes invalidate stale work.
  Verify stored review profile hashes against the actual concept sheet, and
  bind a review checkpoint to its baseline hash and unchanged source sequence.
  Matching translation checkpoint headers alone do not authorize misaligned pairs.
  Structural remapping requires matching source-independent instruction hashes and
  exact in-order source coverage of at least 95%. Legacy checkpoints without a
  compatible instruction hash are not silently reused.
- **Final audit:** every final text must pass deterministic integrity checks.
  A semantic correction is an **unapproved proposal**, including when the model
  repeats unchanged text with `action=correct`. Only a separate call returning
  `keep` for the exact final wording approves it. Up to four semantic review rounds
  and three semantic/terminology convergence rounds are allowed; conflicts block
  output. Terminology repairs trigger semantic re-review. Semantic cache signatures
  bind source + final Korean + adjacent source/Korean context (across chapter
  boundaries too) + work metadata + model and quality profile. Prompt/audit versions invalidate older approvals.
- **Grounded edits and recovery:** semantic corrections require a reason and a
  supporting source quotation; only explicitly classified Korean grammar/spelling
  repairs may omit the quotation. Numeric/punctuation-only source labels may cite
  their exact nonalphabetic text; a prose correction may not cite punctuation alone.
  NFC/elision typography and line-initial book.line reference anchors may
  normalize for evidence matching, but spelling, accents and inflections may not. Valid verdicts survive a
  partially malformed response; retry only unresolved rows, up to three attempts.
  Review batches are bounded by paragraph count, 24,000 serialized input row
  characters and 6,000 estimated repair/evidence characters (including row overhead);
  oversized units fail visibly instead of being truncated. No oversized/empty API
  input is silently submitted. Repairs invalidate neighboring approvals when their
  context changes; those units must pass again before completion. Checkpoints persist
  `audit_log` (before/after, evidence, profile, signatures, approval) after each accepted response, including
  proposals that still need re-review. A failed attempt cannot certify completion.
  Terminology verdicts also require one unambiguous row per key with integer
  indices; duplicate rows and coerced boolean/string/fractional indices are rejected.
- **Legacy label integrity:** reject unexpanded tag/batch markers absent from the
  source, and substantive prose copied into short numeric/Roman label slots.
  Preserve author initials such as `J. M.` as identifiers; they are not untranslated
  sentences. These integrity checks apply even to cached approvals.
- **Context exceptions:** a phrase exception covers a specific source occurrence,
  never a separate standalone concept in the same paragraph. Avoided Korean strings
  also require occurrence-specific exceptions: repeated collisions need separate
  `ko_start` offsets in plain Korean or one-based `ko_occurrence` indices, so a legitimate ethnonym cannot exempt an
  unrelated mistranslated name elsewhere in the same paragraph.
- **Publication:** validate before atomic output replacement and before publication.
  Compare every original/Korean visible-text pair in EPUB spine order with the
  approved book, not merely the number of pairs. This is separate from semantic
  accuracy and EPUBCheck conformance.
  Keep the canonical Calibre record and acquisition URL when replacing its file.
  Delete only redundant records with the same stable edition identifier, after
  public acquisition verification. Delete explicitly identified old local EPUBs
  only after verification and catalog commit; never use title-based glob deletion.
  Changed hashes, symlinks, shared other-edition paths and paths outside the managed
  library block cleanup. Do not retain old EPUB backups. Source text and editorial
  audit records remain distinct from obsolete deliverable files.
- **Concurrency:** use catalog/queue transaction helpers; `os.replace()` alone is
  not a read-modify-write lock. Hold `catalog.request_worker(req_id)` for the run.
  MCP startup, status lookup and recent-request listing recover orphaned worker
  states, including legacy rows with no PID. Recovery holds the free worker lock
  through the catalog update, preserves progress and records the prior state.
  Live locks and fresh launch reservations take precedence; untracked legacy
  worker stages have a 60-second startup grace. Waiting search/selection requests
  and terminal records are preserved. Missing persisted candidates require a new
  source selection rather than promising an impossible retry.
- **Completion:** local output is `completed`; published output is `published`.
  Revision promotion remains retryable until obsolete cleanup finishes; CLI
  publication owns the same request lock as normal pipeline workers.
  Device/notification failures do not regress either state. Retries preserve the
  stored local-versus-publish choice and notification setting unless explicitly
  overridden. Legacy records with no notification preference default to silent.
- **Anna's Archive:** only explicitly selected `annas:` searches may use it.
- **Structured API:** use provider `response_format=json_object`. Business error
  `1213` is not a retryable rate-limit error. Empty inputs must not reach the API.

## Validation and development

```bash
./venv/bin/python3 -m unittest discover -s tests -v
./venv/bin/python3 scripts/setup_epubcheck.py
./venv/bin/python3 epub_validate.py /path/to/book.epub
```

The setup script installs under `data/tools/`, which is ignored by Git. The validator
auto-detects that distribution, an `epubcheck` executable, or `EPUBCHECK_JAR`.
`BABELFORGE_REQUIRE_EPUBCHECK=1` makes absence of the full checker an error. Otherwise
local structural checks still run and the report explicitly says `not_available`.

Tests use temporary state, mock model/Calibre calls and concurrency fixtures. They
must not translate, publish, notify, scan a network or modify production data.

## Deployment context

The configured model endpoint is Z.ai (`config.ZAI_MODEL`); web grounding uses its
MCP search endpoint through `mcp_client.py`. Individual searches fail softly while
structured source/concept adjudication is mandatory.

Calibre Content Server is optional for local EPUB generation; publication normally
uses `calibredb` via `http://localhost:8080`. A proven managed read-only local server
is paused for CLI writes to its matching library and restarted in `finally` before
public verification. Never grant anonymous writes or edit the LaunchAgent policy.
A durable maintenance marker permits the next publication to recover a service
interrupted by process termination. Recovery requires the exact same configuration
and never starts a manually stopped service with no publisher marker.
Public feed: `https://books.foundby.kr/opds`. The old `books.getlingo.store`
route is only a compatibility alias and is due to expire.

The MCP LaunchAgent is `com.local.babelforge-mcp`. The legacy device watcher is
`com.local.book-translator-watcher` (20-second interval); it returns without
network activity unless `BABELFORGE_DEVICE_PUSH=1`. Moving the checkout requires
updating installed plist paths. No launchd configuration is changed automatically
by code edits.

Invoke `venv/bin/python3` directly. This venv was moved historically, so activation
scripts/console-script shebangs may have stale paths; recreate from requirements
when needed. `.env`, `data/`, `logs/`, `venv/` and caches are ignored by Git.
