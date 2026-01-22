#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FDO Metadata Crosswalk Builder + CFF->RDF(TTL) Reference Generator
==================================================================

Goal
----
(1) Build ONE canonical crosswalk graph as directed edges:

    (from_namespace, from_term)  --->  (to_namespace, to_term)

    plus reverse edges where applicable, so you can start from ANY schema term.

(2) Reference implementation step:
    Read a CFF YAML (CITATION.cff / CFFplus.cff), map keys to preferred target
    vocabularies (CodeMeta > schema.org > CFF > wdt) and emit a Turtle file.

New in this version
-------------------
- Load CFF from:
    * local path (CFF/YAML)
    * URL (CFF/YAML)
    * local ZIP containing a CFF
    * URL ZIP containing a CFF
- The loader is a reusable function you can import from other scripts.

Run
---
Run directly in VS Code via "Run Python File".
No CLI arguments required.
"""

from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import urllib.request
import zipfile
from collections import defaultdict, deque
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# =====================================================
# PATH CONFIG (robust for VS Code)
# =====================================================

HERE = Path(__file__).resolve().parent

CSV_SCHEMA_CODEMETA = HERE / "schema-org--codemeta.csv"
CSV_CFF = HERE / "cwreference--cff.csv"
CSV_WDT = HERE / "cwreference--wikidata.csv"
CFF_SCHEMA_JSON = HERE / "cff-schema-1-2-0.json"  # optional

OUT_JSON = HERE / "crosswalk.fdo-metadata.json"
OUT_YAML = HERE / "crosswalk.fdo-metadata.yaml"

# Optional demo output
OUT_TTL = HERE / "fdo-metadata.ttl"

CSV_SEP = "|"
ENCODING = "utf-8-sig"

# “FDO world” base for local identifiers in RDF
FDO_BASE = "https://w3id.org/fdo-squirrel/"
FDO_PREFIX = "fdo"

# -----------------------------------------------------
# OPTIONAL: Configure a single source here (VS Code run)
# -----------------------------------------------------
# Examples:
#   CFF_SOURCE = r"C:\git\fdo-squirrel\CITATION.cff"
#   CFF_SOURCE = "https://example.org/my/CITATION.cff"
#   CFF_SOURCE = r"C:\data\my-fdo.zip"
#   CFF_SOURCE = "https://zenodo.org/record/.../files/my-fdo.zip"
#
# If the source is a ZIP, you can optionally specify the CFF member path:
#   CFF_IN_ZIP = "CITATION.cff"
#   CFF_IN_ZIP = "metadata/CFFplus.cff"
#
# CFF_SOURCE: Optional[str] = None
# CFF_IN_ZIP: Optional[str] = None

CFF_SOURCE = "C:/git/fdo-squirrel/crosswalk/CITATION.cff"
CFF_IN_ZIP: Optional[str] = None

# =====================================================
# HELPERS
# =====================================================


def norm(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v if v else None


def is_nav(v: Optional[str]) -> bool:
    """nav = 'no available value' in this crosswalk context."""
    return norm(v) == "nav"


def ensure_curie(prefix: str, term: Optional[str]) -> Optional[str]:
    """
    Ensure CURIE formatting, unless already CURIE or IRI.
    Examples:
      ensure_curie("schema", "author") -> "schema:author"
      ensure_curie("cff", "title") -> "cff:title"
      ensure_curie("wdt", "P123") -> "wdt:P123"
    """
    if term is None:
        return None
    t = term.strip()
    if not t:
        return None
    if t.startswith("http://") or t.startswith("https://"):
        return t
    if ":" in t:
        return t
    return f"{prefix}:{t}"


def sha_id(*parts: Optional[str]) -> str:
    payload = "|".join([p or "null" for p in parts])
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"cw-{h}"


def split_targets(cell: Optional[str]) -> List[str]:
    """
    Split target cells like:
      - 'doi or identifiers'
      - 'license or license-url'
      - 'repository or url'
    into multiple target terms.
    """
    raw = norm(cell)
    if not raw:
        return []
    raw = raw.replace("reposirotry", "repository")  # known typo fix
    parts = [p.strip() for p in raw.split(" or ") if p.strip()]
    return parts if parts else [raw]


def safe_read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        print(f"[WARN] CSV not found (skipped): {path.name}")
        return []
    with path.open("r", encoding=ENCODING, newline="") as f:
        reader = csv.DictReader(f, delimiter=CSV_SEP)
        return [dict(r) for r in reader]


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def write_yaml(path: Path, obj: Any) -> None:
    """Write YAML if PyYAML installed; else fallback to YAML-ish text."""
    try:
        import yaml  # type: ignore

        path.write_text(
            yaml.safe_dump(obj, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:
        # minimal fallback
        lines: List[str] = []
        if isinstance(obj, list):
            for item in obj:
                lines.append("-")
                if isinstance(item, dict):
                    for k, v in item.items():
                        if v is None:
                            lines.append(f"  {k}: null")
                        elif isinstance(v, bool):
                            lines.append(f"  {k}: {'true' if v else 'false'}")
                        else:
                            sv = str(v).replace('"', '\\"')
                            lines.append(f'  {k}: "{sv}"')
                else:
                    sv = str(item).replace('"', '\\"')
                    lines.append(f'  value: "{sv}"')
        else:
            lines.append('value: "' + json.dumps(obj).replace('"', '\\"') + '"')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_edge(
    from_ns: str,
    from_term: str,
    to_ns: Optional[str],
    to_term: Optional[str],
    mapping_scope: str,
    *,
    range_: Optional[str] = None,
    description: Optional[str] = None,
    comment: Optional[str] = None,
    provenance: Optional[str] = None,
    is_reverse: bool = False,
) -> Dict[str, Any]:
    return {
        "id": sha_id(from_ns, from_term, to_ns, to_term, mapping_scope),
        "from_namespace": from_ns,
        "from_term": from_term,
        "to_namespace": to_ns,
        "to_term": to_term,
        "mapping_scope": mapping_scope,
        "is_reverse": is_reverse,
        "range": range_,
        "description": description,
        "comment": comment,
        "provenance": provenance,
    }


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def guess_is_zip(name_or_url: str) -> bool:
    lowered = name_or_url.lower()
    return lowered.endswith(".zip")


def guess_is_cff(name_or_url: str) -> bool:
    lowered = name_or_url.lower()
    return (
        lowered.endswith(".cff")
        or lowered.endswith(".yml")
        or lowered.endswith(".yaml")
    )


# =====================================================
# CFF SOURCE LOADING (LOCAL / URL / ZIP)
# =====================================================

DEFAULT_CFF_MEMBER_CANDIDATES = [
    "CITATION.cff",
    "citation.cff",
    "CFFplus.cff",
    "cffplus.cff",
]


def _download_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "fdo-squirrel-crosswalk/1.0 (+https://w3id.org/fdo-squirrel/)"
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _read_local_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _pick_cff_member_from_zip(
    zf: zipfile.ZipFile, preferred_member: Optional[str]
) -> str:
    members = zf.namelist()

    if preferred_member:
        if preferred_member in members:
            return preferred_member
        # allow loose matching (e.g., user gave just "CITATION.cff")
        matches = [m for m in members if m.endswith(preferred_member)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FileNotFoundError(
                f"Ambiguous CFF member '{preferred_member}' in ZIP. Matches: {matches}"
            )
        raise FileNotFoundError(
            f"CFF member '{preferred_member}' not found in ZIP. Available: {members[:50]}"
        )

    # auto-detect common names
    for cand in DEFAULT_CFF_MEMBER_CANDIDATES:
        if cand in members:
            return cand

    # fallback: any .cff file
    cff_files = [m for m in members if m.lower().endswith(".cff")]
    if len(cff_files) == 1:
        return cff_files[0]
    if len(cff_files) > 1:
        raise FileNotFoundError(
            f"Multiple .cff files in ZIP; specify which via cff_member_in_zip. Found: {cff_files}"
        )

    raise FileNotFoundError(
        "No CFF file found in ZIP. Expected one of "
        f"{DEFAULT_CFF_MEMBER_CANDIDATES} or any *.cff. ZIP members sample: {members[:50]}"
    )


def load_yaml_from_text(text: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "PyYAML is required to load CFF YAML files. Install: pip install pyyaml"
        ) from e

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("YAML root must be a mapping/dict")
    return data


def load_cff_yaml_from_source(
    source: str,
    *,
    cff_member_in_zip: Optional[str] = None,
    timeout: int = 30,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Load a CFF YAML dict from:
      - local file path (CFF/YAML)
      - URL to CFF/YAML
      - local ZIP containing a CFF
      - URL to ZIP containing a CFF

    Returns:
      (cff_dict, info_dict)

    info_dict includes:
      - source
      - source_type: "local_cff" | "url_cff" | "local_zip" | "url_zip"
      - used_member_in_zip (if ZIP)
    """
    if not source or not isinstance(source, str):
        raise ValueError("source must be a non-empty string")

    info: Dict[str, Any] = {
        "source": source,
        "source_type": None,
        "used_member_in_zip": None,
    }

    # URL
    if is_url(source):
        if guess_is_zip(source):
            info["source_type"] = "url_zip"
            zbytes = _download_bytes(source, timeout=timeout)
            with zipfile.ZipFile(BytesIO(zbytes), "r") as zf:
                member = _pick_cff_member_from_zip(zf, cff_member_in_zip)
                info["used_member_in_zip"] = member
                cff_text = zf.read(member).decode("utf-8", errors="replace")
                return load_yaml_from_text(cff_text), info

        # assume direct cff/yaml
        info["source_type"] = "url_cff"
        ybytes = _download_bytes(source, timeout=timeout)
        ytext = ybytes.decode("utf-8", errors="replace")
        return load_yaml_from_text(ytext), info

    # LOCAL PATH
    p = Path(source)
    if not p.exists():
        raise FileNotFoundError(f"Local source not found: {p}")

    if p.is_dir():
        raise IsADirectoryError(f"Source is a directory, expected file: {p}")

    if guess_is_zip(p.name):
        info["source_type"] = "local_zip"
        with zipfile.ZipFile(p, "r") as zf:
            member = _pick_cff_member_from_zip(zf, cff_member_in_zip)
            info["used_member_in_zip"] = member
            cff_text = zf.read(member).decode("utf-8", errors="replace")
            return load_yaml_from_text(cff_text), info

    # assume local cff/yaml
    info["source_type"] = "local_cff"
    text = p.read_text(encoding="utf-8", errors="replace")
    return load_yaml_from_text(text), info


