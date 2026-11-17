"""Build the Claude Desktop extension: dist/knowledge-hub-<version>.mcpb.

An .mcpb file is a zip with manifest.json at its root. This writes it with Python (no npx/mcpb needed):
manifest.json and pyproject.toml carry kh_helper.__version__, src/server.py is the entry point and the
kh_helper package goes to src/kh_helper.

    python tools/kh-helper/desktop/build_mcpb.py [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(os.path.dirname(HERE), "kh_helper")


def version() -> str:
    with open(os.path.join(PACKAGE, "__init__.py"), encoding="utf-8") as f:
        return re.search(r'^__version__ = "([^"]+)"', f.read(), re.M).group(1)


def package_files() -> list[tuple[str, str]]:
    """(source path, path inside the bundle) for every kh_helper source file."""
    out = []
    for root, dirs, files in os.walk(PACKAGE):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, os.path.dirname(PACKAGE)).replace(os.sep, "/")
            out.append((src, "src/" + rel))
    return out


def build(out_dir: str | None = None) -> str:
    ver = version()
    out_dir = out_dir or os.path.join(HERE, "dist")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(HERE, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["version"] = ver
    with open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8") as f:
        pyproject = re.sub(r'^version = "[^"]*"', f'version = "{ver}"', f.read(), count=1, flags=re.M)
    path = os.path.join(out_dir, f"knowledge-hub-{ver}.mcpb")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        z.writestr("pyproject.toml", pyproject)
        z.write(os.path.join(HERE, "src", "server.py"), "src/server.py")
        for src, arc in package_files():
            z.write(src, arc)
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", help="output folder (default: desktop/dist)")
    print(build(p.parse_args(argv).out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
