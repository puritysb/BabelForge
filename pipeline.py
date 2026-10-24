"""End-to-end pipeline orchestrator.

Given a chosen Candidate (JSON), run: source research → fetch → extract →
concept research/cross-check → precision translate/audit → assemble → validate,
then optional Calibre publication and delivery. Updates catalog.json at each step.

Invoked by run_request.sh, which the OpenClaw agent calls after the user picks
a candidate from the search results.
"""
from __future__ import annotations
import json
import hashlib
import os
import sys
import traceback
from urllib.parse import urlsplit

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
from device_push import PushResult, push_to_device
from epub_validate import validate_epub
from precision import (PRECISION_PROFILE_VERSION, PRECISION_SCHEMA_VERSION,
                       PrecisionError, build_concept_sheet,
                       choose_original_candidate, flat_glossary,
                       persist_concept_sheet, precision_profile_hash,
                       research_source,
                       source_fingerprint)


def run(candidate: dict, req_id: str | None = None,
        notify: bool = True, publish_output: bool = True) -> dict:
    """Run one request under an exclusive worker lock."""
    if req_id is None:
        req_id = catalog.new_request(candidate.get("title", ""))
    try:
        with catalog.request_worker(req_id):
            previous = catalog.get(req_id) or {}
            fields = {"publish_output": publish_output, "notify": notify}
            if not previous.get("selected"):
                fields["selected"] = candidate
            catalog.update(req_id, **fields)
            return _run(candidate, req_id, notify, publish_output)
    except catalog.RequestAlreadyRunning as exc:
        return {"ok": False, "req_id": req_id, "error": str(exc),
                "already_running": True}


def _safe_notify(message: str) -> None:
    try:
        search_mod.notify_line(message)
    except Exception as exc:
        sys.stderr.write(f"[notification] delivery failed: {exc}\n")