# =====================================================
# PARSERS
# =====================================================


def parse_schema_codemeta() -> List[Dict[str, Any]]:
    """
    Parse schema.org -> CodeMeta mapping CSV.
    Rules:
      - schemaorg may be "nav" -> schema missing
      - codemeta:
          yes -> same term name as schemaorg
          no  -> codemeta missing (null)
          other -> codemeta term name exactly that cell
      - duplicates allowed (keep all)
    """
    rows = safe_read_csv(CSV_SCHEMA_CODEMETA)
    edges: List[Dict[str, Any]] = []
    if not rows:
        return edges

    for row in rows:
        schema_raw = norm(row.get("schemaorg"))
        cm_raw = norm(row.get("codemeta"))

        schema_term = None if is_nav(schema_raw) else ensure_curie("schema", schema_raw)

        codemeta_term: Optional[str] = None
        scope: str

        if (cm_raw or "").lower() == "yes" and schema_term:
            codemeta_term = ensure_curie("codemeta", schema_term.split(":", 1)[1])
            scope = "schema_and_codemeta_same"
        elif (cm_raw or "").lower() == "no" or cm_raw is None or cm_raw == "":
            codemeta_term = None
            scope = "schema_only" if schema_term else "codemeta_only"
        else:
            codemeta_term = ensure_curie("codemeta", cm_raw)
            if schema_term:
                s_local = schema_term.split(":", 1)[1]
                c_local = (
                    codemeta_term.split(":", 1)[1]
                    if ":" in codemeta_term
                    else codemeta_term
                )
                scope = (
                    "schema_and_codemeta_same"
                    if s_local == c_local
                    else "schema_and_codemeta_different"
                )
            else:
                scope = "codemeta_only"

        if schema_term:
            edges.append(
                make_edge(
                    "schema",
                    schema_term,
                    "codemeta" if codemeta_term else None,
                    codemeta_term,
                    scope,
                    range_=norm(row.get("range")),
                    description=norm(row.get("description")),
                    comment=norm(row.get("comment")),
                    provenance=CSV_SCHEMA_CODEMETA.name,
                )
            )
            if codemeta_term:
                edges.append(
                    make_edge(
                        "codemeta",
                        codemeta_term,
                        "schema",
                        schema_term,
                        scope,
                        range_=norm(row.get("range")),
                        description=norm(row.get("description")),
                        comment=norm(row.get("comment")),
                        provenance=CSV_SCHEMA_CODEMETA.name,
                        is_reverse=True,
                    )
                )
        else:
            if codemeta_term:
                edges.append(
                    make_edge(
                        "codemeta",
                        codemeta_term,
                        None,
                        None,
                        "codemeta_only",
                        range_=norm(row.get("range")),
                        description=norm(row.get("description")),
                        comment=norm(row.get("comment")),
                        provenance=CSV_SCHEMA_CODEMETA.name,
                    )
                )

    return edges


