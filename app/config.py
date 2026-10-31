"""Central configuration (spec §1.4).

Loaded once at import time. Other areas add their own constants in their own modules using the
parsing helpers exported here (`_env`, `_bool`, `_flag`, `_int`, `_float`, `_identifier`, `_anchor`).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

try:  # python-dotenv is a runtime dependency, but never make config import fail on it
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


# ---------------------------------------------------------------------------
# App root and .env
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):  # pragma: no cover - desktop exe only
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")
load_dotenv()


# ---------------------------------------------------------------------------
# Parsing helpers (§1.4)
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _env(name: str, default: str | None = None) -> str | None:
    """Env value; blank counts as unset."""
    v = os.environ.get(name)
    if v is None:
        return default
    v = _strip(v)
    return v if v != "" else default


def _bool(name: str, default: bool) -> bool:
    """Value stripped of quotes, lower-cased, == "true"."""
    v = os.environ.get(name)
    if v is None or _strip(v) == "":
        return bool(default)
    return _strip(v).lower() == "true"


def _flag(name: str, default: str = "") -> bool:
    return (_env(name, default) or "").lower() == "true"


def _int(name: str, default: int) -> int:
    v = _env(name)
    if v is None:
        return int(default)
    try:
        return int(float(v))
    except ValueError:
        return int(default)


def _float(name: str, default: float) -> float:
    v = _env(name)
    if v is None:
        return float(default)
    try:
        return float(v)
    except ValueError:
        return float(default)


def _identifier(name: str, default: str) -> str:
    v = _env(name, default) or default
    if not _IDENT_RE.match(v):
        raise ValueError(f"{name}={v!r} is not a valid SQL identifier")
    return v


def _anchor(path: str | os.PathLike) -> str:
    """Relative paths are anchored to the app root, never cwd."""
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return str(p)


def _on_off(name: str, default: bool) -> bool:
    """Truthy unless 0/false/no/off (used by KH_HOTLOG)."""
    v = _env(name)
    if v is None:
        return default
    return v.lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Server / version
# ---------------------------------------------------------------------------

HOST = _env("KH_HOST", "0.0.0.0")
PORT = _int("KH_PORT", _int("PORT", 8000))
NO_TRAY = _env("KH_NO_TRAY") is not None
PRELOAD_VISION = _bool("KH_PRELOAD_VISION", True)


def _resolve_version() -> tuple[str, str]:
    v = _env("KH_VERSION")
    if v:
        return v, "env:KH_VERSION"
    f = ROOT / "VERSION"
    try:
        txt = f.read_text(encoding="utf-8").strip()
        if txt:
            return txt, "file:VERSION"
    except OSError:
        pass
    return "v6.5", "default"


KH_VERSION, KH_VERSION_SOURCE = _resolve_version()

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

DB_BACKEND = (_env("KH_DB_BACKEND") or "").lower() or None
if DB_BACKEND is not None and DB_BACKEND not in ("local", "hana"):
    raise ValueError(f"KH_DB_BACKEND must be 'local' or 'hana', got {DB_BACKEND!r}")

# Rebuild: the local preset maps to the dependency-free SQLite adapters (no Kùzu / Chroma required).
_PRESETS = {
    "local": {"graph": "sqlite", "store": "sqlite", "vector": "sqlite"},
    "hana": {"graph": "hana", "store": "hana", "vector": "hana"},
}
_preset = _PRESETS.get(DB_BACKEND or "", {})

STORE_BACKEND = (_env("KH_STORE_BACKEND") or _preset.get("store") or "sqlite").lower()
VECTOR_BACKEND = (_env("KH_VECTOR_BACKEND") or _preset.get("vector") or "sqlite").lower()
GRAPH_BACKEND = (_env("GRAPH_BACKEND") or _preset.get("graph") or "auto").lower()
if STORE_BACKEND not in ("sqlite", "hana"):
    raise ValueError(f"KH_STORE_BACKEND must be 'sqlite' or 'hana', got {STORE_BACKEND!r}")
if VECTOR_BACKEND not in ("sqlite", "hana", "chroma"):
    raise ValueError(f"KH_VECTOR_BACKEND must be 'sqlite', 'hana' or 'chroma', got {VECTOR_BACKEND!r}")
if GRAPH_BACKEND not in ("auto", "kuzu", "hana", "sqlite"):
    raise ValueError(f"GRAPH_BACKEND must be auto|kuzu|hana|sqlite, got {GRAPH_BACKEND!r}")

# ---------------------------------------------------------------------------
# HANA (explicit env wins, else VCAP_SERVICES binding)
# ---------------------------------------------------------------------------

HANA_SERVICE = _env("KH_HANA_SERVICE", "") or ""


def _hana_binding() -> dict:
    raw = os.environ.get("VCAP_SERVICES")
    if not raw:
        return {}
    try:
        vcap = json.loads(raw)
    except ValueError:
        return {}
    for entries in vcap.values():
        for e in entries or []:
            label = (e.get("label") or "").lower()
            tags = [str(t).lower() for t in (e.get("tags") or [])]
            if label != "hana" and "hana" not in tags:
                continue
            if HANA_SERVICE and e.get("name") != HANA_SERVICE:
                continue
            return e.get("credentials") or {}
    return {}


_HB = _hana_binding()
HANA_BOUND = bool(_HB)
HANA_HOST = _env("HANA_HOST") or _HB.get("host") or "localhost"
HANA_PORT = int(_env("HANA_PORT") or _HB.get("port") or 39041)
HANA_USERNAME = _env("HANA_USERNAME") or _HB.get("user") or "SYSTEM"
HANA_PASSWORD = _env("HANA_PASSWORD") or _HB.get("password") or ""
HANA_SCHEMA = _env("HANA_SCHEMA") or _HB.get("schema") or "KNOWLEDGE_HUB"
HANA_ENCRYPT = _bool("HANA_ENCRYPT", HANA_BOUND)
HANA_SSL_VALIDATE = _bool("HANA_SSL_VALIDATE", True)
HANA_CONNECT_TIMEOUT_MS = _int("HANA_CONNECT_TIMEOUT_MS", 0)
HANA_POOL_SIZE = _int("HANA_POOL_SIZE", 5)
HANA_MAX_OVERFLOW = _int("HANA_MAX_OVERFLOW", 0)
HANA_POOL_RECYCLE = _int("HANA_POOL_RECYCLE", 0)
HANA_POOL_TIMEOUT = _float("HANA_POOL_TIMEOUT", 30)
HANA_GRAPH_POOL_TIMEOUT = _float("HANA_GRAPH_POOL_TIMEOUT", 10)
HANA_GRAPH_WORKSPACE = _identifier("HANA_GRAPH_WORKSPACE", "KH_GRAPH_WORKSPACE")
GRAPH_TABLE_PREFIX = _identifier("KH_GRAPH_TABLE_PREFIX", "KH_")
HANA_VEC_COLLECTIONS_TABLE = _identifier("KH_HANA_VEC_COLLECTIONS_TABLE", "KH_VEC_COLLECTIONS")
HANA_VEC_ROWS_TABLE = _identifier("KH_HANA_VEC_ROWS_TABLE", "KH_VEC_ROWS")
DOCS_TABLE = _identifier("KH_DOCS_TABLE", "kh_docs")

# ---------------------------------------------------------------------------
# Local file stores
# ---------------------------------------------------------------------------

CHROMA_PATH = _anchor(_env("CHROMA_PATH", "./chroma_data"))
COLLECTION_NAME = _env("KH_COLLECTION", "knowledge_hub_vectors")
KEYS_COLLECTION_NAME = _env("KH_KEYS_COLLECTION", "knowledge_hub_keys")
KUZU_PATH = _anchor(_env("KUZU_PATH", "./kuzu_data"))  # DEAD in the original
KUZU_PROJECTS_PATH = _anchor(_env("KH_KUZU_PROJECTS_PATH", "./kuzu_projects"))
PROJECTS_PATH = _anchor(_env("KH_PROJECTS_PATH", "./projects"))
HOOKS_DB_PATH = _anchor(_env("KH_HOOKS_DB", "./hooks.db"))
SQLITE_TIMEOUT_S = _float("KH_SQLITE_TIMEOUT", 10)
SQLITE_WAL = _bool("KH_SQLITE_WAL", True)
KUZU_BUFFER_POOL_MB = _int("KH_KUZU_BUFFER_POOL_MB", 1024)
MAX_OPEN_PROJECTS = _int("KH_MAX_OPEN_PROJECTS", 6)
MAX_OPEN_PROJECT_GRAPHS = MAX_OPEN_PROJECTS
MAX_OPEN_COLLECTIONS = _int("KH_MAX_OPEN_COLLECTIONS", 10)

# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = _env("KH_EMBEDDING_MODEL", "bge-small-en-v1.5")
MODEL_DIR = _env("KH_MODEL_DIR")
EMBEDDER = (_env("KH_EMBEDDER", "local") or "local").lower()
if EMBEDDER not in ("local", "remote", "auto", "hash"):
    raise ValueError(f"KH_EMBEDDER must be local|remote|auto|hash, got {EMBEDDER!r}")
EMBED_URL = (_env("KH_EMBED_URL", "") or "").rstrip("/")
if EMBEDDER in ("remote", "auto") and not EMBED_URL:
    raise ValueError(f"KH_EMBEDDER={EMBEDDER} requires KH_EMBED_URL")
EMBED_TOKEN = _env("KH_EMBED_TOKEN", "") or ""
EMBED_TIMEOUT_S = _float("KH_EMBED_TIMEOUT_S", 15)
EMBED_BATCH_MAX = _int("KH_EMBED_BATCH_MAX", 64)
EMBED_RETRIES = _int("KH_EMBED_RETRIES", 2)
EMBED_FAIL_THRESHOLD = _int("KH_EMBED_FAIL_THRESHOLD", 3)
EMBED_COOLDOWN_S = _float("KH_EMBED_COOLDOWN_S", 60)
# service side (app/embed_server.py)
EMBED_BATCH_WINDOW_MS = _float("KH_EMBED_BATCH_WINDOW_MS", 5)
EMBED_MAX_BATCH = _int("KH_EMBED_MAX_BATCH", 64)
EMBED_MAX_TEXTS = _int("KH_EMBED_MAX_TEXTS", 256)
EMBED_MAX_CHARS = _int("KH_EMBED_MAX_CHARS", 20000)
EMBED_MAX_BODY = _int("KH_EMBED_MAX_BODY", 8388608)
EMBED_THREADS = _int("KH_EMBED_THREADS", 0)
EMBED_ALLOW_NO_TOKEN = _bool("KH_EMBED_ALLOW_NO_TOKEN", False)
EMBED_HOST = _env("KH_EMBED_HOST", "0.0.0.0")
EMBED_PORT = _int("KH_EMBED_PORT", _int("PORT", 8010))
EMB_VER = _env("KH_EMB_VER", "") or ""


def _model_roots() -> list[Path]:
    if MODEL_DIR:
        return [Path(_anchor(MODEL_DIR))]
    roots: list[Path] = []
    if getattr(sys, "frozen", False):  # pragma: no cover
        exe = Path(sys.executable).resolve().parent
        if exe.name == "dist":
            roots.append(exe.parent / "models")
        roots.append(exe / "models")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass) / "models")
    else:
        roots.append(ROOT / "models")
    return roots


MODEL_ROOTS = _model_roots()


def local_model_path(name: str | None = None) -> Path | None:
    """First root containing `<name>/config_sentence_transformers.json` (a complete save)."""
    name = name or EMBEDDING_MODEL
    for r in MODEL_ROOTS:
        if (r / name / "config_sentence_transformers.json").is_file():
            return r / name
    return None


# ---------------------------------------------------------------------------
# Retrieval / chunking / hot window
# ---------------------------------------------------------------------------

CHUNK_WORD_THRESHOLD = _int("CHUNK_WORD_THRESHOLD", 200)
ONTOLOGY_HOPS = _int("ONTOLOGY_HOPS", 3)
VECTOR_SCORE_THRESHOLD = _float("VECTOR_SCORE_THRESHOLD", 0.35)  # DEAD (never read)
CHUNK_MIN_CHARS = _int("KH_CHUNK_MIN_CHARS", 1800)
CHUNK_CHARS = _int("KH_CHUNK_CHARS", 800)
CHUNK_STRIDE = _int("KH_CHUNK_STRIDE", 700)
CLIP_HEAD_CHARS = _int("KH_CLIP_HEAD_CHARS", 160)
HOTLOG_ENABLED = _on_off("KH_HOTLOG", True)
HOTLOG_SLOTS = _int("KH_HOTLOG_SLOTS", MAX_OPEN_PROJECTS)
HOTLOG_MAX_PER_SLOT = _int("KH_HOTLOG_MAX_PER_SLOT", 50)
HOTLOG_MAX_AGE_DAYS = _float("KH_HOTLOG_MAX_AGE_DAYS", 14)
HOTLOG_SCOPE = _env("KH_HOTLOG_SCOPE", "log")
HOTLOG_COLLECTION = "kh_hotlog"

HIERARCHY_EDGES = ("IS_PART_OF", "BELONGS_TO", "SUBCLASS_OF", "PART_OF", "SUBCATEGORY_OF")

# ---------------------------------------------------------------------------
# Security / identity
# ---------------------------------------------------------------------------

API_KEY = _env("KH_API_KEY")
AUTH_ISSUER = (_env("KH_AUTH_ISSUER") or "").rstrip("/") or None
AUTH_AUDIENCE = _env("KH_AUTH_AUDIENCE", "kh-api")
AUTH_CLIENT_ID = _env("KH_AUTH_CLIENT_ID", "kh-cli")
ACTING_ISSUERS_RAW = _env("KH_ACTING_ISSUERS")
PERSONAL_PREFIX = _env("KH_PERSONAL_PREFIX", "personal-")
PERSONAL_SCOPE = _env("KH_PERSONAL_SCOPE", "personalization")
PERSONAL_LESSON_SCOPE = _env("KH_PERSONAL_LESSON_SCOPE", "lesson")
PROJECT_ACCESS_CACHE_S = _float("KH_PROJECT_ACCESS_CACHE_S", 5)

ORCHESTRATOR_ID = "orchestrator"
LESSON_PROJECT = _env("KH_TURN_LESSON_PROJECT", "lesson-learned")

# ---------------------------------------------------------------------------
# Jobs / eventing
# ---------------------------------------------------------------------------

FEEDBACK_CRON = _bool("KH_FEEDBACK_CRON", True)
FEEDBACK_CRON_SECONDS = _int("KH_FEEDBACK_CRON_SECONDS", 300)
MESSAGE_CRON = _bool("KH_MESSAGE_CRON", True)
MESSAGE_CRON_MINUTES = _int("KH_MESSAGE_CRON_MINUTES", 10)
CAPS_CRONS = _bool("KH_CAPS_CRONS", True)
LEARNBACK_LLM = _env("KH_LEARNBACK_LLM")
EVENTS_ON = (_env("KH_EVENTS", "off") or "off").lower() == "on"
EVENTS_NS = _env("KH_EVENTS_NS", "kh")
EVENTS_OUTBOX_MAX = _int("KH_EVENTS_OUTBOX_MAX", 20000)

CF_INSTANCE_GUID = _env("CF_INSTANCE_GUID")
