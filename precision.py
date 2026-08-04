"""Autonomous precision research for source editions and book concepts.

This module is intentionally independent of the pipeline and translation
engine.  Callers inject the LLM and web-search functions, which keeps the
research logic testable and lets the pipeline fail closed when the mandatory
precision preflight cannot be completed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from difflib import SequenceMatcher
from typing import Callable


PRECISION_SCHEMA_VERSION = 1
PRECISION_PROFILE_VERSION = "precision-v1"
MAX_CONCEPTS = 24
MAX_WEB_CONCEPTS = 12
_DISTINCT_ROLE_HINTS = ("denker", "thinker", "penseur")


class PrecisionError(RuntimeError):
    pass


_LATIN_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,}")
_COMMON = frozenset(
    "the and for that with this from have not but are was were been being into "
    "than then there their they them his her she him you your our out who what "
    "when where which would could should will may might can one two all any some "
    "more most much such only very also even still just every because therefore "
    "thus chapter book part section footnote state nature people many without "
    "after before through under over again about upon while how why each other "
    "der die das den dem des ein eine einer eines und oder aber als auch auf aus "
    "bei für mit nach nicht sich von vor wie was wer zur zum ist sind war waren "
    "dans les des une pour avec pas que qui est sur par plus cette sont aux au "
    "los las una para con por del que como más esta este son".split()
)


def _as_dict(candidate) -> dict:
    if is_dataclass(candidate):
        return asdict(candidate)
    return dict(candidate)


def _json_object(raw: str) -> dict | None:
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(raw[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def _call_json(chat_fn: Callable, system: str, user: str, stage: str) -> dict:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    failures: list[str] = []
    raw = ""
    try:
        response = chat_fn(messages)
        raw = response if isinstance(response, str) else ""
    except Exception as exc:
        failures.append(f"initial call: {exc}")
    parsed = _json_object(raw)
    if parsed is not None:
        return parsed

    # Precise mode repairs malformed/truncated structured output autonomously
    # instead of asking the user or silently dropping the research stage. Never
    # send an empty model response as the next user message: Z.ai reports that
    # invalid request as business code 1213. Wrapping malformed output in a
    # non-empty JSON object also keeps control text separate from model output.
    if raw.strip():
        try:
            repaired = chat_fn([
                {"role": "system", "content": (
                    "You repair malformed model output into strict valid JSON. "
                    "Preserve the recoverable decisions, remove prose and "
                    "markdown fences, close truncated structures, and keep "
                    "rationales under 60 words. Return one JSON object only."
                )},
                {"role": "user", "content": json.dumps({
                    "stage": stage,
                    "malformed_output": raw,
                }, ensure_ascii=False)},
            ])
        except Exception as exc:
            failures.append(f"repair call: {exc}")
        else:
            parsed = _json_object(repaired)
            if parsed is not None:
                return parsed
            failures.append("repair call returned invalid JSON")

    # A repair request can itself be rejected or may have nothing recoverable
    # when the first response was empty. Re-run the original task as a fresh
    # structured-generation request, preserving its complete evidence payload.
    # This is safer than slicing malformed JSON at arbitrary character offsets,
    # which could silently discard a late but important concept.
    regeneration_system = (
        system
        + "\n\nThis is a fresh JSON regeneration after an unusable prior response. "
          "Return one complete JSON object only. Do not mention the prior attempt."
    )
    regeneration_user = user if isinstance(user, str) and user.strip() else "{}"
    try:
        regenerated = chat_fn([
            {"role": "system", "content": regeneration_system},
            {"role": "user", "content": regeneration_user},
        ])
    except Exception as exc:
        failures.append(f"regeneration call: {exc}")
    else:
        parsed = _json_object(regenerated)
        if parsed is not None:
            return parsed
        failures.append("regeneration call returned invalid JSON")

    detail = "; ".join(failures) or "all responses were empty"
    raise PrecisionError(
        f"{stage} returned no valid JSON after repair and regeneration: {detail}"
    )


def _normalise_lang(value: str | None) -> str:
    value = (value or "").strip().lower().replace("_", "-")
    aliases = {
        "english": "en", "german": "de", "deutsch": "de",
        "french": "fr", "japanese": "ja", "chinese": "zh",
        "spanish": "es", "russian": "ru", "italian": "it",
    }
    return aliases.get(value, value.split("-")[0])


def research_source(candidate, chat_fn: Callable, search_fn: Callable) -> dict:
    """Research the work's original title/language and relay-translation risk."""
    cand = _as_dict(candidate)
    title = cand.get("title", "")
    author = cand.get("author", "")
    source_lang = _normalise_lang(cand.get("language") or "en")
    query = f'"{title}" "{author}" original language original title'[:180]
    results = search_fn(query, count=6) or []
    evidence = [
        {"title": r.get("title", ""), "url": r.get("link", ""),
         "snippet": r.get("content", "")[:700]}
        for r in results[:6] if isinstance(r, dict)
    ]
    payload = _call_json(
        chat_fn,
        "You are a bibliographic researcher. Determine the work's original "
        "language and original title from the supplied catalog metadata and web "
        "evidence. Distinguish the author from an edition translator. Never guess "
        "an original title from literal word substitution. Return JSON only with "
        "original_language (ISO 639-1), original_title, source_is_translation "
        "(boolean), confidence (high|medium|low), and reasoning.",
        json.dumps({"candidate": cand, "web_evidence": evidence},
                   ensure_ascii=False),
        "source research",
    )
    original_lang = _normalise_lang(payload.get("original_language"))
    if not original_lang:
        original_lang = source_lang
    confidence = str(payload.get("confidence", "low")).lower()
    is_translation = bool(payload.get("source_is_translation"))
    if original_lang != source_lang:
        is_translation = True
    return {
        "source_language": source_lang,
        "original_language": original_lang,
        "original_title": str(payload.get("original_title") or title).strip(),
        "source_is_translation": is_translation,
        "confidence": confidence if confidence in {"high", "medium", "low"} else "low",
        "reasoning": str(payload.get("reasoning") or "").strip(),
        "evidence": evidence,
    }


