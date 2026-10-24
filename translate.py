"""GLM-5.2 paragraph-wise translation.

Translate each chapter's paragraphs in batches, preserving 1:1 paragraph
alignment so assemble.py can emit portable original/translation pairs.

API: Zhipu AI (ZAI) OpenAI-compatible chat endpoint.
  - baseUrl: https://api.z.ai/api/coding/paas/v4
  - model:   glm-5.2 (1M context, 8192 maxTokens)
  - auth:    Bearer $ZAI_API_KEY  (read from env, never hard-coded)

Key design:
  - Production calls require a researched concept sheet. Its hash and the
    precision prompt version participate in checkpoint compatibility.
  - One GLM call per batch of ~20 paragraphs. Output is delimited Korean
    paragraphs that we split back into a list aligned 1:1 with the input.
  - Exponential backoff on 429/5xx. After TRANSLATE_MAX_RETRIES we surface a
    TranslationError so the orchestrator can mark the request failed.
  - If $ZAI_API_KEY is unset, we fail fast with a clear actionable message.
  - A final whole-book terminology audit repairs violations and fails closed if
    any strict concept remains inconsistent.
"""
from __future__ import annotations
import difflib
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
from model_json import ModelResponseError, ModelJSONError, parse_model_object
from extract import Chapter
from epub_markup import (placeholders_preserved, restore_inline_tags,
                         stash_inline_tags)


@dataclass
class TranslatedChapter:
    title: str
    pairs: list[tuple[str, str]] = field(default_factory=list)  # (original, korean)
    template: str | None = None
    assets: dict = field(default_factory=dict)


@dataclass
class TranslatedBook:
    title: str
    author: str
    chapters: list[TranslatedChapter] = field(default_factory=list)
    quality_report: dict | None = None
    audit_log: list[dict] = field(default_factory=list)


class TranslationError(RuntimeError):
    pass


class UnusableModelOutput(TranslationError, ModelResponseError):
    """Response-format failure, distinct from permanent request/auth errors."""


# The system prompt is built per-call so the source language can vary
# (en/fr/ja/…). The marker discipline and 1:1 paragraph mapping stay fixed.
PARA_DELIM = "⟦P⟧"
PRECISION_PROMPT_VERSION = "literary-precision-v4-contextual-fidelity"

# ─── HTML Tag Placeholder System ───
# GLM-5.2 often strips or mangles inline HTML tags (<i>, <b>, <em>, <strong>).
# We replace them with Unicode private-use placeholders before sending text
# to the model, then restore them after translation.
def _stash_tags(text: str, prefix: str = "T") -> tuple[str, dict[str, str]]:
    """Replace HTML tags with ◀TAG0▶, ◀TAG1▶, ... placeholders.
    Returns (placeholder_text, mapping) where mapping[placeholder] = original_tag.
    """
    return stash_inline_tags(text, prefix)

def _restore_tags(text: str, mapping: dict[str, str]) -> str:
    """Restore HTML tags from placeholders."""
    return restore_inline_tags(text, mapping)


def _concept_rules(concept_sheet: dict | None) -> str:
    if not concept_sheet:
        return ""
    rules = []
    for concept in concept_sheet.get("concepts") or []:
        forms = ", ".join(repr(str(v)) for v in concept.get("source_terms") or [])
        target = str(concept.get("canonical_ko") or "").strip()
        if not forms or not target:
            continue
        original = ", ".join(str(v) for v in concept.get("original_terms") or [])
        avoid = ", ".join(repr(str(v)) for v in concept.get("avoid_ko") or [])
        rule = f"- {forms} => '{target}'"
        if original:
            rule += f" (underlying original: {original})"
        if avoid:
            rule += f"; never use {avoid} for this concept"
        rationale = str(concept.get("rationale") or "").strip()
        if rationale:
            rule += f"; distinction: {rationale}"
        if not concept.get("strict", True):
            rule += "; contextual term—use the mapping only in the documented sense"
        rules.append(rule)
    if not rules:
        return ""
    return (
        "\n\nBook-level Concept Sheet (philologically researched and "
        "cross-checked; preserve distinctions exactly):\n" + "\n".join(rules)
    )


