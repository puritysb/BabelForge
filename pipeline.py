"""End-to-end pipeline orchestrator.

Given a chosen Candidate (JSON), run: fetch → extract → translate → assemble
→ publish, updating catalog.json at each step and notifying LINE on success.

Invoked by run_request.sh, which the OpenClaw agent calls after the user picks
a candidate from the search results.
"""
from __future__ import annotations
import json
import os
import sys
import traceback

import config
import catalog
import search as search_mod
import pending_queue
import cover as cover_mod
from sources import get_adapter
from extract import extract
from translate import translate_book, TranslationError
from assemble import assemble as assemble_epub
from publish import publish as publish_epub
from device_push import push_to_device


def run(candidate: dict, req_id: str | None = None,
        notify: bool = True) -> dict:
    query = candidate.get("title", "")
    if req_id is None:
        req_id = catalog.new_request(query)

    try:
        # 1. fetch
        catalog.update(req_id, status="selected")
        adapter = get_adapter(candidate["source"])
        from sources import Candidate
        cand = Candidate(**{k: v for k, v in candidate.items() if k != "extra"})
        cand.extra = candidate.get("extra")
        src = adapter.fetch(cand)

        # 2. extract
        chapters = extract(src)
        catalog.update(req_id, status="translating",
                       progress={"done": 0, "total": sum(len(c.paragraphs) for c in chapters), "pct": 0})

        # 3. translate
        source_lang = (cand.language or "en").split("-")[0].lower()
        checkpoint_path = os.path.join(config.CHECKPOINT_DIR, f"{req_id}.json")

        # 3a. auto-glossary: extract recurring proper nouns/terms and (best-
        # effort) look up their canonical Korean rendering, so every one of the
        # concurrent workers translates a name/place the same way. Persisted to
        # config.GLOSSARY_PATH for debug visibility, then passed explicitly.
        glossary = None
        if config.GLOSSARY_ENABLED:
            try:
                from glossary_builder import (build_glossary_from_chapters,
                                              enrich_glossary_with_llm,
                                              enrich_glossary_grounded)
                glossary = build_glossary_from_chapters(chapters, source_lang)
                if glossary and config.GLOSSARY_ENRICH:
                    import translate as _translate
                    if config.GLOSSARY_WEB_SEARCH:
                        # Ground name/place renderings in real published usage
                        # via the Z.ai web_search_prime MCP tool; falls back to
                        # a plain LLM guess if no web results come back.
                        glossary = enrich_glossary_grounded(
                            glossary, source_lang, title=cand.title,
                            _chat_fn=_translate._chat,
                            top_terms=config.GLOSSARY_WEB_SEARCH_TERMS)
                    else:
                        glossary = enrich_glossary_with_llm(
                            glossary, source_lang, _chat_fn=_translate._chat)
                if glossary:
                    os.makedirs(os.path.dirname(config.GLOSSARY_PATH), exist_ok=True)
                    with open(config.GLOSSARY_PATH, "w", encoding="utf-8") as f:
                        json.dump(glossary, f, ensure_ascii=False, indent=2)
                    sys.stderr.write(
                        f"[pipeline] built glossary: {len(glossary)} terms "
                        f"(enrich={config.GLOSSARY_ENRICH})\n"
                    )
            except Exception as e:
                # Glossary is a quality boost, never a hard dependency — a
                # failure here must not sink the whole translation.
                sys.stderr.write(f"[pipeline] glossary build skipped: {e}\n")
                glossary = None

        def progress_cb(done, total):
            catalog.set_progress(req_id, done, total)
            # Stream progress to LINE at ~20% intervals so the user isn't
            # blind during a long translation (Nietzsche took ~2h). Throttle
            # by percentage bucket to avoid spamming on short books.
            pct = int(100 * done / total) if total else 0
            bucket = pct // 20
            last_bucket = progress_cb._last_bucket  # type: ignore[attr-defined]
            if bucket > last_bucket and bucket > 0:
                progress_cb._last_bucket = bucket  # type: ignore[attr-defined]
                search_mod.notify_line(
                    f"🔄 '{cand.title[:40]}' 번역 중… "
                    f"{done}/{total} ({pct}%)"
                )
        progress_cb._last_bucket = -1  # type: ignore[attr-defined]

        book = translate_book(
            title=cand.title, author=cand.author,
            chapters=chapters, progress_cb=progress_cb,
            source_lang=source_lang,
            checkpoint_path=checkpoint_path,
            glossary=glossary,
        )

        # 4. assemble
        catalog.update(req_id, status="assembling")
        book_dict = {
            "title": book.title,
            "author": book.author,
            "chapters": [{"title": c.title, "pairs": c.pairs} for c in book.chapters],
        }
        # Try to fetch an original-edition cover so the device library grid
        # shows a real thumbnail rather than a blank tile. Best-effort —
        # assemble gracefully omits cover if this returns None.
        cover_bytes = None
        try:
            cover_bytes = cover_mod.find_cover(
                title=cand.title, author=cand.author,
                source=cand.source, source_id=cand.source_id,
                extra=cand.extra or {},
            )
            if cover_bytes:
                sys.stderr.write(
                    f"[pipeline] cover found: {len(cover_bytes)} bytes\n"
                )
        except Exception as e:
            sys.stderr.write(f"[pipeline] cover fetch failed (non-fatal): {e}\n")
        epub_path = assemble_epub(book_dict, cover_bytes=cover_bytes,
                                  source_lang=source_lang)

        # 5. publish
        book_id, opds_url = publish_epub(
            epub_path,
            title=f"{book.title} {config.BILINGUAL_SUFFIX}",
            author=book.author,
            source=cand.source,
            rights=cand.rights or "",
        )
        catalog.mark_published(req_id, epub_path, opds_url)

        # 6. best-effort device push (File Transfer must be on, on the device)
        push_result = push_to_device(epub_path)
        catalog.update(req_id, device_push=push_result.to_dict())

        # If the push didn't land (device off / not in File Transfer mode),
        # enqueue it for the auto_push_watcher. The watcher (launchd) will
        # drain the queue as soon as the device reappears — so the user just
        # has to put the device in File Transfer mode, and pending books flow
        # automatically without re-running anything.
        if not push_result.pushed:
            pending_queue.enqueue(
                epub_path=epub_path,
                title=f"{book.title} {config.BILINGUAL_SUFFIX}",
                author=book.author,
                source=cand.source,
                rights=cand.rights or "",
                req_id=req_id,
            )

        if notify:
            _notify_success(book.title, book.author, opds_url, cand.source,
                            push_result)

        entry = catalog.get(req_id)
        return {"ok": True, "req_id": req_id, "epub_path": epub_path,
                "opds_url": opds_url, "book_id": book_id,
                "device_push": push_result.to_dict(),
                "entry": entry}
    except TranslationError as e:
        catalog.mark_failed(req_id, f"translation: {e}")
        if notify:
            search_mod.notify_line(
                f"❌ 번역 실패: {query}\n{e}\n— ZAI_API_KEY 또는 GLM-5.2 endpoint 확인"
            )
        return {"ok": False, "req_id": req_id, "error": str(e)}
    except Exception as e:
        catalog.mark_failed(req_id, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        if notify:
            search_mod.notify_line(f"❌ 파이프라인 오류: {query}\n{type(e).__name__}: {e}")
        return {"ok": False, "req_id": req_id, "error": str(e)}


def _notify_success(title: str, author: str, opds_url: str, source: str,
                    push=None) -> None:
    pickup_line = (
        f"기기 Settings → System → OPDS Servers → 'OpenClaw Library'\n"
        f"  → '{title} (KO bilingual)' 검색 → 다운로드"
    )
    if push is not None and getattr(push, "pushed", False):
        pickup_line = (
            f"✅ 기기 {getattr(push, 'path', '/Books')}로 직접 전송 완료"
            f" ({getattr(push, 'elapsed_ms', '?')}ms)"
        )
    elif push is not None and not getattr(push, "skipped", True):
        # push was attempted but failed (non-skip) — surface the error
        pickup_line += (
            f"\n⚠️ 기기 push 시도 실패: {getattr(push, 'error', 'unknown')}"
        )
    msg = (
        f"📖 번역 완료: {title} / {author}\n"
        f"소스: {source}\n"
        f"{pickup_line}\n"
        f"열면 long-press Confirm으로 Both/Original/Translation 전환"
    )
    search_mod.notify_line(msg)


def main():
    if len(sys.argv) < 2:
        print("usage: pipeline.py <candidate.json> [req_id] [--no-notify]",
              file=sys.stderr)
        sys.exit(2)
    cand_file = sys.argv[1]
    req_id = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
    notify = "--no-notify" not in sys.argv

    if cand_file == "-":
        candidate = json.loads(sys.stdin.read())
    else:
        with open(cand_file, "r", encoding="utf-8") as f:
            candidate = json.load(f)

    result = run(candidate, req_id=req_id, notify=notify)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
