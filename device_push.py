"""Best-effort HTTP POST push of the bilingual EPUB to the Xteink device.

Mirrors `curl -X POST -F "file=@book.epub" http://<device>/upload?path=/Books`
— the File Transfer server endpoint documented in
crosspoint-agentdeck/docs/webserver-endpoints.md.

The device's HTTP server only runs while it's in **File Transfer** mode. We
auto-discover its IP via (1) mDNS service browsing, then (2) local-subnet
port-80 scan with CrossPoint fingerprinting. A short-lived discovery cache
avoids re-scanning on every call within the same session. If the device is
off/asleep/not in File Transfer, discovery fails fast and push returns
"skipped" so publish() success is preserved. The user can always fall back
to OPDS pull from the device's OPDS Browser.

No IP is hard-coded — DHCP changes are tolerated by re-running discovery.
"""
from __future__ import annotations
import json
import os
import socket
import struct
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from typing import Optional

import requests

import config


@dataclass
class PushResult:
    pushed: bool                  # file landed on the device
    skipped: bool = False         # True = device not reachable, not an error
    url: Optional[str] = None     # device URL we tried
    path: Optional[str] = None    # SD card destination
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    discovered_ip: Optional[str] = None
    discovery_source: Optional[str] = None    # mdns | scan | cache | env | none

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Discovery ───

DEVICE_CACHE = os.path.join(config.DATA_DIR, ".device-cache.json")
DEVICE_CACHE_TTL_S = 300         # 5 min — short so we tolerate DHCP/mode changes
DEVICE_FINGERPRINTS = (b"crosspoint", b"CrossPoint", b"Crosspoint",
                       b"file manager", b"file transfer", b"CrossPoint-Reader")


def _local_subnets() -> list[tuple[int, int]]:
    """Yield (ip, mask_bits) for subnets to scan. Uses the primary interface's
    IPv4 address; assumes the device is on the same L2 segment (typical for
    home Wi-Fi where the Mac and the e-reader share the AP)."""
    # UDP socket trick to find the primary interface's source IP without
    # actually sending anything on the wire.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return []
    if not local_ip or local_ip.startswith("127."):
        return []
    # Scan /24 first (most home networks), then widen to /22 if it looks like a
    # private 192.168.x range (the /24 case is also emitted for /22 networks so
    # the immediate neighbours are checked first).
    subnets: list[tuple[str, int]] = [(local_ip, 24)]
    if local_ip.startswith("192.168."):
        subnets.append((local_ip, 22))
    return subnets


def _subnet_hosts(ip: str, mask_bits: int) -> list[str]:
    """Enumerate host IPs in the given subnet. Caps to /22 max for safety."""
    if mask_bits < 24:
        mask_bits = 24
    if mask_bits > 24:
        mask_bits = 22 if mask_bits <= 22 else 24
    octets = [int(x) for x in ip.split(".")]
    # /24 scan
    if mask_bits == 24:
        return [f"{octets[0]}.{octets[1]}.{octets[2]}.{i}" for i in range(1, 255)]
    # /22 scan (256 * 4 = 1024 hosts — capped via timeout in callers)
    third = octets[2] & 0xFC  # round down to /22 boundary
    out = []
    for t in range(third, third + 4):
        for i in range(1, 255):
            out.append(f"{octets[0]}.{octets[1]}.{t}.{i}")
    return out


def _looks_like_crosspoint(ip: str, timeout: float = 0.8) -> bool:
    """HTTP GET / and check the body for CrossPoint fingerprints.

    Uses requests so the response is gzip-decoded automatically — the device
    server compresses its pages, and reading raw bytes hid the keywords from
    the earlier urllib version (root cause of the silent discovery miss).
    """
    try:
        r = requests.get(f"http://{ip}/", timeout=timeout)
        body = r.content[:4096]
        return any(fp in body for fp in DEVICE_FINGERPRINTS)
    except Exception:
        return False