def parse_cwreference_cff() -> List[Dict[str, Any]]:
    """
    Parse cwreference (mostly schema term or nav) -> cff term mapping.
    CSV header: cwreference|cff|comment
    """
    rows = safe_read_csv(CSV_CFF)
    edges: List[Dict[str, Any]] = []
    if not rows:
        return edges

    for row in rows:
        src_raw = norm(row.get("cwreference"))
        tgt_raw = norm(row.get("cff"))
        comment = norm(row.get("comment"))

        src_term = None if is_nav(src_raw) else ensure_curie("schema", src_raw)
        targets = split_targets(tgt_raw)
        if not targets:
            continue

        for t in targets:
            cff_term = ensure_curie("cff", t)

            if src_term:
                scope = "schema_and_cff"
                edges.append(
                    make_edge(
                        "schema",
                        src_term,
                        "cff",
                        cff_term,
                        scope,
                        comment=comment,
                        provenance=CSV_CFF.name,
                    )
                )
                edges.append(
                    make_edge(
                        "cff",
                        cff_term,
                        "schema",
                        src_term,
                        scope,
                        comment=comment,
                        provenance=CSV_CFF.name,
                        is_reverse=True,
                    )
                )
            else:
                edges.append(
                    make_edge(
                        "cff",
                        cff_term,
                        None,
                        None,
                        "cff_only",
                        comment=comment,
                        provenance=CSV_CFF.name,
                    )
                )

    return edges