def _system_prompt(source_lang: str, glossary: dict | None = None,
                   concept_sheet: dict | None = None) -> str:
    src = {"en": "English", "ja": "Japanese", "fr": "French",
           "de": "German", "zh": "Chinese", "es": "Spanish", "grc": "Ancient Greek", "la": "Latin"}.get(source_lang, source_lang)
    prompt = (
        f"You are a careful literary translator from {src} to Korean.\n"
        "Rules:\n"
        "1. Translate every input paragraph faithfully into natural, publishable Korean. "
        "Conceptual and logical accuracy outranks surface fluency.\n"
        "2. Preserve the exact paragraph count and order: one output paragraph per "
        f"input paragraph, separated by the marker {PARA_DELIM} on its own line.\n"
        "3. Do NOT add commentary, notes, or summaries.\n"
        "4. Keep dialogue dashes, emphasis, and paragraph breaks as in the source.\n"
        "5. If a paragraph is a chapter heading or pure punctuation, translate it "
        "as a short heading-style line and still emit the marker after it.\n"
        f"6. Output ONLY the Korean paragraphs separated by {PARA_DELIM}. No preamble.\n"
        "7. Preserve negation, modality, contrasts, causal relations, metaphors, "
        "and distinctions between neighboring technical terms. Never silently "
        "generalize or beautify a difficult claim."
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
    from quality_audit import FIDELITY_RULES
    prompt += "\n" + FIDELITY_RULES
    prompt += _concept_rules(concept_sheet)
    return prompt


def _validate_messages(messages: list[dict]) -> list[dict]:
    """Return a clean text-only message list or fail before calling Z.ai.

    Z.ai business error 1213 means a required field was not received properly.
    Catching empty/non-text content locally prevents an empty malformed-output
    repair from being sent as a seemingly valid chat request.
    """
    if not isinstance(messages, list) or not messages:
        raise TranslationError("Z.ai request requires at least one message")
    clean: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise TranslationError(f"Z.ai message {index} must be an object")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            raise TranslationError(f"Z.ai message {index} has invalid role {role!r}")
        if not isinstance(content, str) or not content.strip():
            raise TranslationError(
                f"Z.ai message {index} has empty or non-text content"
            )
        clean.append({"role": role, "content": content})
    return clean


def _error_payload(body_text: bytes) -> tuple[str | None, str]:
    try:
        payload = json.loads(body_text.decode("utf-8", errors="replace"))
        error = payload.get("error") or {}
        return str(error.get("code") or "") or None, str(error.get("message") or "")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None, body_text.decode("utf-8", errors="replace")[:200]


def _chat(messages: list[dict], timeout: int = config.TRANSLATE_TIMEOUT_S,
          json_mode: bool = False) -> str:
    """POST /chat/completions; return non-empty assistant text.

    ``json_mode`` uses Z.ai's documented structured-output contract. Parameter
    errors such as business code 1213 are not retried with the same invalid
    body; callers can instead rebuild a valid request with different content.
    """
    api_key = config.get_zai_api_key()
    if not api_key:
        raise TranslationError(
            f"ZAI API key missing. Export {config.ZAI_API_KEY_ENV} before running "
            f"translate, e.g.:\n"
            f"  export {config.ZAI_API_KEY_ENV}=$(openclaw config get …)\n"
            f"or paste the GLM Coding Plan key into the environment."
        )
    payload = {
        "model": config.ZAI_MODEL,
        "messages": _validate_messages(messages),
        "max_tokens": config.ZAI_MAX_TOKENS,
        "temperature": 0.3,
        # The Coding Plan endpoint enables thinking by default. These calls are
        # bounded transformations (draft, bilingual proofread, or JSON schema),
        # and a separate proofread pass already provides semantic review. Hidden
        # reasoning otherwise consumes the full output budget and can leave the
        # actual translation empty with finish_reason=length.
        "thinking": {"type": "disabled"},
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
                "Accept-Language": "en-US,en",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                    choices = data["choices"]
                    if not isinstance(choices, list) or len(choices) != 1:
                        raise UnusableModelOutput("Z.ai must return exactly one completion")
                    choice = choices[0]
                    content = choice["message"].get("content")
                    finish = choice.get("finish_reason")
                except (ValueError, UnicodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
                    raise UnusableModelOutput("Z.ai returned a malformed completion envelope") from exc
                if choice.get("finish_reason") == "length":
                    raise UnusableModelOutput("Z.ai truncated assistant output (finish_reason=length)")
                if finish != "stop":
                    raise UnusableModelOutput("Z.ai did not finish a text response")
                if not isinstance(content, str) or not content.strip():
                    finish = choice.get("finish_reason") or "unknown"
                    reasoning = choice["message"].get("reasoning_content") or ""
                    if not isinstance(reasoning, str):
                        reasoning = ""
                    raise UnusableModelOutput(
                        f"Z.ai returned empty assistant content "
                        f"(finish_reason={finish}, reasoning_chars={len(reasoning)})"
                    )
                return content
        except urllib.error.HTTPError as e:
            last_err = e
            body_text = b""
            try:
                body_text = e.read()[:400]
            except Exception:
                pass
            business_code, error_message = _error_payload(body_text)
            if e.code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                sys.stderr.write(
                    f"[translate] HTTP {e.code}, retry in {wait}s "
                    f"(business_code={business_code or 'none'})\n"
                )
                time.sleep(wait)
                continue
            raise TranslationError(
                f"Z.ai HTTP {e.code} business_code={business_code or 'unknown'}: "
                f"{error_message or 'parameter error'}"
            ) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            wait = 2 ** attempt
            sys.stderr.write(f"[translate] network {type(e).__name__}, retry in {wait}s\n")
            time.sleep(wait)
    raise TranslationError(f"exhausted retries: {last_err}")


def _chat_json(messages: list[dict],
               timeout: int = config.TRANSLATE_TIMEOUT_S) -> str:
    """Validate one response; stage owners bound format/schema retries together."""
    clean = _validate_messages(messages)
    instruction = ("Return exactly one complete JSON object only. No Markdown or "
                   "text outside the object. Never repeat keys or use NaN/Infinity.")
    if clean[0]["role"] == "system":
        clean[0] = {**clean[0], "content": clean[0]["content"] + "\n" + instruction}
    else:
        clean.insert(0, {"role": "system", "content": instruction})
    raw = _chat(clean, timeout=timeout, json_mode=True)
    try:
        parsed = parse_model_object(raw)
    except ModelJSONError as exc:
        fingerprint = hashlib.sha256(raw.encode('utf-8', errors='surrogatepass')).hexdigest()
        sys.stderr.write(f"[model-json] rejected chars={len(raw)} sha256={fingerprint}: {exc}\n")
        raise
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


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


def _translate_one(paragraph: str, source_lang: str = "en",
                   glossary: dict | None = None,
                   concept_sheet: dict | None = None) -> str:
    """Translate a single paragraph. Returns "" on failure (never raises).

    Used as a fallback when a batch is under-returned — single-paragraph calls
    rarely get dropped by the model because there's no alignment ambiguity.
    """
    # Stash HTML tags before translation
    stashed, tag_map = _stash_tags(paragraph)
    try:
        out = _chat([
            {"role": "system", "content": _system_prompt(
                source_lang, glossary, concept_sheet)},
            {"role": "user", "content": (
                f"Translate this single {source_lang} paragraph into Korean. "
                f"Emit exactly one Korean paragraph (no markers, no numbering):\n\n{stashed}"
            )},
        ])
        # Strip any leading/trailing whitespace + accidental numbering/markers.
        cleaned = out.strip()
        cleaned = re.sub(r"^\[\d+\]\s*", "", cleaned)
        cleaned = cleaned.replace(PARA_DELIM, "").strip()
        if not placeholders_preserved(cleaned, tag_map):
            raise TranslationError("single-paragraph output changed inline placeholders")
        # Restore HTML tags
        cleaned = _restore_tags(cleaned, tag_map)
        return cleaned
    except TranslationError as e:
        sys.stderr.write(f"[translate] single-paragraph fallback failed: {e}\n")
        return ""



def _proofread_batch(originals: list[str], translations: list[str],
                     source_lang: str, glossary: dict | None = None,
                     concept_sheet: dict | None = None) -> list[str]:
    """Semantic accuracy review followed by restrained literary polishing."""
    # An empty failed draft has no protected tag mapping. Let the checkpoint
    # retain that gap instead of allowing proofreading to invent an unformatted
    # replacement with no source-tag preservation checks.
    if any(not translation.strip() for translation in translations):
        return translations
    n = len(originals)
    src_name = {"en": "English", "ja": "Japanese", "fr": "French",
                "de": "German", "zh": "Chinese", "es": "Spanish", "grc": "Ancient Greek", "la": "Latin"}.get(source_lang, source_lang)
    
    system_prompt = (
        f"You are a rigorous bilingual philological editor. Compare the original {src_name} paragraphs "
        "with their Korean drafts. First correct semantic errors: mistranslated concepts, logical "
        "relations, negation, modality, referents, omissions, and unjustified paraphrase. Only then "
        "polish awkward Korean without weakening or beautifying the claim.\n\n"
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
    from quality_audit import FIDELITY_RULES
    system_prompt += "\n" + FIDELITY_RULES
    system_prompt += _concept_rules(concept_sheet)

    # Stash HTML tags before the proofreading pass too — this second pass
    # rewrites the Korean, so raw <i>/<b>/… would be just as vulnerable to
    # mangling here as in the draft pass. Placeholders keep them intact; we
    # restore only draft placeholders, with distinct prefixes for each side.
    stashed_orig = []
    for orig in originals:
        s, _ = _stash_tags(orig, "O")
        stashed_orig.append(s)
    stashed_trans, maps_t = [], []
    for trans in translations:
        s, m = _stash_tags(trans, "T")
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
    user_msg += "\nPreserve every draft Translation placeholder (T prefix) exactly once in order; do not output Original placeholders (O prefix)."

    try:
        raw = _chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ])
        slots = _parse_numbered_slots(raw, n)
        if all(s.strip() and placeholders_preserved(s, maps_t[i])
               and not re.search(r"◀OTAG\d+▶", s)
               for i, s in enumerate(slots)):
            # Only the exact draft mapping may be emitted. Independent source
            # and draft prefixes cannot alias tags with different attributes.
            restored = [
                _restore_tags(s, maps_t[i])
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
                        context_before: list[str] | None = None,
                        concept_sheet: dict | None = None) -> list[str]:
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
        {"role": "system", "content": _system_prompt(
            source_lang, glossary, concept_sheet)},
        {"role": "user", "content": user_msg},
    ])
    # Align by the [k] markers (robust to a dropped ⟦P⟧). Slots hold raw model
    # text with tag placeholders still in; restore tags on whatever came back.
    slots = _parse_numbered_slots(raw, n)
    for i in range(n):
        if slots[i]:
            slots[i] = (_restore_tags(slots[i], stashed_maps[i])
                        if placeholders_preserved(slots[i], stashed_maps[i]) else "")

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
                                      glossary=glossary,
                                      concept_sheet=concept_sheet)

    return _proofread_batch(paragraphs, slots, source_lang, glossary,
                            concept_sheet)



