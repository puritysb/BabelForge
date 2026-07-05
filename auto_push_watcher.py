"""Auto-push watcher: drains the pending queue whenever the device is reachable.

Designed to run under launchd (com.local.book-translator-watcher) on a short
cadence (~1 minute). Each run:

  1. Read pending-push.json. Empty → exit immediately (cheap).
  2. Force-discover the device (mDNS + subnet scan).
  3. If found, push every queued EPUB to /Books via HTTP POST.
  4. Successful pushes are removed from the queue; LINE notification sent.
  5. Unreachable → no-op (the next run will try again).

The watcher is stateless and safe to run concurrently with pipeline.py —
both touch pending-push.json only via pending_queue's atomic writes.
"""
from __future__ import annotations
import os
import sys
import time
import subprocess

import config
import pending_queue
import search as search_mod  # for notify_line
from device_push import discover_device, push_to_device


def drain_once(verbose: bool = False) -> dict:
    """One pass: discover device, push all pending books if found.

    Returns a summary dict: {queued, discovered, pushed, failed, ip, source}.
    """
    log = (sys.stderr.write if verbose else lambda *a, **k: None)
    pending = pending_queue.list_pending()
    summary = {"queued": len(pending), "discovered": False,
               "pushed": 0, "failed": 0,
               "ip": None, "source": None}

    if not pending:
        log("[watcher] queue empty\n")
        return summary

    log(f"[watcher] {len(pending)} book(s) pending; discovering device…\n")
    discovered = discover_device(force=True, verbose=verbose)
    if not discovered:
        log("[watcher] device not found — will retry next tick\n")
        return summary

    ip, source = discovered
    summary["discovered"] = True
    summary["ip"] = ip
    summary["source"] = source
    log(f"[watcher] device at {ip} ({source}); pushing {len(pending)} book(s)\n")
    # Announce the batch start so the user has real-time feedback on LINE
    # (the device's own screen shows upload progress too, but a phone ping is
    # less ambiguous than watching the e-ink refresh).
    search_mod.notify_line(
        f"📤 기기 전송 시작 ({len(pending)}권, 기기 {ip})\n"
        f"File Transfer 모드를 유지해 주세요…"
    )

    pushed_titles = []
    for i, item in enumerate(pending, 1):
        epub = item.get("epub_path")
        if not epub or not os.path.isfile(epub):
            pending_queue.remove(epub)
            continue
        size_kb = os.path.getsize(epub) // 1024
        log(f"[watcher] [{i}/{len(pending)}] pushing {epub} ({size_kb} KB)\n")
        result = push_to_device(epub)
        pending_queue.increment_attempts(epub)
        if result.pushed:
            pending_queue.remove(epub)
            summary["pushed"] += 1
            title = item.get("title") or os.path.basename(epub)
            pushed_titles.append(title)
            log(f"[watcher] ✓ pushed {epub}\n")
            # Per-book progress ping (only meaningful when there's >1 book,
            # but cheap enough to always send).
            search_mod.notify_line(
                f"  [{i}/{len(pending)}] ✓ {title} ({size_kb} KB, "
                f"{result.elapsed_ms}ms)"
            )
        else:
            summary["failed"] += 1
            log(f"[watcher] ✗ {epub}: {result.error}\n")
            search_mod.notify_line(
                f"  [{i}/{len(pending)}] ✗ {item.get('title','?')[:40]} — "
                f"{result.error[:60]}"
            )
            # If the device vanished mid-batch, stop trying further books.
            if result.skipped:
                log("[watcher] device disappeared — stopping this pass\n")
                break

    if pushed_titles:
        msg = ("📖 기기 자동 전송 완료:\n" +
               "\n".join(f"  · {t}" for t in pushed_titles) +
               f"\n기기 /Books 폴더를 확인하세요 (총 {len(pushed_titles)}권). "
               f"File Transfer 모드를 빠져나가 읽으실 수 있습니다.")
        search_mod.notify_line(msg)
    elif summary["failed"]:
        search_mod.notify_line(
            f"⚠️ 전송 실패 ({summary['failed']}권). 기기가 File Transfer "
            f"모드인지 확인 후 다시 둬 주세요 — 다음 폴링에 재시도합니다."
        )
    return summary


def main():
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--once", action="store_true",
                    help="run a single drain and exit (default)")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="loop forever, sleeping SECONDS between drains")
    args = ap.parse_args()

    if args.loop:
        log = sys.stderr.write if args.verbose else lambda *a, **k: None
        while True:
            t0 = time.time()
            try:
                drain_once(verbose=args.verbose)
            except Exception as e:
                log(f"[watcher] loop error: {e}\n")
            elapsed = time.time() - t0
            sleep_for = max(5, args.loop - elapsed)
            log(f"[watcher] sleeping {sleep_for:.0f}s\n")
            time.sleep(sleep_for)
    else:
        s = drain_once(verbose=args.verbose)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        sys.exit(0 if s["pushed"] or not s["queued"] else 0)


if __name__ == "__main__":
    main()
