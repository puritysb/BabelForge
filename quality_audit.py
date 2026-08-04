"""Fail-closed terminology audit for precision translations."""
from __future__ import annotations

import json
import re
import sys


class QualityAuditError(RuntimeError):
    pass


_TAG_PATTERN = re.compile(
    r"</?(?:i|b|em|strong|span|sup|sub|a\b[^>]*)\s*>", re.IGNORECASE)
_HANGUL_PATTERN = re.compile(r"[가-힣]")
_LETTER_PATTERN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-žΑ-ωА-я]")


def _stash_tags(text: str, prefix: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}

    def replace(match: re.Match) -> str:
        key = f"◀{prefix}TAG{len(mapping)}▶"
        mapping[key] = match.group(0)
        return key

    return _TAG_PATTERN.sub(replace, text), mapping


def _restore_tags(text: str, mapping: dict[str, str]) -> str:
    for key, tag in mapping.items():
        text = text.replace(key, tag)
    return text


def _json_object(raw: str) -> dict | None:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(raw[start:end + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                pass
    return None


def _source_has(text: str, form: str) -> bool:
    escaped = re.escape(form).replace(r"\-", r"[-‐‑‒–— ]")
    if re.fullmatch(r"[\w\s'’\-]+", form, re.UNICODE):
        return re.search(rf"(?<!\w){escaped}(?!\w)", text, re.I) is not None
    return re.search(escaped, text, re.I) is not None


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", _TAG_PATTERN.sub("", value)).strip()


def _requires_korean_translation(source: str) -> bool:
    plain = _plain_text(source)
    return len(plain) >= 20 and len(_LETTER_PATTERN.findall(plain)) >= 8


def find_translation_integrity_violations(book) -> list[dict]:
    """Find deterministic omissions that a fluent-looking draft can hide."""
    violations: list[dict] = []
    for ci, chapter in enumerate(book.chapters):
        for pi, (source, korean) in enumerate(chapter.pairs):
            source_plain = _plain_text(source)
            korean_plain = _plain_text(korean)
            reasons: list[str] = []
            if not korean_plain:
                reasons.append("blank translation")
            if (_requires_korean_translation(source)
                    and source_plain.casefold() == korean_plain.casefold()):
                reasons.append("source copied unchanged")
            if (_requires_korean_translation(source)
                    and not _HANGUL_PATTERN.search(korean_plain)):
                reasons.append("substantive passage contains no Korean")
            if reasons:
                violations.append({
                    "chapter_index": ci,
                    "paragraph_index": pi,
                    "chapter_title": chapter.title,
                    "source": source,
                    "current_ko": korean,
                    "reasons": reasons,
                })
    return violations


def audit_translation_quality(book, chat_fn, chunk_size: int = 12) -> dict:
    """Review every pair for meaning, omissions, and malformed Korean.

    Batch-level proofreading is valuable but can still leave isolated copied
    quotations or plausible-looking typos (the live Nietzsche run contained
    ``비도델적`` and ``분척``).  This independent whole-book pass sees the final
    checkpoint in reading order and must return an explicit verdict for every
    pair.  Invalid/incomplete reviews fail closed.
    """
    rows = [
        {"chapter_index": ci, "paragraph_index": pi,
         "chapter_title": chapter.title, "source": source,
         "current_ko": korean}
        for ci, chapter in enumerate(book.chapters)
        for pi, (source, korean) in enumerate(chapter.pairs)
    ]
    corrected = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        payload_rows = []
        tag_maps: dict[tuple[int, int], dict[str, str]] = {}
        for offset, original in enumerate(chunk):
            row = dict(original)
            key = (row["chapter_index"], row["paragraph_index"])
            row["current_ko"], tag_maps[key] = _stash_tags(
                row["current_ko"], f"S{offset}")
            payload_rows.append(row)
        prompt = json.dumps({"paragraphs": payload_rows}, ensure_ascii=False)
        expected = {
            (row["chapter_index"], row["paragraph_index"]): row
            for row in chunk
        }
        accepted: dict[tuple[int, int], str] | None = None
        feedback = ""
        for attempt in range(3):
            user_payload = prompt
            if feedback:
                user_payload = json.dumps({
                    "paragraphs": payload_rows,
                    "validation_feedback": feedback,
                }, ensure_ascii=False)
            raw = chat_fn([
                {"role": "system", "content": (
                    "You are the final whole-book Korean translation editor. "
                    "Independently compare every source passage with current_ko. "
                    "Correct mistranslation, omission, broken logic, bad referents, "
                    "untranslated quotations (including embedded English or Latin), "
                    "Korean spelling/word-formation errors, duplicated syllables, and "
                    "unnatural model artifacts. Preserve a correct translation exactly; "
                    "do not rewrite for mere stylistic preference. Preserve every HTML "
                    "placeholder exactly. Return JSON only with exactly one verdict per "
                    "input: {\"paragraphs\":[{\"chapter_index\":0,"
                    "\"paragraph_index\":0,\"action\":\"keep|correct\","
                    "\"corrected_ko\":\"required only for correct\","
                    "\"reason\":\"short source-grounded reason\"}]}."
                )},
                {"role": "user", "content": user_payload},
            ])
            parsed = _json_object(raw)
            reviews = parsed.get("paragraphs") if parsed else None
            if not isinstance(reviews, list):
                feedback = "Return the required paragraphs array as valid JSON."
                continue
            candidate: dict[tuple[int, int], str] = {}
            valid = True
            for item in reviews:
                if not isinstance(item, dict):
                    valid = False
                    break
                try:
                    key = (int(item["chapter_index"]),
                           int(item["paragraph_index"]))
                except (KeyError, TypeError, ValueError):
                    valid = False
                    break
                if key not in expected or key in candidate:
                    valid = False
                    break
                action = str(item.get("action") or "").strip().lower()
                if action == "keep":
                    text = expected[key]["current_ko"]
                elif action == "correct":
                    stashed = str(item.get("corrected_ko") or "").strip()
                    mapping = tag_maps[key]
                    if (not stashed
                            or any(marker not in stashed for marker in mapping)):
                        valid = False
                        break
                    text = _restore_tags(stashed, mapping)
                else:
                    valid = False
                    break
                candidate[key] = text
            if valid and set(candidate) == set(expected):
                # Apply to a temporary view, then enforce obvious omissions
                # locally before accepting a model's keep/correct decisions.
                bad_keys = []
                for key, text in candidate.items():
                    source = expected[key]["source"]
                    if (_requires_korean_translation(source)
                            and (not _HANGUL_PATTERN.search(_plain_text(text))
                                 or _plain_text(source).casefold()
                                 == _plain_text(text).casefold())):
                        bad_keys.append(key)
                if not bad_keys:
                    accepted = candidate
                    break
                feedback = (
                    "These substantive passages remain untranslated or contain "
                    f"no Korean: {sorted(bad_keys)}. Correct them."
                )
            else:
                feedback = (
                    "Return exactly one unique, valid verdict for every supplied "
                    "chapter_index/paragraph_index pair."
                )
        if accepted is None:
            keys = sorted(expected)
            raise QualityAuditError(
                "whole-book semantic auditor failed closed for paragraph keys "
                f"{keys}"
            )
        for key, text in accepted.items():
            ci, pi = key
            source, previous = book.chapters[ci].pairs[pi]
            book.chapters[ci].pairs[pi] = (source, text)
            if text != previous:
                corrected += 1

    remaining = find_translation_integrity_violations(book)
    if remaining:
        preview = ", ".join(
            f"ch{row['chapter_index'] + 1}/p{row['paragraph_index'] + 1}"
            for row in remaining[:8]
        )
        raise QualityAuditError(
            f"{len(remaining)} translation integrity violation(s) remain: {preview}"
        )
    return {
        "semantic_paragraphs_reviewed": len(rows),
        "semantic_paragraphs_corrected": corrected,
        "integrity_violations_remaining": 0,
    }


def find_violations(
        book, concept_sheet: dict,
        contextual_exemptions: set[tuple[int, int, str, str]] | None = None,
        source_sense_exemptions: set[tuple[int, int, str]] | None = None,
        ) -> list[dict]:
    """Find strict-term failures, honoring reviewed paragraph-local exceptions.

    ``avoid_ko`` is a warning about how the *matched source concept* should not
    be translated. It is not a global ban on that Korean string everywhere in
    the paragraph: a neighboring source lemma can legitimately require it
    (Erkenntniss→인식 alongside Wissen→지식, for example). Exceptions are
    accepted only after the contextual review in ``audit_and_correct`` and are
    scoped to one paragraph, concept, and literal avoided term.
    """
    contextual_exemptions = contextual_exemptions or set()
    source_sense_exemptions = source_sense_exemptions or set()
    violations: list[dict] = []
    for ci, chapter in enumerate(book.chapters):
        for pi, (source, korean) in enumerate(chapter.pairs):
            for concept in concept_sheet.get("concepts") or []:
                if not concept.get("strict", True):
                    continue
                concept_id = str(concept.get("concept_id") or "")
                if (ci, pi, concept_id) in source_sense_exemptions:
                    continue
                forms = [str(v) for v in concept.get("source_terms") or []]
                matched = [form for form in forms if _source_has(source, form)]
                if not matched:
                    continue
                canonical = str(concept.get("canonical_ko") or "").strip()
                avoid_all = [str(v) for v in concept.get("avoid_ko") or []
                             if str(v)]
                avoided = [v for v in avoid_all
                           if str(v) and str(v) in korean
                           and (ci, pi, concept_id, str(v))
                           not in contextual_exemptions]
                if canonical not in korean or avoided:
                    violations.append({
                        "chapter_index": ci,
                        "paragraph_index": pi,
                        "chapter_title": chapter.title,
                        "concept_id": concept_id,
                        "source_terms": matched,
                        "canonical_ko": canonical,
                        "canonical_missing": canonical not in korean,
                        "avoid_ko_all": avoid_all,
                        "avoid_ko_found": avoided,
                        "source": source,
                        "current_ko": korean,
                        "rationale": concept.get("rationale", ""),
                    })
    return violations


def audit_and_correct(book, concept_sheet: dict, chat_fn,
                      chunk_size: int = 1) -> dict:
    """Contextually review/correct concept violations and fail if any remain.

    The reviewer may retain an ``avoid_ko`` string only when it translates a
    different source expression in the same paragraph. Such decisions are
    narrowly allow-listed for the final deterministic recheck; the canonical
    term itself is never exempted and must still be present.
    """
    before = find_violations(book, concept_sheet)
    corrected = 0
    contextual_exemptions: set[tuple[int, int, str, str]] = set()
    source_sense_exemptions: set[tuple[int, int, str]] = set()

    # One paragraph can violate multiple concept rules. Review and apply it
    # once so separate model rows cannot overwrite each other's corrections.
    grouped: dict[tuple[int, int], dict] = {}
    for violation in before:
        key = (violation["chapter_index"], violation["paragraph_index"])
        row = grouped.setdefault(key, {
            "chapter_index": key[0],
            "paragraph_index": key[1],
            "chapter_title": violation["chapter_title"],
            "source": violation["source"],
            "current_ko": violation["current_ko"],
            "violations": [],
        })
        row["violations"].append({
            key: violation[key] for key in (
                "concept_id", "source_terms", "canonical_ko",
                "canonical_missing", "avoid_ko_all", "avoid_ko_found",
                "rationale",
            )
        })
    paragraph_rows = list(grouped.values())

    for start in range(0, len(paragraph_rows), chunk_size):
        chunk = paragraph_rows[start:start + chunk_size]
        prompt_paragraphs = []
        tag_maps: dict[tuple[int, int], dict[str, str]] = {}
        for offset, paragraph in enumerate(chunk):
            row = dict(paragraph)
            key = (row["chapter_index"], row["paragraph_index"])
            row["current_ko"], tag_maps[key] = _stash_tags(
                row["current_ko"], f"Q{offset}")
            prompt_paragraphs.append(row)
        prompt = {
            "rules": [
                {"concept_id": c.get("concept_id"),
                 "source_terms": c.get("source_terms"),
                 "canonical_ko": c.get("canonical_ko"),
                 "avoid_ko": c.get("avoid_ko"),
                 "rationale": c.get("rationale")}
                for c in concept_sheet.get("concepts") or []
                if c.get("strict", True) and any(
                    _source_has(row["source"], str(form))
                    for row in chunk
                    for form in c.get("source_terms") or []
                )
            ],
            "paragraphs": prompt_paragraphs,
        }
        system_prompt = (
                "You are the final fail-closed contextual terminology auditor for "
                "a Korean literary translation. Review each supplied paragraph. "
                "The canonical Korean term for every matched concept must appear. "
                "An avoid_ko string is forbidden only when it mistranslates that "
                "matched concept; do not remove it when it faithfully translates "
                "a different source expression in the same paragraph. For example, "
                "Erkenntniss→인식 may legitimately coexist with Wissen→지식. "
                "Use action=correct when changing the paragraph and action=keep "
                "when it is already correct. A retained avoided string must be "
                "listed under contextual_exemptions with its concept_id, avoid_ko, "
                "the exact different source_expression that it translates, and a "
                "short source-grounded reason. The source_expression must occur "
                "verbatim in the source paragraph and must not be the matched "
                "concept itself. Check every string in avoid_ko_all after editing: "
                "never replace one forbidden synonym with another. When the "
                "canonical is already present and an avoided string belongs to a "
                "different source expression, preserve the Korean paragraph exactly "
                "and use action=keep. A canonical term may be absent only when the "
                "matched spelling is used in a clearly non-conceptual idiom. In that "
                "case, add source_sense_exemptions with an exact longer "
                "source_expression that contains the matched term verbatim (for "
                "example 'in Wahrheit') and a short reason. Never use this exception "
                "for stylistic preference or an exact standalone concept term. "
                "Preserve all source meaning and every HTML tag. "
                "Return exactly one row per input paragraph as JSON only: "
                "{\"paragraphs\":[{\"chapter_index\":0,"
                "\"paragraph_index\":0,\"action\":\"correct|keep\","
                "\"corrected_ko\":\"...\",\"contextual_exemptions\":[{"
                "\"concept_id\":\"...\",\"avoid_ko\":\"...\","
                "\"source_expression\":\"...\",\"reason\":\"...\"}],"
                "\"source_sense_exemptions\":[{\"concept_id\":\"...\","
                "\"source_expression\":\"in Wahrheit\","
                "\"reason\":\"idiomatic non-conceptual use\"}]}]}."
        )
        expected = {
            (row["chapter_index"], row["paragraph_index"]): row
            for row in chunk
        }
        matched_rules: dict[tuple[int, int], list[dict]] = {}
        for key, row in expected.items():
            matched_rules[key] = []
            for concept in concept_sheet.get("concepts") or []:
                if not concept.get("strict", True):
                    continue
                forms = [str(v) for v in concept.get("source_terms") or []]
                if not any(_source_has(row["source"], form) for form in forms):
                    continue
                matched_rules[key].append({
                    "concept_id": str(concept.get("concept_id") or ""),
                    "canonical_ko": str(concept.get("canonical_ko") or "").strip(),
                    "avoid_ko_all": [str(v) for v in concept.get("avoid_ko") or []
                                     if str(v)],
                })
        accepted_rows: list[tuple[
            tuple[int, int], str, set[tuple[int, int, str, str]],
            set[tuple[int, int, str]],
        ]] = []
        received: set[tuple[int, int]] = set()
        feedback = ""
        max_review_attempts = 3
        for review_attempt in range(max_review_attempts):
            attempt_prompt = dict(prompt)
            if feedback:
                attempt_prompt["validation_feedback"] = feedback
            raw = chat_fn([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(
                    attempt_prompt, ensure_ascii=False)},
            ])
            parsed = _json_object(raw)
            if parsed is None:
                feedback = (
                    "The previous response was not valid JSON. Return the exact "
                    "requested schema and every paragraph."
                )
                continue
            reviews = parsed.get("paragraphs")
            # Accept the previous correction-only schema as a conservative
            # compatibility path for callers/tests that do not need exemptions.
            if (not isinstance(reviews, list)
                    and isinstance(parsed.get("corrections"), list)):
                reviews = [dict(item, action="correct",
                                contextual_exemptions=[])
                           for item in parsed["corrections"]
                           if isinstance(item, dict)]
            if not isinstance(reviews, list):
                feedback = (
                    "The previous response omitted the top-level paragraphs list."
                )
                continue

            candidate_rows = []
            received = set()
            for item in reviews:
                if not isinstance(item, dict):
                    continue
                try:
                    key = (int(item["chapter_index"]),
                           int(item["paragraph_index"]))
                except (KeyError, TypeError, ValueError):
                    continue
                action = str(item.get("action") or "").strip().lower()
                text = str(item.get("corrected_ko") or "").strip()
                if key not in expected or key in received or not text \
                        or action not in {"correct", "keep"}:
                    continue
                mapping = tag_maps.get(key, {})
                if any(placeholder not in text for placeholder in mapping):
                    continue
                text = _restore_tags(text, mapping)
                expected_row = expected[key]
                original_text = expected_row["current_ko"]
                claimed = item.get("contextual_exemptions") or []
                claimed_senses = item.get("source_sense_exemptions") or []
                if (not isinstance(claimed, list)
                        or not isinstance(claimed_senses, list)):
                    continue
                accepted: set[tuple[int, int, str, str]] = set()
                accepted_senses: set[tuple[int, int, str]] = set()
                violations_by_id = {
                    str(v["concept_id"]): v
                    for v in expected_row["violations"]
                }
                for exemption in claimed:
                    if not isinstance(exemption, dict):
                        continue
                    concept_id = str(exemption.get("concept_id") or "")
                    avoid_ko = str(exemption.get("avoid_ko") or "")
                    source_expression = str(
                        exemption.get("source_expression") or "").strip()
                    reason = str(exemption.get("reason") or "").strip()
                    violation = violations_by_id.get(concept_id)
                    if (not violation or not reason or not source_expression
                            or avoid_ko not in violation["avoid_ko_all"]
                            or (avoid_ko not in text
                                and avoid_ko not in original_text)
                            or violation["canonical_ko"] not in original_text
                            or not _source_has(
                                expected_row["source"], source_expression)
                            or any(source_expression.casefold()
                                   == term.casefold()
                                   for term in violation["source_terms"])):
                        continue
                    accepted.add((key[0], key[1], concept_id, avoid_ko))

                for exemption in claimed_senses:
                    if not isinstance(exemption, dict):
                        continue
                    concept_id = str(exemption.get("concept_id") or "")
                    source_expression = str(
                        exemption.get("source_expression") or "").strip()
                    reason = str(exemption.get("reason") or "").strip()
                    violation = violations_by_id.get(concept_id)
                    if (not violation or not reason or not source_expression
                            or not _source_has(
                                expected_row["source"], source_expression)
                            or any(source_expression.casefold()
                                   == term.casefold()
                                   for term in violation["source_terms"])
                            or not any(_source_has(source_expression, term)
                                       for term in violation["source_terms"])):
                        continue
                    accepted_senses.add((key[0], key[1], concept_id))

                # If every original problem was merely a contextually justified
                # avoid-string collision and no canonical term was missing, the
                # review is classificatory: preserve the original paragraph.
                # This prevents an unnecessary rewrite from breaking another
                # already-correct concept in the same sentence.
                classification_complete = all(
                    not violation["canonical_missing"] and all(
                        (key[0], key[1], str(violation["concept_id"]), avoid_ko)
                        in accepted
                        for avoid_ko in violation["avoid_ko_found"]
                    )
                    for violation in expected_row["violations"]
                )
                if classification_complete:
                    text = original_text

                # Validate against the complete forbidden list, including a
                # different forbidden synonym introduced by this very edit.
                valid = True
                for rule in matched_rules[key]:
                    concept_id = rule["concept_id"]
                    if (key[0], key[1], concept_id) in accepted_senses:
                        continue
                    if rule["canonical_ko"] not in text:
                        valid = False
                        break
                    for avoid_ko in rule["avoid_ko_all"]:
                        if (avoid_ko in text
                                and (key[0], key[1], concept_id, avoid_ko)
                                not in accepted):
                            valid = False
                            break
                    if not valid:
                        break
                if not valid:
                    continue
                candidate_rows.append(
                    (key, text, accepted, accepted_senses))
                received.add(key)

            if received == set(expected):
                accepted_rows = candidate_rows
                break
            missing = sorted(set(expected) - received)
            feedback = (
                "Local fail-closed validation rejected paragraph keys "
                f"{missing}. Ensure every canonical term remains, inspect every "
                "string in avoid_ko_all after editing, and provide an exact "
                "source_expression for each retained avoided string. Do not "
                "replace one avoided synonym with another. If the matched source "
                "term is genuinely idiomatic/non-conceptual, provide an exact "
                "longer source_sense_exemption expression containing that term."
            )
            if review_attempt + 1 < max_review_attempts:
                sys.stderr.write(
                    "[quality-audit] local validation rejected paragraph keys "
                    f"{missing}; focused retry {review_attempt + 2}/"
                    f"{max_review_attempts}\n"
                )

        if received != set(expected):
            missing = sorted(set(expected) - received)
            raise QualityAuditError(
                f"final terminology auditor reviewed {len(received)}/{len(expected)} "
                f"paragraphs; invalid keys: {missing}"
            )
        for key, text, accepted, accepted_senses in accepted_rows:
            ci, pi = key
            source, previous_ko = book.chapters[ci].pairs[pi]
            book.chapters[ci].pairs[pi] = (source, text)
            contextual_exemptions.update(accepted)
            source_sense_exemptions.update(accepted_senses)
            if text != previous_ko:
                corrected += 1

    after = find_violations(
        book, concept_sheet, contextual_exemptions, source_sense_exemptions)
    if after:
        preview = ", ".join(
            f"ch{v['chapter_index'] + 1}/p{v['paragraph_index'] + 1}:"
            f"{v['concept_id']}" for v in after[:8]
        )
        raise QualityAuditError(
            f"{len(after)} strict terminology violation(s) remain after correction: {preview}"
        )
    return {
        "mode": "precision",
        "strict_concepts": sum(
            1 for c in concept_sheet.get("concepts") or [] if c.get("strict", True)
        ),
        "violations_found": len(before),
        "paragraphs_reviewed": len(paragraph_rows),
        "paragraphs_corrected": corrected,
        "contextual_exemptions": len(contextual_exemptions),
        "source_sense_exemptions": len(source_sense_exemptions),
        "violations_remaining": 0,
        "passed": True,
    }
