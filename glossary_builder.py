"""Auto-glossary builder for the translation pipeline.

Analyzes the source text before translation to identify:
1. Character names (capitalized words appearing N+ times)
2. Place names (capitalized words not in common English vocabulary)
3. Recurring domain terms (uncommon words appearing frequently)

Outputs a JSON glossary file that translate.py loads automatically.

Usage:
    python glossary_builder.py <source_epub_or_txt> [--output glossary.json] [--source-lang en]

Can also be imported:
    from glossary_builder import build_glossary
    glossary = build_glossary(chapters, source_lang="en")
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from collections import Counter


# Common English words to exclude from name detection.
# Not comprehensive — just the most frequent non-names that start with capitals.
_STOPWORDS_CAPITALIZED = frozenset(
    "The A An And Or But Nor For Yet So In On At To Of For With By From "
    "Up Down Out Off Over Under Again Further Then Once Here There When "
    "Where Why How All Any Both Each Few More Most Other Some Such No Not "
    "Only Own Same So Than Too Very Can Will Just Should Now I You He She "
    "It We They Me Him Her Us Them My Your His Its Our Their This That "
    "These Those What Which Who Whom Whose Whichever Whoever Whomever Is Am "
    "Are Was Were Be Been Being Have Has Had Do Does Did Shall Should Would "
    "May Might Must Can Could Will Would Ought Mr Mrs Ms Miss Dr Sir Madam "
    "Lord Lady King Queen Prince Princess Captain Major General Colonel "
    "Father Mother Brother Sister Uncle Aunt Cousin Son Daughter Child "
    "Children Man Woman Boy Girl God Jesus Christ Heaven Hell Monday "
    "Tuesday Wednesday Thursday Friday Saturday Sunday January February "
    "March April May June July August September October November December "
    "Spring Summer Autumn Winter North South East West Yes No Oh Ah Eh "
    "Well Still Even Ever Never Always Often Sometimes Never However "
    "Therefore Moreover Meanwhile Besides Nevertheless Nonetheless "
    "Indeed Perhaps Probably Possibly Certainly Surely Truly Really "
    "Chapter Book Part Section One Two Three Four Five Six Seven Eight "
    "Nine Ten First Second Third Fourth Fifth Sixth Seventh Eighth Ninth "
    "Tenth".split()
)


def _extract_candidates(text: str, source_lang: str = "en") -> tuple[Counter, dict[str, str]]:
    """Extract potential proper nouns and recurring uncommon terms from text.

    Returns (word_counts, first_context): a Counter of candidate phrase -> count,
    and a map of phrase -> a short first-occurrence context window. The context
    map is currently informational (unused by the glossary build) but kept for
    future enrichment prompts.
    """
    if source_lang != "en":
        # For non-English sources, we skip auto-detection for now.
        return Counter(), {}

    # Find all capitalized words/phrases (1-3 words)
    # Pattern: Capitalized word optionally followed by 1-2 more capitalized words
    cap_pattern = re.compile(
        r'\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2})\b'
    )

    word_counts = Counter()
    first_context: dict[str, str] = {}

    sentences = re.split(r'(?<=[.!?])\s+', text)
    for sent in sentences:
        for m in cap_pattern.finditer(sent):
            phrase = m.group(1).strip()
            # Skip if all words are stopwords
            words = phrase.split()
            if all(w in _STOPWORDS_CAPITALIZED for w in words):
                continue
            word_counts[phrase] += 1
            if phrase not in first_context:
                # Store a short context window
                start = max(0, m.start() - 30)
                end = min(len(sent), m.end() + 30)
                first_context[phrase] = sent[start:end].strip()

    return word_counts, first_context


def build_glossary_from_text(text: str, source_lang: str = "en",
                              min_count: int = 3) -> dict[str, str]:
    """Build a glossary dictionary from source text.

    Returns {source_term: korean_translation_hint} where the hint is
    initially just the source term (to be filled in by the translator).
    The translate.py system prompt enforces that these terms are translated
    consistently.
    """
    word_counts, first_context = _extract_candidates(text, source_lang)

    # Sort by frequency (descending), then alphabetically
    sorted_terms = sorted(
        ((term, count) for term, count in word_counts.items() if count >= min_count),
        key=lambda x: (-x[1], x[0])
    )

    # Build glossary — at this stage each term is flagged for *consistent*
    # treatment with a <CONSISTENT:term> placeholder (the system prompt turns
    # this into "always translate the same way"). The actual canonical Korean
    # rendering is filled in later by enrich_glossary_grounded (web-search) or
    # enrich_glossary_with_llm; an unenriched glossary still enforces
    # consistency, just without a fixed target string.
    glossary: dict[str, str] = {}
    for term, count in sorted_terms[:30]:  # cap — keep the highest-value terms
        glossary[term] = f"<CONSISTENT:{term}>"

    return glossary


def build_glossary_from_chapters(chapters: list, source_lang: str = "en") -> dict[str, str]:
    """Build glossary from extracted chapters.

    chapters: list of objects with .title and .paragraphs attributes
              (as produced by extract.py)
    """
    full_text = "\n\n".join(
        ch.title + "\n" + "\n".join(ch.paragraphs)
        for ch in chapters
    )
    return build_glossary_from_text(full_text, source_lang)


_SRC_NAME = {"en": "English", "ja": "Japanese", "fr": "French",
             "de": "German", "zh": "Chinese", "es": "Spanish"}

# Terms per LLM extraction call. A single 50-term request tended to return
# truncated/malformed JSON (observed: parse error mid-array); smaller chunks
# parse reliably and a bad chunk only loses its own terms, not the whole book.
_ENRICH_CHUNK = 20


def _lenient_json(raw: str):
    """Parse a JSON object from an LLM reply, tolerating ```-fences and trailing
    prose. Returns a dict, or None if nothing parseable is found."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Recover the outermost { ... } if the model wrapped it in extra text.
    i, j = raw.find("{"), raw.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(raw[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def _llm_extract_korean(terms: list[str], source_lang: str, _chat_fn,
                        grounding: list[str] | None = None) -> dict[str, str]:
    """Ask the LLM for each term's Korean rendering, returning {term: 번역} for
    the terms it answered. When `grounding` (web-search snippets) is provided,
    the model is told to prefer the rendering that actually appears there —
    published usage — rather than inventing one. Runs in chunks so a single
    oversized/malformed reply can't wipe the whole glossary. Returns {} if every
    chunk fails.
    """
    src_name = _SRC_NAME.get(source_lang, source_lang)
    context = ("\n".join(grounding)) if grounding else ""
    out: dict[str, str] = {}

    for start in range(0, len(terms), _ENRICH_CHUNK):
        chunk = terms[start:start + _ENRICH_CHUNK]
        terms_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(chunk))
        if grounding:
            system = (
                f"You are an expert {src_name}-to-Korean literary translator. For "
                "each term, give the single Korean rendering used in published "
                "Korean editions. Prefer a rendering that appears in the Search "
                "Context below; only if a term is absent there, fall back to the "
                "most natural published transliteration. Respond ONLY as JSON: "
                "{\"term\": \"번역\"}"
            )
            user = (f"Search Context (real web results):\n{context}\n\n"
                    f"Give the Korean rendering for each term:\n{terms_text}")
        else:
            system = (
                f"You are an expert {src_name}-to-Korean literary translator. For "
                "each term below, provide the single most natural Korean translation "
                "used in published literature. Respond ONLY as JSON: {\"term\": \"번역\"}"
            )
            user = f"Translate these terms to Korean:\n{terms_text}"

        try:
            raw = _chat_fn([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
        except Exception as e:
            sys.stderr.write(f"[glossary_builder] extraction chunk failed: {e}\n")
            continue
        parsed = _lenient_json(raw)
        if parsed:
            for t in chunk:
                v = parsed.get(t)
                if isinstance(v, str) and v.strip():
                    out[t] = v
    return out


def enrich_glossary_with_llm(glossary: dict[str, str], source_lang: str = "en",
                              _chat_fn=None) -> dict[str, str]:
    """Use one LLM call to suggest Korean translations for glossary terms.

    _chat_fn: callable(messages: list[dict]) -> str  (inject translate._chat)
    """
    if not _chat_fn or not glossary:
        return glossary
    terms = list(glossary.keys())
    parsed = _llm_extract_korean(terms, source_lang, _chat_fn)
    return {t: parsed.get(t, glossary[t]) for t in terms}


def enrich_glossary_grounded(glossary: dict[str, str], source_lang: str = "en",
                             title: str | None = None, _chat_fn=None,
                             _search_fn=None, top_terms: int = 8) -> dict[str, str]:
    """Web-search the most frequent glossary terms, then have the LLM extract
    each term's canonical Korean rendering grounded in those snippets.

    The glossary is frequency-ordered, so the first `top_terms` are the main
    characters/places where a consistent, established rendering matters most.
    Only those are searched (a handful of MCP calls); the LLM then renders ALL
    terms, grounded where snippets exist. Falls back to the plain LLM guess if
    no web results come back (offline / MCP unavailable), so this never blocks.

    _chat_fn: translate._chat.  _search_fn: mcp_client.web_search (injectable
    for tests).
    """
    if not _chat_fn or not glossary:
        return glossary
    if _search_fn is None:
        from mcp_client import web_search as _search_fn

    terms = list(glossary.keys())
    snippets: list[str] = []
    for term in terms[:top_terms]:
        query = (f"{term} {title} 한국어 번역" if title
                 else f"{term} 한국어 번역")[:70]
        for r in (_search_fn(query, count=3) or [])[:3]:
            txt = f"{r.get('title', '')} {r.get('content', '')}".strip()
            if txt:
                snippets.append(f"[{term}] {txt}")

    if not snippets:
        sys.stderr.write("[glossary_builder] no web grounding; LLM-only enrich\n")
        return enrich_glossary_with_llm(glossary, source_lang, _chat_fn)

    sys.stderr.write(
        f"[glossary_builder] grounded enrich: {len(snippets)} snippets for "
        f"{min(top_terms, len(terms))}/{len(terms)} terms\n"
    )
    parsed = _llm_extract_korean(terms, source_lang, _chat_fn, grounding=snippets)
    return {t: parsed.get(t, glossary[t]) for t in terms}


def main():
    parser = argparse.ArgumentParser(description="Build a glossary from source text")
    parser.add_argument("source", help="Source EPUB or text file")
    parser.add_argument("--output", "-o", default="data/glossary.json",
                        help="Output glossary JSON path")
    parser.add_argument("--source-lang", "-s", default="en",
                        help="Source language code (en, ja, fr, ...)")
    parser.add_argument("--enrich", action="store_true",
                        help="Use LLM to suggest Korean translations")
    parser.add_argument("--no-web-search", action="store_true",
                        help="With --enrich, skip web-search grounding (LLM guess only)")
    args = parser.parse_args()

    # Extract text from source
    if args.source.endswith(".epub"):
        import extract as ext
        chapters = ext.extract_epub(args.source)
        text = "\n\n".join(
            ch.title + "\n" + "\n".join(ch.paragraphs)
            for ch in chapters
        )
    else:
        with open(args.source, "r", encoding="utf-8") as f:
            text = f.read()

    glossary = build_glossary_from_text(text, args.source_lang)

    if args.enrich and glossary:
        # Import translate module for _chat
        sys.path.insert(0, os.path.dirname(__file__))
        try:
            import translate
            if args.no_web_search:
                glossary = enrich_glossary_with_llm(glossary, args.source_lang,
                                                    _chat_fn=translate._chat)
            else:
                glossary = enrich_glossary_grounded(glossary, args.source_lang,
                                                    _chat_fn=translate._chat)
        except ImportError:
            sys.stderr.write("[glossary_builder] translate.py not found, skipping enrichment\n")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(glossary, f, ensure_ascii=False, indent=2)

    sys.stderr.write(
        f"[glossary_builder] wrote {len(glossary)} terms to {args.output}\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
