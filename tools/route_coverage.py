"""Compare the app's routes with Appendix B of the spec (the prod route list).

Run from the repo root: .venv/Scripts/python tools/route_coverage.py
Prints "built: N/296", then every spec operation the app lacks and every app route the spec does not list.
Exit code 1 when a spec operation is missing.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "spec" / "KH-6.5-co-che-prod.md"
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def spec_routes() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = text.split("## Appendix B.", 1)[1].split("```", 2)[1]
    out = set()
    for line in block.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in METHODS:
            out.add(f"{parts[0]} {_norm(parts[1])}")
    return out


def app_routes() -> set[str]:
    os.environ.update(KH_EMBEDDER="hash", KH_VECTOR_BACKEND="sqlite", KH_STORE_BACKEND="sqlite",
                      GRAPH_BACKEND="sqlite", KH_HOOKS_DB=tempfile.mktemp(suffix=".db"),
                      KH_PROJECTS_PATH=tempfile.mkdtemp(), KH_FEEDBACK_CRON="false",
                      KH_MESSAGE_CRON="false", KH_CAPS_CRONS="false")
    sys.path.insert(0, str(ROOT))
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app):
        pass
    out = set()
    for r in main.app.routes:
        for m in getattr(r, "methods", None) or []:
            if m in METHODS:
                out.add(f"{m} {_norm(r.path)}")
    return out


def main_() -> int:
    spec, app = spec_routes(), app_routes()
    missing = sorted(spec - app)
    extra = sorted(app - spec)
    print(f"built: {len(spec & app)}/{len(spec)}")
    for r in missing:
        print(f"MISSING {r}")
    for r in extra:
        print(f"EXTRA   {r}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main_())
