"""Install the pinned official EPUBCheck distribution under data/tools.

Run with the repository Python. Downloads are checked against the SHA-256
published on the official GitHub release; no Java/system installation is made.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

VERSION = "5.4.0"
SHA256 = "33350c61038e71dfb3d45a76aed04bf5481e6d5500cb780f6e98db8bbd15a28c"
BASE_DIR = Path(__file__).resolve().parents[1]
DEST = BASE_DIR / "data" / "tools" / f"epubcheck-{VERSION}"
URL = f"https://github.com/w3c/epubcheck/releases/download/v{VERSION}/epubcheck-{VERSION}.zip"


def main():
    if (DEST / "epubcheck.jar").is_file():
        print(DEST / "epubcheck.jar")
        return
    request = urllib.request.Request(URL, headers={"User-Agent": "BabelForge"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise RuntimeError("EPUBCheck archive checksum mismatch")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DEST.parent) as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RuntimeError("unsafe EPUBCheck archive member")
            archive.extractall(tmp)
        source = Path(tmp) / f"epubcheck-{VERSION}"
        if not (source / "epubcheck.jar").is_file():
            raise RuntimeError("EPUBCheck archive is missing its launcher")
        shutil.move(str(source), DEST)
    print(DEST / "epubcheck.jar")


if __name__ == "__main__":
    main()
