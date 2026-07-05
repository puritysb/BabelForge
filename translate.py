"""GLM-5.2 paragraph-wise translation.

Translate each chapter's paragraphs in batches, preserving 1:1 paragraph
alignment so assemble.py can emit cp-original/cp-translation pairs.

API: Zhipu AI (ZAI) OpenAI-compatible chat endpoint.
  - baseUrl: https://api.z.ai/api/coding/paas/v4
  - model:   glm-5.2 (1M context, 8192 maxTokens)
  - auth:    Bearer $ZAI_API_KEY  (read from env, never hard-coded)

Key design:
  - One GLM call per batch of ~20 paragraphs. Output is delimited Korean
    paragraphs that we split back into a list aligned 1:1 with the input.
  - Exponential backoff on 429/5xx. After TRANSLATE_MAX_RETRIES we surface a
    TranslationError so the orchestrator can mark the request failed.
  - If $ZAI_API_KEY is unset, we fail fast with a clear actionable message.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field

import config
from extract import Chapter


@dataclass
class TranslatedChapter:
    title: str
    pairs: list[tuple[str, str]] = field(default_factory=list)  # (original, korean)


@dataclass
class TranslatedBook:
    title: str
    author: str
    chapters: list[TranslatedChapter] = field(default_factory=list)


class TranslationError(RuntimeError):
    pass


# The system prompt is built per-call so the source language can vary
# (en/fr/ja/…). The marker discipline and 1:1 paragraph mapping stay fixed.
PARA_DELIM = "⟦P⟧"

# ─── HTML Tag Placeholder System ───
# GLM-5.2 often strips or mangles inline HTML tags (<i>, <b>, <em>, <strong>).
# We replace them with Unicode private-use placeholders before sending text
# to the model, then restore them after translation.
_TAG_PATTERN = re.compile(r'</?(i|b|em|strong|span|sup|sub|a\b[^>]*)\s*>', re.IGNORECASE)

def _stash_tags(text: str) -> tuple[str, dict[str, str]]:
    """Replace HTML tags with ◀TAG0▶, ◀TAG1▶, ... placeholders.
    Returns (placeholder_text, mapping) where mapping[placeholder] = original_tag.
    """
    mapping: dict[str, str] = {}
    def _replace(m: re.Match) -> str:
        tag = m.group(0)
        key = f"\u25c0TAG{len(mapping)}\u25b6"
        mapping[key] = tag
        return key
    return _TAG_PATTERN.sub(_replace, text), mapping

def _restore_tags(text: str, mapping: dict[str, str]) -> str:
    """Restore HTML tags from placeholders."""
    for placeholder, original in mapping.items():
        text = text.replace(placeholder, original)
    return text


def _system_prompt(source_lang: str, glossary: dict | None = None) -> str:
    src = {"en": "English", "ja": "Japanese", "fr": "French",
           "de": "German", "zh": "Chinese", "es": "Spanish"}.get(source_lang, source_lang)
    prompt = (
        f"You are a careful literary translator from {src} to Korean.\n"
        "Rules:\n"
        "1. Translate every input paragraph into natural, publishable Korean.\n"
        "2. Preserve the exact paragraph count and order: one output paragraph per "
        f"input paragraph, separated by the marker {PARA_DELIM} on its own line.\n"
        "3. Do NOT add commentary, notes, or summaries.\n"
        "4. Keep dialogue dashes, emphasis, and paragraph breaks as in the source.\n"
        "5. If a paragraph is a chapter heading or pure punctuation, translate it "
        "as a short heading-style line and still emit the marker after it.\n"
        f"6. Output ONLY the Korean paragraphs separated by {PARA_DELIM}. No preamble."
    )
    if glossary:
        rules = []
        for src_word, tgt_word in glossary.items():
            # Handle auto-glossary placeholders like <CONSISTENT:Term>
            if isinstance(tgt_word, str) and tgt_word.startswith("<CONSISTENT:"):
                rules.append(f"- '{src_word}' must always be translated the same way throughout the book (use the most natural Korean rendering and keep it consistent)")
            else:
                rules.append(f"- '{src_word}' must be translated as '{tgt_word}'")
        glossary_txt = "\n".join(rules)
        prompt += f"\n\nTerminology Glossary (strictly respect these mappings):\n{glossary_txt}"
    return prompt


def _chat(messages: list[dict], timeout: int = config.TRANSLATE_TIMEOUT_S) -> str:
    """POST /chat/completions; return the assistant content string."""
    api_key = config.get_zai_api_key()
    if not api_key:
        raise TranslationError(
            f"ZAI API key missing. Export {config.ZAI_API_KEY_ENV} before running "
            f"translate, e.g.:\n"
            f"  export {config.ZAI_API_KEY_ENV}=$(openclaw config get …)\n"
            f"or paste the GLM Coding Plan key into the environment."
        )
    body = json.dumps({
        "model": config.ZAI_MODEL,
        "messages": messages,
        "max_tokens": config.ZAI_MAX_TOKENS,
        "temperature": 0.3,
    }).encode("utf-8")
    last_err = None
    for attempt in range(config.TRANSLATE_MAX_RETRIES):
        # Re-create Request each attempt: defensive against any internal
        # state mutation by urlopen, and harmless (body is immutable bytes).
        req = urllib.request.Request(
            f"{config.ZAI_BASE_URL}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                sys.stderr.write(f"[translate] HTTP {e.code}, retry in {wait}s\n")
                time.sleep(wait)
                continue
            raise TranslationError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            last_err = e
            wait = 2 ** attempt
            sys.stderr.write(f"[translate] URLError {e}, retry in {wait}s\n")
            time.sleep(wait)
    raise TranslationError(f"exhausted retries: {last_err}")


# Match a "[k] text..." block: the number at line start, then everything up to
# the next "[k]" line. GLM keeps an explicit numbered list far more reliably
# than a lone ⟦P⟧ delimiter, so this is the primary alignment signal.
_NUM_MARKER_RE = re.compile(
    r'(?m)^[ \t]*\[(\d+)\][ \t]*(.*(?:\n(?!\s*\[\d+\][ \t]*).*)*)')


def _parse_numbered_slots(raw: str, n: int) -> list[str]:
    """Split a batch response into exactly n slots ('' for any not recovered).

    Primary signal is the [k] markers the model was told to keep; falls back to
    a PARA_DELIM split only when no usable markers are present. A lone marker on
    a multi-paragraph batch means the model collapsed everything into one blob,
    so we discard it and let the caller re-translate each paragraph. Slots hold
    the model's raw text (tag placeholders intact) — the caller restores tags.
    """
    slots = [""] * n
    got = 0
    for m in _NUM_MARKER_RE.finditer(raw):
        idx = int(m.group(1)) - 1
        if 0 <= idx < n and not slots[idx]:
            text = m.group(2).strip().replace(PARA_DELIM, "").strip()
            if text:
                slots[idx] = text
                got += 1
    if got >= 2 or (got == 1 and n <= 3):
        return slots
    if got == 1:
        # Collapsed into one blob under a single marker — treat as unaligned.
        return [""] * n
    # No markers at all — legacy positional split on the delimiter.
    parts = [re.sub(r"^[ \t]*\[\d+\][ \t]*", "", p.strip())
             for p in raw.split(PARA_DELIM) if p.strip()]
    if len(parts) <= 1 and n > 1:
        # One blob with neither markers nor delimiters → collapsed; re-translate.
        return [""] * n
    for i, p in enumerate(parts[:n]):
        slots[i] = p
    return slots


def _translate_one(paragraph: str, source_lang: str = "en", glossary: dict | None = None) -> str:
    """Translate a single paragraph. Returns "" on failure (never raises).

    Used as a fallback when a batch is under-returned — single-paragraph calls
    rarely get dropped by the model because there's no alignment ambiguity.
    """
    # Stash HTML tags before translation
    stashed, tag_map = _stash_tags(paragraph)
    try:
        out = _chat([
            {"role": "system", "content": _system_prompt(source_lang, glossary)},
            {"role": "user", "content": (
                f"Translate this single {source_lang} paragraph into Korean. "
                f"Emit exactly one Korean paragraph (no markers, no numbering):\n\n{stashed}"
            )},
        ])
        # Strip any leading/trailing whitespace + accidental numbering/markers.
        cleaned = out.strip()
        cleaned = re.sub(r"^\[\d+\]\s*", "", cleaned)
        cleaned = cleaned.replace(PARA_DELIM, "").strip()
        # Restore HTML tags
        cleaned = _restore_tags(cleaned, tag_map)
        return cleaned
    except TranslationError as e:
        sys.stderr.write(f"[translate] single-paragraph fallback failed: {e}\n")
        return ""



def _proofread_batch(originals: list[str], translations: list[str], source_lang: str, glossary: dict | None = None) -> list[str]:
    """Compare draft translations to originals and polish them via a second LLM pass."""
    n = len(originals)
    src_name = {"en": "English", "ja": "Japanese", "fr": "French",
                "de": "German", "zh": "Chinese", "es": "Spanish"}.get(source_lang, source_lang)
    
    system_prompt = (
        f"You are a rigorous, professional literary editor. Compare the original {src_name} paragraphs "
        "with their Korean translations to correct any translation errors, omissions, awkward phrasing, "
        "or unnatural Korean structures.\n\n"
        "Rules:\n"
        "1. Generate polished, natural literary Korean translations.\n"
        f"2. Keep the exact paragraph count and order: output exactly {n} Korean "
        f"paragraphs, each on its own line prefixed with its number in square "
        f"brackets — [1], [2], … through [{n}]. Never merge, skip, or renumber.\n"
        "3. Maintain original formatting, punctuation, and style.\n"
        "4. Output ONLY the numbered polished Korean paragraphs. No comments or explanations."
    )
    if glossary:
        rules = []
        for src_word, tgt_word in glossary.items():
            if isinstance(tgt_word, str) and tgt_word.startswith("<CONSISTENT:"):
                rules.append(f"- '{src_word}' must always be translated the same way throughout the book")
            else:
                rules.append(f"- '{src_word}' must be translated as '{tgt_word}'")
        glossary_txt = "\n".join(rules)
        system_prompt += f"\n\nGlossary (ensure these are applied):\n{glossary_txt}"

    # Stash HTML tags before the proofreading pass too — this second pass
    # rewrites the Korean, so raw <i>/<b>/… would be just as vulnerable to
    # mangling here as in the draft pass. Placeholders keep them intact; we
    # restore once on the polished output using the original+draft tag maps.
    stashed_orig, maps_o = [], []
    for orig in originals:
        s, m = _stash_tags(orig)
        stashed_orig.append(s)
        maps_o.append(m)
    stashed_trans, maps_t = [], []
    for trans in translations:
        s, m = _stash_tags(trans)
        stashed_trans.append(s)
        maps_t.append(m)

    # Pair them up in the user prompt so the model can easily inspect
    paired = []
    for idx, (orig, trans) in enumerate(zip(stashed_orig, stashed_trans), 1):
        paired.append(f"[{idx}] Original:\n{orig}\n[{idx}] Draft Translation:\n{trans}")

    user_msg = (
        f"Please proofread these {n} draft translation(s) against the originals.\n"
        f"Output exactly {n} corrected paragraphs, each prefixed with its [k] "
        f"number.\n\n" + "\n\n".join(paired)
    )

    try:
        raw = _chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ])
        slots = _parse_numbered_slots(raw, n)
        if all(s.strip() for s in slots):
            # Restore tags. Merge each paragraph's original + draft maps so any
            # placeholder the proofreader emits (from either side) resolves.
            restored = [
                _restore_tags(s, {**maps_o[i], **maps_t[i]})
                for i, s in enumerate(slots)
            ]
            sys.stderr.write(f"[translate] 2-Pass Proofread succeeded for batch of {n}\n")
            return restored
        else:
            filled = sum(1 for s in slots if s.strip())
            sys.stderr.write(f"[translate] 2-Pass Proofread returned mismatch ({filled}/{n}). Using draft.\n")
    except Exception as e:
        sys.stderr.write(f"[translate] 2-Pass Proofread failed ({e}). Using draft.\n")

    return translations


def _translate_batch(paragraphs: list[str], source_lang: str = "en",
                        glossary: dict | None = None,
                        context_before: list[str] | None = None) -> list[str]:
    """Translate a batch of source paragraphs -> list of Korean paragraphs.

    The model is told to emit exactly len(paragraphs) chunks separated by the
    delimiter. If it returns far fewer (severe alignment break — e.g. the 18/0
    case seen on Pride & Prejudice Ch. IV), we fall back to single-paragraph
    calls so the bilingual EPUB doesn't end up with whole untranslated
    chapters.

    context_before: optional list of 2-3 source paragraphs immediately
    preceding this batch, passed as surrounding context so the model keeps
    terminology, tone and pronoun reference consistent across batch
    boundaries. Source-side (not translated) so it is available up front and
    safe to compute for concurrent, out-of-order batches.
    """
    n = len(paragraphs)
    # Stash HTML tags in all paragraphs before sending to the model
    stashed_maps: list[dict[str, str]] = []
    stashed_paras = []
    for p in paragraphs:
        stashed_text, tag_map = _stash_tags(p)
        stashed_paras.append(stashed_text)
        stashed_maps.append(tag_map)

    # Number each paragraph so the model can't silently merge them.
    numbered = "\n\n".join(
        f"[{i+1}]\n{p}" for i, p in enumerate(stashed_paras)
    )

    # Build context preamble if provided. This is the *source* text right
    # before the batch — surrounding context only, NOT to be translated.
    context_block = ""
    if context_before:
        context_samples = "\n\n".join(context_before[-3:])
        context_block = (
            f"\n\n--- Preceding {source_lang} context (do NOT translate; for "
            f"terminology and tone consistency only) ---\n"
            f"{context_samples}\n"
            f"--- End context ---\n\n"
        )

    user_msg = (
        f"Translate the following {n} {source_lang} paragraph(s) into Korean. "
        f"Output each Korean paragraph on its own line, prefixed with its number "
        f"in square brackets exactly as in the input — [1], [2], … through "
        f"[{n}]. Keep all {n} numbers, one per source paragraph, in order; "
        f"never merge, skip, or renumber."
        f"{context_block}\n\n{numbered}"
    )
    raw = _chat([
        {"role": "system", "content": _system_prompt(source_lang, glossary)},
        {"role": "user", "content": user_msg},
    ])
    # Align by the [k] markers (robust to a dropped ⟦P⟧). Slots hold raw model
    # text with tag placeholders still in; restore tags on whatever came back.
    slots = _parse_numbered_slots(raw, n)
    for i in range(n):
        if slots[i]:
            slots[i] = _restore_tags(slots[i], stashed_maps[i])

    # Fill exactly the slots the batch didn't produce with single-paragraph
    # calls (rarely dropped — no alignment ambiguity). Handles non-contiguous
    # holes, so a partial batch keeps its good paragraphs instead of re-doing
    # the whole thing.
    missing = [i for i in range(n) if not slots[i].strip()]
    if missing:
        sys.stderr.write(
            f"[translate] batch recovered {n - len(missing)}/{n}; "
            f"filling {len(missing)} slot(s) individually\n"
        )
        for i in missing:
            slots[i] = _translate_one(paragraphs[i], source_lang=source_lang,
                                      glossary=glossary)

    if config.TWO_PASS_TRANSLATION:
        return _proofread_batch(paragraphs, slots, source_lang, glossary)
    return slots



def translate_book(title: str, author: str, chapters: list[Chapter],
                   progress_cb=None, source_lang: str = "en",
                   checkpoint_path: str | None = None,
                   max_workers: int | None = None,
                   glossary: dict | None = None) -> TranslatedBook:
    """Translate every chapter, paragraph-batched, concurrently.

    progress_cb(done_paragraphs, total_paragraphs) is called periodically so
    the orchestrator can update catalog.json.
    source_lang selects the translator prompt's source language (en/ja/…).
    checkpoint_path: if given, translation state is persisted there and resumed
        on restart. A long novel (9000+ sentences) is unsafe to run without
        this — a single network hiccup or process kill would lose everything.
        The checkpoint is validated against a hash of the source paragraphs so
        a stale file from a different book is never wrongly resumed.
    max_workers: concurrent GLM calls. The coding-plan endpoint takes ~20s per
        call, so serial translation of a 9000-sentence novel takes ~12h; 8
        workers brings it to ~1.5h. Default: config.TRANSLATE_WORKERS.
    glossary: optional dictionary of term mappings. If not provided, attempts to
        load a default glossary from config.GLOSSARY_PATH.

    Batches run concurrently and are integrated into per-chapter pair storage
    as they complete (out of order). The checkpoint is flushed every 30 batches
    or 60s, so a crash never loses more than ~a minute of work.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Load default glossary if none provided
    if glossary is None and os.path.isfile(config.GLOSSARY_PATH):
        try:
            with open(config.GLOSSARY_PATH, "r", encoding="utf-8") as f:
                glossary = json.load(f)
                sys.stderr.write(
                    f"[translate] loaded glossary from {config.GLOSSARY_PATH} "
                    f"({len(glossary)} mappings)\n"
                )
        except Exception as e:
            sys.stderr.write(f"[translate] failed to load glossary: {e}\n")

    max_workers = max_workers or config.TRANSLATE_WORKERS
    total = sum(len(c.paragraphs) for c in chapters)
    fp = _source_fingerprint(chapters)

    # Resume from checkpoint: rebuild per-chapter pair lists so we can skip
    # every batch that already has a translation.
    resumed: dict[int, list[list[str]]] = {}
    if checkpoint_path and os.path.isfile(checkpoint_path):
        try:
            ckpt = json.load(open(checkpoint_path, "r", encoding="utf-8"))
            if ckpt.get("source_hash") == fp and len(ckpt.get("chapters", [])) == len(chapters):
                for ci, c in enumerate(ckpt["chapters"]):
                    resumed[ci] = [list(p) for p in c.get("pairs", [])]
                done = sum(len(v) for v in resumed.values())
                sys.stderr.write(
                    f"[translate] resumed from checkpoint: {done}/{total} units "
                    f"already translated\n"
                )
            else:
                sys.stderr.write(
                    "[translate] checkpoint ignored (source mismatch) — "
                    "starting fresh\n"
                )
        except (json.JSONDecodeError, OSError) as e:
            sys.stderr.write(f"[translate] checkpoint unreadable ({e}), fresh start\n")

    # Per-chapter pair storage: [orig, ko] lists, ko="" until translated.
    # Seeded from checkpoint so we only re-run unfinished batches.
    chapter_pairs: dict[int, list[list[str]]] = {}
    for ci, ch in enumerate(chapters):
        pairs = [[p, ""] for p in ch.paragraphs]
        for j, pair in enumerate(resumed.get(ci, [])):
            if j < len(pairs):
                pairs[j][1] = pair[1] if len(pair) >= 2 else ""
        chapter_pairs[ci] = pairs

    batch = config.TRANSLATE_BATCH_PARAGRAPHS
    # Build the remaining work queue: (chapter_idx, start_offset, window).
    # A batch is re-queued if ANY of its slots is still blank — this fills
    # gaps left by earlier 429-exhaustion failures without redoing good work.
    remaining: list[tuple[int, int, list[str]]] = []
    for ci, ch in enumerate(chapters):
        pairs = chapter_pairs[ci]
        for i in range(0, len(ch.paragraphs), batch):
            window = ch.paragraphs[i:i + batch]
            if any(not pairs[i + j][1].strip() for j in range(len(window))):
                remaining.append((ci, i, window))

    if not remaining:
        sys.stderr.write("[translate] nothing to do (all units already translated)\n")

    # Run batches concurrently, filling translated slots as they complete.
    # Each batch also receives the 3 source paragraphs immediately before it
    # as surrounding context (computed up front from the source, so it works
    # for out-of-order concurrent completion). Hard terminology consistency is
    # carried by `glossary`; this context keeps tone/pronoun reference steady.
    last_ckpt = time.time()
    done_batches = 0
    total_batches = len(remaining)

    def _commit_batch(ci: int, i: int, window: list[str], ko: list[str]) -> None:
        """Write a completed batch's translations into chapter_pairs and flush
        the checkpoint periodically. Mutates done_batches / last_ckpt."""
        nonlocal done_batches, last_ckpt
        for j, tr in enumerate(ko):
            chapter_pairs[ci][i + j][1] = tr
        done_batches += 1
        if done_batches % 20 == 0:
            sys.stderr.write(
                f"[translate] {done_batches}/{total_batches} batches, "
                f"{_count_done(chapter_pairs)}/{total} units\n"
            )
        # Flush checkpoint periodically so a crash loses ≤ ~1 min.
        now = time.time()
        if checkpoint_path and (done_batches % 30 == 0 or now - last_ckpt > 60):
            _flush_checkpoint(checkpoint_path, title, author, fp,
                              source_lang, chapters, chapter_pairs)
            last_ckpt = now
            if progress_cb:
                progress_cb(_count_done(chapter_pairs), total)

    if remaining:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {}
            for ci, i, window in remaining:
                # Preceding source paragraphs (same chapter) as context.
                context_before = chapters[ci].paragraphs[max(0, i - 3):i] or None
                fut = ex.submit(_translate_batch, window, source_lang,
                                glossary, context_before)
                futs[fut] = (ci, i, window)
            for fut in as_completed(futs):
                ci, i, window = futs[fut]
                try:
                    ko = fut.result()
                except TranslationError as e:
                    ko = [""] * len(window)
                    sys.stderr.write(
                        f"[translate] batch ch{ci}@{i} failed "
                        f"(left blank): {e}\n"
                    )
                _commit_batch(ci, i, window, ko)

    # Final flush + assemble the TranslatedBook in chapter order.
    if checkpoint_path:
        _flush_checkpoint(checkpoint_path, title, author, fp,
                          source_lang, chapters, chapter_pairs)
    if progress_cb:
        progress_cb(_count_done(chapter_pairs), total)

    book = TranslatedBook(title=title, author=author)
    for ci, ch in enumerate(chapters):
        tch = TranslatedChapter(title=ch.title)
        for orig, ko in chapter_pairs[ci]:
            tch.pairs.append((orig, ko))
        book.chapters.append(tch)

    # Sanity check: a near-zero completion rate means a systemic failure
    # (bad API key, wrong endpoint, quota exhausted) rather than individual
    # batch hiccups. Also reject any remaining blank translations; publishing
    # a bilingual EPUB with missing cp-translation blocks looks successful in
    # the catalog but puts incomplete content on the device.
    nonempty = sum(1 for tch in book.chapters for _, ko in tch.pairs if ko.strip())
    rate = nonempty / total if total else 1.0
    if rate < 0.5:
        raise TranslationError(
            f"only {nonempty}/{total} units translated ({rate:.0%}) — likely a "
            f"systemic failure (API key / endpoint / quota). Checkpoint kept at "
            f"{checkpoint_path}; fix the cause and rerun to resume."
        )
    missing = [
        (ci + 1, pi + 1, tch.title)
        for ci, tch in enumerate(book.chapters)
        for pi, (_, ko) in enumerate(tch.pairs)
        if not ko.strip()
    ]
    if missing:
        preview = ", ".join(
            f"ch{ci} unit{pi} {title!r}" for ci, pi, title in missing[:5]
        )
        extra = "" if len(missing) <= 5 else f", +{len(missing) - 5} more"
        raise TranslationError(
            f"{len(missing)}/{total} translation units are blank; refusing to "
            f"publish incomplete EPUB ({preview}{extra}). Checkpoint kept at "
            f"{checkpoint_path}; fill blanks and rerun."
        )
    return book