def _run(candidate: dict, req_id: str,
         notify: bool, publish_output: bool) -> dict:
    query = candidate.get("title", "")

    try:
        # 1. Mandatory precision source research. If the selected catalog item
        # is a relay translation, autonomously prefer an original-language,
        # public-domain edition from the safe default sources when available.
        catalog.update(req_id, status="researching")
        from sources import Candidate
        import translate as _translate
        from mcp_client import web_search
        concept_path = os.path.join(config.CONCEPT_DIR, f"{req_id}.json")
        previous = catalog.get(req_id) or {}
        saved_candidate = previous.get("selected")
        if (isinstance(saved_candidate, dict)
                and isinstance(previous.get("source_research"), dict)):
            cand = Candidate(**{k: v for k, v in saved_candidate.items()
                                if k != "extra"})
            cand.extra = saved_candidate.get("extra")
            source_research = previous["source_research"]
            source_replaced = bool(previous.get("source_replaced"))
            sys.stderr.write(
                "[precision] reused request-scoped source resolution\n"
            )
        else:
            cand = Candidate(**{k: v for k, v in candidate.items()
                                if k != "extra"})
            cand.extra = candidate.get("extra")
            source_research = research_source(
                cand, chat_fn=_translate._chat_json, search_fn=web_search)

            def _search_original(q: str):
                return search_mod.search_all(
                    q, sources=list(config.DEFAULT_SOURCES), limit_per_source=10)

            resolved, source_replaced = choose_original_candidate(
                cand, source_research, _search_original)
            cand = Candidate(**{k: v for k, v in resolved.items()
                                if k != "extra"})
            cand.extra = resolved.get("extra")
            if source_replaced:
                source_research = {
                    **source_research,
                    "relay_source_replaced": True,
                    "source_language": (cand.language or "").split("-")[0].lower(),
                    "source_is_translation": False,
                    "resolved_source": {
                        "source": cand.source, "source_id": cand.source_id,
                        "title": cand.title, "language": cand.language,
                    },
                }
        catalog.update(req_id, status="selected", selected=cand.to_dict(),
                       source_research=source_research,
                       source_replaced=source_replaced)
        if source_replaced:
            sys.stderr.write(
                f"[precision] replaced relay source with original-language "
                f"edition: {cand.title} [{cand.language}]\n"
            )

        # 2. fetch + extract
        adapter = get_adapter(cand.source)
        src = adapter.fetch(cand)
        chapters = extract(src)
        source_lang = (cand.language or "en").split("-")[0].lower()
        checkpoint_path = os.path.join(config.CHECKPOINT_DIR, f"{req_id}.json")

        # 3. Mandatory autonomous concept research. This is fail-closed: a bad
        # research response never degrades silently into the old fast path.
        catalog.update(req_id, status="analyzing",
                       progress={"done": 0,
                                 "total": sum(len(c.paragraphs) for c in chapters),
                                 "pct": 0})
        concept_sheet = None
        if os.path.isfile(concept_path):
            try:
                with open(concept_path, "r", encoding="utf-8") as f:
                    saved_sheet = json.load(f)
                if (saved_sheet.get("source_hash") == source_fingerprint(chapters)
                        and saved_sheet.get("schema_version") == PRECISION_SCHEMA_VERSION
                        and saved_sheet.get("profile_version") == PRECISION_PROFILE_VERSION
                        and saved_sheet.get("profile_hash") ==
                            precision_profile_hash(saved_sheet)):
                    concept_sheet = saved_sheet
                    sys.stderr.write(
                        f"[precision] reused request-scoped concept sheet: "
                        f"{concept_path}\n"
                    )
            except (OSError, json.JSONDecodeError) as e:
                sys.stderr.write(
                    f"[precision] concept sheet unreadable ({e}); rebuilding\n"
                )
        if concept_sheet is None:
            concept_sheet = build_concept_sheet(
                chapters=chapters, title=cand.title, author=cand.author,
                source_lang=source_lang, source_research=source_research,
                chat_fn=_translate._chat_json, search_fn=web_search)
            persist_concept_sheet(concept_path, concept_sheet)
        glossary = flat_glossary(concept_sheet)
        catalog.update(req_id, status="translating",
                       concept_sheet_path=concept_path,
                       precision_profile=concept_sheet["profile_hash"],
                       concepts=len(concept_sheet.get("concepts") or []))

        def progress_cb(done, total):
            catalog.set_progress(req_id, done, total)
            # Stream progress to LINE at ~20% intervals so the user isn't
            # blind during a long translation (Nietzsche took ~2h). Throttle
            # by percentage bucket to avoid spamming on short books.
            pct = int(100 * done / total) if total else 0
            bucket = pct // 20
            last_bucket = progress_cb._last_bucket  # type: ignore[attr-defined]
            if notify and bucket > last_bucket and bucket > 0:
                progress_cb._last_bucket = bucket  # type: ignore[attr-defined]
                _safe_notify(
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
            concept_sheet=concept_sheet,
        )

        # translate_book performs the fail-closed whole-book concept audit and
        # only returns after it passes.
        catalog.update(req_id, status="quality_checked",
                       quality_report=book.quality_report)

        # 4. assemble
        catalog.update(req_id, status="assembling")
        book_dict = {
            "title": book.title,
            "author": book.author,
            "chapters": [{"title": c.title, "pairs": c.pairs,
                          "template": getattr(c, "template", None),
                          "assets": getattr(c, "assets", {})}
                         for c in book.chapters],
        }
        with open(src.path, "rb") as source_file:
            source_sha256 = hashlib.file_digest(source_file, "sha256").hexdigest()
        book_dict["provenance"] = {
            "source": cand.source,
            "source_id": (source_sha256 if cand.source == "local" else cand.source_id),
            "url": (cand.fetch_url if urlsplit(cand.fetch_url or "").scheme
                    in {"https", "http"} else ""),
            "rights": cand.rights or "",
            "source_sha256": source_sha256,
            "quality_profile": concept_sheet["profile_hash"],
        }
        catalog.update(req_id, source_sha256=source_sha256)
        # Try to fetch a cover for the reader's library thumbnail. Best-effort —
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
        assembly_validation = {}
        epub_path = assemble_epub(book_dict, cover_bytes=cover_bytes,
                                  source_lang=source_lang,
                                  validation_report=assembly_validation)
        validation = validate_epub(
            epub_path, expected_pairs=sum(len(c.pairs) for c in book.chapters),
            external=False,  # assemble already runs EPUBCheck when configured
            expected_content=[pair for chapter in book.chapters for pair in chapter.pairs],
        )
        validation.update(assembly_validation)
        with open(epub_path, "rb") as artifact:
            artifact_sha256 = hashlib.file_digest(artifact, "sha256").hexdigest()
        catalog.update(req_id, status="validated", epub_path=epub_path,
                       artifact_sha256=artifact_sha256, epub_validation=validation)

        # 5. Optional publication. Local EPUB generation has no server dependency.
        book_id = opds_url = None
        if publish_output:
            book_id, opds_url = publish_epub(
                epub_path,
                title=f"{book.title} {config.BILINGUAL_SUFFIX}",
                author=book.author, source=cand.source, rights=cand.rights or "",
                source_id=cand.source_id, source_lang=source_lang,
                request_id=req_id,
                expected_sha256=artifact_sha256,
                source_sha256=source_sha256,
                expected_content=[pair for chapter in book.chapters for pair in chapter.pairs],
            )
            catalog.mark_published(req_id, epub_path, opds_url)
        else:
            catalog.mark_completed(req_id, epub_path)

        # 6. Device integration is explicitly opt-in. Auxiliary failures never
        # undo a successfully generated/published EPUB or replay the translation.
        warnings = []
        push_result = PushResult(pushed=False, skipped=True,
                                 error="device push disabled")
        if config.DEVICE_PUSH_ENABLED:
            try:
                push_result = push_to_device(epub_path)
            except Exception as exc:
                push_result = PushResult(pushed=False, error=str(exc))
            if not push_result.pushed:
                try:
                    pending_queue.enqueue(
                        epub_path=epub_path,
                        title=f"{book.title} {config.BILINGUAL_SUFFIX}",
                        author=book.author, source=cand.source,
                        rights=cand.rights or "", req_id=req_id, notify=notify)
                except Exception as exc:
                    warnings.append(f"device retry queue: {exc}")
        try:
            catalog.update(req_id, device_push=push_result.to_dict(),
                           delivery_warnings=warnings)
        except Exception as exc:
            warnings.append(f"delivery status: {exc}")

        if notify:
            _notify_success(book.title, book.author, opds_url, cand.source,
                            push_result, epub_path=epub_path)

        try:
            entry = catalog.get(req_id)
        except Exception as exc:
            warnings.append(f"final status read: {exc}")
            entry = None
        return {"ok": True, "req_id": req_id, "epub_path": epub_path,
                "opds_url": opds_url, "book_id": book_id,
                "device_push": push_result.to_dict(),
                "warnings": warnings,
                "entry": entry}
    except (TranslationError, PrecisionError) as e:
        catalog.mark_failed(req_id, f"translation: {e}")
        if notify:
            _safe_notify(
                f"❌ 정밀 번역 실패: {query}\n{e}\n"
                "— 원전 조사·개념 교차검증·GLM endpoint 로그 확인"
            )
        return {"ok": False, "req_id": req_id, "error": str(e)}
    except Exception as e:
        catalog.mark_failed(req_id, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        if notify:
            _safe_notify(f"❌ 파이프라인 오류: {query}\n{type(e).__name__}: {e}")
        return {"ok": False, "req_id": req_id, "error": str(e)}


def _notify_success(title: str, author: str, opds_url: str | None, source: str,
                    push=None, epub_path: str | None = None) -> None:
    pickup_line = (f"EPUB 다운로드: {opds_url}" if opds_url
                   else f"EPUB 저장: {epub_path}")
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
        "일반 EPUB 리더에서 원문과 한국어 번역을 함께 읽을 수 있습니다."
    )
    _safe_notify(msg)


def main():
    if len(sys.argv) < 2:
        print("usage: pipeline.py <candidate.json> [req_id] [--no-notify] [--no-publish]",
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

    result = run(candidate, req_id=req_id, notify=notify,
                 publish_output="--no-publish" not in sys.argv)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