def parse_cwreference_wdt() -> List[Dict[str, Any]]:
    """
    Parse cwreference (mostly schema term or nav) -> Wikidata direct property mapping.
    CSV header: cwreference|wikidata|comment

    We store wikidata properties as: wdt:P123
    and use namespace key: "wdt"
    """
    rows = safe_read_csv(CSV_WDT)
    edges: List[Dict[str, Any]] = []
    if not rows:
        return edges

    for row in rows:
        src_raw = norm(row.get("cwreference"))
        wd_raw = norm(row.get("wikidata"))
        comment = norm(row.get("comment"))
        if not wd_raw:
            continue

        src_term = None if is_nav(src_raw) else ensure_curie("schema", src_raw)
        wdt_term = ensure_curie("wdt", wd_raw)

        if src_term:
            scope = "schema_and_wdt"
            edges.append(
                make_edge(
                    "schema",
                    src_term,
                    "wdt",
                    wdt_term,
                    scope,
                    comment=comment,
                    provenance=CSV_WDT.name,
                )
            )
            edges.append(
                make_edge(
                    "wdt",
                    wdt_term,
                    "schema",
                    src_term,
                    scope,
                    comment=comment,
                    provenance=CSV_WDT.name,
                    is_reverse=True,
                )
            )
        else:
            edges.append(
                make_edge(
                    "wdt",
                    wdt_term,
                    None,
                    None,
                    "wdt_only",
                    comment=comment,
                    provenance=CSV_WDT.name,
                )
            )

    return edges


