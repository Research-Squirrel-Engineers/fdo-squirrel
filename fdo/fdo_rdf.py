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
            # keep a few examples only
            if len(agg[f]["examples"]) < 5 and e.get("detail"):
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

    for rule in rules:
        match = rule.get("match") or {}
        exts = match.get("extension") or []
        if isinstance(exts, list) and ext in [e.lower() for e in exts]:
            role = rule.get("role") or default_role
            if tracker:
                tracker.record(
                    field="distribution.role",
                    source="ZIP + classification_rules.yaml",
                    detail={
                        "file": filename,
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


def _spdx_url(license_id: str) -> Optional[str]:
    if not license_id:
        return None
    lid = license_id.strip()
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
        return members

    # Fallback: best-effort if ingest already provided members
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
) -> str:
    """
    Write Turtle similar to the earlier "full" fdo-metadata.ttl:
      - dataset-ish core (dcat:Dataset + fdo:*FDO)
      - rich MD.cff fields if present on CrosswalkRecord
      - ZIP structure as distributions with deterministic URN IDs + accessURL + sha256
      - crosswalk triples appended (schema/codemeta/wdt/cff etc.), post-processed
      - JSON provenance report (console + file)
    """
    tracker = ProvenanceTracker()
    rules_cfg = _load_classification_rules(tracker=tracker)
    info = info or {}

    lines: List[str] = []

    # Prefixes
    lines.extend(
        [
            "@prefix dcat: <http://www.w3.org/ns/dcat#> .",
            "@prefix dct: <http://purl.org/dc/terms/> .",
            "@prefix fdo: <https://w3id.org/fdo-squirrel/> .",
            "@prefix schema: <https://schema.org/> .",
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
            "@prefix foaf: <http://xmlns.com/foaf/0.1/> .",
            "@prefix codemeta: <https://codemeta.github.io/terms/> .",
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
            "@prefix cff: <https://citation-file-format.github.io/terms/> .",
            "@prefix wd: <http://www.wikidata.org/entity/> .",
            "@prefix wdt: <http://www.wikidata.org/prop/direct/> .",
            "",
        ]
    )

    subj = f"<{cw.id}>"

    tracker.record(
        field="dataset.id",
        source="MD.cff",
        detail={"id": cw.id, "fdo_type": cw.fdo_type},
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
    lines.append(f"{subj} a dcat:Dataset,")
    lines.append(f"        {cw.fdo_type} ;")

    # Optional MD.cff fields (use getattr so it doesn't crash if absent)
    created = getattr(cw, "created", None)
    issued = getattr(cw, "issued", None)
    modified = getattr(cw, "modified", None)
    description = getattr(cw, "description", None)
    version = getattr(cw, "version", None)
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

    # Creators (dataset -> persons)
    for cid, _clabel in cw.creators:
        lines.append(f"    dct:creator <{cid}> ;")
    if cw.creators:
        tracker.record(
            field="dataset.creators",
            source="MD.cff",
            detail={"count": len(cw.creators)},
            count_inc=len(cw.creators),
        )

    # License from MD.cff if present (kept; CITATION.cff license handled in postprocess)
    lic = getattr(cw, "license", None)
    if lic:
        spdx = _spdx_url(lic)
        if spdx:
            lines.append(f"    dct:license <{spdx}>,")
            lines.append(f'        "{lic}" ;')
            tracker.record(
                field="dataset.license",
                source="MD.cff",
                detail={"license": lic, "spdx": spdx},
            )
        else:
            lines.append(f'    dct:license "{lic}" ;')
            tracker.record(
                field="dataset.license", source="MD.cff", detail={"license": lic}
            )

    # Publisher / title
    lines.append(f"    dct:publisher <{cw.publisher_id}> ;")
    lines.append(f'    dct:title "{cw.title}" ;')
    tracker.record(
        field="dataset.publisher",
        source="MD.cff",
        detail={"publisher_id": cw.publisher_id, "publisher_label": cw.publisher_label},
    )
    tracker.record(field="dataset.title", source="MD.cff", detail={"title": cw.title})

    # Spatial / temporal if present
    spatial = getattr(cw, "spatial", None)
    if spatial and isinstance(spatial, list):
        objs = []
        for s in spatial:
            if isinstance(s, dict) and s.get("id"):
                objs.append(f"<{s['id']}>")
                if s.get("label"):
                    objs.append(f'"{s["label"]}"')
            elif isinstance(s, str):
                objs.append(f'"{s}"')
        if objs:
            lines.append("    dct:spatial " + ",\n        ".join(objs) + " ;")
            tracker.record(
                field="dataset.spatial", source="MD.cff", detail={"items": len(objs)}
            )

    temporal = getattr(cw, "temporal", None)
    if temporal:
        if isinstance(temporal, list):
            for t in temporal:
                if isinstance(t, dict) and t.get("label"):
                    lines.append(f'    dct:temporal "{t["label"]}" ;')
                elif isinstance(t, str):
                    lines.append(f'    dct:temporal "{t}" ;')
            tracker.record(
                field="dataset.temporal", source="MD.cff", detail={"kind": "list"}
            )
        elif isinstance(temporal, str):
            lines.append(f'    dct:temporal "{temporal}" ;')
            tracker.record(
                field="dataset.temporal", source="MD.cff", detail={"kind": "string"}
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

    # Keywords (MD.cff) → dcat:keyword + dct:subject
    keywords = getattr(cw, "keywords", None)
    if keywords and isinstance(keywords, list):
        kw_objs_keyword = []
        kw_objs_subject = []
        for kw in keywords:
            if isinstance(kw, dict) and kw.get("id"):
                kw_objs_keyword.append(f"<{kw['id']}>")
                kw_objs_subject.append(f"<{kw['id']}>")
                if kw.get("label"):
                    kw_objs_keyword.append(f'"{kw["label"]}"')
                    kw_objs_subject.append(f'"{kw["label"]}"')
            elif isinstance(kw, str):
                kw_objs_keyword.append(f'"{kw}"')
                kw_objs_subject.append(f'"{kw}"')
        if kw_objs_keyword:
            lines.append(
                "    dcat:keyword " + ",\n        ".join(kw_objs_keyword) + " ;"
            )
        if kw_objs_subject:
            lines.append(
                "    dct:subject " + ",\n        ".join(kw_objs_subject) + " ;"
            )
        tracker.record(
            field="dataset.keywords",
            source="MD.cff",
            detail={"count": len(keywords)},
            count_inc=len(keywords),
        )

    # Distributions on dataset
    if dists:
        dist_uris = ",\n        ".join([d[0] for d in dists])
        lines.append(f"    dcat:distribution {dist_uris} ;")
        tracker.record(
            field="dataset.distributions",
            source="ZIP",
            detail={"count": len(dists)},
            count_inc=len(dists),
        )

    # close dataset block
    if lines[-1].strip().endswith(";"):
        lines[-1] = lines[-1].rstrip().rstrip(";") + " ."
    else:
        lines[-1] = lines[-1].rstrip() + " ."

    lines.append("")

    # Publisher + creators nodes
    lines.append(f"<{cw.publisher_id}> a schema:Organization ;")
    lines.append(f'    schema:name "{cw.publisher_label}" .')
    lines.append("")
    tracker.record(
        field="agent.publisher",
        source="MD.cff",
        detail={"publisher_id": cw.publisher_id},
    )

    for cid, clabel in cw.creators:
        lines.append(f"<{cid}> a schema:Person ;")
        lines.append(f'    schema:name "{clabel}" .')
        lines.append("")
        tracker.record(
            field="agent.creator",
            source="MD.cff",
            detail={"creator_id": cid, "creator_name": clabel},
        )

    # ---------- Distributions ----------
    for dist_uri, m in dists:
        name = m["name"]
        size = m.get("size")
        sha256_hex = m.get("sha256")

        role = _classify_role(name, cw.fdo_type, rules_cfg, tracker=tracker)
        mt = _media_type(name, tracker=tracker)

        access_url = f"<urn:fdo-squirrel:content/{quote(name, safe='')}>"

        lines.append(f"{dist_uri} a dcat:Distribution ;")
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

        if lines[-1].strip().endswith(";"):
            lines[-1] = lines[-1].rstrip().rstrip(";") + " ."
        else:
            lines[-1] = lines[-1].rstrip() + " ."

        lines.append("")

    # ---------- Append (post-processed) citation triples ----------
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

    # --------------------------------------------------
    # JSON provenance report (console + file)
    # --------------------------------------------------
    report_path = Path.cwd() / "rdf_modelling_report.json"
    try:
        tracker.write_json(report_path)
        print(f"\n✔ JSON provenance report written: {report_path.resolve()}")
    except Exception as e:
        print(f"\n⚠ Could not write JSON provenance report to {report_path}: {e}")

    # small console summary
    try:
        rep = tracker.report()
        keys = sorted(rep.get("summary", {}).keys())
        print(f"ℹ Provenance fields recorded: {len(keys)}")
        # show a few most relevant
        for k in [k for k in keys if k.startswith("dataset.")][:8]:
            print(f"  - {k}: {', '.join(rep['summary'][k]['sources'])}")
        print(
            "  - citation.triples.used:",
            ", ".join(
                rep["summary"].get("citation.triples.used", {}).get("sources", [])
            ),
        )
        print(
            "  - dataset.distributions:",
            ", ".join(
                rep["summary"].get("dataset.distributions", {}).get("sources", [])
            ),
        )
    except Exception:
        pass

    return "\n".join(lines)
