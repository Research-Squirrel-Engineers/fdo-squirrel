from __future__ import annotations

import zipfile
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import yaml

# -----------------------------------------------------
# OPTIONAL: Configure a single source here (VS Code run)
# -----------------------------------------------------
# Examples:
#   PACKAGE_SOURCE = r"C:\data\my-fdo.zip"
#   PACKAGE_SOURCE = "https://zenodo.org/record/.../files/my-fdo.zip"
#   PACKAGE_SOURCE = r"C:\git\fdo-squirrel\MD.cff"   (also allowed, then CITATION must be separate)
#
# If the source is a ZIP, you can optionally specify member paths:
#   MD_IN_ZIP = "MD.cff"
#   CFF_IN_ZIP = "CITATION.cff"
#   CFF_IN_ZIP = "metadata/CFFplus.cff"
#

# PACKAGE_SOURCE: Optional[str] = None
# MD_IN_ZIP: Optional[str] = None
# CFF_IN_ZIP: Optional[str] = None

PACKAGE_SOURCE = r"C:/git/fdo-squirrel/example_fdo.zip"
MD_IN_ZIP = None
CFF_IN_ZIP = None

DEFAULT_MD_MEMBER_CANDIDATES = ["MD.cff"]
DEFAULT_CFF_MEMBER_CANDIDATES = ["CITATION.cff", "citation.cff"]


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def guess_is_zip(name_or_url: str) -> bool:
    return name_or_url.lower().endswith(".zip")


def _download_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "fdo-squirrel/1.0 (+https://w3id.org/fdo-squirrel/)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _load_yaml_bytes(data: bytes, source_hint: str) -> Dict[str, Any]:
    obj = yaml.safe_load(data.decode("utf-8", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError(f"YAML root must be a mapping/dict in {source_hint}")
    return obj


def _pick_member_from_zip(
    zf: zipfile.ZipFile,
    preferred_member: Optional[str],
    candidates: List[str],
    kind: str,
) -> str:
    """
    Pick a member file inside zip.
    - If preferred_member given: exact or endswith match (must be unambiguous).
    - Else: try candidates exact
    - Else: fallback endswith candidates
    - Else: error
    """
    members = zf.namelist()

    if preferred_member:
        if preferred_member in members:
            return preferred_member
        matches = [m for m in members if m.endswith(preferred_member)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FileNotFoundError(
                f"Ambiguous {kind} member '{preferred_member}' in ZIP. Matches: {matches}"
            )
        raise FileNotFoundError(f"{kind} member '{preferred_member}' not found in ZIP.")

    # exact candidates first
    for cand in candidates:
        if cand in members:
            return cand

    # endswith candidates next
    for cand in candidates:
        matches = [m for m in members if m.endswith("/" + cand) or m.endswith(cand)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # pick shortest path to keep deterministic
            matches.sort(key=len)
            return matches[0]

    raise FileNotFoundError(
        f"No {kind} file found in ZIP. Candidates: {candidates}. ZIP members sample: {members[:50]}"
    )


def load_package_from_source(
    source: str,
    *,
    md_member_in_zip: Optional[str] = None,
    cff_member_in_zip: Optional[str] = None,
    timeout: int = 30,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Load (md_dict, cff_dict, info_dict) from:
      - local ZIP
      - URL ZIP

    info_dict includes:
      - source
      - source_type: "local_zip" | "url_zip"
      - used_md_member_in_zip
      - used_cff_member_in_zip
      - zip_members
    """
    if not source:
        raise ValueError("source must be a non-empty string")

    info: Dict[str, Any] = {
        "source": source,
        "source_type": None,
        "used_md_member_in_zip": None,
        "used_cff_member_in_zip": None,
        "zip_members": None,
    }

    # URL ZIP
    if is_url(source):
        if not guess_is_zip(source):
            raise ValueError("For now, PACKAGE_SOURCE must be a .zip (URL).")
        info["source_type"] = "url_zip"
        zbytes = _download_bytes(source, timeout=timeout)
        with zipfile.ZipFile(BytesIO(zbytes), "r") as zf:
            members = zf.namelist()
            info["zip_members"] = members
            md_member = _pick_member_from_zip(
                zf, md_member_in_zip, DEFAULT_MD_MEMBER_CANDIDATES, "MD.cff"
            )
            cff_member = _pick_member_from_zip(
                zf, cff_member_in_zip, DEFAULT_CFF_MEMBER_CANDIDATES, "CITATION.cff"
            )
            info["used_md_member_in_zip"] = md_member
            info["used_cff_member_in_zip"] = cff_member

            md = _load_yaml_bytes(zf.read(md_member), f"{source}:{md_member}")
            cff = _load_yaml_bytes(zf.read(cff_member), f"{source}:{cff_member}")
            return md, cff, info

    # LOCAL ZIP
    p = Path(source)
    if not p.exists():
        raise FileNotFoundError(f"Local source not found: {p}")
    if not guess_is_zip(p.name):
        raise ValueError("For now, PACKAGE_SOURCE must be a .zip (local path).")

    info["source_type"] = "local_zip"
    with zipfile.ZipFile(p, "r") as zf:
        members = zf.namelist()
        info["zip_members"] = members
        md_member = _pick_member_from_zip(
            zf, md_member_in_zip, DEFAULT_MD_MEMBER_CANDIDATES, "MD.cff"
        )
        cff_member = _pick_member_from_zip(
            zf, cff_member_in_zip, DEFAULT_CFF_MEMBER_CANDIDATES, "CITATION.cff"
        )
        info["used_md_member_in_zip"] = md_member
        info["used_cff_member_in_zip"] = cff_member

        md = _load_yaml_bytes(zf.read(md_member), f"{p}:{md_member}")
        cff = _load_yaml_bytes(zf.read(cff_member), f"{p}:{cff_member}")
        return md, cff, info