# =====================================================
# CFF SCHEMA ENRICHMENT / VALIDATION
# =====================================================


def load_cff_schema_terms() -> Set[str]:
    """
    Extract top-level property names from a CFF JSON schema.
    We treat these as valid CFF keys (cff:<key>).
    """
    if not CFF_SCHEMA_JSON.exists():
        print(
            f"[INFO] No CFF schema JSON found (skipped enrichment): {CFF_SCHEMA_JSON.name}"
        )
        return set()

    try:
        schema = json.loads(CFF_SCHEMA_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] Failed to parse CFF schema JSON ({CFF_SCHEMA_JSON.name}): {e}")
        return set()

    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return set()

    return {ensure_curie("cff", k) for k in props.keys() if k}


def enrich_with_cff_only_terms(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Add cff-only edges for any CFF schema terms not already present in the graph.
    Warn if mapped CFF terms are not present in the schema.
    """
    cff_schema_terms = load_cff_schema_terms()
    if not cff_schema_terms:
        return edges

    mentioned: Set[str] = set()
    mentioned_cff: Set[str] = set()

    for e in edges:
        ft = e.get("from_term")
        tt = e.get("to_term")
        if isinstance(ft, str):
            mentioned.add(ft)
            if e.get("from_namespace") == "cff":
                mentioned_cff.add(ft)
        if isinstance(tt, str):
            mentioned.add(tt)
            if e.get("to_namespace") == "cff":
                mentioned_cff.add(tt)

    not_in_schema = sorted([t for t in mentioned_cff if t not in cff_schema_terms])
    if not_in_schema:
        print("[WARN] CFF terms used in mappings but NOT found in CFF schema JSON:")
        for t in not_in_schema[:30]:
            print(f"  - {t}")
        if len(not_in_schema) > 30:
            print(f"  ... (+{len(not_in_schema)-30} more)")

    added = 0
    for t in sorted(cff_schema_terms):
        if t not in mentioned:
            edges.append(
                make_edge(
                    "cff",
                    t,
                    None,
                    None,
                    "cff_only",
                    comment="From CFF schema JSON (enrichment)",
                    provenance=CFF_SCHEMA_JSON.name,
                )
            )
            added += 1

    print(
        f"[INFO] CFF schema enrichment: added {added} cff-only terms from {CFF_SCHEMA_JSON.name}"
    )
    return edges


# =====================================================
# GRAPH + STATS
# =====================================================

NAMESPACES = ["schema", "codemeta", "cff", "wdt"]


def build_adjacency(
    edges: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """
    Build adjacency list keyed by (from_namespace, from_term) -> list of (to_namespace, to_term).
    Only includes edges with a real target (non-null).
    """
    adj: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
    for e in edges:
        fns = e.get("from_namespace")
        fterm = e.get("from_term")
        tns = e.get("to_namespace")
        tterm = e.get("to_term")
        if (
            isinstance(fns, str)
            and isinstance(fterm, str)
            and isinstance(tns, str)
            and isinstance(tterm, str)
        ):
            adj[(fns, fterm)].append((tns, tterm))
    return adj


def print_stats(edges: List[Dict[str, Any]]) -> None:
    """
    Console stats:
      - unique terms per namespace
      - outgoing crosswalkable terms per namespace
      - pair stats from -> to
    """
    terms_by_ns: Dict[str, Set[str]] = {ns: set() for ns in NAMESPACES}
    outgoing_crosswalkable: Dict[str, Set[str]] = {ns: set() for ns in NAMESPACES}

    pair_edges = defaultdict(int)
    pair_from_terms = defaultdict(set)

    for e in edges:
        fns = e.get("from_namespace")
        fterm = e.get("from_term")
        tns = e.get("to_namespace")
        tterm = e.get("to_term")

        if isinstance(fns, str) and isinstance(fterm, str) and fns in terms_by_ns:
            terms_by_ns[fns].add(fterm)

        if isinstance(tns, str) and isinstance(tterm, str) and tns in terms_by_ns:
            terms_by_ns[tns].add(tterm)

        if (
            isinstance(fns, str)
            and isinstance(fterm, str)
            and isinstance(tns, str)
            and isinstance(tterm, str)
            and fns in NAMESPACES
            and tns in NAMESPACES
            and fns != tns
        ):
            outgoing_crosswalkable[fns].add(fterm)
            pair_edges[(fns, tns)] += 1
            pair_from_terms[(fns, tns)].add(fterm)

    print("\n=== Crosswalk Stats (unique terms + outgoing crosswalkable) ===")
    for ns in NAMESPACES:
        total = len(terms_by_ns[ns])
        cw = len(outgoing_crosswalkable[ns])
        label = "wdt (Wikidata)" if ns == "wdt" else ns
        print(f"- {label:14s}: terms={total:4d} | crosswalkable(outgoing)={cw:4d}")

    print("\n=== Crosswalkability by pair (from -> to) ===")
    for fns, tns in sorted(pair_edges.keys()):
        lf = "wdt" if fns != "wdt" else "wdt (Wikidata)"
        lt = "wdt" if tns != "wdt" else "wdt (Wikidata)"
        print(
            f"- {lf:14s} -> {lt:14s}: edges={pair_edges[(fns, tns)]:4d} | unique-from-terms={len(pair_from_terms[(fns, tns)]):4d}"
        )
    print("")


# =====================================================
# RESOLUTION (ANY start term -> preferred target namespaces)
# =====================================================


def resolve_term_preferred(
    edges: List[Dict[str, Any]],
    from_term: str,
    from_namespace: str,
    preferred_targets: Optional[List[str]] = None,
    max_depth: int = 6,
) -> Dict[str, Any]:
    """
    Resolve a starting term to the best reachable term in preferred target namespaces,
    using BFS over the crosswalk graph.

    Example:
      cff:title -> schema:name -> codemeta:name

    preferred_targets default:
      CodeMeta > schema.org > CFF > wdt
    """
    if preferred_targets is None:
        preferred_targets = ["codemeta", "schema", "cff", "wdt"]

    start_term = (
        ensure_curie(from_namespace, from_term) if ":" not in from_term else from_term
    )

    adj = build_adjacency(edges)

    start = (from_namespace, start_term)
    q = deque([(start, [start])])
    seen = {start}

    best_hit = None  # (rank, path_len, ns, term, path)

    while q:
        (cur_ns, cur_term), path = q.popleft()
        if len(path) > max_depth:
            continue

        if cur_ns in preferred_targets:
            rank = preferred_targets.index(cur_ns)
            cand = (rank, len(path), cur_ns, cur_term, path)
            if best_hit is None or cand < best_hit:
                best_hit = cand

        for nxt_ns, nxt_term in adj.get((cur_ns, cur_term), []):
            nxt = (nxt_ns, nxt_term)
            if nxt in seen:
                continue
            seen.add(nxt)
            q.append((nxt, path + [nxt]))

    if best_hit is None:
        return {
            "start": {"namespace": from_namespace, "term": start_term},
            "resolved": None,
            "path": [{"namespace": from_namespace, "term": start_term}],
            "note": "No reachable term in preferred target namespaces",
        }

    _, _, rns, rterm, rpath = best_hit
    return {
        "start": {"namespace": from_namespace, "term": start_term},
        "resolved": {"namespace": rns, "term": rterm},
        "path": [{"namespace": ns, "term": t} for (ns, t) in rpath],
        "note": f"Resolved via BFS (preferred order: {preferred_targets})",
    }


# =====================================================
# CFF YAML -> RDF(TTL) reference implementation
# =====================================================


def curie_to_rdflib_qname(curie: str) -> Tuple[str, str]:
    """
    'schema:name' -> ('schema', 'name')
    also supports 'wdt:P123' etc.
    """
    if ":" not in curie:
        raise ValueError(f"Not a CURIE: {curie}")
    pfx, local = curie.split(":", 1)
    return pfx, local


def generate_rdf_ttl_from_cff_dict(
    edges: List[Dict[str, Any]],
    cff_data: Dict[str, Any],
    out_ttl: Path,
    *,
    subject_iri: str = f"{FDO_BASE}metadata",
    preferred_targets: Optional[List[str]] = None,
    source_info: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Same as before, but consumes an already-loaded dict (from any source).
    """
    if preferred_targets is None:
        preferred_targets = ["codemeta", "schema", "cff", "wdt"]

    try:
        from rdflib import Graph, Literal, Namespace, URIRef, BNode  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "rdflib is required to generate TTL. Install: pip install rdflib"
        ) from e

    g = Graph()

    # Bind prefixes
    g.bind("schema", Namespace("https://schema.org/"))
    g.bind("codemeta", Namespace("https://codemeta.github.io/terms/"))
    g.bind(
        "cff", Namespace(FDO_BASE + "cff/")
    )  # internal fallback namespace for CFF-only predicates
    g.bind("wdt", Namespace("http://www.wikidata.org/prop/direct/"))
    g.bind(FDO_PREFIX, Namespace(FDO_BASE))

    subj = URIRef(subject_iri)

    def pred_uri_from_curie(curie: str):
        pfx, local = curie_to_rdflib_qname(curie)
        if pfx == "schema":
            return URIRef(f"https://schema.org/{local}")
        if pfx == "codemeta":
            return URIRef(f"https://codemeta.github.io/terms/{local}")
        if pfx == "wdt":
            return URIRef(f"http://www.wikidata.org/prop/direct/{local}")
        if pfx == "cff":
            # keep CFF-only predicates in internal namespace
            return URIRef(f"{FDO_BASE}cff/{local}")
        return URIRef(f"{FDO_BASE}unmapped/{pfx}/{local}")

    def add_value(node, pred_curie: str, value: Any):
        pred = pred_uri_from_curie(pred_curie)

        if value is None:
            return

        if isinstance(value, (str, int, float, bool)):
            g.add((node, pred, Literal(value)))
            return

        if isinstance(value, list):
            for item in value:
                add_value(node, pred_curie, item)
            return

        if isinstance(value, dict):
            b = BNode()
            g.add((node, pred, b))
            for k, v in value.items():
                if not isinstance(k, str):
                    continue
                nested_start = f"cff:{k}"
                res = resolve_term_preferred(
                    edges,
                    nested_start,
                    from_namespace="cff",
                    preferred_targets=preferred_targets,
                )
                if res["resolved"] is not None:
                    nested_pred = res["resolved"]["term"]
                else:
                    nested_pred = nested_start  # keep as CFF-only
                add_value(b, nested_pred, v)
            return

        g.add((node, pred, Literal(str(value))))

    # Main mapping loop (top-level keys)
    for key, value in cff_data.items():
        if not isinstance(key, str):
            continue

        start = f"cff:{key}"
        res = resolve_term_preferred(
            edges, start, from_namespace="cff", preferred_targets=preferred_targets
        )

        pred_curie = res["resolved"]["term"] if res["resolved"] is not None else start
        add_value(subj, pred_curie, value)

    out_ttl.write_text(g.serialize(format="turtle"), encoding="utf-8")

    print(f"[INFO] Wrote TTL: {out_ttl.resolve()}")
    print(f"[INFO] subject: {subject_iri}")
    print(f"[INFO] Preferred targets: {preferred_targets}")
    if source_info:
        print(f"[INFO] CFF source_type: {source_info.get('source_type')}")
        print(f"[INFO] CFF source: {source_info.get('source')}")
        if source_info.get("used_member_in_zip"):
            print(f"[INFO] ZIP member: {source_info.get('used_member_in_zip')}")