def choose_original_candidate(candidate, research: dict,
                              candidate_search_fn: Callable) -> tuple[dict, bool]:
    """Choose an original-language public-domain candidate when one is found.

    `candidate_search_fn(query)` must return candidate objects/dicts.  Local and
    opt-in Anna's Archive inputs are never silently replaced.
    """
    cand = _as_dict(candidate)
    if cand.get("source") in {"local", "annas_archive"}:
        return cand, False
    original_lang = _normalise_lang(research.get("original_language"))
    source_lang = _normalise_lang(cand.get("language"))
    if (research.get("confidence") not in {"high", "medium"}
            or not research.get("source_is_translation") or not original_lang
            or original_lang == source_lang):
        return cand, False

    title = str(research.get("original_title") or cand.get("title") or "")
    author = str(cand.get("author") or "")
    candidates = candidate_search_fn(f"{title} {author}".strip()) or []
    author_tokens = {t.lower() for t in _LATIN_WORD.findall(author)
                     if t.lower() not in _COMMON}
    title_tokens = {t.lower() for t in _LATIN_WORD.findall(title)
                    if t.lower() not in _COMMON}

    scored: list[tuple[float, dict]] = []
    for item in candidates:
        d = _as_dict(item)
        if _normalise_lang(d.get("language")) != original_lang:
            continue
        score = 10.0
        item_author = str(d.get("author") or "").lower()
        author_match = any(token in item_author for token in author_tokens)
        if author_match:
            score += 5.0
        item_title_tokens = {t.lower() for t in _LATIN_WORD.findall(
            str(d.get("title") or ""))}
        title_overlap = (len(title_tokens & item_title_tokens) / len(title_tokens)
                         if title_tokens else 0.0)
        if title_tokens:
            score += 5.0 * title_overlap
        if not author_match and title_overlap < 0.5:
            continue
        if d.get("rights") == "public domain":
            score += 1.0
        scored.append((score, d))
    if not scored:
        return cand, False
    scored.sort(key=lambda row: row[0], reverse=True)
    chosen = scored[0][1]
    extra = dict(chosen.get("extra") or {})
    extra["precision_source_replaced"] = {
        "selected_source": {k: cand.get(k) for k in
                            ("source", "source_id", "title", "language")},
        "reason": research,
    }
    chosen["extra"] = extra
    return chosen, True


def _book_text(chapters: list) -> str:
    return "\n\n".join(
        str(getattr(ch, "title", "")) + "\n" +
        "\n".join(str(p) for p in getattr(ch, "paragraphs", []))
        for ch in chapters
    )


def source_fingerprint(chapters: list) -> str:
    """Hash the exact extracted source units for safe concept-sheet resume."""
    digest = hashlib.sha1()
    for chapter in chapters:
        digest.update(str(getattr(chapter, "title", "")).encode("utf-8"))
        digest.update(b"\x00")
        for paragraph in getattr(chapter, "paragraphs", []):
            digest.update(str(paragraph).encode("utf-8"))
            digest.update(b"\x00")
    return digest.hexdigest()