def _count_done(chapter_pairs: dict[int, list[list[str]]]) -> int:
    """Count units with a non-empty translation."""
    return sum(1 for pairs in chapter_pairs.values()
               for pair in pairs if pair[1].strip())


def _flush_checkpoint(path: str, title: str, author: str, source_hash: str,
                      source_lang: str, chapters: list[Chapter],
                      chapter_pairs: dict[int, list[list[str]]]) -> None:
    """Persist current translation state from the pair storage."""
    tchs = [TranslatedChapter(title=ch.title,
                              pairs=[(p[0], p[1]) for p in chapter_pairs[ci]])
            for ci, ch in enumerate(chapters)]
    _save_checkpoint(path, title, author, source_hash, source_lang, tchs)


def _source_fingerprint(chapters: list[Chapter]) -> str:
    """Stable hash of the SOURCE text (titles + paragraphs) so a checkpoint
    from a different book, or the same book after re-extraction changed the
    sentence splits, is detected and discarded rather than wrongly resumed."""
    h = hashlib.sha1()
    for ch in chapters:
        h.update(ch.title.encode("utf-8"))
        h.update(b"\x00")
        for p in ch.paragraphs:
            h.update(p.encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()


def _save_checkpoint(path: str, title: str, author: str, source_hash: str,
                     source_lang: str, chapters: list[TranslatedChapter],
                     partial: tuple[int, int] | None = None) -> None:
    """Atomically write the translation checkpoint."""
    data = {
        "title": title,
        "author": author,
        "source_hash": source_hash,
        "source_lang": source_lang,
        "chapters": [{"title": c.title, "pairs": [list(p) for p in c.pairs]}
                     for c in chapters],
    }
    if partial is not None:
        data["partial_chapter"], data["partial_unit"] = partial
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    """Stand-alone smoke: read chapter JSON on stdin, print translated JSON.
    Used by run_request.sh for the translate-only step. Format:
        {"title": "...", "author": "...", "chapters": [{"title":..., "paragraphs":[...]}]}
    """
    payload = json.loads(sys.stdin.read())
    chapters = [Chapter(title=c["title"], paragraphs=c["paragraphs"])
                for c in payload["chapters"]]
    book = translate_book(payload["title"], payload["author"], chapters)
    out = {
        "title": book.title,
        "author": book.author,
        "chapters": [{"title": c.title, "pairs": c.pairs} for c in book.chapters],
    }
    json.dump(out, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