def generate_rdf_ttl_from_cff_source(
    edges: List[Dict[str, Any]],
    cff_source: str,
    out_ttl: Path,
    *,
    cff_member_in_zip: Optional[str] = None,
    subject_iri: str = f"{FDO_BASE}metadata",
    preferred_targets: Optional[List[str]] = None,
    timeout: int = 30,
) -> None:
    """
    Convenience wrapper:
      - load CFF from source (local/url/zip)
      - generate TTL
    """
    cff_data, info = load_cff_yaml_from_source(
        cff_source, cff_member_in_zip=cff_member_in_zip, timeout=timeout
    )
    generate_rdf_ttl_from_cff_dict(
        edges,
        cff_data,
        out_ttl,
        subject_iri=subject_iri,
        preferred_targets=preferred_targets,
        source_info=info,
    )


# =====================================================
# DEMO
# =====================================================


def demo_resolution(edges: List[Dict[str, Any]]) -> None:
    demo_terms = [
        "cff:title",
        "cff:authors",
        "cff:preferred-citation",
        "cff:repository-code",
        "cff:version",
    ]
    print(
        "=== Demo: resolve CFF term to preferred targets (codemeta > schema > cff > wdt) ==="
    )
    for t in demo_terms:
        res = resolve_term_preferred(edges, t, from_namespace="cff")
        resolved = res["resolved"]
        if resolved:
            print(f"- {t:28s} -> {resolved['namespace']:7s} {resolved['term']}")
        else:
            print(f"- {t:28s} -> (no mapping found)")
    print("")


