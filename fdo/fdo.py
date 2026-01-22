#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import io
import mimetypes
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable
from urllib.parse import quote

import requests
from rdflib import Graph, Namespace, URIRef, BNode, Literal
from rdflib.namespace import RDF, DCTERMS, XSD

try:
    import yaml  # PyYAML
except Exception as e:
    raise ImportError("PyYAML is required (pip install pyyaml).") from e


# ------------------------------------------------------------
# Repo layout (as per your screenshot)
# fdo.py and metadata_ingest.py live in: fdo/
# classification_rules.yaml also in: fdo/
# crosswalk YAML lives in: crosswalk/
# ------------------------------------------------------------
THIS_DIR = Path(__file__).resolve().parent  # .../fdo/
REPO_ROOT = THIS_DIR.parent  # .../fdo-squirrel/
CROSSWALK_DIR = REPO_ROOT / "crosswalk"

if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

try:
    import metadata_ingest  # type: ignore
except Exception as e:
    raise ImportError(
        "Could not import metadata_ingest.py. Expected it in the same folder as fdo.py (fdo/)."
    ) from e


# ------------------------------------------------------------
# CONFIG (edit here in VS Code)
# ------------------------------------------------------------
INPUT_ZIP: str = r"C:\git\fdo-squirrel\example_fdo.zip"
OUTPUT_TTL: str = str(THIS_DIR / "fdo-metadata.ttl")
RULES_YAML_FILENAME: str = "classification_rules.yaml"

# crosswalk produced by your crosswalk builder
CROSSWALK_YAML: str = str(CROSSWALK_DIR / "crosswalk.fdo-metadata.yaml")

# If True: do not emit MD.cff / CITATION.cff as Distributions
SKIP_METADATA_FILES_AS_DISTRIBUTIONS: bool = False

# Offline-safe base for identifiers of ZIP contents + distributions
URN_BASE: str = "urn:fdo-squirrel:"

# OPTIONAL: if later you have a real public resolver/endpoint, set it
# so file "data/a.txt" => https://.../data%2Fa.txt and it will ALSO emit dcat:downloadURL
PUBLIC_CONTENT_BASE_URL: Optional[str] = None


# ------------------------------------------------------------
# Namespaces (ontology terms only)
# ------------------------------------------------------------
FDO = Namespace("https://w3id.org/fdo-squirrel/")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
SCHEMA = Namespace("https://schema.org/")
CODEMETA = Namespace("https://codemeta.github.io/terms/")
WD = Namespace("http://www.wikidata.org/entity/")
WDT = Namespace("http://www.wikidata.org/prop/direct/")
# (Optional) if you later want raw CFF predicates:
CFFNS = Namespace("urn:cff:")


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def read_bytes_from_source(source: str) -> bytes:
    if not source:
        raise ValueError("INPUT_ZIP is empty. Please set INPUT_ZIP at top of fdo.py.")
    if is_url(source):
        r = requests.get(source)
        r.raise_for_status()
        return r.content
    p = Path(source)
    if not p.exists():
        raise FileNotFoundError(f"ZIP not found: {p}")
    return p.read_bytes()


