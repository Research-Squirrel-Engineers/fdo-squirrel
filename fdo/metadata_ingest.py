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
#
# + Step A: MD.cff schema validation (JSON Schema draft 2020-12)
# ============================================================

from __future__ import annotations

import io
import zipfile
import requests
from pathlib import Path
from typing import Dict, Any, List, Union, Optional
import yaml

# Step A validation deps
from jsonschema import Draft202012Validator


# ------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------


class MetadataIngestError(Exception):
    pass


class MetadataValidationError(MetadataIngestError):
    """Raised when MD.cff does not conform to the schema."""

    pass


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _read_yaml_bytes(data: bytes) -> Dict[str, Any]:
    try:
        obj = yaml.safe_load(data.decode("utf-8"))
        if not isinstance(obj, dict):
            raise MetadataIngestError("YAML root must be a mapping/object")
        return obj
    except MetadataIngestError:
        raise
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


def _read_source_bytes(source: Union[str, Path]) -> tuple[bytes, str]:
    """
    Returns (raw_bytes, source_label_str).
    """
    if isinstance(source, Path):
        source = str(source)

    if _is_url(source):
        raw_bytes = _read_from_url(source)
        source_label = source
    else:
        raw_bytes = _read_from_file(Path(source))
        source_label = str(source)

    return raw_bytes, source_label


def _read_yaml_from_source(source: Union[str, Path], filename: str) -> Dict[str, Any]:
    """
    Reads YAML either directly from a YAML file/URL or from within a ZIP (if source endswith .zip).
    If source is ZIP: reads `filename` from archive.
    If source is not ZIP: reads the whole source as YAML (filename is informational then).
    """
    raw_bytes, source_label = _read_source_bytes(source)

    if source_label.lower().endswith(".zip"):
        zf = _load_zip(raw_bytes)
        if filename not in zf.namelist():
            raise MetadataIngestError(f"{filename} not found in ZIP archive")
        yaml_bytes = zf.read(filename)
        return _read_yaml_bytes(yaml_bytes)

    # not a zip: interpret the source as YAML directly
    return _read_yaml_bytes(raw_bytes)


# ------------------------------------------------------------
# JSON Schema validation (Step A)
# ------------------------------------------------------------


def validate_against_schema(data: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """
    Validate `data` against JSON Schema `schema` (Draft 2020-12).
    Raises MetadataValidationError with a readable message on failure.
    """
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))

    if not errors:
        return

    lines: List[str] = []
    for e in errors:
        loc = "$" if not e.path else "$." + ".".join(str(p) for p in e.path)
        lines.append(f"- {loc}: {e.message}")

    raise MetadataValidationError("MD.cff validation failed:\n" + "\n".join(lines))


# ------------------------------------------------------------
# Schema loader (Step A)
# ------------------------------------------------------------


def load_md_cff_schema(
    source: Union[str, Path], filename: str = "MD.cff-schema.yaml"
) -> Dict[str, Any]:
    """
    Load MD.cff JSON Schema in YAML form from local/URL/ZIP (same mechanics as CFF loader).
    """
    return _read_yaml_from_source(source, filename=filename)


# ------------------------------------------------------------
# CFF loader
# ------------------------------------------------------------


def load_cff(
    source: Union[str, Path],
    filename: str = "CITATION.cff",
) -> Dict[str, Any]:
    """
    Load a YAML-based CFF-like file from local/URL/ZIP.
    Returns dict with:
      - source (filename)
      - entries (list of key/value)
      - raw (parsed mapping)
    """
    yaml_data = _read_yaml_from_source(source, filename=filename)
    entries = [{"key": k, "value": v} for k, v in yaml_data.items()]
    return {"source": filename, "entries": entries, "raw": yaml_data}


# ------------------------------------------------------------
# Convenience wrappers
# ------------------------------------------------------------


def load_md_cff(
    source: Union[str, Path],
    *,
    validate: bool = True,
    schema_source: Union[str, Path] = "schemas/MD.cff-schema.yaml",
    schema_filename: str = "MD.cff-schema.yaml",
) -> Dict[str, Any]:
    """
    Load MD.cff and (optionally) validate it against the MD.cff schema.
    - schema_source can be a path/URL to YAML schema or a ZIP containing schema_filename.
    """
    payload = load_cff(source, filename="MD.cff")

    if validate:
        schema = load_md_cff_schema(schema_source, filename=schema_filename)
        validate_against_schema(payload["raw"], schema)

    return payload


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
    obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise MetadataIngestError(
            "Classification rules YAML root must be a mapping/object"
        )
    return obj


# ------------------------------------------------------------
# Minimal manual test
# ------------------------------------------------------------

if __name__ == "__main__":
    # Example:
    # md = load_md_cff("examples/MD.cff.minimal.yaml", schema_source="schemas/MD.cff-schema.yaml")
    # print("Loaded MD.cff OK:", md["raw"].get("title"))
    pass
