# ============================================================
# metadata_ingest.py
#
# Input layer for FAIR Digital Object metadata.
# Reads MD.cff / CITATION.cff from:
#  - local file
#  - URL
#  - ZIP archive (local or URL)
#
# Loads classification rules for later FDO processing.
# No RDF, no crosswalk logic here!
# ============================================================

from __future__ import annotations

import io
import zipfile
import requests
from pathlib import Path
from typing import Dict, Any, List, Union
import yaml


# ------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------


class MetadataIngestError(Exception):
    pass


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _read_yaml_bytes(data: bytes) -> Dict[str, Any]:
    try:
        return yaml.safe_load(data.decode("utf-8"))
    except Exception as e:
        raise MetadataIngestError(f"Invalid YAML content: {e}")


def _read_from_url(url: str) -> bytes:
    r = requests.get(url)
    if r.status_code != 200:
        raise MetadataIngestError(f"Failed to fetch URL: {url}")
    return r.content


def _read_from_file(path: Path) -> bytes:
    if not path.exists():
        raise MetadataIngestError(f"File not found: {path}")
    return path.read_bytes()


def _load_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MetadataIngestError("Provided ZIP is not a valid archive")


# ------------------------------------------------------------
# CFF loader
# ------------------------------------------------------------


def load_cff(
    source: Union[str, Path], filename: str = "CITATION.cff"
) -> Dict[str, Any]:

    if isinstance(source, Path):
        source = str(source)

    if _is_url(source):
        raw_bytes = _read_from_url(source)
        source_label = source
    else:
        raw_bytes = _read_from_file(Path(source))
        source_label = str(source)

    if source_label.lower().endswith(".zip"):
        zf = _load_zip(raw_bytes)

        if filename not in zf.namelist():
            raise MetadataIngestError(f"{filename} not found in ZIP archive")

        yaml_bytes = zf.read(filename)
        yaml_data = _read_yaml_bytes(yaml_bytes)
    else:
        yaml_data = _read_yaml_bytes(raw_bytes)

    entries = [{"key": k, "value": v} for k, v in yaml_data.items()]

    return {"source": filename, "entries": entries, "raw": yaml_data}


# ------------------------------------------------------------
# Convenience wrappers
# ------------------------------------------------------------


def load_md_cff(source: Union[str, Path]) -> Dict[str, Any]:
    return load_cff(source, filename="MD.cff")


def load_citation_cff(source: Union[str, Path]) -> Dict[str, Any]:
    return load_cff(source, filename="CITATION.cff")


# ------------------------------------------------------------
# Classification rules loader
# ------------------------------------------------------------


def load_classification_rules(
    path: Union[str, Path] = "classification_rules.yaml",
) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise MetadataIngestError(f"Classification rules not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------
# Minimal manual test
# ------------------------------------------------------------

if __name__ == "__main__":
    # md = load_md_cff("example_fdo.zip")
    # rules = load_classification_rules()
    pass
