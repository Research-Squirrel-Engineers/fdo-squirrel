from __future__ import annotations

from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path
import mimetypes
import yaml
import zipfile
import hashlib
from urllib.parse import quote
import json
from datetime import datetime, timezone

from crosswalks import CrosswalkRecord


# --------------------------------------------------
# Provenance tracking (JSON report)
# --------------------------------------------------


class ProvenanceTracker:
    """
    Tracks which information was sourced from where for RDF modelling.
    Produces a machine-readable JSON report.
    """

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._counters: Dict[str, int] = {}

    def record(
        self,
        field: str,
        source: str,
        detail: Optional[Dict[str, Any]] = None,
        count_inc: int = 0,
    ) -> None:
        self._events.append(
            {
                "field": field,
                "source": source,
                "detail": detail or {},
            }
        )
        if count_inc:
            self._counters[field] = self._counters.get(field, 0) + count_inc

    def report(self) -> Dict[str, Any]:
        # aggregate sources per field
        agg: Dict[str, Dict[str, Any]] = {}
        for e in self._events:
            f = e["field"]
            s = e["source"]
            agg.setdefault(f, {"sources": set(), "examples": []})
            agg[f]["sources"].add(s)
            # distribution.*: keep filename only (technical details not needed)
            if f.startswith("distribution."):
                file_name = (
                    e.get("detail", {}).get("file")
                    if isinstance(e.get("detail"), dict)
                    else None
                )
                if file_name:
                    agg[f]["examples"].append(file_name)
            # distributions (per-file full objects): no cap, store all
            elif f == "distributions":
                if e.get("detail"):
                    agg[f]["examples"].append(e["detail"])
            elif len(agg[f]["examples"]) < 5 and e.get("detail"):
                agg[f]["examples"].append(e["detail"])

        # make JSON-serializable
        agg_out: Dict[str, Any] = {}
        for f, v in agg.items():
            agg_out[f] = {
                "sources": sorted(list(v["sources"])),
                "examples": v["examples"],
                "count": self._counters.get(f, 0),
            }

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": agg_out,
        }

    def write_json(self, path: Path) -> Path:
        path.write_text(
            json.dumps(self.report(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path


# --------------------------------------------------
# Rules + helpers
# --------------------------------------------------


def _load_classification_rules(
    tracker: Optional[ProvenanceTracker] = None,
) -> Dict[str, Any]:
    rules_path = Path(__file__).resolve().parent / "classification_rules.yaml"
    if not rules_path.exists():
        raise FileNotFoundError(f"Missing classification rules: {rules_path}")

    if tracker:
        tracker.record(
            field="file roles rules",
            source="classification_rules.yaml",
            detail={"path": str(rules_path)},
        )

    return yaml.safe_load(rules_path.read_text(encoding="utf-8"))


def _classify_role(
    filename: str,
    fdo_type: str,
    rules_cfg: Dict[str, Any],
    tracker: Optional[ProvenanceTracker] = None,
) -> str:
    """
    Classify role via fdo/classification_rules.yaml.
    """
    class_cfg = (rules_cfg.get("fdo_classes") or {}).get(fdo_type) or {}
    default_role = class_cfg.get("default_role") or "file"
    rules = class_cfg.get("rules") or []

    ext = Path(filename).suffix.lower()
    basename = Path(filename).name

    for rule in rules:
        match = rule.get("match") or {}
        role = rule.get("role") or default_role

        # 1) filename: exact match (e.g. "MD.cff", "CITATION.cff")
        filenames = match.get("filename") or []
        if isinstance(filenames, list) and basename in filenames:
            if tracker:
                tracker.record(
                    field="distribution.role",
                    source="ZIP + classification_rules.yaml",
                    detail={
                        "file": filename,
                        "matched": "filename",
                        "role": role,
                        "fdo_type": fdo_type,
                    },
                    count_inc=1,
                )
            return role

        # 2) filename_prefix + extension (e.g. README.md)
        prefixes = match.get("filename_prefix") or []
        exts = match.get("extension") or []
        if isinstance(prefixes, list) and prefixes:
            if any(basename.startswith(p) for p in prefixes):
                if not exts or ext in [e.lower() for e in exts]:
                    if tracker:
                        tracker.record(
                            field="distribution.role",
                            source="ZIP + classification_rules.yaml",
                            detail={
                                "file": filename,
                                "matched": "filename_prefix",
                                "role": role,
                                "fdo_type": fdo_type,
                            },
                            count_inc=1,
                        )
                    return role

        # 3) path_prefix (e.g. "textures/")
        path_prefixes = match.get("path_prefix") or []
        if isinstance(path_prefixes, list) and any(
            filename.startswith(p) for p in path_prefixes
        ):
            if tracker:
                tracker.record(
                    field="distribution.role",
                    source="ZIP + classification_rules.yaml",
                    detail={
                        "file": filename,
                        "matched": "path_prefix",
                        "role": role,
                        "fdo_type": fdo_type,
                    },
                    count_inc=1,
                )
            return role

        # 4) extension only
        if isinstance(exts, list) and exts and not prefixes:
            if ext in [e.lower() for e in exts]:
                if tracker:
                    tracker.record(
                        field="distribution.role",
                        source="ZIP + classification_rules.yaml",
                        detail={
                            "file": filename,
                            "matched": "extension",
                            "ext": ext,
                            "role": role,
                            "fdo_type": fdo_type,
                        },
                        count_inc=1,
                    )
                return role

    if tracker:
        tracker.record(
            field="distribution.role",
            source="ZIP + classification_rules.yaml",
            detail={
                "file": filename,
                "ext": ext,
                "role": default_role,
                "fdo_type": fdo_type,
                "note": "default_role",
            },
            count_inc=1,
        )
    return default_role


def _media_type(
    filename: str,
    tracker: Optional[ProvenanceTracker] = None,
) -> str:
    """
    Prefer explicit CH/3D relevant types, fallback to mimetypes.
    """
    ext = Path(filename).suffix.lower()
    overrides = {
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
        ".obj": "model/obj",
        ".ply": "application/octet-stream",
        ".stl": "model/stl",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        # YAML / CFF
        ".cff": "text/yaml",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
    }
    if ext in overrides:
        mt = overrides[ext]
        if tracker:
            tracker.record(
                field="distribution.mediaType",
                source="ZIP (by extension override)",
                detail={"file": filename, "ext": ext, "mediaType": mt},
                count_inc=1,
            )
        return mt

    mt, _ = mimetypes.guess_type(filename)
    mt = mt or "application/octet-stream"
    if tracker:
        tracker.record(
            field="distribution.mediaType",
            source="ZIP (mimetypes.guess_type fallback)",
            detail={"file": filename, "ext": ext, "mediaType": mt},
            count_inc=1,
        )
    return mt


# --------------------------------------------------
# Mapping-driven MD.cff -> RDF emitter
# --------------------------------------------------


def _find_md_cff_crosswalk_file() -> Path:
    """Locate schema/md_cff/crosswalk_md_cff_to_rdf.yaml relative to repository."""
    base = Path(__file__).resolve().parent  # .../fdo
    candidates = [
        base.parent / "schema" / "md_cff" / "crosswalk_md_cff_to_rdf.yaml",
        base.parent / "schemas" / "md_cff" / "crosswalk_md_cff_to_rdf.yaml",
        base / "schema" / "md_cff" / "crosswalk_md_cff_to_rdf.yaml",
        base / "schemas" / "md_cff" / "crosswalk_md_cff_to_rdf.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "crosswalk_md_cff_to_rdf.yaml not found in schema/md_cff/ (or schemas/md_cff/)."
    )


def _load_md_cff_crosswalk() -> Dict[str, Any]:
    p = _find_md_cff_crosswalk_file()
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _md_get(root: Any, key: str) -> Any:
    """Resolve dotted-path keys into nested dicts."""
    cur = root
    for part in key.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return cur


def _ttl_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _ttl_lit(value: Any, datatype: Optional[str] = None) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return ('"true"' if value else '"false"') + "^^xsd:boolean"
    if isinstance(value, int):
        return f'"{value}"^^xsd:integer'
    if isinstance(value, float):
        # keep as decimal
        return f'"{value}"^^xsd:decimal'
    s = _ttl_escape(str(value))
    return f'"{s}"^^{datatype}' if datatype else f'"{s}"'


def _format_gyear(value: Any) -> str:
    """Format an integer year as a valid xsd:gYear lexical value.

    xsd:gYear requires an optional leading '-' followed by at least four
    digits, zero-padded on the left when the year itself has fewer than
    four digits (e.g. 300 -> "0300", -776 -> "-0776"). Years already at or
    above four digits, positive or negative, are left as-is.
    """
    year = int(value)
    sign = "-" if year < 0 else ""
    digits = str(abs(year)).zfill(4)
    return f"{sign}{digits}"


def _format_bbox_envelope(bbox: str) -> Optional[str]:
    """Reorder a 'west,south,east,north' bbox string into the GeoSPARQL
    Simple Features ENVELOPE WKT extension: ENVELOPE(minX, maxX, maxY, minY),
    i.e. (west, east, north, south). Returns None if the value does not
    split into exactly four comma-separated parts.
    """
    parts = [p.strip() for p in bbox.split(",")]
    if len(parts) != 4:
        return None
    west, south, east, north = parts
    return f"ENVELOPE({west}, {east}, {north}, {south})"


def _is_iri(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith(("http://", "https://", "urn:"))


def _as_iri(v: str) -> str:
    v = v.strip()
    return v if (v.startswith("<") and v.endswith(">")) else f"<{v}>"


def _infer_sf_type_from_wkt(wkt: str) -> str:
    w = (wkt or "").strip().upper()
    if w.startswith("POINT"):
        return "sf:Point"
    if w.startswith("LINESTRING"):
        return "sf:LineString"
    if w.startswith("POLYGON"):
        return "sf:Polygon"
    if w.startswith("MULTIPOINT"):
        return "sf:MultiPoint"
    if w.startswith("MULTILINESTRING"):
        return "sf:MultiLineString"
    if w.startswith("MULTIPOLYGON"):
        return "sf:MultiPolygon"
    return "sf:Geometry"


def _emit_object_id_label_inline(
    predicate: str,
    value: Any,
    inline: List[str],
    post: List[str],
    label_predicate: str = "rdfs:label",
    bnode_prefix: str = "_:obj",
) -> None:
    items = value if isinstance(value, list) else [value]
    bcount = 0
    for it in items:
        iri = None
        label = None
        if isinstance(it, dict):
            if isinstance(it.get("id"), str) and it["id"].strip():
                iri = it["id"].strip()
            if isinstance(it.get("label"), str) and it["label"].strip():
                label = it["label"].strip()
        elif _is_iri(it):
            iri = str(it).strip()
        elif isinstance(it, str) and it.strip():
            label = it.strip()

        if iri:
            inline.append(f"    {predicate} {_as_iri(iri)} ;")
            if label:
                post.append(f"{_as_iri(iri)} {label_predicate} {_ttl_lit(label)} .")
        elif label:
            bcount += 1
            node = f"{bnode_prefix}{bcount}"
            inline.append(f"    {predicate} {node} ;")
            post.append(f"{node} {label_predicate} {_ttl_lit(label)} .")


def _apply_md_cff_mapping(
    md: Dict[str, Any], subj: str, tracker: "ProvenanceTracker"
) -> Tuple[List[str], List[str]]:
    """
    Returns (inline_predicate_lines, post_triples_lines)
    Inline lines must be used inside the dataset block (with trailing ';').
    Post triples are full triples to append after the dataset block.
    """
    cfg = _load_md_cff_crosswalk()
    mappings: Dict[str, Any] = cfg.get("mappings") or {}
    handlers: Dict[str, Any] = cfg.get("handlers") or {}
    rules: Dict[str, Any] = cfg.get("rules") or {}
    label_pred = (rules.get("object_id_label") or {}).get(
        "label_predicate"
    ) or "rdfs:label"

    inline: List[str] = []
    post: List[str] = []

    # keys we already emit explicitly in fdo_rdf (avoid duplicates)
    skip_keys = {
        "title",
        "description",
        "publishers",
        "creators",
        "license",
        "version",
        "fdo_type",
    }

    for key, spec in mappings.items():
        if key in skip_keys:
            continue

        val = _md_get(md, key)
        if val is None:
            continue

        # handler-only (e.g., temporal)
        if (
            isinstance(spec, dict)
            and "handler" in spec
            and "predicate" not in spec
            and "emit" not in spec
        ):
            hname = spec["handler"]
            if hname == "temporal_node" and isinstance(val, dict):
                # mint temporal node IRI deterministically
                node = f"<{md.get('id')}_temporal>"
                post.append(f"{subj} dct:temporal {node} .")
                post.append(f"{node} a dct:PeriodOfTime .")
                # Link ChronOntology (or any provided temporal 'id') to the minted temporal node
                tid = val.get("id")
                if isinstance(tid, str) and tid.strip():
                    tid = tid.strip()
                    if _is_iri(tid):
                        # Use owl:sameAs as a clean "identity link" to the external ChronOntology resource
                        post.append(f"{node} owl:sameAs {_as_iri(tid)} .")
                    else:
                        # If it's not an IRI, keep it as identifier literal
                        post.append(f"{node} dct:identifier {_ttl_lit(tid)} .")
                tracker.record(
                    field="dataset.temporal.id",
                    source="MD.cff",
                    detail={"id": tid},
                    count_inc=1,
                )
                if isinstance(val.get("label"), str) and val["label"].strip():
                    post.append(f"{node} rdfs:label {_ttl_lit(val['label'].strip())} .")
                if val.get("start") is not None:
                    post.append(
                        f"{node} dcat:startDate "
                        f"{_ttl_lit(_format_gyear(val['start']), datatype='xsd:gYear')} ."
                    )
                if val.get("end") is not None:
                    post.append(
                        f"{node} dcat:endDate "
                        f"{_ttl_lit(_format_gyear(val['end']), datatype='xsd:gYear')} ."
                    )
                tracker.record(
                    field="dataset.temporal", source="MD.cff", detail=val, count_inc=1
                )
            continue

        # emit list (used by spatial)
        if isinstance(spec, dict) and "emit" in spec and isinstance(spec["emit"], list):
            for e in spec["emit"]:
                if "predicate" in e:
                    pred = e["predicate"]
                    src_key = e.get("from")
                    v = (
                        val
                        if not src_key
                        else (val.get(src_key) if isinstance(val, dict) else None)
                    )
                    if v is None:
                        continue
                    vtype = e.get("value_type", "literal")
                    if vtype == "iri_optional":
                        if _is_iri(v):
                            inline.append(f"    {pred} {_as_iri(str(v))} ;")
                            tracker.record(
                                field=f"dataset.{key}.{src_key}",
                                source="MD.cff",
                                detail=v,
                            )
                            # spatial has no {id, label} pairing in the
                            # schema - label sits as a sibling field on the
                            # same spatial object, so pick it up from there.
                            if (
                                key == "spatial"
                                and isinstance(val, dict)
                                and isinstance(val.get("label"), str)
                                and val["label"].strip()
                            ):
                                post.append(
                                    f"{_as_iri(str(v))} {label_pred} "
                                    f"{_ttl_lit(val['label'].strip())} ."
                                )
                    elif vtype == "literal":
                        inline.append(f"    {pred} {_ttl_lit(v)} ;")
                        tracker.record(
                            field=f"dataset.{key}.{src_key}", source="MD.cff", detail=v
                        )
                    elif vtype == "bbox_envelope":
                        envelope = _format_bbox_envelope(str(v))
                        if envelope:
                            crs_wkt = (
                                "<http://www.opengis.net/def/crs/EPSG/0/4326> "
                                + envelope
                            )
                            inline.append(
                                f'    {pred} "{_ttl_escape(crs_wkt)}"^^geosparql:wktLiteral ;'
                            )
                            tracker.record(
                                field=f"dataset.{key}.{src_key}",
                                source="MD.cff",
                                detail=v,
                            )
                    else:
                        inline.append(f"    {pred} {_ttl_lit(v)} ;")
                        tracker.record(
                            field=f"dataset.{key}.{src_key}", source="MD.cff", detail=v
                        )
                elif "handler" in e:
                    # currently only geosparql_geometry from wkt
                    hname = e["handler"]
                    src_key = e.get("from")
                    wkt = (
                        val.get(src_key)
                        if (isinstance(val, dict) and src_key)
                        else None
                    )
                    if (
                        hname == "geosparql_geometry"
                        and isinstance(wkt, str)
                        and wkt.strip()
                    ):
                        # inline explicit lat/lon if present (as requested)
                        if (
                            isinstance(val.get("lat"), (int, float, str))
                            and str(val.get("lat")).strip()
                        ):
                            inline.append(
                                f"    schema:latitude {_ttl_lit(val.get('lat'), datatype='xsd:decimal')} ;"
                            )
                        if (
                            isinstance(val.get("lon"), (int, float, str))
                            and str(val.get("lon")).strip()
                        ):
                            inline.append(
                                f"    schema:longitude {_ttl_lit(val.get('lon'), datatype='xsd:decimal')} ;"
                            )

                        node = f"<{md.get('id')}_geom>"
                        sf_type = _infer_sf_type_from_wkt(wkt)
                        post.append(f"{subj} geosparql:hasGeometry {node} .")
                        post.append(f"{node} a {sf_type} .")
                        post.append(
                            f'{node} geosparql:asWKT "{_ttl_escape("<http://www.opengis.net/def/crs/EPSG/0/4326> " + wkt.strip())}"^^geosparql:wktLiteral .'
                        )
                        tracker.record(
                            field="dataset.spatial.geometry",
                            source="MD.cff",
                            detail={"sf_type": sf_type},
                            count_inc=1,
                        )
            continue

        # normal predicate mappings
        if isinstance(spec, dict) and "predicate" in spec:
            pred = spec["predicate"]
            vtype = spec.get("value_type", "literal")
            multiple = bool(spec.get("multiple"))

            values = val if (multiple and isinstance(val, list)) else [val]

            for v in values:
                if v is None:
                    continue
                if vtype == "literal":
                    inline.append(f"    {pred} {_ttl_lit(v)} ;")
                elif vtype == "literal_date":
                    inline.append(f"    {pred} {_ttl_lit(v, datatype='xsd:date')} ;")
                elif vtype == "iri":
                    if _is_iri(v):
                        inline.append(f"    {pred} {_as_iri(str(v))} ;")
                elif vtype == "curie_or_iri":
                    s = str(v).strip()
                    inline.append(f"    {pred} {_as_iri(s) if _is_iri(s) else s} ;")
                elif vtype == "object_id_label":
                    _emit_object_id_label_inline(
                        pred, v, inline, post, label_predicate=label_pred
                    )
                elif vtype == "literal_or_object":
                    # if dict with id -> iri, if dict with label -> literal,
                    # else: keep the object as JSON literal (useful for provenance-like blocks)
                    if isinstance(v, dict):
                        if isinstance(v.get("id"), str) and v["id"].strip():
                            inline.append(f"    {pred} {_as_iri(v['id'].strip())} ;")
                        elif isinstance(v.get("label"), str) and v["label"].strip():
                            inline.append(
                                f"    {pred} {_ttl_lit(v['label'].strip())} ;"
                            )
                        else:
                            try:
                                blob = json.dumps(v, ensure_ascii=False, sort_keys=True)
                            except Exception:
                                blob = str(v)
                            inline.append(f"    {pred} {_ttl_lit(blob)} ;")
                    elif _is_iri(v):
                        inline.append(f"    {pred} {_as_iri(str(v))} ;")
                    else:
                        inline.append(f"    {pred} {_ttl_lit(v)} ;")
                else:
                    inline.append(f"    {pred} {_ttl_lit(v)} ;")

            # Store the first actual value so the HTML report can show it
            first_val = next((v for v in values if v is not None), None)
            tracker.record(
                field=f"dataset.{key}",
                source="MD.cff",
                detail={
                    "value": str(first_val) if first_val is not None else None,
                    "value_type": vtype,
                },
                count_inc=len(values),
            )

    return inline, post


def _spdx_url(license_value: Any) -> Optional[str]:
    """Return SPDX URL for string or {id,label} dict (MD.cff license).
    Normalises human-readable license strings to SPDX kebab-case identifiers,
    e.g. "CC BY 4.0" → "CC-BY-4.0" → https://spdx.org/licenses/CC-BY-4.0.html
    """
    import re as _re

    if license_value is None:
        return None
    if isinstance(license_value, dict):
        lid = str(license_value.get("id") or license_value.get("label") or "").strip()
    else:
        lid = str(license_value).strip()
    if not lid:
        return None
    if lid.startswith(("http://", "https://")):
        return lid
    # Normalise to SPDX kebab-case: collapse whitespace runs to single hyphens
    lid = _re.sub(r"\s+", "-", lid)
    lid = _re.sub(r"-+", "-", lid)
    return f"https://spdx.org/licenses/{lid}.html"


def _get_project_root_from_info(info: Dict[str, Any]) -> Optional[Path]:
    for k in ("zip_path", "local_path", "source_path", "path", "source"):
        v = info.get(k)
        if isinstance(v, str):
            p = Path(v)
            if p.exists() and p.suffix.lower() == ".zip":
                return p
    return None


def _zip_members_with_hashes(
    info: Optional[Dict[str, Any]],
    tracker: Optional[ProvenanceTracker] = None,
) -> List[Dict[str, Any]]:
    """
    Produce list of members with: name, size, sha256.
    Prefers computing from local ZIP if available.
    """
    if not info or not isinstance(info, dict):
        if tracker:
            tracker.record(
                field="zip.members",
                source="ZIP",
                detail={"note": "no info dict available"},
            )
        return []

    zip_path = _get_project_root_from_info(info)
    members: List[Dict[str, Any]] = []

    if zip_path:
        if tracker:
            tracker.record(
                field="zip.source",
                source="ZIP",
                detail={
                    "zip_path": str(zip_path),
                    "mode": "computed sha256 from archive",
                },
            )

        with zipfile.ZipFile(zip_path, "r") as zf:
            for zi in zf.infolist():
                if zi.is_dir():
                    continue
                name = zi.filename
                data = zf.read(zi)
                sha = hashlib.sha256(data).hexdigest()
                members.append({"name": name, "size": zi.file_size, "sha256": sha})

        if tracker:
            tracker.record(
                field="zip.members",
                source="ZIP",
                detail={"count": len(members)},
                count_inc=len(members),
            )
    # Fallback: only if we got nothing from the ZIP file directly
    if members:
        return members

    candidates = None
    for key in ("zip_members", "members", "zip_content", "files"):
        if key in info:
            candidates = info[key]
            break

    if isinstance(candidates, list):
        for item in candidates:
            if isinstance(item, str):
                members.append({"name": item})
            elif isinstance(item, dict):
                name = item.get("name") or item.get("path") or item.get("filename")
                if not name:
                    continue
                members.append(
                    {
                        "name": name,
                        "size": item.get("size")
                        or item.get("file_size")
                        or item.get("bytes"),
                        "sha256": item.get("sha256") or item.get("hash"),
                    }
                )

    if tracker:
        tracker.record(
            field="zip.source",
            source="ZIP",
            detail={
                "mode": "ingest-provided members",
                "key": str(type(candidates)),
                "count": len(members),
            },
        )
        tracker.record(
            field="zip.members",
            source="ZIP",
            detail={"count": len(members)},
            count_inc=len(members),
        )

    return members


def build_generated_distributions_ttl(
    subject_uri: str,
    fdo_type: str,
    files: List[Tuple[Path, Optional[str]]],
    tracker: Optional[ProvenanceTracker] = None,
) -> str:
    """
    Describe locally generated companion files - the JSON/HTML modelling
    reports, the mermaid diagram, its rendered JPG - as additional
    dcat:Distribution entries, using the same content-addressing
    (urn:fdo-squirrel:dist/<sha256 prefix>), role classification
    (classification_rules.yaml) and media-type detection that
    _zip_members_with_hashes()/the main dataset block already use for the
    original ZIP members. These files do not exist yet while the main
    dataset block is being built, so this returns a standalone block of
    triples to append to an already-written fdo-metadata.ttl rather than
    trying to splice into the (already closed) inline dataset block.

    `files` is a list of (path_on_disk, role_override); pass role_override
    as None to fall back to the classification rules for `fdo_type`.

    Note on the fdo-metadata.ttl file describing itself: call this with the
    *already-written* fdo-metadata.ttl still on disk in its pre-append form,
    so the sha256/byteSize recorded for it are well-defined (a manifest
    cannot include a hash of its own final bytes - the same limitation any
    self-describing checksum file has). The bytes actually shipped are this
    content plus the block this function returns.
    """
    if not files:
        return ""

    rules_cfg = _load_classification_rules(tracker=tracker)
    lines: List[str] = []
    dist_uris: List[str] = []

    for path, role_override in files:
        if not path.exists():
            continue
        data = path.read_bytes()
        size = len(data)
        sha256_hex = hashlib.sha256(data).hexdigest()
        name = path.name
        dist_uri = f"<urn:fdo-squirrel:dist/{sha256_hex[:16]}>"
        access_url = f"<urn:fdo-squirrel:content/{quote(name, safe='')}>"
        role = role_override or _classify_role(
            name, fdo_type, rules_cfg, tracker=tracker
        )
        mt = _media_type(name, tracker=tracker)

        dist_uris.append(dist_uri)
        lines.append(f"{dist_uri} a dcat:Distribution, crmdig:D9_Data_Object ;")
        lines.append(f"    dcat:accessURL {access_url} ;")
        lines.append(f"    dcat:byteSize {size} ;")
        lines.append(f'    dcat:mediaType "{_ttl_escape(mt)}" ;')
        lines.append(f'    fdo:path "{_ttl_escape(name)}" ;')
        lines.append(f'    fdo:role "{_ttl_escape(role)}" ;')
        lines.append(f'    fdo:sha256 "{sha256_hex}" .')
        lines.append("")

        if tracker:
            tracker.record(
                field="distribution.id",
                source="fdo-squirrel (generated)",
                detail={"file": name, "dist_uri": dist_uri, "id_basis": "sha256"},
                count_inc=1,
            )

    if not dist_uris:
        return ""

    header = f"{subject_uri} dcat:distribution {','.join(dist_uris)} ."
    if tracker:
        tracker.record(
            field="dataset.distributions.generated",
            source="fdo-squirrel (generated)",
            detail={"count": len(dist_uris)},
            count_inc=len(dist_uris),
        )

    return header + "\n\n" + "\n".join(lines)


def _load_md_raw_from_zip(
    info: Optional[Dict[str, Any]],
    tracker: Optional[ProvenanceTracker] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort: read MD.cff from the ZIP path in info and return it as dict.

    This makes the ZIP the single source-of-truth for modelling, independent of caller behaviour.
    """
    if not info or not isinstance(info, dict):
        return None

    zip_path = _get_project_root_from_info(info)
    if not zip_path:
        return None

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            candidates = ["MD.cff", "md.cff", "MD.CFF"]
            name_in_zip = None
            for n in candidates:
                try:
                    zf.getinfo(n)
                    name_in_zip = n
                    break
                except KeyError:
                    continue

            if not name_in_zip:
                lower_map = {zi.filename.lower(): zi.filename for zi in zf.infolist()}
                if "md.cff" in lower_map:
                    name_in_zip = lower_map["md.cff"]

            if not name_in_zip:
                return None

            raw_txt = zf.read(name_in_zip).decode("utf-8", errors="replace")
            data = yaml.safe_load(raw_txt)
            if isinstance(data, dict):
                if tracker:
                    tracker.record(
                        field="md.raw",
                        source="MD.cff",
                        detail={"mode": "read from ZIP", "path": name_in_zip},
                        count_inc=1,
                    )
                return data
    except Exception as e:
        if tracker:
            tracker.record(
                field="md.raw",
                source="MD.cff",
                detail={"mode": "read from ZIP failed", "error": str(e)},
            )
        return None

    return None


def _load_citation_raw_from_zip(
    info: Optional[Dict[str, Any]],
    tracker: Optional[ProvenanceTracker] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort: read CITATION.cff from the ZIP path in info and return it as dict.

    This allows consistent CITATION triple generation even if the caller did not
    forward the parsed CITATION.cff content.
    """
    if not info or not isinstance(info, dict):
        return None

    zip_path = _get_project_root_from_info(info)
    if not zip_path:
        return None

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Common locations / names
            candidates = ["CITATION.cff", "citation.cff", "CITATION.CFF"]
            name_in_zip = None
            for n in candidates:
                try:
                    zf.getinfo(n)
                    name_in_zip = n
                    break
                except KeyError:
                    continue

            if not name_in_zip:
                # fallback: search case-insensitive
                lower_map = {zi.filename.lower(): zi.filename for zi in zf.infolist()}
                if "citation.cff" in lower_map:
                    name_in_zip = lower_map["citation.cff"]

            if not name_in_zip:
                return None

            raw_txt = zf.read(name_in_zip).decode("utf-8", errors="replace")
            data = yaml.safe_load(raw_txt)
            if isinstance(data, dict):
                if tracker:
                    tracker.record(
                        field="citation.raw",
                        source="CITATION.cff",
                        detail={"mode": "read from ZIP", "path": name_in_zip},
                        count_inc=1,
                    )
                return data
    except Exception as e:
        if tracker:
            tracker.record(
                field="citation.raw",
                source="CITATION.cff",
                detail={"mode": "read from ZIP failed", "error": str(e)},
            )
        return None

    return None

    return members


# --------------------------------------------------
# Crosswalk triple post-processing
# --------------------------------------------------


def _is_uri_object(obj: str) -> bool:
    obj = obj.strip()
    return obj.startswith("<") and obj.endswith(">")


def _is_literal_object(obj: str) -> bool:
    obj = obj.strip()
    return obj.startswith('"') and obj.endswith('"')


def _strip_literal_quotes(obj: str) -> str:
    o = obj.strip()
    if o.startswith('"') and o.endswith('"'):
        return o[1:-1]
    return o


def _postprocess_citation_triples(
    triples: List[str],
    tracker: Optional[ProvenanceTracker] = None,
) -> List[str]:
    """
    Improve output quality without changing the crosswalk engine:

    - Deduplicate triples
    - Upgrade SPDX-like license literals (e.g., "MIT") to SPDX URI variants
    - Convert keyword-like triples to dcat:keyword and dct:subject
    - If wdt:P921 is used with literals, also emit dct:subject literals

    Provenance:
      - CITATION.cff is the origin for these triples
    """
    out_set: set[str] = set()

    if tracker:
        tracker.record(
            field="citation.triples.input",
            source="CITATION.cff",
            detail={"count": len(triples)},
            count_inc=len(triples),
        )

    # Collect keyword literals per subject (so we can add dcat:keyword/dct:subject once)
    keyword_literals: Dict[str, set[str]] = {}

    def add(tr: str) -> None:
        out_set.add(tr)

    for t in triples:
        t = t.strip()
        if not t or not t.endswith("."):
            continue

        # Basic parse: "<S> P O ."
        parts = t[:-1].strip().split(" ", 2)
        if len(parts) != 3:
            add(t)
            continue

        s, p, o = parts[0], parts[1], parts[2].strip()

        # --- Keywords: collect from multiple namespaces ---
        if p in {"cff:keywords", "schema:keywords", "codemeta:keywords"}:
            add(t)
            if tracker:
                tracker.record(
                    field="keywords",
                    source="CITATION.cff",
                    detail={"predicate": p, "object": o},
                    count_inc=1,
                )

            if _is_literal_object(o):
                keyword_literals.setdefault(s, set()).add(_strip_literal_quotes(o))
            elif _is_uri_object(o):
                # keyword entity URI → subject is better than keyword literal
                add(f"{s} dct:subject {o} .")
                if tracker:
                    tracker.record(
                        field="subject (URI)",
                        source="CITATION.cff",
                        detail={"via": p, "uri": o},
                        count_inc=1,
                    )
            continue

        # Wikidata subject property (P921) should normally be an item URI.
        if p == "wdt:P921":
            add(t)
            if tracker:
                tracker.record(
                    field="subject",
                    source="CITATION.cff",
                    detail={"predicate": p, "object": o},
                    count_inc=1,
                )

            if _is_literal_object(o):
                keyword_literals.setdefault(s, set()).add(_strip_literal_quotes(o))
            elif _is_uri_object(o):
                add(f"{s} dct:subject {o} .")
            continue

        # --- License: upgrade known SPDX IDs to URI variants ---
        if p in {
            "schema:license",
            "codemeta:license",
            "cff:license",
            "wdt:P275",
            "cff:license-url",
        }:
            add(t)
            if tracker:
                tracker.record(
                    field="license",
                    source="CITATION.cff",
                    detail={"predicate": p, "object": o},
                    count_inc=1,
                )

            if _is_literal_object(o):
                lic = _strip_literal_quotes(o).strip()
                spdx = _spdx_url(lic)
                if spdx:
                    add(f"{s} {p} <{spdx}> .")
                    add(f"{s} dct:license <{spdx}> .")

                    if tracker:
                        tracker.record(
                            field="license (SPDX URI added)",
                            source="CITATION.cff",
                            detail={"spdx": spdx, "from_literal": lic, "predicate": p},
                            count_inc=1,
                        )

            elif _is_uri_object(o):
                add(f"{s} dct:license {o} .")
            continue

        # default: keep as-is
        add(t)

    # Emit dcat:keyword and dct:subject literals (equivalents)
    for s, kws in keyword_literals.items():
        for kw in sorted(kws):
            lit = f'"{kw}"'
            add(f"{s} dcat:keyword {lit} .")
            add(f"{s} dct:subject {lit} .")
            if tracker:
                tracker.record(
                    field="keywords (DCAT/DC added)",
                    source="CITATION.cff",
                    detail={"keyword": kw},
                    count_inc=1,
                )

    out = sorted(out_set)

    if tracker:
        tracker.record(
            field="citation.triples.output",
            source="CITATION.cff",
            detail={"count": len(out)},
            count_inc=len(out),
        )

    return out


# --------------------------------------------------
# RDF writer
# --------------------------------------------------


def crosswalk_to_rdf_turtle(
    cw: CrosswalkRecord,
    citation_triples: list[str],
    info: Optional[Dict[str, Any]] = None,
    citation_raw: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Write Turtle similar to the earlier "full" fdo-metadata.ttl:
      - dataset-ish core (dcat:Dataset + fdo:*FDO)
      - rich MD.cff fields if present on CrosswalkRecord
      - ZIP structure as distributions with deterministic URN IDs + accessURL + sha256
      - crosswalk triples appended (schema/codemeta/wdt/cff etc.), post-processed
      - JSON provenance report (console + file)
      - identifiers mapped from MD.cff (cw.identifiers)
    """
    tracker = ProvenanceTracker()
    rules_cfg = _load_classification_rules(tracker=tracker)
    info = info or {}

    # --------------------------------------------------
    # Source-of-truth: always read MD.cff and CITATION.cff from the ZIP package
    # --------------------------------------------------
    md_zip = _load_md_raw_from_zip(info, tracker=tracker)
    citation_zip = _load_citation_raw_from_zip(info, tracker=tracker)

    # Prefer ZIP-derived metadata when available (caller-provided values remain as fallback).
    md_root = md_zip if isinstance(md_zip, dict) else getattr(cw, "md_raw", None)
    citation_raw = citation_zip if isinstance(citation_zip, dict) else citation_raw

    # Derive key display fields from MD.cff (ZIP) where possible
    if isinstance(md_root, dict):
        cw_title = md_root.get("title") or cw.title
        cw_description = md_root.get("description") or getattr(cw, "description", None)
        cw_version = md_root.get("version") or getattr(cw, "version", None)
        cw_fdo_type = md_root.get("fdo_type") or cw.fdo_type
        cw_id = md_root.get("id") or cw.id
    else:
        cw_title = cw.title
        cw_description = getattr(cw, "description", None)
        cw_version = getattr(cw, "version", None)
        cw_fdo_type = cw.fdo_type
        cw_id = cw.id

    lines: List[str] = []
    post_dataset_triples: List[str] = []

    # Prefixes
    lines.extend(
        [
            "@prefix dcat: <http://www.w3.org/ns/dcat#> .",
            "@prefix dct: <http://purl.org/dc/terms/> .",
            "@prefix fdo: <https://w3id.org/fdo-squirrel/> .",
            "@prefix schema: <https://schema.org/> .",
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
            "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
            "@prefix geosparql: <http://www.opengis.net/ont/geosparql#> .",
            "@prefix sf: <http://www.opengis.net/ont/sf#> .",
            "@prefix foaf: <http://xmlns.com/foaf/0.1/> .",
            "@prefix codemeta: <https://codemeta.github.io/terms/> .",
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
            "@prefix cff: <https://citation-file-format.github.io/terms/> .",
            "@prefix wd: <http://www.wikidata.org/entity/> .",
            "@prefix wdt: <http://www.wikidata.org/prop/direct/> .",
            "@prefix crm: <http://www.cidoc-crm.org/cidoc-crm/> .",
            "@prefix crmdig: <http://www.ics.forth.gr/isl/CRMdig/> .",
        ]
    )

    subj = f"<{cw_id}>"

    tracker.record(
        field="dataset.id",
        source="MD.cff",
        detail={"id": cw_id, "fdo_type": cw_fdo_type},
    )

    # ZIP members (with sha256 if possible)
    members = _zip_members_with_hashes(info, tracker=tracker)

    # create distribution URIs: urn:fdo-squirrel:dist/<sha256prefix>
    dists: List[Tuple[str, Dict[str, Any]]] = []
    for m in members:
        name = m.get("name") or ""
        if not name:
            continue

        sha256_hex = m.get("sha256")
        if isinstance(sha256_hex, str) and len(sha256_hex) >= 16:
            dist_uri = f"<urn:fdo-squirrel:dist/{sha256_hex[:16]}>"
            tracker.record(
                field="distribution.id",
                source="ZIP",
                detail={"file": name, "dist_uri": dist_uri, "id_basis": "sha256prefix"},
                count_inc=1,
            )
        else:
            sha_path = hashlib.sha256(name.encode("utf-8")).hexdigest()
            dist_uri = f"<urn:fdo-squirrel:dist/{sha_path[:16]}>"
            sha256_hex = None
            tracker.record(
                field="distribution.id",
                source="ZIP",
                detail={
                    "file": name,
                    "dist_uri": dist_uri,
                    "id_basis": "sha256(path) fallback",
                },
                count_inc=1,
            )

        dists.append(
            (dist_uri, {"name": name, "size": m.get("size"), "sha256": sha256_hex})
        )

    # ---------- Core dataset block ----------
    lines.append(
        f"{subj} a dcat:Dataset, crmdig:D1_Digital_Object, "
        f"crm:E73_Information_Object, {cw_fdo_type} ;"
    )

    created = getattr(cw, "created", None)
    issued = getattr(cw, "issued", None)
    modified = getattr(cw, "modified", None)
    description = cw_description
    version = cw_version
    identifier = getattr(cw, "identifier", None) or getattr(cw, "fdo_id", None)

    if created:
        lines.append(f'    dct:created "{created}"^^xsd:date ;')
        tracker.record(
            field="dataset.created", source="MD.cff", detail={"created": created}
        )
    if description:
        lines.append(f'    dct:description "{description}" ;')
        tracker.record(
            field="dataset.description",
            source="MD.cff",
            detail={
                "description": description[:120]
                + ("…" if len(description) > 120 else "")
            },
        )
    if version:
        lines.append(f'    dct:hasVersion "{version}" ;')
        tracker.record(
            field="dataset.version", source="MD.cff", detail={"version": version}
        )
    if identifier:
        lines.append(f'    dct:identifier "{identifier}" ;')
        tracker.record(
            field="dataset.identifier",
            source="MD.cff",
            detail={"identifier": identifier},
        )
    if issued:
        lines.append(f'    dct:issued "{issued}"^^xsd:date ;')
        tracker.record(
            field="dataset.issued", source="MD.cff", detail={"issued": issued}
        )
    if modified:
        lines.append(f'    dct:modified "{modified}"^^xsd:date ;')
        tracker.record(
            field="dataset.modified", source="MD.cff", detail={"modified": modified}
        )

    # Creators (dataset -> persons) derived from CITATION.cff (ZIP) if possible
    derived_creators: List[Tuple[str, str]] = []
    if isinstance(citation_raw, dict):
        authors = citation_raw.get("authors")
        if isinstance(authors, list):
            for a in authors:
                if not isinstance(a, dict):
                    continue
                orcid = a.get("orcid")
                family = a.get("family-names")
                given = a.get("given-names")
                name = a.get("name")
                label = None
                if isinstance(name, str) and name.strip():
                    label = name.strip()
                elif isinstance(family, str) and isinstance(given, str):
                    label = f"{family.strip()}, {given.strip()}"
                elif isinstance(family, str) and family.strip():
                    label = family.strip()

                if isinstance(orcid, str) and orcid.strip().startswith("http"):
                    derived_creators.append((orcid.strip(), label or orcid.strip()))
                elif label:
                    hid = hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]
                    derived_creators.append((f"urn:fdo-squirrel:person/{hid}", label))

    creators_to_use = (
        derived_creators if derived_creators else list(getattr(cw, "creators", []))
    )

    for cid, _clabel in creators_to_use:
        if isinstance(cid, str) and cid.startswith(("http://", "https://", "urn:")):
            lines.append(f"    dct:creator <{cid}> ;")
        else:
            lines.append(f"    dct:creator {_ttl_lit(cid)} ;")

    if creators_to_use:
        creators_source = "CITATION.cff" if derived_creators else "MD.cff"
        creator_names = [clabel for _, clabel in creators_to_use]
        tracker.record(
            field="dataset.creators",
            source=creators_source,
            detail={"value": ", ".join(creator_names)},
            count_inc=len(creators_to_use),
        )

    # License from MD.cff if present (CITATION.cff license handled in postprocess)
    lic = getattr(cw, "license", None)
    if lic:
        spdx = _spdx_url(lic)
        if spdx:
            lines.append(f"    dct:license <{spdx}> ;")
            llabel = None
            if isinstance(lic, dict):
                llabel = (
                    (lic.get("label") or lic.get("id") or "").strip()
                    if isinstance((lic.get("label") or lic.get("id") or ""), str)
                    else None
                )
            elif isinstance(lic, str):
                llabel = lic.strip()
            if llabel:
                post_dataset_triples.append(f"<{spdx}> rdfs:label {_ttl_lit(llabel)} .")

            tracker.record(
                field="dataset.license",
                source="MD.cff",
                detail={"license": lic, "spdx": spdx},
                count_inc=1,
            )
        else:
            if isinstance(lic, dict):
                try:
                    lic_lit = json.dumps(lic, ensure_ascii=False, sort_keys=True)
                except Exception:
                    lic_lit = str(lic)
                lines.append(f"    dct:license {_ttl_lit(lic_lit)} ;")
            else:
                lines.append(f"    dct:license {_ttl_lit(lic)} ;")
            tracker.record(
                field="dataset.license",
                source="MD.cff",
                detail={"license": lic},
                count_inc=1,
            )

    # Publisher / title
    lines.append(f"    dct:publisher <{cw.publisher_id}> ;")
    lines.append(f'    dct:title "{cw_title}" ;')
    tracker.record(
        field="dataset.publisher",
        source="MD.cff",
        detail={"publisher_id": cw.publisher_id, "publisher_label": cw.publisher_label},
    )
    tracker.record(field="dataset.title", source="MD.cff", detail={"title": cw_title})

    # identifiers (MD.cff) → RDF
    identifiers = getattr(cw, "identifiers", None)
    if identifiers and isinstance(identifiers, list):
        ident_count = 0
        sameas_count = 0
        for ident in identifiers:
            if isinstance(ident, str):
                if ident.startswith(("http://", "https://")):
                    lines.append(f"    dct:identifier <{ident}> ;")
                else:
                    lines.append(f'    dct:identifier "{ident}" ;')
                ident_count += 1
                continue

            if not isinstance(ident, dict):
                continue

            ident_id = ident.get("id")
            ident_label = ident.get("label")
            same_as = ident.get("sameAs") or ident.get("same_as") or []

            if isinstance(ident_id, str) and ident_id.startswith(
                ("http://", "https://")
            ):
                lines.append(f"    dct:identifier <{ident_id}> ;")
                ident_count += 1
            elif isinstance(ident_id, str) and ident_id.strip():
                lines.append(f'    dct:identifier "{ident_id.strip()}" ;')
                ident_count += 1

            if isinstance(ident_label, str) and ident_label.strip():
                lines.append(f'    dct:identifier "{ident_label.strip()}" ;')
                ident_count += 1

            if isinstance(same_as, list):
                for sa in same_as:
                    if isinstance(sa, str) and sa.startswith(("http://", "https://")):
                        lines.append(f"    owl:sameAs <{sa}> ;")
                        sameas_count += 1

        tracker.record(
            field="dataset.identifiers",
            source="MD.cff",
            detail={
                "dct:identifier_count": ident_count,
                "owl:sameAs_count": sameas_count,
            },
            count_inc=ident_count,
        )

    # Landing page if present
    landing = getattr(cw, "landing_page", None) or getattr(cw, "landingPage", None)
    if (
        landing
        and isinstance(landing, str)
        and landing.startswith(("http://", "https://"))
    ):
        lines.append(f"    dcat:landingPage <{landing}> ;")
        tracker.record(
            field="dataset.landingPage",
            source="MD.cff",
            detail={"landingPage": landing},
        )

    # Distributions on dataset
    if dists:
        dist_uris = ",".join([d[0] for d in dists])
        lines.append(f"    dcat:distribution {dist_uris} ;")
        tracker.record(
            field="dataset.distributions",
            source="ZIP",
            detail={"count": len(dists)},
            count_inc=len(dists),
        )

    # Mapping-driven MD.cff emission
    if isinstance(md_root, dict):
        inline_map, post_map = _apply_md_cff_mapping(md_root, subj, tracker)
        for ln in inline_map:
            lines.append(ln)
    else:
        post_map = []

    # close dataset block
    if lines[-1].strip().endswith(";"):
        lines[-1] = lines[-1].rstrip().rstrip(";") + " ."
    else:
        lines[-1] = lines[-1].rstrip() + " ."

    lines.append("")

    # Post-triples from mapping-driven handlers (GeoSPARQL, temporal)
    for t in post_map:
        lines.append(t)
    if post_map:
        lines.append("")

    # Publisher node
    lines.append(f"<{cw.publisher_id}> a schema:Organization ;")
    lines.append(f'    schema:name "{cw.publisher_label}" .')
    lines.append("")
    tracker.record(
        field="agent.publisher",
        source="MD.cff",
        detail={"publisher_id": cw.publisher_id},
    )

    # Creator agent nodes
    agent_creators_source = "CITATION.cff" if derived_creators else "MD.cff"
    for cid, clabel in creators_to_use:
        lines.append(f"<{cid}> a schema:Person ;")
        lines.append(f'    schema:name "{clabel}" .')
        lines.append("")
        tracker.record(
            field="agent.creator",
            source=agent_creators_source,
            detail={"creator_id": cid, "creator_name": clabel},
        )

    # Distributions
    for dist_uri, m in dists:
        name = m["name"]
        size = m.get("size")
        sha256_hex = m.get("sha256")

        role = _classify_role(name, cw_fdo_type, rules_cfg, tracker=tracker)
        mt = _media_type(name, tracker=tracker)
        access_url = f"<urn:fdo-squirrel:content/{quote(name, safe='')}>"

        lines.append(f"{dist_uri} a dcat:Distribution, crmdig:D9_Data_Object ;")
        lines.append(f"    dcat:accessURL {access_url} ;")
        if isinstance(size, int):
            lines.append(f"    dcat:byteSize {size} ;")
            tracker.record(
                field="distribution.byteSize",
                source="ZIP",
                detail={"file": name, "byteSize": size},
                count_inc=1,
            )

        lines.append(f'    dcat:mediaType "{mt}" ;')
        lines.append(f'    fdo:path "{name}" ;')
        lines.append(f'    fdo:role "{role}" ;')

        if isinstance(sha256_hex, str) and len(sha256_hex) >= 64:
            lines.append(f'    fdo:sha256 "{sha256_hex}" ;')
            tracker.record(
                field="distribution.sha256",
                source="ZIP (computed)",
                detail={"file": name},
                count_inc=1,
            )
        else:
            tracker.record(
                field="distribution.sha256",
                source="ZIP (not available)",
                detail={"file": name},
                count_inc=1,
            )

        # Record complete per-file entry for the HTML report ZIP section
        tracker.record(
            field="distributions",
            source="ZIP",
            detail={
                "path": name,
                "role": role,
                "mediaType": mt,
                "byteSize": size if isinstance(size, int) else None,
                "sha256": (
                    sha256_hex[:16]
                    if isinstance(sha256_hex, str) and len(sha256_hex) >= 16
                    else None
                ),
            },
            count_inc=0,
        )

        if lines[-1].strip().endswith(";"):
            lines[-1] = lines[-1].rstrip().rstrip(";") + " ."
        else:
            lines[-1] = lines[-1].rstrip() + " ."

        lines.append("")

    # If no upstream citation triples were provided, derive a minimal set from CITATION.cff (ZIP)
    if not citation_triples and isinstance(citation_raw, dict):
        subj_uri = subj
        derived: list[str] = []

        lic2 = citation_raw.get("license")
        if isinstance(lic2, str) and lic2.strip():
            lic_lit = '"' + lic2.strip().replace('"', '"') + '"'
            derived.extend(
                [
                    f"{subj_uri} cff:license {lic_lit} .",
                    f"{subj_uri} cff:license-url {lic_lit} .",
                    f"{subj_uri} schema:license {lic_lit} .",
                    f"{subj_uri} codemeta:license {lic_lit} .",
                    f"{subj_uri} wdt:P275 {lic_lit} .",
                ]
            )

        kws = citation_raw.get("keywords")
        if isinstance(kws, list):
            for kw in kws:
                if isinstance(kw, str) and kw.strip():
                    kw_lit = '"' + kw.strip().replace('"', '"') + '"'
                    derived.extend(
                        [
                            f"{subj_uri} cff:keywords {kw_lit} .",
                            f"{subj_uri} schema:keywords {kw_lit} .",
                            f"{subj_uri} codemeta:keywords {kw_lit} .",
                            f"{subj_uri} wdt:P921 {kw_lit} .",
                        ]
                    )

        authors = citation_raw.get("authors")
        if isinstance(authors, list):
            for a in authors:
                if not isinstance(a, dict):
                    continue
                orcid = a.get("orcid")
                if isinstance(orcid, str) and orcid.strip().startswith("http"):
                    derived.append(f"{subj_uri} dct:creator <{orcid.strip()}> .")
                    derived.append(f"{subj_uri} schema:creator <{orcid.strip()}> .")

        citation_triples = derived

    # Append (post-processed) citation triples
    post = _postprocess_citation_triples(citation_triples, tracker=tracker)
    for t in post:
        lines.append(t)
    tracker.record(
        field="citation.triples.used",
        source="CITATION.cff",
        detail={"count": len(post)},
        count_inc=len(post),
    )

    lines.append("")

    # JSON provenance report
    out_dir = Path(info.get("output_dir", "output"))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        out_dir = Path.cwd()

    report_path = out_dir / "rdf_modelling_report.json"
    try:
        tracker.write_json(report_path)
        print(f"✔ JSON provenance report written: {report_path.resolve()}")
    except Exception as e:
        print(f"⚠ Could not write JSON provenance report to {report_path}: {e}")

    try:
        rep = tracker.report()
        keys = sorted(rep.get("summary", {}).keys())
        print(f"ℹ Provenance fields recorded: {len(keys)}")
        for k in [k for k in keys if k.startswith("dataset.")][:10]:
            print(f"  - {k}: {', '.join(rep['summary'][k]['sources'])}")
        print(
            "  - citation.triples.used:",
            ", ".join(
                rep["summary"].get("citation.triples.used", {}).get("sources", [])
            ),
        )
    except Exception:
        pass

    if post_dataset_triples:
        lines.extend(post_dataset_triples)

    return "\n".join(lines)
