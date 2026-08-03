"""Fail-closed terminology audit for precision translations."""
from __future__ import annotations

import json
import re


class QualityAuditError(RuntimeError):
    pass


_TAG_PATTERN = re.compile(
    r"</?(?:i|b|em|strong|span|sup|sub|a\b[^>]*)\s*>", re.IGNORECASE)


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


def find_violations(book, concept_sheet: dict) -> list[dict]:
    violations: list[dict] = []
    for ci, chapter in enumerate(book.chapters):
        for pi, (source, korean) in enumerate(chapter.pairs):
            for concept in concept_sheet.get("concepts") or []:
                if not concept.get("strict", True):
                    continue
                forms = [str(v) for v in concept.get("source_terms") or []]
                matched = [form for form in forms if _source_has(source, form)]
                if not matched:
                    continue
                canonical = str(concept.get("canonical_ko") or "").strip()
                avoided = [str(v) for v in concept.get("avoid_ko") or []
                           if str(v) and str(v) in korean]
                if canonical not in korean or avoided:
                    violations.append({
                        "chapter_index": ci,
                        "paragraph_index": pi,
                        "chapter_title": chapter.title,
                        "concept_id": concept.get("concept_id"),
                        "source_terms": matched,
                        "canonical_ko": canonical,
                        "avoid_ko_found": avoided,
                        "source": source,
                        "current_ko": korean,
                        "rationale": concept.get("rationale", ""),
                    })
    return violations


def audit_and_correct(book, concept_sheet: dict, chat_fn,
                      chunk_size: int = 12) -> dict:
    """Correct concept violations with the LLM and fail if any remain."""
    before = find_violations(book, concept_sheet)
    corrected = 0
    for start in range(0, len(before), chunk_size):
        chunk = before[start:start + chunk_size]
        prompt_violations = []
        tag_maps: dict[tuple[int, int], dict[str, str]] = {}
        for offset, violation in enumerate(chunk):
            row = dict(violation)
            key = (row["chapter_index"], row["paragraph_index"])
            row["current_ko"], tag_maps[key] = _stash_tags(
                row["current_ko"], f"Q{offset}")
            prompt_violations.append(row)
        prompt = {
            "rules": [
                {"concept_id": c.get("concept_id"),
                 "source_terms": c.get("source_terms"),
                 "canonical_ko": c.get("canonical_ko"),
                 "avoid_ko": c.get("avoid_ko"),
                 "rationale": c.get("rationale")}
                for c in concept_sheet.get("concepts") or []
                if c.get("strict", True)
            ],
            "violations": prompt_violations,
        }
        raw = chat_fn([
            {"role": "system", "content": (
                "You are the final fail-closed terminology auditor for a Korean "
                "literary translation. Correct each supplied Korean paragraph so "
                "the canonical concept is used, forbidden alternatives disappear, "
                "and the sentence remains faithful and natural. Do not alter HTML "
                "tags or omit source meaning. Return JSON only as "
                "{\"corrections\":[{\"chapter_index\":0,"
                "\"paragraph_index\":0,\"corrected_ko\":\"...\"}]}."
            )},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ])
        parsed = _json_object(raw)
        if parsed is None or not isinstance(parsed.get("corrections"), list):
            raise QualityAuditError("final terminology auditor returned invalid JSON")
        expected = {(v["chapter_index"], v["paragraph_index"]) for v in chunk}
        received: set[tuple[int, int]] = set()
        for item in parsed["corrections"]:
            if not isinstance(item, dict):
                continue
            try:
                key = (int(item["chapter_index"]), int(item["paragraph_index"]))
            except (KeyError, TypeError, ValueError):
                continue
            text = str(item.get("corrected_ko") or "").strip()
            if key not in expected or not text:
                continue
            mapping = tag_maps.get(key, {})
            if any(placeholder not in text for placeholder in mapping):
                continue
            text = _restore_tags(text, mapping)
            ci, pi = key
            source, _ = book.chapters[ci].pairs[pi]
            book.chapters[ci].pairs[pi] = (source, text)
            received.add(key)
            corrected += 1
        if received != expected:
            raise QualityAuditError(
                f"final terminology auditor corrected {len(received)}/{len(expected)} paragraphs"
            )

    after = find_violations(book, concept_sheet)
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
        "paragraphs_corrected": corrected,
        "violations_remaining": 0,
        "passed": True,
    }
