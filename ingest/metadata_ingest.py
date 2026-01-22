from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Dict, Any, List, Union, Tuple

import requests
import yaml
from jsonschema import Draft202012Validator


class MetadataIngestError(Exception):
    pass


class MetadataValidationError(MetadataIngestError):
    """Raised when MD.cff does not conform to the schema."""

    pass


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _read_yaml_bytes(data: bytes, *, source_hint: str = "<bytes>") -> Dict[str, Any]:
    try:
        obj = yaml.safe_load(data.decode("utf-8"))
    except Exception as e:
        raise MetadataIngestError(f"Invalid YAML content in {source_hint}: {e}")
    if not isinstance(obj, dict):
        raise MetadataIngestError(
            f"YAML root must be a mapping/object in {source_hint}"
        )
    return obj


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


def _read_source_bytes(source: Union[str, Path]) -> Tuple[bytes, str]:
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
    raw_bytes, source_label = _read_source_bytes(source)

    if source_label.lower().endswith(".zip"):
        zf = _load_zip(raw_bytes)
        if filename not in zf.namelist():
            raise MetadataIngestError(f"{filename} not found in ZIP archive")
        yaml_bytes = zf.read(filename)
        return _read_yaml_bytes(yaml_bytes, source_hint=f"{source_label}:{filename}")

    return _read_yaml_bytes(raw_bytes, source_hint=source_label)


def validate_against_schema(data: Dict[str, Any], schema: Dict[str, Any]) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))

    if not errors:
        return

    lines: List[str] = []
    for e in errors:
        loc = "$" if not e.path else "$." + ".".join(str(p) for p in e.path)
        lines.append(f"- {loc}: {e.message}")

    raise MetadataValidationError("MD.cff validation failed:\n" + "\n".join(lines))


def load_md_cff_schema(
    schema_source: Union[str, Path],
    *,
    schema_filename: str = "MD.cff-schema.yaml",
) -> Dict[str, Any]:
    return _read_yaml_from_source(schema_source, filename=schema_filename)


def load_cff(source: Union[str, Path], filename: str) -> Dict[str, Any]:
    yaml_data = _read_yaml_from_source(source, filename=filename)
    entries = [{"key": k, "value": v} for k, v in yaml_data.items()]
    return {"source": filename, "entries": entries, "raw": yaml_data}


def load_md_cff(
    source: Union[str, Path],
    *,
    validate: bool = True,
    schema_source: Union[str, Path] = "schemas/md_cff/MD.cff-schema.yaml",
    schema_filename: str = "MD.cff-schema.yaml",
) -> Dict[str, Any]:
    payload = load_cff(source, filename="MD.cff")

    if validate:
        schema = load_md_cff_schema(schema_source, schema_filename=schema_filename)
        validate_against_schema(payload["raw"], schema)

    return payload


def load_citation_cff(source: Union[str, Path]) -> Dict[str, Any]:
    return load_cff(source, filename="CITATION.cff")


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


def _find_member_by_name(zf: zipfile.ZipFile, target_name: str) -> str:
    """
    Find a member in a ZIP by filename, allowing subfolders.
    Returns the full member path inside the zip.
    """
    # exact first
    if target_name in zf.namelist():
        return target_name

    # fallback: match by basename / endswith
    candidates = [
        n
        for n in zf.namelist()
        if n.endswith("/" + target_name) or n.endswith(target_name)
    ]
    if not candidates:
        raise MetadataIngestError(
            f"{target_name} not found in ZIP archive (checked nested paths too)."
        )

    # If multiple, pick the shortest path (usually closest to root)
    candidates.sort(key=len)
    return candidates[0]


def list_zip_members(source: Union[str, Path]) -> List[str]:
    """
    Return the list of file paths inside a ZIP (local or URL).
    """
    raw_bytes, source_label = _read_source_bytes(source)
    if not str(source_label).lower().endswith(".zip"):
        raise MetadataIngestError(f"Source is not a .zip: {source_label}")

    zf = _load_zip(raw_bytes)
    return zf.namelist()


def load_package_zip(
    source: Union[str, Path],
    *,
    validate_md: bool = True,
    schema_source: Union[str, Path] = "schemas/md_cff/MD.cff-schema.yaml",
    schema_filename: str = "MD.cff-schema.yaml",
) -> Dict[str, Any]:
    """
    Load a single ZIP package that contains:
      - MD.cff
      - CITATION.cff
    Additionally returns the ZIP member list for object-structure awareness.

    Returns:
      {
        "md": <dict>,
        "citation": <dict>,
        "zip_members": <list[str]>
      }
    """
    raw_bytes, source_label = _read_source_bytes(source)
    if not str(source_label).lower().endswith(".zip"):
        raise MetadataIngestError(f"Package source must be a .zip: {source_label}")

    zf = _load_zip(raw_bytes)
    members = zf.namelist()

    md_member = _find_member_by_name(zf, "MD.cff")
    citation_member = _find_member_by_name(zf, "CITATION.cff")

    md = _read_yaml_bytes(zf.read(md_member), source_hint=f"{source_label}:{md_member}")
    citation = _read_yaml_bytes(
        zf.read(citation_member), source_hint=f"{source_label}:{citation_member}"
    )

    if validate_md:
        schema = load_md_cff_schema(schema_source, schema_filename=schema_filename)
        validate_against_schema(md, schema)

    return {"md": md, "citation": citation, "zip_members": members}