def _discover_via_scan(timeout_s: float = 6.0) -> Optional[str]:
    """Parallel subnet scan for port 80, fingerprint each hit."""
    import concurrent.futures
    candidates: list[str] = []
    seen: set[str] = set()
    for ip, bits in _local_subnets():
        for h in _subnet_hosts(ip, bits):
            if h not in seen:
                seen.add(h)
                candidates.append(h)
    deadline = time.time() + timeout_s

    def probe(ip: str) -> Optional[str]:
        if time.time() > deadline:
            return None
        try:
            s = socket.create_connection((ip, 80), timeout=0.4)
            s.close()
            if _looks_like_crosspoint(ip, timeout=0.6):
                return ip
        except Exception:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        futs = {ex.submit(probe, ip): ip for ip in candidates}
        for f in concurrent.futures.as_completed(futs, timeout=timeout_s + 1):
            try:
                r = f.result()
                if r:
                    # Cancel the rest as soon as we find it.
                    for other in futs:
                        other.cancel()
                    return r
            except Exception:
                continue
    return None


def _discover_via_mdns(timeout_s: float = 3.0) -> Optional[str]:
    """mDNS service browsing. Looks for CrossPoint's HTTP service advertisement.
    Uses multicast 224.0.0.251:5353 directly — does NOT rely on the system
    resolver, so it bypasses ISP DNS hijacking of the .local TLD."""
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        return None

    found_ips: list[str] = []
    found_crosspoint: list[str] = []

    class _L:
        def add_service(self, zc, type_, name):
            self._maybe(zc, type_, name)
        def update_service(self, zc, type_, name):
            self._maybe(zc, type_, name)
        def remove_service(self, zc, type_, name):
            pass
        def _maybe(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, timeout=800)
            except Exception:
                info = None
            if not info or not info.addresses:
                return
            ip = socket.inet_ntoa(info.addresses[0])
            found_ips.append(ip)
            # Match by service name or by HTTP fingerprint.
            name_l = (name or "").lower()
            if "crosspoint" in name_l or "reader" in name_l:
                found_crosspoint.append(ip)
            else:
                # Verify via HTTP fingerprint as a secondary signal.
                if _looks_like_crosspoint(ip, timeout=0.6):
                    found_crosspoint.append(ip)

    zc = None
    try:
        zc = Zeroconf()
        listener = _L()
        # CrossPoint may advertise under _http._tcp or a custom _crosspoint._tcp
        for stype in ("_crosspoint._tcp.local.", "_http._tcp.local."):
            try:
                ServiceBrowser(zc, stype, listener)
            except Exception:
                pass
        time.sleep(timeout_s)
        if found_crosspoint:
            return found_crosspoint[0]
        if found_ips:
            return found_ips[0]
    except Exception:
        pass
    finally:
        if zc:
            try:
                zc.close()
            except Exception:
                pass
    return None


def _load_cache() -> Optional[tuple[str, str]]:
    if not os.path.isfile(DEVICE_CACHE):
        return None
    try:
        d = json.load(open(DEVICE_CACHE, "r"))
        if time.time() * 1000 - d.get("discovered_at_ms", 0) < DEVICE_CACHE_TTL_S * 1000:
            return d.get("ip"), "cache"
    except Exception:
        pass
    return None


def _save_cache(ip: str, source: str) -> None:
    try:
        os.makedirs(os.path.dirname(DEVICE_CACHE), exist_ok=True)
        json.dump({"ip": ip, "discovered_at_ms": int(time.time() * 1000),
                   "source": source}, open(DEVICE_CACHE, "w"))
    except Exception:
        pass


