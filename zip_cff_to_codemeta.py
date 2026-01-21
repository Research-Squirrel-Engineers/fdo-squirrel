#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reference implementation step 1:
Load a ZIP (local or remote), extract CITATION.cff,
convert it to CodeMeta using cffconvert.
Designed to be run directly from VS Code (no CLI args).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlretrieve


# =====================================================
# CONFIGURATION (EDIT THIS IN VS CODE)
# =====================================================

# Either:
#   - local path: "C:/data/my_zenodo_export.zip"
#   - or URL:     "https://zenodo.org/record/.../files/example.zip?download=1"
ZIP_SOURCE = r"C:/git/fuzzy-wobbly-semanticalignment/fuzzy-wobbly-semanticalignment.zip"

# Output CodeMeta file
OUTPUT_CODEMETA = Path("codemeta.json")

# Optional: write extracted CITATION.cff for inspection/debugging
WRITE_EXTRACTED_CFF = True
EXTRACTED_CFF_PATH = Path("CITATION_extracted.cff")

# =====================================================

CFF_FILENAMES = ("CITATION.cff", "citation.cff")


def is_url(s: str) -> bool:
    try:
        return urlparse(s).scheme in ("http", "https")
    except Exception:
        return False


def obtain_zip_to_local(source: str, workdir: Path) -> Path:
    if is_url(source):
        target = workdir / "input.zip"
        print(f"Downloading ZIP from URL:\n  {source}")
        urlretrieve(source, target)
        return target
    else:
        p = Path(source).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"ZIP not found: {p}")
        print(f"Using local ZIP:\n  {p}")
        return p


def find_cff_in_zip(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()

    # Prefer top-level exact names
    for fn in CFF_FILENAMES:
        if fn in names:
            return fn

    # Case-insensitive match
    lower_map = {n.lower(): n for n in names}
    for fn in CFF_FILENAMES:
        if fn.lower() in lower_map:
            return lower_map[fn.lower()]

    # Last resort: anything containing "citation" and ending in .cff
    candidates = [
        n for n in names if n.lower().endswith(".cff") and "citation" in n.lower()
    ]
    if candidates:
        return sorted(candidates, key=lambda s: (s.count("/"), len(s)))[0]

    raise FileNotFoundError("No CITATION.cff found inside the ZIP.")


def extract_member(zip_path: Path, member: str, out_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        out_path.write_bytes(zf.read(member))


def run_cffconvert_to_codemeta(cff_path: Path) -> dict:
    exe = shutil.which("cffconvert")
    if not exe:
        raise RuntimeError(
            "cffconvert not found.\n" "Install it via:\n" "  pip install cffconvert"
        )

    cmd = [exe, "--format", "codemeta", str(cff_path)]
    print("Running:", " ".join(cmd))

    proc = subprocess.run(cmd, capture_output=True, text=True)

    if proc.returncode != 0:
        raise RuntimeError(
            "cffconvert failed:\n" f"STDOUT:\n{proc.stdout}\n" f"STDERR:\n{proc.stderr}"
        )

    return json.loads(proc.stdout)


def main() -> None:
    print("=== ZIP → CFF → CodeMeta (Reference Implementation) ===")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        zip_path = obtain_zip_to_local(ZIP_SOURCE, td_path)

        cff_member = find_cff_in_zip(zip_path)
        print(f"Found CITATION file in ZIP:\n  {cff_member}")

        cff_tmp = td_path / "CITATION.cff"
        extract_member(zip_path, cff_member, cff_tmp)

        if WRITE_EXTRACTED_CFF:
            EXTRACTED_CFF_PATH.write_bytes(cff_tmp.read_bytes())
            print(f"Extracted CFF written to:\n  {EXTRACTED_CFF_PATH.resolve()}")

        codemeta = run_cffconvert_to_codemeta(cff_tmp)

    OUTPUT_CODEMETA.write_text(
        json.dumps(codemeta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"CodeMeta written to:\n  {OUTPUT_CODEMETA.resolve()}")
    print("=== DONE ===")


if __name__ == "__main__":
    main()