def _plan_windows(paragraphs: list[str], max_paras: int, max_chars: int) -> list[tuple[int, list[str]]]:
    """Pack paragraphs into (start_index, window) batches bounded by BOTH a
    paragraph count and a source-character budget. Dense prose thus yields
    small batches (whose Korean output stays under the model's token ceiling)
    while short paragraphs still pack up to max_paras. An oversized single
    paragraph is its own window."""
    windows: list[tuple[int, list[str]]] = []
    i, n = 0, len(paragraphs)
    while i < n:
        j, chars = i, 0
        while j < n and (j - i) < max_paras:
            plen = len(paragraphs[j])
            if j > i and chars + plen > max_chars:
                break
            chars += plen
            j += 1
        windows.append((i, paragraphs[i:j]))
        i = j
    return windows


def _remap_structural_checkpoint(
        checkpoint: dict, chapters: list[Chapter], title: str, author: str,
        source_lang: str, minimum_coverage: float = 0.95,
        ) -> dict[int, list[list[str]]] | None:
    """Reuse translations when only chapter boundaries/boilerplate changed.

    A better extractor can turn one ``Untitled`` chapter into real sections
    without changing the underlying prose.  The strict source hash correctly
    rejects that checkpoint, but translating the whole book again would waste
    the already aligned work.  Recover only exact, in-order source strings and
    only when they cover almost the entire newly extracted book.  The normal
    whole-book semantic and terminology audits still run afterward, so an old
    draft cannot bypass the current quality profile.
    """
    if (checkpoint.get("title") != title
            or checkpoint.get("author") != author
            or checkpoint.get("source_lang") != source_lang):
        return None
    old_pairs = [
        list(pair)
        for chapter in checkpoint.get("chapters") or []
        for pair in chapter.get("pairs") or []
        if isinstance(pair, (list, tuple)) and len(pair) >= 2
    ]
    new_positions = [
        (ci, pi, paragraph)
        for ci, chapter in enumerate(chapters)
        for pi, paragraph in enumerate(chapter.paragraphs)
    ]
    if not old_pairs or not new_positions:
        return None
    def source_key(value: str) -> str:
        # Extractor upgrades may collapse Gutenberg's hard-wrapped lines while
        # leaving the actual words unchanged. Whitespace normalization is safe
        # here because recovered pairs still retain the newly extracted source.
        return re.sub(r"\s+", " ", str(value)).strip()

    old_keys = [source_key(pair[0]) for pair in old_pairs]
    new_keys = [source_key(position[2]) for position in new_positions]
    matcher = difflib.SequenceMatcher(
        a=old_keys,
        b=new_keys,
        autojunk=False,
    )
    recovered: dict[int, list[list[str]]] = {
        ci: [[paragraph, ""] for paragraph in chapter.paragraphs]
        for ci, chapter in enumerate(chapters)
    }
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            old_pair = old_pairs[block.a + offset]
            ci, pi, source = new_positions[block.b + offset]
            if (old_keys[block.a + offset] == source_key(source)
                    and str(old_pair[1]).strip()):
                recovered[ci][pi][1] = str(old_pair[1])
                matched += 1
    coverage = matched / len(new_positions)
    if coverage < minimum_coverage:
        return None
    return recovered