def _representative_sample(chapters: list, max_chars: int = 30000) -> str:
    blocks: list[str] = []
    for ch in chapters:
        paragraphs = list(getattr(ch, "paragraphs", []))
        if not paragraphs:
            continue
        indexes = sorted({0, min(1, len(paragraphs) - 1), len(paragraphs) // 2,
                          len(paragraphs) - 1})
        excerpt = "\n".join(paragraphs[i] for i in indexes)
        blocks.append(f"## {getattr(ch, 'title', '')}\n{excerpt}")
    sample = "\n\n".join(blocks)
    if len(sample) <= max_chars:
        return sample
    # Evenly sample the assembled text rather than keeping only the beginning.
    width = max_chars // 3
    middle = max(0, len(sample) // 2 - width // 2)
    return sample[:width] + "\n[…middle…]\n" + sample[middle:middle + width] + \
        "\n[…end…]\n" + sample[-width:]


def extract_repeated_phrases(text: str, limit: int = 45) -> list[dict]:
    """Find lowercase multiword terms as well as capitalized proper names."""
    tokens = _LATIN_WORD.findall(re.sub(r"<[^>]+>", " ", text))
    lowered = [t.lower().strip("-'’") for t in tokens]
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for n in (2, 3):
        for i in range(0, len(tokens) - n + 1):
            low = lowered[i:i + n]
            if any(not w or w in _COMMON for w in low):
                continue
            phrase = " ".join(low)
            counts[phrase] += 1
            display.setdefault(phrase, " ".join(tokens[i:i + n]))
    for token, low in zip(tokens, lowered):
        if token[:1].isupper() and low not in _COMMON:
            counts[low] += 1
            display.setdefault(low, token)
    ranked = sorted(
        ((term, count) for term, count in counts.items() if count >= 3),
        key=lambda row: (-row[1] * (1 + row[0].count(" ")), row[0]),
    )
    return [{"term": display[t], "count": c} for t, c in ranked[:limit]]


def _contexts(text: str, terms: list[str], per_term: int = 2) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for term in terms:
        if not term:
            continue
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        found = []
        for match in pattern.finditer(text):
            start = max(0, match.start() - 180)
            end = min(len(text), match.end() + 220)
            found.append(re.sub(r"\s+", " ", text[start:end]).strip())
            if len(found) >= per_term:
                break
        if found:
            out[term] = found
    return out


def _draft_concepts(chapters: list, title: str, author: str, source_lang: str,
                    source_research: dict, chat_fn: Callable) -> list[dict]:
    full_text = _book_text(chapters)
    repeated = extract_repeated_phrases(full_text)
    sample = _representative_sample(chapters)
    response = _call_json(
        chat_fn,
        "You are the terminology analyst for a precision literary translation. "
        "Identify only high-impact recurring proper names, technical terms, "
        "philosophical concepts, motifs, titles, and deliberately contrasted "
        "terms. Include lowercase multiword and hyphenated expressions. If the "
        "source is itself a translation, recover the underlying original-language "
        "lemma only when evidence supports it and keep ambiguous source forms "
        "separate. Exclude ordinary discourse words. Return JSON only as "
        "{\"concepts\":[{\"source_terms\":[...],\"original_terms\":[...],"
        "\"kind\":\"concept|name|place|title|motif|other\","
        "\"why_important\":\"...\",\"search_query\":\"...\"}]}. "
        f"Return at most {MAX_CONCEPTS} concepts.",
        json.dumps({
            "book": {"title": title, "author": author,
                     "source_language": source_lang},
            "source_research": source_research,
            "repeated_candidates": repeated,
            "representative_excerpts": sample,
        }, ensure_ascii=False),
        "concept extraction",
    )
    concepts = response.get("concepts")
    if not isinstance(concepts, list):
        raise PrecisionError("concept extraction omitted concepts list")
    return [c for c in concepts[:MAX_CONCEPTS] if isinstance(c, dict)]


def _first_source_term(concept: dict) -> str:
    terms = concept.get("source_terms") or []
    if isinstance(terms, str):
        return terms.strip()
    if isinstance(terms, list) and terms:
        return str(terms[0]).strip()
    return ""


def _form_list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in (value or []) if str(item).strip()]


