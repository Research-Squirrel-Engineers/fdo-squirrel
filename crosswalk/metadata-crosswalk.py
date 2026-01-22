#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FDO Metadata Crosswalk Builder
==============================

Builds a single canonical crosswalk from:
  1) schema-org--codemeta.csv
  2) cwreference--cff.csv
  3) cwreference--wikidata.csv

Outputs:
  - crosswalk.fdo-metadata.json
  - crosswalk.fdo-metadata.yaml

Canonical rule model (schema-agnostic):
  - source_term: CURIE or null
  - source_schema: derived from CURIE prefix or null
  - target_term: CURIE or string or null
  - target_schema: derived from CURIE prefix or 'cff' (or null)
  - mapping_scope: e.g. schemaorg_and_codemeta_same, codemeta_and_wikidata, cff_only, wikidata_only
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


# =====================================================
# PATH CONFIG (robust for VS Code)
# =====================================================

HERE = Path(__file__).resolve().parent

CSV_SCHEMA_CODEMETA = HERE / "schema-org--codemeta.csv"
CSV_CFF = HERE / "cwreference--cff.csv"
CSV_WIKIDATA = HERE / "cwreference--wikidata.csv"

OUT_JSON = HERE / "crosswalk.fdo-metadata.json"
OUT_YAML = HERE / "crosswalk.fdo-metadata.yaml"

CSV_SEP = "|"
ENCODING = "utf-8-sig"


# =====================================================
# HELPERS
# =====================================================


