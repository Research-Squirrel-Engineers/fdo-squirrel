"""
Finishing touches for a fdo-squirrel run:

- render_mermaid_to_jpg(): best-effort high-resolution JPG render of the
  generated fdo_overview.mermaid diagram, via the Mermaid CLI (`mmdc`) plus
  Pillow for the PNG->JPG conversion. Neither is a hard dependency of the
  rest of this tool; if either is missing, or rendering fails for any
  reason, this prints one short, actionable warning and returns None rather
  than failing the run - the same pattern main.py already uses around the
  mermaid *text* generation.

- build_finished_bundle(): package the original source ZIP plus every
  freshly generated companion file (fdo-metadata.ttl, the two modelling
  reports, the mermaid diagram, its JPG render) into one self-contained
  "finished" ZIP, replacing any stale copies of those same filenames the
  original ZIP already carried. This automates a step that was previously
  done by hand before (re-)publishing a package.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Optional


def render_mermaid_to_jpg(
    mermaid_path: Path,
    jpg_path: Path,
    width: int = 2400,
    height: int = 1600,
    scale: int = 3,
    timeout: int = 120,
) -> Optional[Path]:
    """Render `mermaid_path` to a high-resolution JPG at `jpg_path`.

    Returns the output path on success, None if rendering was skipped or
    failed (a warning is printed either way - never raises).
    """
    if not mermaid_path.exists():
        return None

    mmdc = shutil.which("mmdc") or shutil.which("mmdc.cmd")
    if not mmdc:
        print(
            "⚠ Mermaid image render skipped: 'mmdc' not found on PATH.\n"
            "   Install Node.js, then run:\n"
            "     npm install -g @mermaid-js/mermaid-cli\n"
            "   to enable the high-resolution JPG render."
        )
        return None

    try:
        from PIL import Image
    except ImportError:
        print(
            "⚠ Mermaid image render skipped: Pillow is not installed.\n"
            "   Run `pip install Pillow` to enable the PNG->JPG conversion."
        )
        return None

    tmp_png = jpg_path.with_suffix(".tmp.png")
    tmp_cfg: Optional[Path] = None
    cmd = [
        mmdc,
        "-i",
        str(mermaid_path),
        "-o",
        str(tmp_png),
        "-w",
        str(width),
        "-H",
        str(height),
        "--backgroundColor",
        "white",
        "--scale",
        str(scale),
    ]

    # Headless Chromium refuses to launch as root without --no-sandbox
    # (containers/CI); harmless to add anywhere else, so only bother when
    # actually running as root.
    try:
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        is_root = False
    if is_root:
        tmp_cfg = jpg_path.with_suffix(".puppeteer.json")
        tmp_cfg.write_text(json.dumps({"args": ["--no-sandbox"]}), encoding="utf-8")
        cmd += ["--puppeteerConfigFile", str(tmp_cfg)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        with Image.open(tmp_png) as im:
            im.convert("RGB").save(jpg_path, "JPEG", quality=92, optimize=True)
        print(f"✔ Mermaid diagram rendered as high-res JPG: {jpg_path}")
        return jpg_path
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        print(f"⚠ Mermaid image render skipped: mmdc failed - {stderr[-500:]}")
        return None
    except Exception as e:
        print(f"⚠ Mermaid image render skipped: {e}")
        return None
    finally:
        if tmp_png.exists():
            tmp_png.unlink()
        if tmp_cfg is not None and tmp_cfg.exists():
            tmp_cfg.unlink()


def build_finished_bundle(
    original_zip_path: Optional[Path],
    generated_files: Iterable[Path],
    output_zip_path: Path,
) -> Optional[Path]:
    """
    Build one self-contained "finished" FDO package at `output_zip_path`:
    every member of `original_zip_path`, plus the freshly generated
    companion files, added/overwritten at the ZIP root by filename. Members
    of the original ZIP whose basename matches a generated file's name are
    skipped in favour of the fresh version (superseding stale copies of
    fdo-metadata.ttl, the modelling reports, etc. that a previous manual
    round of this same workflow may have left in the package).

    Streams member content rather than loading the archive into memory,
    since real packages here run into the hundreds of MB.
    """
    if original_zip_path is None or not Path(original_zip_path).exists():
        print(
            f"⚠ Finished bundle skipped: original package not found at "
            f"{original_zip_path}"
        )
        return None

    generated_by_name: Dict[str, Path] = {
        p.name: p for p in generated_files if p.exists()
    }
    if not generated_by_name:
        print("⚠ Finished bundle skipped: no generated files to add.")
        return None

    try:
        with zipfile.ZipFile(original_zip_path, "r") as zin, zipfile.ZipFile(
            output_zip_path, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            superseded = []
            for item in zin.infolist():
                base = Path(item.filename).name
                if base in generated_by_name:
                    superseded.append(item.filename)
                    continue
                with zin.open(item) as src, zout.open(item, "w") as dst:
                    shutil.copyfileobj(src, dst)

            for name, path in generated_by_name.items():
                zout.write(path, arcname=name)

        if superseded:
            print(
                f"ℹ Finished bundle: replaced {len(superseded)} stale "
                f"member(s) with freshly generated versions: "
                f"{', '.join(superseded)}"
            )
        return output_zip_path
    except Exception as e:
        print(f"⚠ Could not build finished bundle: {e}")
        return None