def _lexically_related(left: str, right: str, threshold: float = 0.72) -> bool:
    left = left.casefold().strip("-'’")
    right = right.casefold().strip("-'’")
    if not left or not right:
        return False
    return (left in right or right in left
            or SequenceMatcher(None, left, right).ratio() >= threshold)


def _is_distinct_role_neighbor(term: str) -> bool:
    """True for prefixed role compounds, not generic Denker/Denkers forms."""
    low = term.casefold()
    for hint in _DISTINCT_ROLE_HINTS:
        position = low.find(hint)
        if position >= 3 and low[:position].strip("-'’"):
            return True
    return False


def _cohesive_source_forms(forms: list[str]) -> list[str]:
    """Keep only one lexical family inside a deterministic strict mapping.

    A model may group phrases that merely share an inflected adjective, such
    as German ``freier Geist`` and ``freien Willen``. A single canonical target
    cannot safely enforce both. Prefer the largest head-noun family for
    multiword terms, then admit single-word compounds only when they contain or
    closely resemble that head. With single-word-only concepts, keep the
    largest mutually similar lemma family.
    """
    if len(forms) < 2:
        return forms
    tokenised = [(form, _LATIN_WORD.findall(form)) for form in forms]
    multi = [(form, tokens) for form, tokens in tokenised if len(tokens) > 1]
    if multi:
        heads = [tokens[-1] for _, tokens in multi]
        clusters: list[list[int]] = []
        for index, head in enumerate(heads):
            for cluster in clusters:
                if _lexically_related(head, heads[cluster[0]]):
                    cluster.append(index)
                    break
            else:
                clusters.append([index])
        chosen = max(clusters, key=lambda cluster: (len(cluster), -cluster[0]))
        reference_heads = [heads[index] for index in chosen]
        related_single_words = [
            tokens[0]
            for _, tokens in tokenised
            if len(tokens) == 1
            and any(_lexically_related(tokens[0], head)
                    for head in reference_heads)
        ]
        # When both a base compound and a longer derivative are present,
        # retain ordinary inflections (Freigeist/Freigeister) but do not make
        # a separate abstract formation (Freigeisterei) share one literal
        # Korean target merely because it contains the same head noun.
        single_base_length = (
            min(len(word) for word in related_single_words)
            if related_single_words else None
        )
        out = []
        for form, tokens in tokenised:
            if len(tokens) > 1:
                if any(_lexically_related(tokens[-1], head)
                       for head in reference_heads):
                    out.append(form)
            elif tokens and any(_lexically_related(tokens[0], head)
                                for head in reference_heads) and (
                                    single_base_length is None
                                    or len(tokens[0]) <= single_base_length + 2
                                ):
                out.append(form)
        return out

    words = [(form, tokens[0]) for form, tokens in tokenised if tokens]
    clusters: list[list[int]] = []
    for index, (_, word) in enumerate(words):
        for cluster in clusters:
            if _lexically_related(word, words[cluster[0]][1]):
                cluster.append(index)
                break
        else:
            clusters.append([index])
    chosen = max(clusters, key=lambda cluster: (len(cluster), -cluster[0]))
    base_length = min(len(words[index][1]) for index in chosen)
    return [words[index][0] for index in chosen
            if len(words[index][1]) <= base_length + 2]


def _lemma_signatures(concept: dict) -> list[str]:
    return [
        re.sub(r"[^a-zà-öø-ÿ]", "", form.casefold())
        for form in (_form_list(concept.get("source_terms"))
                     + _form_list(concept.get("original_terms")))
        if form.strip()
    ]


def _reconcile_lexical_targets(concepts: list[dict]) -> list[dict]:
    """Unify contradictory targets for near-identical inflectional lemmas."""
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    for right_index in range(1, len(concepts)):
        right = concepts[right_index]
        for left_index in range(right_index):
            left = concepts[left_index]
            same_family = any(
                a and b and SequenceMatcher(None, a, b).ratio() >= 0.90
                for a in _lemma_signatures(left)
                for b in _lemma_signatures(right)
            )
            if not same_family or left["canonical_ko"] == right["canonical_ko"]:
                continue
            left_rank = confidence_rank.get(left.get("confidence"), 0)
            right_rank = confidence_rank.get(right.get("confidence"), 0)
            winner, loser = ((right, left) if right_rank > left_rank
                             else (left, right))
            replaced = loser["canonical_ko"]
            loser["canonical_ko"] = winner["canonical_ko"]
            loser["avoid_ko"] = [
                value for value in loser.get("avoid_ko") or []
                if value != winner["canonical_ko"]
            ]
            if replaced and replaced != winner["canonical_ko"]:
                loser["avoid_ko"].append(replaced)
            loser["avoid_ko"] = list(dict.fromkeys(loser["avoid_ko"]))
            loser["rationale"] = (
                str(loser.get("rationale") or "").strip()
                + " Harmonized with the higher-confidence inflectional lemma."
            ).strip()
    return concepts