def discover_device(force: bool = False, verbose: bool = False) -> Optional[tuple[str, str]]:
    """Find the device IP. Returns (ip, source) or None.

    Order: explicit env override > cache (≤5min) > mDNS > subnet scan.
    `force=True` bypasses cache and re-runs mDNS+scan.
    """
    # 0. Explicit override (rare — opt-in only)
    env_host = os.environ.get("OPENCLAW_XTEINK_HOST") or config.DEVICE_HOST
    if env_host and env_host not in ("auto", "crosspoint.local"):
        return env_host, "env"

    if not force:
        cached = _load_cache()
        if cached:
            return cached

    log = sys.stderr.write if verbose else lambda *a, **k: None
    # 1. mDNS service browsing (bypasses ISP DNS by using multicast directly)
    log("[discover] trying mDNS…\n")
    ip = _discover_via_mdns(timeout_s=3.0)
    if ip:
        log(f"[discover] mDNS found {ip}\n")
        _save_cache(ip, "mdns")
        return ip, "mdns"

    # 2. Subnet scan + fingerprint
    log("[discover] mDNS empty, scanning subnet…\n")
    ip = _discover_via_scan(timeout_s=6.0)
    if ip:
        log(f"[discover] scan found {ip}\n")
        _save_cache(ip, "scan")
        return ip, "scan"
    return None


def _server_alive(base_url: str, timeout: int) -> bool:
    try:
        r = requests.get(base_url + "/", timeout=timeout)
        return r.status_code < 500
    except requests.RequestException:
        return False


def push_to_device(epub_path: str,
                   dest_path: Optional[str] = None) -> PushResult:
    """Attempt to POST the EPUB to /upload on the device. Never raises."""
    if not config.DEVICE_PUSH_ENABLED:
        return PushResult(pushed=False, skipped=True,
                          error="device push disabled in config")
    if not os.path.isfile(epub_path):
        return PushResult(pushed=False, error=f"no such file: {epub_path}")

    dest = dest_path or config.DEVICE_PUSH_PATH
    started = time.time()

    discovered = discover_device()
    if not discovered:
        return PushResult(pushed=False, skipped=True,
                          error="device not found via mDNS or subnet scan "
                                "(is it in File Transfer mode?)",
                          elapsed_ms=int((time.time() - started) * 1000),
                          discovery_source="none")
    ip, source = discovered

    base_url = f"http://{ip}"
    if not _server_alive(base_url, config.DEVICE_PROBE_TIMEOUT):
        # Server found at discovery time may have gone to sleep — retry once.
        discovered = discover_device(force=True)
        if discovered:
            ip, source = discovered
            base_url = f"http://{ip}"
        if not _server_alive(base_url, config.DEVICE_PROBE_TIMEOUT):
            return PushResult(pushed=False, skipped=True, url=base_url,
                              error="HTTP server not responding "
                                    "(device not in File Transfer mode?)",
                              elapsed_ms=int((time.time() - started) * 1000),
                              discovered_ip=ip, discovery_source=source)

    upload_url = f"{base_url}/upload"
    fname = os.path.basename(epub_path)
    try:
        with open(epub_path, "rb") as f:
            files = {"file": (fname, f, "application/epub+zip")}
            # CrossPoint's File Manager posts the destination as a query
            # parameter. Keep this aligned with the firmware endpoint so the
            # book always lands in /Books instead of the server's current path.
            r = requests.post(upload_url, params={"path": dest}, files=files,
                              timeout=config.DEVICE_PUSH_TIMEOUT)
        elapsed = int((time.time() - started) * 1000)
        if r.status_code < 400:
            return PushResult(pushed=True, url=upload_url, path=dest,
                              elapsed_ms=elapsed,
                              discovered_ip=ip, discovery_source=source)
        return PushResult(pushed=False, url=upload_url, path=dest,
                          error=f"HTTP {r.status_code}: {r.text[:200]!r}",
                          elapsed_ms=elapsed,
                          discovered_ip=ip, discovery_source=source)
    except requests.RequestException as e:
        return PushResult(pushed=False, skipped=True, url=base_url,
                          error=f"{type(e).__name__}: {e}",
                          elapsed_ms=int((time.time() - started) * 1000),
                          discovered_ip=ip, discovery_source=source)