def norm(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v if v else None


def is_nav(v: Optional[str]) -> bool:
    return norm(v) == "nav"


def curie(ns: str, term: Optional[str]) -> Optional[str]:
    if term is None:
        return None
    if ":" in term:
        return term
    return f"{ns}:{term}"


def curie_prefix(cur: Optional[str]) -> Optional[str]:
    if cur is None:
        return None
    if ":" not in cur:
        return None
    return cur.split(":", 1)[0]


def local_name(cur: Optional[str]) -> Optional[str]:
    if cur is None:
        return None
    if ":" not in cur:
        return cur
    return cur.split(":", 1)[1]


def schema_label_from_prefix(prefix: Optional[str]) -> Optional[str]:
    """
    Normalise CURIE prefixes to stable schema labels used in mapping_scope / metadata.
    """
    if prefix is None:
        return None
    p = prefix.strip()

    # our canonical labels
    if p == "schema":
        return "schemaorg"
    if p == "codemeta":
        return "codemeta"
    if p == "wdt":
        return "wikidata"

    # fallback
    return p


def sha_id(*parts: Optional[str]) -> str:
    payload = "|".join([p or "null" for p in parts])
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"cw-{h}"


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def write_yaml(path: Path, obj: Any) -> None:
    try:
        import yaml  # type: ignore

        path.write_text(
            yaml.safe_dump(obj, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:
        # Fallback YAML-like output
        lines: List[str] = []
        for rule in obj:
            lines.append("-")
            for k, v in rule.items():
                if v is None:
                    lines.append(f"  {k}: null")
                else:
                    sv = str(v).replace('"', '\\"')
                    lines.append(f'  {k}: "{sv}"')
        path.write_text("\n".join(lines), encoding="utf-8")


# =====================================================
# cwreference source heuristic
# =====================================================

CODEMETA_PREFERRED_SOURCES = {
    # CodeMeta terms that appear in cwreference files and should be treated as codemeta:*
    "issueTracker",
    "referencePublication",
    "softwareRequirements",
    "runtimePlatform",
    "downloadUrl",
    "codeRepository",
    "programmingLanguage",
    "softwareVersion",
    "fileSize",
    "hasPart",
    "isPartOf",
    "dateCreated",
    "dateModified",
    "datePublished",
    "funder",
    "keywords",
    "license",
    "publisher",
    "producer",
    "author",
    "contributor",
    "editor",
    "citation",
    "sameAs",
    "url",
    "encodingFormat",
    "identifier",
    "maintainer",
    "relatedLink",
    "name",
    "version",
}


def cwreference_to_source_term(cwref: Optional[str]) -> Optional[str]:
    """
    Turn cwreference term into a CURIE.
    - If cwref is already a CURIE, keep it.
    - If cwref is in CODEMETA_PREFERRED_SOURCES, use codemeta:<term>
    - Else default schema:<term>
    - 'nav' is handled by caller (source_term becomes None)
    """
    t = norm(cwref)
    if t is None:
        return None
    if ":" in t:
        return t
    if t in CODEMETA_PREFERRED_SOURCES:
        return curie("codemeta", t)
    return curie("schema", t)


# =====================================================
# PARSERS
# =====================================================


def parse_schema_codemeta() -> List[Dict[str, Any]]:
    """
    Parse schema-org--codemeta.csv into canonical rule model.
    """
    rules: List[Dict[str, Any]] = []

    with CSV_SCHEMA_CODEMETA.open("r", encoding=ENCODING, newline="") as f:
        reader = csv.DictReader(f, delimiter=CSV_SEP)

        for row in reader:
            schema_term = (
                None
                if is_nav(row.get("schemaorg"))
                else curie("schema", norm(row.get("schemaorg")))
            )
            cm_raw = norm(row.get("codemeta"))

            # Determine target term
            if cm_raw == "yes" and schema_term:
                codemeta_term = curie("codemeta", local_name(schema_term))
            elif cm_raw and cm_raw not in ("yes", "no"):
                codemeta_term = curie("codemeta", cm_raw)
            else:
                codemeta_term = None

            # Determine mapping scope (fix: decide SAME vs DIFFERENT via local-name comparison)
            if schema_term and codemeta_term:
                if local_name(schema_term) == local_name(codemeta_term):
                    scope = "schemaorg_and_codemeta_same"
                else:
                    scope = "schemaorg_and_codemeta_different"
            elif schema_term and codemeta_term is None:
                scope = "schemaorg_only"
            elif schema_term is None and codemeta_term:
                scope = "codemeta_only"
            else:
                # both None (rare/degenerate)
                scope = "unmapped"

            source_schema = schema_label_from_prefix(curie_prefix(schema_term))
            target_schema = "codemeta"

            rules.append(
                {
                    "id": sha_id(schema_term, codemeta_term, norm(row.get("range"))),
                    "source_term": schema_term,
                    "source_schema": source_schema,
                    "target_term": codemeta_term,
                    "target_schema": target_schema,
                    "mapping_scope": scope,
                    "range": norm(row.get("range")),
                    "description": norm(row.get("description")),
                    "comment": norm(row.get("comment")),
                }
            )

    return rules


def parse_reference_csv(path: Path, target_schema: str) -> List[Dict[str, Any]]:
    """
    Parse cwreference--cff.csv or cwreference--wikidata.csv into canonical rule model.

    - cwreference is interpreted into source_term (schema:* by default, codemeta:* for selected terms).
    - target_schema is 'cff' or 'wikidata'
    - For wikidata, target_term becomes wdt:Pxyz
    - For cff, target_term is kept verbatim (string), to preserve "doi or identifiers" etc.
    """
    rules: List[Dict[str, Any]] = []

    with path.open("r", encoding=ENCODING, newline="") as f:
        reader = csv.DictReader(f, delimiter=CSV_SEP)

        # Identify the target column name (2nd column)
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            raise ValueError(f"CSV {path.name} must have at least two columns.")
        target_col = reader.fieldnames[1]

        for row in reader:
            src_raw = norm(row.get("cwreference"))
            tgt_raw = norm(row.get(target_col))

            # Source term
            if is_nav(src_raw):
                source_term = None
                source_schema = None
                scope = f"{target_schema}_only"  # e.g. cff_only, wikidata_only
            else:
                source_term = cwreference_to_source_term(src_raw)
                source_schema = schema_label_from_prefix(curie_prefix(source_term))
                scope = (
                    f"{source_schema}_and_{target_schema}"
                    if source_schema
                    else f"unmapped_and_{target_schema}"
                )

            # Target term
            if target_schema == "wikidata":
                target_term = curie("wdt", tgt_raw)  # wdt:Pxyz
            else:
                target_term = tgt_raw  # keep verbatim (no split, no CURIE forcing)

            rules.append(
                {
                    "id": sha_id(
                        source_term,
                        str(target_term) if target_term is not None else None,
                        target_schema,
                    ),
                    "source_term": source_term,
                    "source_schema": source_schema,
                    "target_term": target_term,
                    "target_schema": target_schema,
                    "mapping_scope": scope,
                    "range": None,
                    "description": None,
                    "comment": norm(row.get("comment")),
                }
            )

    return rules


# =====================================================
# MAIN
# =====================================================


def main() -> None:
    all_rules: List[Dict[str, Any]] = []

    # 1) schema.org -> CodeMeta
    all_rules.extend(parse_schema_codemeta())

    # 2) cwreference -> CFF
    if CSV_CFF.exists():
        all_rules.extend(parse_reference_csv(CSV_CFF, "cff"))
    else:
        print(f"WARNING: Missing {CSV_CFF.name} (skipped).")

    # 3) cwreference -> Wikidata
    if CSV_WIKIDATA.exists():
        all_rules.extend(parse_reference_csv(CSV_WIKIDATA, "wikidata"))
    else:
        print(f"WARNING: Missing {CSV_WIKIDATA.name} (skipped).")

    write_json(OUT_JSON, all_rules)
    write_yaml(OUT_YAML, all_rules)

    # Small sanity print
    counts: Dict[str, int] = {}
    for r in all_rules:
        ts = r.get("target_schema") or "unknown"
        counts[ts] = counts.get(ts, 0) + 1

    print("=== FDO Metadata Crosswalk built ===")
    print(f"Total rules: {len(all_rules)}")
    print(
        "Rules per target_schema:",
        ", ".join([f"{k}={v}" for k, v in sorted(counts.items())]),
    )
    print(f"JSON: {OUT_JSON}")
    print(f"YAML: {OUT_YAML}")


if __name__ == "__main__":
    main()