def _related_lexical_candidates(text: str, concepts: list[dict],
                                limit: int = 12) -> list[dict]:
    """Surface rare near-neighbor lemmas that frequency ranking would miss.

    Philosophical distinctions often hinge on a rare sibling form (for example
    Freigeist versus Freidenker), so a once-used term can be more important than
    the thirtieth most frequent noun.
    """
    tokens = Counter(_LATIN_WORD.findall(re.sub(r"<[^>]+>", " ", text)))
    known = {
        form.casefold()
        for concept in concepts
        for form in (_form_list(concept.get("source_terms"))
                     + _form_list(concept.get("original_terms")))
    }
    known_parts = {
        token.casefold()
        for concept in concepts
        for form in (_form_list(concept.get("source_terms"))
                     + _form_list(concept.get("original_terms")))
        for token in _LATIN_WORD.findall(form)
    }
    ranked: list[tuple[tuple[int, float, int, int], dict]] = []
    seen: set[str] = set()

    # Thinker-role terms are semantically high-risk even when the draft model
    # never supplied a usable lexical anchor. Register them first with the
    # highest priority so a lower-scored similarity hit cannot claim the same
    # token and leave it outside the bounded candidate list.
    for token, count in tokens.items():
        low = token.casefold()
        if low in known or low in seen or not _is_distinct_role_neighbor(token):
            continue
        seen.add(low)
        ranked.append(((100, 1.0, 0, -count), {
            "source_terms": [token],
            "original_terms": [token],
            "kind": "related_candidate",
            "why_important": (
                "Distinct thinker-role lemma found by whole-book scan; keep it "
                "separate from neighboring spirit, mind, or ideology terms."
            ),
            "search_query": f'"{token}" literary concept Korean translation',
            "source_count": count,
        }))

    for concept in concepts:
        forms = (_form_list(concept.get("source_terms"))
                 + _form_list(concept.get("original_terms")))
        single = [form for form in forms if len(_LATIN_WORD.findall(form)) == 1]
        if single:
            # The shortest supplied lemma is usually the base form (Freigeist),
            # while longer forms are inflections (Freigeister/Freigeistes).
            anchor_form = min(single, key=lambda form: (len(form), form.casefold()))
        else:
            anchor_form = _first_source_term(concept).split()[-1]
        anchor = anchor_form.casefold()
        if len(anchor) < 5:
            continue
        for token, count in tokens.items():
            low = token.casefold()
            if low in known or low in seen or low in _COMMON or len(low) < 5:
                continue
            if anchor_form[:1].isupper() and not token[:1].isupper():
                continue
            common_prefix = 0
            for left, right in zip(anchor, low):
                if left != right:
                    break
                common_prefix += 1
            similarity = SequenceMatcher(None, anchor, low).ratio()
            if common_prefix < 4 and similarity < 0.78:
                continue
            if similarity > 0.86:
                continue
            # Only the small set of plausible lexical neighbors reaches the
            # more expensive inflection/component comparison.
            if any(_lexically_related(low, part, threshold=0.82)
                   for part in known_parts):
                continue
            seen.add(low)
            row = {
                "source_terms": [token],
                "original_terms": [token],
                "kind": "related_candidate",
                "why_important": (
                    f"Rare lexical neighbor of {anchor}; verify whether the author "
                    "uses it as a distinct concept rather than a synonym."
                ),
                "search_query": f'"{token}" "{anchor}" literary concept Korean',
                "source_count": count,
            }
            score = (common_prefix, similarity, -abs(len(anchor) - len(low)),
                     -count)
            ranked.append((score, row))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in ranked[:limit]]


