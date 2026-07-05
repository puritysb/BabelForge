"""Shared configuration for the bilingual book translator pipeline.

All paths are absolute and derived from this file's location so the scripts
work the same whether invoked by OpenClaw, cron, or by hand.

Mirrors yt_dubber/config.py conventions: atomic catalog writes, slug-based
URLs, venv isolation, OpenClaw message send for LINE notifications.
"""
import os


def _load_dotenv(path: str) -> None:
    """Populate os.environ from a .env file (KEY=VALUE per line). Does not
    override values already present in the real environment."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(BASE_DIR, ".env"))

DATA_DIR = os.path.join(BASE_DIR, "data")
LIBRARY_DIR = os.path.join(DATA_DIR, "library")          # produced bilingual EPUBs
CACHE_DIR = os.path.join(DATA_DIR, "cache")              # raw source downloads
LOGS_DIR = os.path.join(BASE_DIR, "logs")

CATALOG_PATH = os.path.join(DATA_DIR, "catalog.json")    # request state machine
ATTEMPTS_PATH = os.path.join(DATA_DIR, "attempts.json")  # book ids we already tried
PENDING_PUSH_PATH = os.path.join(DATA_DIR, "pending-push.json")  # auto-push queue
CHECKPOINT_DIR = os.path.join(DATA_DIR, "checkpoints")   # translation resume state

VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python3")

# ─── BabelForge MCP server (agent-facing typed interface; babelforge_mcp.py) ───
# The pipeline's own MCP *server* (distinct from the Z.ai MCP *client* below):
# exposes search/translate/status tools so any MCP-capable agent drives the
# appliance by URL instead of shelling out to scripts. Local-only bind.
MCP_HOST = os.environ.get("BABELFORGE_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("BABELFORGE_MCP_PORT", "8770"))

# ─── Calibre Content Server (OPDS delivery) ───
CALIBRE_LIBRARY = os.path.expanduser("~/Calibre-Library")
CALIBRE_PORT = 8080
CALIBREDB = "/opt/homebrew/bin/calibredb"
# calibredb must go THROUGH the running Content Server (direct db access is
# blocked while calibre-server holds the library lock). --enable-local-write
# is on, so the server accepts writes from localhost.
CALIBREDB_URL = f"http://localhost:{CALIBRE_PORT}"
OPDS_DOMAIN = "books.getlingo.store"                     # cloudflared → localhost:8080
OPDS_BASE_URL = f"https://{OPDS_DOMAIN}"
OPDS_FEED_URL = f"{OPDS_BASE_URL}/opds"

# ─── Translation (GLM-5.2 via Zhipu AI / OpenAI-compatible) ───
# API key is NOT stored here. Set it in this repo's .env or the environment.
# Accept both ZAI_API_KEY and GLM_API_KEY (Zhipu's preferred name).
ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_MODEL = "glm-5.2"                                    # 1M context, 8192 maxTokens
ZAI_API_KEY_ENV = "ZAI_API_KEY"                          # primary env var
ZAI_API_KEY_FALLBACK_ENV = "GLM_API_KEY"                 # alias Zhipu ships with
ZAI_MAX_TOKENS = 8192
TRANSLATE_BATCH_PARAGRAPHS = 20                          # max paragraphs per GLM call
# Also cap a batch by total *source* characters: dense prose (long paragraphs)
# translated 20-at-a-time can push the Korean output past ZAI_MAX_TOKENS (8192),
# which truncates/collapses the response and forces slow per-paragraph fallback.
# Packing to a char budget keeps output well under the ceiling and improves
# throughput. Small paragraphs still batch up to TRANSLATE_BATCH_PARAGRAPHS.
TRANSLATE_BATCH_MAX_CHARS = 2200
TRANSLATE_MAX_RETRIES = 4
TRANSLATE_TIMEOUT_S = 240
TRANSLATE_WORKERS = 8  # concurrent GLM calls (each ~20s on the coding plan;
                       # serial = ~12h for a 9000-sentence novel, 8 workers ≈ 1.5h)

# Bilingual granularity. When True (default), source paragraphs are split
# into sentences BEFORE translation, so each cp-original / cp-translation
# pair is one sentence — the reader sees short English/Chinese/etc. lines
# alternating with short Korean lines, which is what most bilingual readers
# expect. When False, the pair unit is the source's native paragraph (longer
# blocks, fewer TOC entries per chapter, but each "row" is a big wall of
# text). The firmware's ChapterHtmlSlimParser honours <p>-level markers
# regardless of length, so sentence-level is safe.
SENTENCE_LEVEL_SPLIT = True

# ─── Sources ───
GUTENDEX_API = "https://gutendex.com/books"
STANDARD_EBOOKS_OPDS = "https://standardebooks.org/opds/all"
ANNAS_ARCHIVE_BASE = "https://annas-archive.org"         # scraping fallback, explicit request only

# Default sources enabled for auto-suggest (public domain only). Anna's Archive
# is opt-in via the `annas:` query prefix to keep auto-suggest copyright-safe.
# `local` is excluded here — it only runs when route_query() sees a filesystem
# path (so the user can't accidentally trigger a file ingest with a title).
DEFAULT_SOURCES = ["gutenberg", "standard_ebooks"]

# ─── OpenClaw integration ───
OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"
LINE_TARGET = os.environ.get("OPENCLAW_LINE_TARGET",
                             "U754eaca90e1ca33ff5c06ca0f603dbe7")  # owner LINE id
MAX_CANDIDATES = 8                                       # candidates sent per search

# ─── Device push (HTTP POST /upload — best-effort, complements OPDS pull) ───
# After publish(), we attempt to push the EPUB directly to the device's File
# Transfer server. The device must be in File Transfer mode (HTTP port 80). If
# it isn't reachable, we silently skip — the user can still pull via OPDS.
#
# DEVICE_HOST defaults to "auto": discovery tries mDNS service browsing first
# (multicast, bypasses ISP DNS hijacking of .local), then falls back to a
# local-subnet port-80 scan with CrossPoint fingerprinting. Set
# OPENCLAW_XTEINK_HOST to pin a specific IP only as a last resort.
DEVICE_HOST = os.environ.get("OPENCLAW_XTEINK_HOST", "auto")
DEVICE_PUSH_ENABLED = True
DEVICE_PUSH_PATH = "/Books"                                # SD card destination folder
DEVICE_PUSH_TIMEOUT = 12                                  # total seconds for the push
DEVICE_PROBE_TIMEOUT = 3                                  # is the HTTP server up?

# ─── EPUB format contract (see crosspoint-agentdeck/docs/bilingual-epub.md) ───
ORIGINAL_CLASS = "cp-original"
TRANSLATION_CLASS = "cp-translation"
BILINGUAL_SUFFIX = "(KO bilingual)"
# Source / translation languages for the hybrid xml:lang marker (emitted
# alongside cp-* classes for standard alignment + screen-reader accessibility).
# The reader's parser matches the primary subtag against dc:language.
SOURCE_LANG = "en"
TRANSLATION_LANG = "ko"

# ─── Translation Quality Improvements ───
TWO_PASS_TRANSLATION = True  # Enable 2-Pass (Draft + Proofread) translation
GLOSSARY_PATH = os.path.join(DATA_DIR, "glossary.json")  # Terminology glossary path
# Auto-glossary: pipeline.py extracts recurring proper nouns/terms before
# translation and (when GLOSSARY_ENRICH) asks GLM for their canonical Korean
# rendering, so a name/place is translated the same way across all 8 workers.
# Enrichment costs one extra GLM call per book; on the z.ai Coding Plan that's
# negligible and it clearly lifts terminology consistency, so default ON.
GLOSSARY_ENABLED = True
GLOSSARY_ENRICH = True
# When GLOSSARY_WEB_SEARCH, enrichment first web-searches the top terms via the
# Z.ai web_search_prime MCP tool (see mcp_client.py) and grounds GLM's Korean
# renderings in real published usage instead of letting it guess — the whole
# point of a glossary is the *established* rendering of a name/place. Bounded to
# the GLOSSARY_WEB_SEARCH_TERMS most frequent terms to keep it to a few calls.
GLOSSARY_WEB_SEARCH = True
GLOSSARY_WEB_SEARCH_TERMS = 8

# ─── Z.ai MCP (Streamable HTTP; same Coding-Plan key as the REST API) ───
ZAI_MCP_WEB_SEARCH_URL = "https://api.z.ai/api/mcp/web_search_prime/mcp"
ZAI_MCP_TIMEOUT_S = 60



def ensure_dirs():
    for d in (DATA_DIR, LIBRARY_DIR, CACHE_DIR, LOGS_DIR, CHECKPOINT_DIR):
        os.makedirs(d, exist_ok=True)


def get_zai_api_key():
    """Read API key from env (never hard-coded). Tries ZAI_API_KEY first,
    then GLM_API_KEY (Zhipu's canonical name). Returns None if neither is
    set, so translate() surfaces a clear actionable error.
    """
    return (os.environ.get(ZAI_API_KEY_ENV)
            or os.environ.get(ZAI_API_KEY_FALLBACK_ENV))


def calibredb_cmd(*args):
    """Build a calibredb invocation through the running Content Server.

    Direct --library-path access collides with calibre-server's lock; routing
    via the HTTP API is the supported pattern when the server is running.
    """
    return [CALIBREDB, "--with-library", CALIBREDB_URL, *args]
