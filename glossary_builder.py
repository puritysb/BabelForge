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

    # Build glossary — at this stage, we just flag terms for consistent
    # translation. The translator prompt says "'term' must be translated
    # as 'term'" which effectively means "keep consistent".
    # A future enhancement could call web_search to find established translations.
    glossary: dict[str, str] = {}
    for term, count in sorted_terms[:50]:  # cap at 50 terms
        # For now, we don't provide Korean translations — we just mark
        # them for consistent treatment. The system prompt will say
        # "'Term' must always be translated the same way throughout the book."
        # If we have web_search available, we could look up translations.
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


def enrich_glossary_with_llm(glossary: dict[str, str], source_lang: str = "en",
                              _chat_fn=None) -> dict[str, str]:
    """Use an LLM call to suggest Korean translations for glossary terms.

    _chat_fn: callable(messages: list[dict]) -> str  (inject translate._chat)
    """
    if not _chat_fn or not glossary:
        return glossary

    terms = list(glossary.keys())
    terms_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(terms))

    src_name = {"en": "English", "ja": "Japanese", "fr": "French",
                "de": "German", "zh": "Chinese"}.get(source_lang, source_lang)

    system = (
        f"You are an expert {src_name}-to-Korean literary translator. "
        "For each term below, provide the single most natural Korean translation "
        "used in published literature. Respond in JSON: {\"term\": \"번역\"}"
    )
    user = f"Translate these terms to Korean:\n{terms_text}"

    try:
        raw = _chat_fn([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        # Try to parse as JSON (LLM may wrap in ```json blocks)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        parsed = json.loads(raw)
        enriched = {}
        for term in terms:
            if term in parsed:
                enriched[term] = parsed[term]
            else:
                enriched[term] = glossary[term]  # keep original
        return enriched
    except Exception as e:
        sys.stderr.write(f"[glossary_builder] LLM enrichment failed: {e}\n")
        return glossary


def main():
    parser = argparse.ArgumentParser(description="Build a glossary from source text")
    parser.add_argument("source", help="Source EPUB or text file")
    parser.add_argument("--output", "-o", default="data/glossary.json",
                        help="Output glossary JSON path")
    parser.add_argument("--source-lang", "-s", default="en",
                        help="Source language code (en, ja, fr, ...)")
    parser.add_argument("--enrich", action="store_true",
                        help="Use LLM to suggest Korean translations")
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
            glossary = enrich_glossary_with_llm(glossary, args.source_lang,
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