def _vet_related_candidates(candidates: list[dict], text: str, title: str,
                            author: str, chat_fn: Callable,
                            limit: int = 8) -> list[dict]:
    """Keep rare neighbors that are genuine concepts, not spelling noise.

    Similarity search deliberately over-generates. This focused pass decides
    which rare terms deserve mandatory independent treatment before the larger
    adjudication can spend all of its concept budget on frequent terms.
    """
    if not candidates:
        return []
    contexts = _contexts(
        text, [_first_source_term(candidate) for candidate in candidates],
        per_term=2,
    )
    response = _call_json(
        chat_fn,
        "You screen rare lexical neighbors for a precision literary translation. "
        "Keep only genuine philosophical, intellectual, moral, or technical "
        "concepts and intentionally contrasted categories. Reject mere inflections, "
        "adverbs, names, typographical resemblance, and incidental compounds. "
        "A rare term can be important even if it occurs once. Preserve distinct "
        "lemmas such as a word for an independent spirit versus a conventional "
        "freethinker as separate candidates. Return JSON only as "
        "{\"candidates\":[{\"source_terms\":[\"exact source form\"]}]}, "
        f"with no more than {limit} candidates.",
        json.dumps({
            "book": {"title": title, "author": author},
            "candidates": candidates,
            "source_contexts": contexts,
        }, ensure_ascii=False),
        "rare lexical-neighbor screening",
    )
    allowed = {
        _first_source_term(candidate).casefold(): candidate
        for candidate in candidates if _first_source_term(candidate)
    }
    # A distinct word for a thinker/intellectual role is exactly the kind of
    # once-used neighbor that gets flattened into a more frequent spirit/mind
    # term. Keep it in mandatory adjudication even when this screening model
    # underrates the isolated context; web evidence and later critics still
    # decide its Korean rendering.
    selected = [
        candidate for candidate in candidates
        if _is_distinct_role_neighbor(_first_source_term(candidate))
    ][:limit]
    for item in response.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        key = _first_source_term(item).casefold()
        candidate = allowed.get(key)
        if candidate and candidate not in selected:
            selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _web_evidence(concepts: list[dict], title: str, author: str,
                  search_fn: Callable) -> tuple[list[dict], set[str]]:
    related = [concept for concept in concepts
               if concept.get("kind") == "related_candidate"]
    regular = [concept for concept in concepts
               if concept.get("kind") != "related_candidate"]
    related = related[:min(8, MAX_WEB_CONCEPTS)]
    selected = regular[:MAX_WEB_CONCEPTS - len(related)] + related
    ordered: dict[int, dict] = {}
    allowed_urls: set[str] = set()

    def lookup(index: int, concept: dict) -> tuple[int, dict]:
        term = _first_source_term(concept)
        query = str(concept.get("search_query") or
                    f'"{term}" "{title}" {author} 한국어 번역')[:180]
        if not query.strip():
            return index, {"term": term, "query": query, "results": []}
        results = search_fn(query, count=4) or []
        rows = []
        for result in results[:4]:
            if not isinstance(result, dict):
                continue
            url = str(result.get("link") or "")
            rows.append({
                "title": str(result.get("title") or "")[:220],
                "url": url,
                "snippet": str(result.get("content") or "")[:900],
            })
        return index, {"term": term, "query": query, "results": rows}

    # Each MCP search opens its own session, so bounded parallel research is
    # safe and avoids adding several sequential minutes to every book.
    with ThreadPoolExecutor(max_workers=min(4, len(selected) or 1)) as executor:
        futures = [executor.submit(lookup, i, concept)
                   for i, concept in enumerate(selected)]
        for future in as_completed(futures):
            index, row = future.result()
            ordered[index] = row
            for result in row["results"]:
                if result["url"]:
                    allowed_urls.add(result["url"])
    evidence = [ordered[i] for i in range(len(selected)) if i in ordered]
    return evidence, allowed_urls