def open_zip_from_bytes(zip_bytes: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise zipfile.BadZipFile("Input is not a valid ZIP archive.") from e


def sha256_hex(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def guess_mime(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"


def safe_int_literal(n: Optional[int]) -> Optional[Literal]:
    if n is None:
        return None
    try:
        return Literal(int(n), datatype=XSD.integer)
    except Exception:
        return None


def spdx_to_uri(spdx_id: str) -> URIRef:
    return URIRef(f"https://spdx.org/licenses/{spdx_id}.html")


def normalize_fdo_class(md_raw: Dict[str, Any]) -> URIRef:
    """
    User requirement:
      - Use exactly fdo:3DDataFDO for 3D Data
      - Keep all three classes
    """
    fc = (md_raw.get("fdo_class") or "").strip()
    if not fc:
        return FDO.FDO

    if fc.startswith("fdo:"):
        return URIRef(str(FDO) + fc.split(":", 1)[1])

    fc_lower = fc.lower()
    if fc_lower == "software":
        return FDO.SoftwareFDO
    if fc_lower == "analysis":
        return FDO.AnalysisFDO
    if fc_lower in {"3d data", "3ddata", "3d_data", "3d-data"}:
        return URIRef(str(FDO) + "3DDataFDO")

    token = fc.replace(" ", "").replace("-", "").replace("_", "")
    return URIRef(str(FDO) + token + "FDO")


def resolve_rules_path() -> Path:
    p = THIS_DIR / RULES_YAML_FILENAME
    if p.exists():
        return p
    raise FileNotFoundError(f"Classification rules YAML not found. Expected at: {p}")


# ------------------------------------------------------------
# Crosswalk-driven resolver (CFF key -> preferred RDF predicate)
# ------------------------------------------------------------
def _split_or_terms(term: str) -> List[str]:
    # handle strings like "license or license-url"
    if " or " in term:
        return [t.strip() for t in term.split(" or ") if t.strip()]
    return [term.strip()]


class CrosswalkResolver:
    """
    Uses your unified crosswalk YAML to resolve:
      - CFF key -> schema term (if available)
      - schema term -> codemeta term (preferred)
    fallback strategy:
      codemeta > schema > (cff predicate fallback) > fdo:<key> (last resort)
    """

    def __init__(self, crosswalk_yaml_path: str):
        self.path = Path(crosswalk_yaml_path)
        if not self.path.exists():
            raise FileNotFoundError(f"Crosswalk YAML not found: {self.path}")

        data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Crosswalk YAML must be a list of mapping entries.")

        self.entries: List[Dict[str, Any]] = [e for e in data if isinstance(e, dict)]
        self._index()

    def _index(self) -> None:
        # Index 1: schema source -> codemeta target(s)
        self.schema_to_codemeta: Dict[str, List[str]] = {}
        # Index 2: cff-key -> schema source(s)
        self.cffkey_to_schema: Dict[str, List[str]] = {}
        # Index 3: cff-key that is cff_only (no schema mapping)
        self.cff_only_keys: set[str] = set()

        for e in self.entries:
            src = e.get("source_term")  # e.g. "schema:author"
            tgt = e.get("target_term")  # e.g. "codemeta:author" or "authors"
            tns = e.get("target_namespace")

            if (
                src
                and isinstance(src, str)
                and tns == "codemeta"
                and tgt
                and isinstance(tgt, str)
            ):
                self.schema_to_codemeta.setdefault(src, []).append(tgt)

            # cff mappings in your crosswalk use target_namespace=cff and target_term like "authors"
            if tns == "cff" and tgt and isinstance(tgt, str):
                for alias in _split_or_terms(tgt):
                    if src and isinstance(src, str):
                        self.cffkey_to_schema.setdefault(alias, []).append(src)
                    else:
                        # cff only
                        self.cff_only_keys.add(alias)

        # de-dupe
        for k, v in list(self.schema_to_codemeta.items()):
            self.schema_to_codemeta[k] = sorted(set(v))
        for k, v in list(self.cffkey_to_schema.items()):
            self.cffkey_to_schema[k] = sorted(set(v))

    def _uri_from_prefixed(self, prefixed: str) -> URIRef:
        if prefixed.startswith("codemeta:"):
            return URIRef(str(CODEMETA) + prefixed.split(":", 1)[1])
        if prefixed.startswith("schema:"):
            return URIRef(str(SCHEMA) + prefixed.split(":", 1)[1])
        if prefixed.startswith("wdt:P"):
            return URIRef(str(WDT) + prefixed.split(":", 1)[1])
        if prefixed.startswith("wd:Q"):
            return URIRef(str(WD) + prefixed.split(":", 1)[1])
        return URIRef(prefixed)

    def predicate_for_cff_key(self, cff_key: str) -> URIRef:
        """
        Resolve a CFF YAML key (e.g. "keywords", "date-released", "repository-code")
        into a preferred RDF predicate URI.
        """
        # 1) CFF key -> schema term
        schema_terms = self.cffkey_to_schema.get(cff_key, [])

        # 2) For each schema term, check if codemeta mapping exists; prefer codemeta
        for st in schema_terms:
            cm_terms = self.schema_to_codemeta.get(st, [])
            if cm_terms:
                return self._uri_from_prefixed(cm_terms[0])  # deterministic first
            # else: schema itself
            return self._uri_from_prefixed(st)

        # 3) If CFF key is known cff_only, fallback to a CFF namespace predicate
        if cff_key in self.cff_only_keys:
            # keep it still queryable, but clearly "input-native"
            return URIRef(str(CFFNS) + quote(cff_key))

        # 4) Last resort: fdo:<key>
        safe = cff_key.replace("-", "_")
        return URIRef(str(FDO) + safe)


# ------------------------------------------------------------
# Classification rule engine
# ------------------------------------------------------------
@dataclass
class FileInfo:
    path: str
    data: bytes
    mime_type: str
    sha256: str
    byte_size: int
    role: str
    content_uri: URIRef
    distribution_uri: URIRef
    public_download_uri: Optional[URIRef] = None


def _matches_rule(file_path: str, rule_match: Dict[str, Any]) -> bool:
    norm_path = file_path.replace("\\", "/")
    name = Path(norm_path).name
    ext = Path(norm_path).suffix.lower()

    exts = rule_match.get("extension")
    if exts:
        if ext not in [e.lower() for e in exts]:
            return False

    fnames = rule_match.get("filename")
    if fnames:
        if name not in fnames:
            return False

    prefixes = rule_match.get("filename_prefix")
    if prefixes:
        if not any(name.startswith(p) for p in prefixes):
            return False

    pfxs = rule_match.get("path_prefix")
    if pfxs:
        if not any(norm_path.startswith(pfx) for pfx in pfxs):
            return False

    return True


def classify_file_role(
    fdo_class_uri: URIRef, file_path: str, rules: Dict[str, Any]
) -> str:
    fdo_class_key = None
    if str(fdo_class_uri).startswith(str(FDO)):
        local = str(fdo_class_uri).replace(str(FDO), "")
        fdo_class_key = f"fdo:{local}"

    classes = rules.get("fdo_classes") or {}
    cls_block = classes.get(fdo_class_key)
    if not cls_block:
        return "auxiliary"

    default_role = cls_block.get("default_role") or "auxiliary"
    for rule in cls_block.get("rules") or []:
        match = rule.get("match") or {}
        if _matches_rule(file_path, match):
            return rule.get("role") or default_role

    return default_role


# ------------------------------------------------------------
# Minting URIs (offline-safe)
# ------------------------------------------------------------
def mint_dataset_uri(md_raw: Dict[str, Any], fallback_source: str) -> URIRef:
    fdo_id = (md_raw.get("fdo_id") or "").strip()
    if fdo_id:
        if fdo_id.startswith("10."):
            return URIRef(f"https://doi.org/{fdo_id}")
        if fdo_id.startswith(("http://", "https://", "urn:")):
            return URIRef(fdo_id)

    h = hashlib.sha1(fallback_source.encode("utf-8")).hexdigest()[:16]
    return URIRef(f"{URN_BASE}fdo/{h}")


def mint_content_uri(zip_rel_path: str) -> URIRef:
    norm = zip_rel_path.replace("\\", "/")
    return URIRef(f"{URN_BASE}content/{quote(norm)}")


def mint_public_download_uri(zip_rel_path: str) -> Optional[URIRef]:
    if not PUBLIC_CONTENT_BASE_URL:
        return None
    norm = zip_rel_path.replace("\\", "/")
    return URIRef(PUBLIC_CONTENT_BASE_URL.rstrip("/") + "/" + quote(norm))


def mint_distribution_uri(sha256sum: str) -> URIRef:
    return URIRef(f"{URN_BASE}dist/{sha256sum[:16]}")


# ------------------------------------------------------------
# YAML -> RDF helpers (crosswalk-driven)
# ------------------------------------------------------------
def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _looks_like_uri(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://") or s.startswith("urn:")


def _add_literal_or_uri(g: Graph, subj: URIRef, pred: URIRef, value: Any) -> None:
    if value is None:
        return

    if isinstance(value, (int, float)):
        g.add((subj, pred, Literal(value)))
        return

    if isinstance(value, str):
        v = value.strip()
        if not v:
            return
        if _looks_like_uri(v):
            g.add((subj, pred, URIRef(v)))
        else:
            g.add((subj, pred, Literal(v)))
        return

    # fallback: stringify
    g.add((subj, pred, Literal(str(value))))


def _add_date(g: Graph, subj: URIRef, pred: URIRef, value: Any) -> bool:
    if not value:
        return False
    s = str(value).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        g.add((subj, pred, Literal(s, datatype=XSD.date)))
        return True
    return False


def _person_from_cff_dict(g: Graph, person_dict: Dict[str, Any]) -> BNode:
    """
    CFF Person object:
      - given-names
      - family-names
      - orcid
      - email, affiliation, etc. (optional)
    """
    person = BNode()
    g.add((person, RDF.type, SCHEMA.Person))

    gn = person_dict.get("given-names")
    fn = person_dict.get("family-names")
    if gn:
        g.add((person, SCHEMA.givenName, Literal(str(gn))))
    if fn:
        g.add((person, SCHEMA.familyName, Literal(str(fn))))

    orcid = person_dict.get("orcid")
    if orcid and isinstance(orcid, str) and orcid.strip():
        g.add((person, SCHEMA.identifier, URIRef(orcid.strip())))

    email = person_dict.get("email")
    if email:
        g.add((person, SCHEMA.email, Literal(str(email))))

    aff = person_dict.get("affiliation")
    if aff:
        g.add((person, SCHEMA.affiliation, Literal(str(aff))))

    return person


def ingest_cff_like_yaml(
    g: Graph,
    dataset_uri: URIRef,
    raw: Dict[str, Any],
    resolver: CrosswalkResolver,
    *,
    mode: str,
) -> None:
    """
    mode:
      - "md" for MD.cff (FDO-level)  -> mostly dct/dcat (handled by special-case mapping)
      - "citation" for CITATION.cff -> schema/codemeta (crosswalk-driven)
    """

    # Special-case mapping for MD.cff minimal core (stable, explicit)
    md_key_to_pred: Dict[str, URIRef] = {
        "fdo_id": DCTERMS.identifier,
        "title": DCTERMS.title,
        "description": DCTERMS.description,
        "version": DCTERMS.hasVersion,
        "created": DCTERMS.created,
        "issued": DCTERMS.issued,
        "modified": DCTERMS.modified,
        "landing_page": DCAT.landingPage,
        "license": DCTERMS.license,
        # publisher/keywords/spatial/temporal handled separately
    }

    # CITATION.cff keys that we treat as “URI-ish”
    citation_uriish_keys = {
        "repository-code",
        "repository-artifact",
        "url",
        "doi",
    }

    for key, value in raw.items():
        if value is None:
            continue

        # 1) MD.cff: explicit dct/dcat first
        if mode == "md" and key in md_key_to_pred:
            pred = md_key_to_pred[key]
            # dates
            if pred in {DCTERMS.created, DCTERMS.issued, DCTERMS.modified}:
                if _add_date(g, dataset_uri, pred, value):
                    continue
            # license: spdx OR URL
            if key == "license":
                lic_str = str(value).strip()
                if not lic_str:
                    continue
                if _looks_like_uri(lic_str):
                    g.add((dataset_uri, DCTERMS.license, URIRef(lic_str)))
                else:
                    g.add((dataset_uri, DCTERMS.license, spdx_to_uri(lic_str)))
                    g.add((dataset_uri, DCTERMS.license, Literal(lic_str)))
                continue

            _add_literal_or_uri(g, dataset_uri, pred, value)
            continue

        # 2) Special structures for MD.cff
        if mode == "md":
            if key == "publisher":
                for p in _as_list(value):
                    if isinstance(p, dict):
                        pid = p.get("id")
                        plabel = p.get("label")
                        if pid:
                            pub_uri = URIRef(str(pid))
                            g.add((dataset_uri, DCTERMS.publisher, pub_uri))
                            if plabel:
                                g.add((pub_uri, RDF.type, SCHEMA.Organization))
                                g.add((pub_uri, SCHEMA.name, Literal(str(plabel))))
                        elif plabel:
                            g.add(
                                (dataset_uri, DCTERMS.publisher, Literal(str(plabel)))
                            )
                    else:
                        g.add((dataset_uri, DCTERMS.publisher, Literal(str(p))))
                continue

            if key == "keywords":
                for kw in _as_list(value):
                    if isinstance(kw, dict):
                        kid = kw.get("id")
                        klabel = kw.get("label")
                        if kid:
                            g.add((dataset_uri, DCAT.keyword, URIRef(str(kid))))
                        if klabel:
                            g.add((dataset_uri, DCAT.keyword, Literal(str(klabel))))
                    else:
                        g.add((dataset_uri, DCAT.keyword, Literal(str(kw))))
                continue

            if key == "spatial":
                for s in _as_list(value):
                    if isinstance(s, dict):
                        sid = s.get("id")
                        slabel = s.get("label")
                        if sid:
                            g.add((dataset_uri, DCTERMS.spatial, URIRef(str(sid))))
                        if slabel:
                            g.add((dataset_uri, DCTERMS.spatial, Literal(str(slabel))))
                    else:
                        g.add((dataset_uri, DCTERMS.spatial, Literal(str(s))))
                continue

            if key == "temporal":
                for t in _as_list(value):
                    if isinstance(t, dict):
                        tid = t.get("id")
                        tlabel = t.get("label")
                        if tid:
                            g.add((dataset_uri, DCTERMS.temporal, URIRef(str(tid))))
                        if tlabel:
                            g.add((dataset_uri, DCTERMS.temporal, Literal(str(tlabel))))
                    else:
                        g.add((dataset_uri, DCTERMS.temporal, Literal(str(t))))
                continue

            if key == "fdo_class":
                # already used for rdf:type; keep as informational literal as well
                g.add((dataset_uri, FDO.fdo_class, Literal(str(value))))
                continue

            # ignore distributions in MD.cff (per your requirement: fdo.py derives them from ZIP)
            if key == "distributions":
                continue

        # 3) CITATION.cff people lists (special logic)
        if mode == "citation" and key in {"authors", "contact", "contributors"}:
            people = _as_list(value)
            for p in people:
                if not isinstance(p, dict):
                    continue
                person = _person_from_cff_dict(g, p)

                # map key -> predicate via crosswalk
                # authors -> schema:author -> codemeta:author (preferred)
                pred = resolver.predicate_for_cff_key(key)

                # attach person to dataset
                g.add((dataset_uri, pred, person))
            continue

        # 4) Everything else: crosswalk-driven predicate selection
        pred = resolver.predicate_for_cff_key(key)

        # do some helpful typing for CITATION uri-ish keys
        if mode == "citation" and key in citation_uriish_keys:
            # doi -> https://doi.org/<doi>
            if (
                key == "doi"
                and isinstance(value, str)
                and value.strip().startswith("10.")
            ):
                _add_literal_or_uri(
                    g, dataset_uri, pred, f"https://doi.org/{value.strip()}"
                )
            else:
                # url/repository-code are typically URLs
                if isinstance(value, str):
                    v = value.strip()
                    if v:
                        g.add(
                            (
                                dataset_uri,
                                pred,
                                URIRef(v) if _looks_like_uri(v) else Literal(v),
                            )
                        )
            continue

        # handle lists generically
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    # fallback: stringify dict (keeps completeness without inventing structures)
                    _add_literal_or_uri(g, dataset_uri, pred, str(item))
                else:
                    _add_literal_or_uri(g, dataset_uri, pred, item)
            continue

        # dates in CITATION.cff sometimes: "date-released"
        if mode == "citation":
            if key in {"date-released"}:
                if _add_date(g, dataset_uri, pred, value):
                    continue

        _add_literal_or_uri(g, dataset_uri, pred, value)


# ------------------------------------------------------------
# Build FDO graph
# ------------------------------------------------------------
def build_fdo_graph_from_zip(source_zip: str) -> Tuple[Graph, URIRef]:
    zip_bytes = read_bytes_from_source(source_zip)
    zf = open_zip_from_bytes(zip_bytes)

    # Required: MD.cff
    md = metadata_ingest.load_md_cff(source_zip)
    md_raw = md["raw"]

    # Optional: CITATION.cff
    citation_raw: Optional[Dict[str, Any]] = None
    try:
        citation = metadata_ingest.load_citation_cff(source_zip)
        citation_raw = citation["raw"]
    except Exception:
        citation_raw = None

    # Rules
    rules_path = resolve_rules_path()
    rules = metadata_ingest.load_classification_rules(rules_path)

    dataset_uri = mint_dataset_uri(md_raw, fallback_source=source_zip)
    fdo_class_uri = normalize_fdo_class(md_raw)

    # Crosswalk resolver
    resolver = CrosswalkResolver(CROSSWALK_YAML)

    g = Graph()
    g.bind("fdo", FDO)
    g.bind("dcat", DCAT)
    g.bind("dct", DCTERMS)
    g.bind("schema", SCHEMA)
    g.bind("codemeta", CODEMETA)
    g.bind("wd", WD)
    g.bind("wdt", WDT)
    g.bind("cff", CFFNS)

    # Dataset types
    g.add((dataset_uri, RDF.type, DCAT.Dataset))
    g.add((dataset_uri, RDF.type, fdo_class_uri))

    # --- MD.cff ingest (FDO-level) ---
    ingest_cff_like_yaml(g, dataset_uri, md_raw, resolver, mode="md")

    # --- CITATION.cff ingest (software citation enrichment) ---
    if citation_raw:
        ingest_cff_like_yaml(g, dataset_uri, citation_raw, resolver, mode="citation")

    # Distributions derived from ZIP contents
    file_infos: List[FileInfo] = []

    for zi in zf.infolist():
        if zi.is_dir():
            continue

        rel_path = zi.filename.replace("\\", "/")

        if SKIP_METADATA_FILES_AS_DISTRIBUTIONS and Path(rel_path).name in {
            "MD.cff",
            "CITATION.cff",
        }:
            continue

        data = zf.read(rel_path)
        sha = sha256_hex(data)
        size = zi.file_size

        mime = guess_mime(rel_path)
        role = classify_file_role(fdo_class_uri, rel_path, rules)

        content_uri = mint_content_uri(rel_path)
        dist_uri = mint_distribution_uri(sha)
        pub_dl = mint_public_download_uri(rel_path)

        file_infos.append(
            FileInfo(
                path=rel_path,
                data=data,
                mime_type=mime,
                sha256=sha,
                byte_size=size,
                role=role,
                content_uri=content_uri,
                distribution_uri=dist_uri,
                public_download_uri=pub_dl,
            )
        )

    # Point B (agreed): offline content -> accessURL (URN), downloadURL only if real public base exists
    for fi in file_infos:
        g.add((dataset_uri, DCAT.distribution, fi.distribution_uri))
        g.add((fi.distribution_uri, RDF.type, DCAT.Distribution))

        g.add((fi.distribution_uri, DCAT.accessURL, fi.content_uri))  # offline-safe

        if fi.public_download_uri is not None:
            g.add((fi.distribution_uri, DCAT.downloadURL, fi.public_download_uri))

        g.add((fi.distribution_uri, DCAT.mediaType, Literal(fi.mime_type)))

        bs = safe_int_literal(fi.byte_size)
        if bs:
            g.add((fi.distribution_uri, DCAT.byteSize, bs))

        g.add((fi.distribution_uri, FDO.sha256, Literal(fi.sha256)))
        g.add((fi.distribution_uri, FDO.path, Literal(fi.path)))
        g.add((fi.distribution_uri, FDO.role, Literal(fi.role)))

    return g, dataset_uri


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main() -> None:
    # Make sure .cff is detected as YAML (instead of octet-stream)
    mimetypes.add_type("text/yaml", ".cff")
    mimetypes.add_type("text/yaml", ".yaml")
    mimetypes.add_type("text/yaml", ".yml")

    # 3D / geo additions
    mimetypes.add_type("model/obj", ".obj")
    mimetypes.add_type("model/stl", ".stl")
    mimetypes.add_type("model/gltf+json", ".gltf")
    mimetypes.add_type("model/gltf-binary", ".glb")

    g, dataset_uri = build_fdo_graph_from_zip(INPUT_ZIP)

    out_path = Path(OUTPUT_TTL)
    out_path.write_text(g.serialize(format="turtle"), encoding="utf-8")

    print("FDO RDF created")
    print(f"- Dataset: {dataset_uri}")
    print(f"- Input ZIP: {INPUT_ZIP}")
    print(f"- Rules: {(THIS_DIR / RULES_YAML_FILENAME).resolve()}")
    print(f"- Crosswalk: {Path(CROSSWALK_YAML).resolve()}")
    print(f"- Output TTL: {out_path.resolve()}")
    print(f"- Triples: {len(g)}")


if __name__ == "__main__":
    main()
