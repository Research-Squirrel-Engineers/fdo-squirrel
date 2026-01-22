from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List, Iterable
import csv
import yaml


def _normalize_citation(cff: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rich normalization of CITATION.cff restoring the original FDO metadata output.
    """
    flat: Dict[str, Any] = {}

    # --- core textual fields ---
    for key in ("abstract", "url", "repository-code", "repository"):
        if key in cff and cff[key]:
            flat[key] = cff[key]

    # --- license ---
    if "license" in cff and cff["license"]:
        flat["license"] = cff["license"]

    # --- keywords ---
    if "keywords" in cff and isinstance(cff["keywords"], list):
        flat["keywords"] = cff["keywords"]

    # --- identifiers (DOI etc.) ---
    for ident in cff.get("identifiers", []):
        if not isinstance(ident, dict):
            continue
        scheme = ident.get("type") or ident.get("scheme")
        value = ident.get("value")
        if scheme and value:
            flat[f"identifier_{scheme.lower()}"] = value

    # --- authors ---
    authors = cff.get("authors", [])
    if isinstance(authors, list):
        orcids = []
        names = []
        for a in authors:
            if not isinstance(a, dict):
                continue

            orcid = a.get("orcid") or a.get("ORCID")
            if orcid:
                if not orcid.startswith("http"):
                    orcid = "https://orcid.org/" + orcid
                orcids.append(orcid)

            name = " ".join(
                p for p in (a.get("given-names"), a.get("family-names")) if p
            )
            if name:
                names.append(name)

        if orcids:
            flat["author_orcid"] = orcids
        if names:
            flat["author_name"] = names

    return flat


class CitationCrosswalkEngine:
    def __init__(self, crosswalk_yaml: Path, crosswalk_dir: Path):
        self.crosswalk_yaml = crosswalk_yaml
        self.crosswalk_dir = crosswalk_dir
        self.rules = self._load_rules()

    # --------------------------------------------------
    # Load YAML rules
    # --------------------------------------------------

    def _load_rules(self) -> List[Dict[str, Any]]:
        cfg = yaml.safe_load(self.crosswalk_yaml.read_text(encoding="utf-8"))

        if isinstance(cfg, list):
            return cfg
        if isinstance(cfg, dict):
            return cfg.get("crosswalks", list(cfg.values()))

        raise ValueError("Invalid crosswalk.fdo-metadata.yaml")

    # --------------------------------------------------
    # Main execution
    # --------------------------------------------------

    def crosswalk(self, citation: Dict[str, Any], subject_uri: str) -> List[str]:
        """
        Execute crosswalks correctly:
        - CSVs are reference/provenance, NOT multiplicators
        - Each semantic triple is emitted exactly once
        """

        triples: set[str] = set()

        # Normalize CFF once
        citation = _normalize_citation(citation)

        for cw in self.rules:
            # ------------------------------------------
            # Determine source field (from_term wins)
            # ------------------------------------------
            from_term = cw.get("from_term")
            if not isinstance(from_term, str) or ":" not in from_term:
                continue

            source_field = from_term.split(":", 1)[1]

            value = citation.get(source_field)
            if not value:
                continue

            # ------------------------------------------
            # Target predicate
            # ------------------------------------------
            to_term = cw.get("to_term")
            if not isinstance(to_term, str) or ":" not in to_term:
                continue

            target_predicate = to_term

            values = value if isinstance(value, list) else [value]

            for v in values:
                if isinstance(v, str) and v.startswith(("http://", "https://")):
                    obj = f"<{v}>"
                else:
                    obj = f'"{v}"'

                triples.add(f"<{subject_uri}> {target_predicate} {obj} .")

        # Deterministic output order
        return sorted(triples)