def _validate_concepts(raw: list, full_text: str,
                       allowed_urls: set[str]) -> list[dict]:
    concepts: list[dict] = []
    seen_forms: set[str] = set()
    for item in raw[:MAX_CONCEPTS]:
        if not isinstance(item, dict):
            continue
        original_terms = _form_list(item.get("original_terms"))
        forms = _form_list(item.get("source_terms"))
        forms = [v for v in forms if re.search(re.escape(v), full_text, re.I)]
        # When a concept already has a complete compound or multiword form,
        # reject lowercase one-word fragments such as German inflected
        # adjectives ("freie", "gebundenen"). They are not safe global keys.
        has_complete_form = any(" " in v or v[:1].isupper() or "-" in v
                                for v in forms)
        if has_complete_form:
            forms = [v for v in forms
                     if " " in v or v[:1].isupper() or "-" in v]
        cohesive = set(_cohesive_source_forms(forms + original_terms))
        forms = [form for form in forms if form in cohesive]
        forms = [v for v in forms if v.casefold() not in seen_forms]
        canonical = str(item.get("canonical_ko") or "").strip()
        # A strict mapping must have exactly one literal target. Paired choices
        # belong in separate concept objects, never "A / B" or parenthetical
        # alternatives that the deterministic auditor could not enforce.
        has_alternative_target = bool(
            re.search(r"\s[/|]\s|(?:또는|혹은)", canonical)
            or re.search(r"\([^)]*(?:들|또는|혹은|/)[^)]*\)", canonical)
        )
        if not forms or not canonical or has_alternative_target:
            continue
        for form in forms:
            seen_forms.add(form.casefold())
        avoid = item.get("avoid_ko") or []
        if isinstance(avoid, str):
            avoid = [avoid]
        avoid = [str(v).strip() for v in avoid
                 if str(v).strip() and str(v).strip() != canonical]
        urls = item.get("evidence_urls") or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [str(v) for v in urls if str(v) in allowed_urls]
        confidence = str(item.get("confidence") or "low").lower()
        concepts.append({
            "concept_id": str(item.get("concept_id") or
                              f"term.{len(concepts) + 1}"),
            "source_terms": forms,
            "original_terms": [str(v).strip() for v in original_terms
                               if str(v).strip()],
            "canonical_ko": canonical,
            "avoid_ko": avoid,
            "kind": str(item.get("kind") or "other"),
            "rationale": str(item.get("rationale") or "").strip(),
            "confidence": confidence if confidence in {"high", "medium", "low"}
                          else "low",
            "strict": bool(item.get("strict", confidence != "low")),
            "evidence_urls": urls,
        })
    return concepts