# =====================================================
# MAIN
# =====================================================


def main() -> None:
    edges: List[Dict[str, Any]] = []
    edges.extend(parse_schema_codemeta())
    edges.extend(parse_cwreference_cff())
    edges.extend(parse_cwreference_wdt())

    # enrich with CFF schema terms (cff-only)
    edges = enrich_with_cff_only_terms(edges)

    # De-duplicate by id (deterministic)
    by_id: Dict[str, Dict[str, Any]] = {}
    for e in edges:
        by_id[e["id"]] = e
    edges = list(by_id.values())

    # Stable ordering (diff-friendly)
    edges.sort(
        key=lambda e: (
            e.get("from_namespace") or "",
            e.get("from_term") or "",
            e.get("to_namespace") or "",
            e.get("to_term") or "",
            e.get("mapping_scope") or "",
            str(e.get("is_reverse")),
        )
    )

    # Write outputs
    write_json(OUT_JSON, edges)
    write_yaml(OUT_YAML, edges)

    print("=== Build complete ===")
    print(f"- JSON: {OUT_JSON.resolve()}")
    print(f"- YAML: {OUT_YAML.resolve()}")
    print(f"- edges: {len(edges)}")

    print_stats(edges)
    demo_resolution(edges)

    # Optional TTL generation (if configured)
    if CFF_SOURCE:
        try:
            generate_rdf_ttl_from_cff_source(
                edges,
                CFF_SOURCE,
                OUT_TTL,
                cff_member_in_zip=CFF_IN_ZIP,
                subject_iri=f"{FDO_BASE}metadata",
                preferred_targets=["codemeta", "schema", "cff", "wdt"],
                timeout=30,
            )
        except Exception as e:
            print(f"[WARN] CFF->TTL generation failed: {e}")
    else:
        print("[INFO] CFF_SOURCE not set. Skipping TTL generation.")


if __name__ == "__main__":
    main()