def _checkpoint_pairs_aligned(checkpoint: dict, chapters: list[Chapter]) -> bool:
    saved = checkpoint.get("chapters")
    if not isinstance(saved, list) or len(saved) != len(chapters):
        return False
    for prior, current in zip(saved, chapters):
        if not isinstance(prior, dict) or not isinstance(prior.get("pairs"), list):
            return False
        pairs = prior["pairs"]
        if len(pairs) > len(current.paragraphs):
            return False
        if any(not isinstance(pair, (list, tuple)) or len(pair) != 2
               or pair[0] != current.paragraphs[i] or not isinstance(pair[1], str)
               for i, pair in enumerate(pairs)):
            return False
    return True


def translate_book(title: str, author: str, chapters: list[Chapter],
                   progress_cb=None, source_lang: str = "en",
                   checkpoint_path: str | None = None,
                   max_workers: int | None = None,
                   glossary: dict | None = None,
                   concept_sheet: dict | None = None) -> TranslatedBook:
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
        call, so serial translation of a 9000-sentence novel takes ~12h; more
        workers speeds it up proportionally, bounded by 429 rate limiting.
        Default: config.TRANSLATE_WORKERS.
    glossary: optional dictionary of term mappings. If not provided, attempts to
        load a default glossary from config.GLOSSARY_PATH.
    concept_sheet: mandatory in the production precision pipeline. Its profile
        hash is part of checkpoint compatibility, so changing a researched term
        never silently reuses translations made under the old decision.

    Batches run concurrently and are integrated into per-chapter pair storage
    as they complete (out of order). The checkpoint is flushed every 30 batches
    or 60s, so a crash never loses more than ~a minute of work.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if concept_sheet is None:
        raise TranslationError(
            "precision translation requires an autonomously researched concept sheet"
        )

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
    quality_fp = _quality_profile_fingerprint(concept_sheet)
    instruction_fp = _instruction_profile_fingerprint(concept_sheet, glossary, source_lang)

    # Resume from checkpoint: rebuild per-chapter pair lists so we can skip
    # every batch that already has a translation.
    resumed: dict[int, list[list[str]]] = {}
    semantic_verified: set[str] = set()
    audit_log: list[dict] = []
    if checkpoint_path and os.path.isfile(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                ckpt = json.load(f)
            if (ckpt.get("source_hash") == fp
                    and ckpt.get("quality_profile_hash") == quality_fp
                    and ckpt.get("translation_profile_hash") == instruction_fp
                    and _checkpoint_pairs_aligned(ckpt, chapters)):
                for ci, c in enumerate(ckpt["chapters"]):
                    resumed[ci] = [list(p) for p in c.get("pairs", [])]
                semantic_verified = {value for value in ckpt.get("semantic_verified", [])
                                     if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)}
                audit_log = [row for row in ckpt.get("audit_log", []) if isinstance(row, dict)]
                done = sum(len(v) for v in resumed.values())
                sys.stderr.write(
                    f"[translate] resumed from checkpoint: {done}/{total} units "
                    f"already translated\n"
                )
            else:
                # Only structure changes may remap. The separate instruction
                # fingerprint excludes source_hash, but includes every actual
                # translation rule, prompt version, model and glossary.
                remapped = None
                if (ckpt.get("source_hash") != fp
                        and ckpt.get("translation_profile_hash") == instruction_fp):
                    remapped = _remap_structural_checkpoint(
                        ckpt, chapters, title, author, source_lang)
                if remapped is not None:
                    resumed = remapped
                    done = sum(
                        1 for pairs in resumed.values()
                        for _, korean in pairs if korean.strip()
                    )
                    sys.stderr.write(
                        f"[translate] remapped structural checkpoint: "
                        f"{done}/{total} exact source units reused; current "
                        "quality audits will run\n"
                    )
                else:
                    sys.stderr.write(
                        "[translate] checkpoint ignored (source/precision "
                        "profile mismatch) — starting fresh\n"
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

    max_paras = config.TRANSLATE_BATCH_PARAGRAPHS
    max_chars = config.TRANSLATE_BATCH_MAX_CHARS
    # Build the remaining work queue: (chapter_idx, start_offset, window).
    # A batch is re-queued if ANY of its slots is still blank — this fills
    # gaps left by earlier failures without redoing good work.
    remaining: list[tuple[int, int, list[str]]] = []
    for ci, ch in enumerate(chapters):
        pairs = chapter_pairs[ci]
        for i, window in _plan_windows(ch.paragraphs, max_paras, max_chars):
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
        the checkpoint periodically. Mutates done_batches / last_ckpt.

        A window can mix already-translated paragraphs with blank ones (it's
        re-queued whenever ANY slot is blank, and window boundaries can shift
        between runs — e.g. after a batching-strategy change). If the whole
        batch then fails (retries exhausted), naively overwriting every slot
        with "" would wipe out paragraphs that were already translated in a
        prior run/checkpoint. Never regress a non-blank slot to blank.
        """
        nonlocal done_batches, last_ckpt
        regressions = 0
        for j, tr in enumerate(ko):
            idx = i + j
            if not tr.strip() and chapter_pairs[ci][idx][1].strip():
                regressions += 1
                continue
            chapter_pairs[ci][idx][1] = tr
        if regressions:
            sys.stderr.write(
                f"[translate] ch{ci}@{i}: kept {regressions} pre-existing "
                f"translation(s) instead of blanking them\n"
            )
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
                              source_lang, chapters, chapter_pairs,
                              quality_profile_hash=quality_fp,
                              translation_profile_hash=instruction_fp,
                              semantic_verified=semantic_verified, audit_log=audit_log)
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
                                glossary, context_before, concept_sheet)
                futs[fut] = (ci, i, window)
            for fut in as_completed(futs):
                ci, i, window = futs[fut]
                try:
                    ko = fut.result()
                except Exception as e:
                    # Drain all completed futures and checkpoint successful rows
                    # even when an unexpected parser/transport exception occurs.
                    ko = [""] * len(window)
                    sys.stderr.write(
                        f"[translate] batch ch{ci}@{i} failed "
                        f"(left blank): {e}\n"
                    )
                _commit_batch(ci, i, window, ko)

    # Final flush + assemble the TranslatedBook in chapter order.
    if checkpoint_path:
        _flush_checkpoint(checkpoint_path, title, author, fp,
                          source_lang, chapters, chapter_pairs,
                          quality_profile_hash=quality_fp,
                          translation_profile_hash=instruction_fp,
                          semantic_verified=semantic_verified, audit_log=audit_log)
    if progress_cb:
        progress_cb(_count_done(chapter_pairs), total)

    book = TranslatedBook(title=title, author=author, audit_log=audit_log)
    for ci, ch in enumerate(chapters):
        tch = TranslatedChapter(title=ch.title,
                                template=getattr(ch, "template", None),
                                assets=dict(getattr(ch, "assets", {}) or {}))
        for orig, ko in chapter_pairs[ci]:
            tch.pairs.append((orig, ko))
        book.chapters.append(tch)

    # Sanity check: a near-zero completion rate means a systemic failure
    # (bad API key, wrong endpoint, quota exhausted) rather than individual
    # batch hiccups. Also reject any remaining blank translations; publishing
    # a bilingual EPUB with missing translation blocks looks successful in
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

    # Precision mode is fail-closed. First independently review every final
    # source/translation pair for semantic, omission, and Korean-form errors;
    # then enforce every strict book-level concept over any corrected text.
    from quality_audit import QualityAuditError, audit_book_quality

    def save_audit_progress():
        if checkpoint_path:
            _save_checkpoint(checkpoint_path, title, author, fp, source_lang,
                             book.chapters, quality_profile_hash=quality_fp,
                             translation_profile_hash=instruction_fp,
                             semantic_verified=semantic_verified,
                             audit_log=book.audit_log)

    try:
        book.quality_report = audit_book_quality(
            book, concept_sheet, _chat_json,
            chunk_size=config.QUALITY_AUDIT_BATCH_PARAGRAPHS,
            verified=semantic_verified,
            progress_cb=save_audit_progress,
            audit_profile={"model": config.ZAI_MODEL, "quality_profile": quality_fp},
        )
    except QualityAuditError as exc:
        raise TranslationError(f"quality audit failed: {exc}") from exc

    # Persist any audit corrections. This keeps resume monotonic while ensuring
    # the checkpoint represents the exact quality profile that produced it.
    if checkpoint_path:
        _save_checkpoint(checkpoint_path, title, author, fp, source_lang,
                         book.chapters, quality_profile_hash=quality_fp,
                         translation_profile_hash=instruction_fp,
                         semantic_verified=semantic_verified,
                         quality_report=book.quality_report, audit_log=book.audit_log)
    return book


def _count_done(chapter_pairs: dict[int, list[list[str]]]) -> int:
    """Count units with a non-empty translation."""
    return sum(1 for pairs in chapter_pairs.values()
               for pair in pairs if pair[1].strip())


def _flush_checkpoint(path: str, title: str, author: str, source_hash: str,
                      source_lang: str, chapters: list[Chapter],
                      chapter_pairs: dict[int, list[list[str]]],
                      quality_profile_hash: str | None = None,
                      translation_profile_hash: str | None = None,
                      semantic_verified: set[str] | None = None,
                      audit_log: list[dict] | None = None) -> None:
    """Persist current translation state from the pair storage."""
    tchs = [TranslatedChapter(title=ch.title,
                              pairs=[(p[0], p[1]) for p in chapter_pairs[ci]])
            for ci, ch in enumerate(chapters)]
    _save_checkpoint(path, title, author, source_hash, source_lang, tchs,
                     quality_profile_hash=quality_profile_hash,
                     translation_profile_hash=translation_profile_hash,
                     semantic_verified=semantic_verified, audit_log=audit_log)


def _instruction_profile_fingerprint(concept_sheet: dict | None,
                                     glossary: dict | None, source_lang: str) -> str:
    sheet = concept_sheet or {}
    data = {
        "prompt_version": PRECISION_PROMPT_VERSION,
        "model": config.ZAI_MODEL,
        "source_language": source_lang,
        "schema_version": sheet.get("schema_version"),
        "profile_version": sheet.get("profile_version"),
        "concepts": sheet.get("concepts") or [],
        "glossary": glossary or {},
    }
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _quality_profile_fingerprint(concept_sheet: dict | None) -> str:
    from quality_audit import SEMANTIC_AUDIT_VERSION
    data = {
        "prompt_version": PRECISION_PROMPT_VERSION,
        "semantic_audit_version": SEMANTIC_AUDIT_VERSION,
        "model": config.ZAI_MODEL,
        "concept_profile": (concept_sheet or {}).get("profile_hash"),
        "concepts": (concept_sheet or {}).get("concepts") or [],
    }
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
                     partial: tuple[int, int] | None = None,
                     quality_profile_hash: str | None = None,
                     translation_profile_hash: str | None = None,
                     semantic_verified: set[str] | None = None,
                     quality_report: dict | None = None,
                     audit_log: list[dict] | None = None) -> None:
    """Atomically write the translation checkpoint."""
    data = {
        "title": title,
        "author": author,
        "source_hash": source_hash,
        "source_lang": source_lang,
        "quality_profile_hash": quality_profile_hash,
        "translation_profile_hash": translation_profile_hash,
        "semantic_verified": sorted(semantic_verified or []),
        "chapters": [{"title": c.title, "pairs": [list(p) for p in c.pairs]}
                     for c in chapters],
    }
    if audit_log is not None:
        data["audit_log"] = audit_log
    if quality_report is not None:
        data["quality_report"] = quality_report
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
    book = translate_book(payload["title"], payload["author"], chapters,
                          concept_sheet=payload.get("concept_sheet"),
                          glossary=payload.get("glossary"))
    out = {
        "title": book.title,
        "author": book.author,
        "quality_report": book.quality_report,
        "chapters": [{"title": c.title, "pairs": c.pairs} for c in book.chapters],
    }
    json.dump(out, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