def build_concept_sheet(chapters: list, title: str, author: str,
                        source_lang: str, source_research: dict,
                        chat_fn: Callable, search_fn: Callable) -> dict:
    """Build and independently critique a web-grounded structured term sheet."""
    full_text = _book_text(chapters)
    drafts = _draft_concepts(chapters, title, author, source_lang,
                             source_research, chat_fn)
    related = _vet_related_candidates(
        _related_lexical_candidates(full_text, drafts),
        full_text, title, author, chat_fn,
    )
    # Reserve concept slots for vetted rare distinctions. Without this budget,
    # a full set of frequent draft concepts can crowd out a once-used but
    # philosophically decisive neighbor such as Freidenker.
    research_candidates = drafts[:max(0, MAX_CONCEPTS - len(related))] + related
    web_evidence, allowed_urls = _web_evidence(
        research_candidates, title, author, search_fn)
    contexts = _contexts(full_text, [
        _first_source_term(c)
        for c in research_candidates if c.get("source_terms")
    ])
    adjudicated = _call_json(
        chat_fn,
        "You are a senior Korean literary translator adjudicating terminology. "
        "Use the source contexts first, then reliable primary editions, library "
        "catalogs, scholarly sources, and established Korean editions in the web "
        "evidence. Do not treat search-result frequency as authority. Preserve "
        "lexical distinctions between related original terms. Rare entries marked "
        "related_candidate were found because they resemble a major term; explicitly "
        "decide whether they are distinct concepts even if they occur only once. "
        "Decide autonomously; "
        "do not request human approval. Return JSON only as {\"concepts\":[{"
        "\"concept_id\":\"...\",\"source_terms\":[...],"
        "\"original_terms\":[...],\"canonical_ko\":\"...\","
        "\"avoid_ko\":[...],\"kind\":\"...\",\"rationale\":\"...\","
        "\"confidence\":\"high|medium|low\",\"strict\":true,"
        "\"evidence_urls\":[...]}]}. Mark strict=false only when the source "
        "form is genuinely polysemous and context-dependent. Keep each rationale "
        "under 80 words and return no more than 24 concepts.",
        json.dumps({"book": {"title": title, "author": author,
                             "source_language": source_lang},
                    "source_research": source_research,
                    "draft_concepts": research_candidates,
                    "source_contexts": contexts,
                    "web_evidence": web_evidence}, ensure_ascii=False),
        "terminology adjudication",
    )
    first = _validate_concepts(adjudicated.get("concepts") or [], full_text,
                               allowed_urls)

    # A separate adversarial pass is deliberately mandatory: the prose model
    # that proposed a fluent term is not allowed to be its only judge.
    criticised = _call_json(
        chat_fn,
        "Act as an adversarial philological reviewer. Check every proposed Korean "
        "term against the supplied source contexts and evidence. Correct semantic "
        "flattening, relay-translation artifacts, false friends, name confusion, "
        "and unjustified search-snippet claims. Preserve distinct lemmas. You must "
        "retain vetted thinker-role neighbors as their own concept objects; other "
        "related candidates may be removed when they are merely inflections or "
        "ordinary incidental vocabulary. "
        "make the final decision autonomously. Return JSON only with exactly one "
        "top-level key named concepts, using the same concept object schema. Do not "
        "echo the input payload and do not rename concepts to proposed_concepts. "
        "Keep each rationale under 60 words; omit source quotations and return no "
        "more than 24 concepts so the JSON cannot be truncated.",
        json.dumps({"book": {"title": title, "author": author,
                             "source_language": source_lang},
                    "source_research": source_research,
                    "proposed_concepts": first,
                    "source_contexts": contexts,
                    "web_evidence": web_evidence}, ensure_ascii=False),
        "terminology cross-check",
    )
    reviewed_concepts = (criticised.get("concepts")
                         or criticised.get("proposed_concepts") or [])
    reviewed = _validate_concepts(reviewed_concepts, full_text, allowed_urls)

    # Final schema normalization is distinct from philological adjudication.
    # It prevents a sound decision from becoming an unsafe mechanical rule via
    # adjective fragments, anaphors, or slash-separated target alternatives.
    normalised = _call_json(
        chat_fn,
        "You normalize an adjudicated literary concept sheet for deterministic "
        "translation enforcement. Return JSON only with top-level concepts. "
        "Every source_terms item must be a complete lexical unit occurring "
        "verbatim in the supplied source contexts: never a bare inflected "
        "adjective, article, anaphor, or generic head noun. Every concept must "
        "have exactly one literal canonical_ko string—no slash, alternatives, "
        "or optional parenthetical suffixes. Split antonyms or paired expressions "
        "into separate concept objects with their own mappings. Preserve rare "
        "but intentionally distinct neighboring lemmas. Keep rationales under "
        "40 words and do not add concepts unsupported by the source.",
        json.dumps({"concepts": reviewed, "source_contexts": contexts},
                   ensure_ascii=False),
        "concept schema normalization",
    )
    final = _validate_concepts(
        normalised.get("concepts") or normalised.get("proposed_concepts") or [],
        full_text, allowed_urls)
    final = _reconcile_lexical_targets(final)
    final_forms = {
        form.casefold()
        for concept in final
        for form in _form_list(concept.get("source_terms"))
    }
    mandatory_related = [
        candidate for candidate in related
        if _is_distinct_role_neighbor(_first_source_term(candidate))
    ]
    missing_related = [
        _first_source_term(candidate)
        for candidate in mandatory_related
        if _first_source_term(candidate).casefold() not in final_forms
    ]
    if missing_related:
        raise PrecisionError(
            "concept normalization dropped vetted lexical distinctions: "
            + ", ".join(missing_related)
        )
    if drafts and not final:
        raise PrecisionError("concept normalization produced no usable concepts")
    sheet = {
        "schema_version": PRECISION_SCHEMA_VERSION,
        "profile_version": PRECISION_PROFILE_VERSION,
        "source_hash": source_fingerprint(chapters),
        "book": {"title": title, "author": author,
                 "source_language": source_lang},
        "source_research": source_research,
        "concepts": final,
        "research": {"web_evidence": web_evidence},
    }
    sheet["profile_hash"] = precision_profile_hash(sheet)
    return sheet


def flat_glossary(sheet: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for concept in sheet.get("concepts") or []:
        if not concept.get("strict", True):
            continue
        target = str(concept.get("canonical_ko") or "").strip()
        if not target:
            continue
        for form in concept.get("source_terms") or []:
            if str(form).strip():
                out[str(form).strip()] = target
    return out


def precision_profile_hash(sheet: dict) -> str:
    # Research snippets and URLs are provenance, not translation instructions;
    # excluding them keeps a resumed request stable if live search rankings
    # change. Every field that enters the translation prompt remains included.
    concepts = []
    for concept in sheet.get("concepts") or []:
        concepts.append({
            key: concept.get(key) for key in (
                "concept_id", "source_terms", "original_terms", "canonical_ko",
                "avoid_ko", "kind", "rationale", "strict",
            )
        })
    payload = {
        "schema_version": sheet.get("schema_version"),
        "profile_version": sheet.get("profile_version"),
        "source_hash": sheet.get("source_hash"),
        "book": sheet.get("book"),
        "concepts": concepts,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def persist_concept_sheet(path: str, sheet: dict) -> None:
    import os
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sheet, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    sys.stderr.write(
        f"[precision] concept sheet: {len(sheet.get('concepts') or [])} terms -> {path}\n"
    )