def push_font_family(family_name: str, cpfont_dir: str,
                     timeout: int = 20) -> PushResult:
    """Push a cpfont family to the device via POST /api/fonts/upload.

    This is the font-specific endpoint (not the generic /upload): it validates
    the CPFONT magic bytes, places files under /.fonts/<family>/, and refreshes
    the SD font registry so the family shows up in Settings → Manage Fonts
    without a reboot. One .cpfont per size (12/14/16/18) is the norm.

    The device must be in File Transfer mode (HTTP port 80). Returns a
    PushResult; never raises.
    """
    if not os.path.isdir(cpfont_dir):
        return PushResult(pushed=False, error=f"no such dir: {cpfont_dir}")
    cpfonts = sorted(
        os.path.join(cpfont_dir, f)
        for f in os.listdir(cpfont_dir)
        if f.endswith(".cpfont")
    )
    if not cpfonts:
        return PushResult(pushed=False,
                          error=f"no .cpfont files in {cpfont_dir}")

    started = time.time()
    discovered = discover_device()
    if not discovered:
        return PushResult(pushed=False, skipped=True,
                          error="device not found via mDNS or subnet scan "
                                "(is it in File Transfer mode?)",
                          elapsed_ms=int((time.time() - started) * 1000),
                          discovery_source="none")
    ip, source = discovered
    base_url = f"http://{ip}"
    if not _server_alive(base_url, config.DEVICE_PROBE_TIMEOUT):
        return PushResult(pushed=False, skipped=True, url=base_url,
                          error="HTTP server not responding",
                          elapsed_ms=int((time.time() - started) * 1000),
                          discovered_ip=ip, discovery_source=source)

    upload_url = f"{base_url}/api/fonts/upload"
    pushed, errors = 0, []
    for cf in cpfonts:
        try:
            with open(cf, "rb") as f:
                files = {"file": (os.path.basename(cf), f,
                                  "application/octet-stream")}
                data = {"family": family_name}
                r = requests.post(upload_url, files=files, data=data,
                                  timeout=timeout)
            if r.status_code < 400:
                pushed += 1
            else:
                errors.append(f"{os.path.basename(cf)}: HTTP {r.status_code} "
                              f"{r.text[:120]!r}")
        except requests.RequestException as e:
            errors.append(f"{os.path.basename(cf)}: {type(e).__name__}: {e}")

    elapsed = int((time.time() - started) * 1000)
    if pushed == len(cpfonts):
        return PushResult(pushed=True, url=upload_url,
                          path=f"/.fonts/{family_name}/",
                          elapsed_ms=elapsed,
                          discovered_ip=ip, discovery_source=source)
    return PushResult(pushed=False, url=upload_url,
                      error="; ".join(errors) or "no files pushed",
                      elapsed_ms=elapsed,
                      discovered_ip=ip, discovery_source=source)


def main():
    """CLI:
       device_push.py <epub> [dest_path]               — push one EPUB
       device_push.py --discover [--verbose]            — just run discovery
       device_push.py --push-font-family 'NAME DIR'     — push a cpfont family
    """
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("epub", nargs="?")
    ap.add_argument("dest_path", nargs="?")
    ap.add_argument("--discover", action="store_true",
                    help="run discovery only, print IP and exit")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--push-font-family", dest="font_family",
                    help="Push a cpfont family: 'FamilyName /path/to/dir'")
    args = ap.parse_args()

    if args.font_family:
        parts = args.font_family.split(None, 1)
        if len(parts) != 2:
            print("usage: --push-font-family 'FamilyName /path/to/dir'",
                  file=sys.stderr)
            sys.exit(2)
        res = push_font_family(parts[0], parts[1])
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(0 if res.pushed else 1)

    if args.discover:
        r = discover_device(force=True, verbose=True)
        if r:
            print(f"{r[0]}\t{r[1]}")
            sys.exit(0)
        print("not found", file=sys.stderr)
        sys.exit(1)

    if not args.epub:
        print("usage: device_push.py <epub> [dest] | --discover | "
              "--push-font-family 'NAME DIR'", file=sys.stderr)
        sys.exit(2)
    result = push_to_device(args.epub, args.dest_path)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    sys.exit(0 if (result.pushed or result.skipped) else 1)


if __name__ == "__main__":
    main()
