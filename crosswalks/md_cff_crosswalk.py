from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional


def require_id_label(obj: Dict[str, Any], ctx: str) -> Tuple[str, str]:
    """Enforce {id,label} everywhere."""
    if not isinstance(obj, dict):
        raise ValueError(f"{ctx} must be an object with {{id,label}}, got {type(obj)}")
    if "id" not in obj or "label" not in obj:
        raise ValueError(f"{ctx} must contain keys 'id' and 'label'")
    _id = str(obj["id"]).strip()
    label = str(obj["label"]).strip()
    if not _id or not label:
        raise ValueError(f"{ctx} requires non-empty 'id' and 'label'")
    return _id, label


def _citation_authors_to_idlabel(citation: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Convert CITATION.cff authors to best-effort (id,label).
    If ORCID exists -> use it; else fallback to 'name:<Full Name>' pseudo id.
    """
    authors = citation.get("authors") or []
    out: List[Tuple[str, str]] = []

    for a in authors:
        if not isinstance(a, dict):
            continue

        given = str(a.get("given-names") or "").strip()
        family = str(a.get("family-names") or "").strip()
        name = " ".join([p for p in [given, family] if p]).strip()
        if not name:
            continue

        orcid = str(a.get("orcid") or a.get("ORCID") or "").strip()
        if orcid:
            cid = orcid if orcid.startswith("http") else f"https://orcid.org/{orcid}"
        else:
            cid = f"name:{name}"

        out.append((cid, name))

    return out


@dataclass(frozen=True)
class CrosswalkRecord:
    fdo_type: str
    id: str
    title: str

    publisher_id: str
    publisher_label: str

    creators: List[Tuple[str, str]]

    # optional enrichment
    repository_url: Optional[str] = None


def md_cff_to_crosswalk(
    md: Dict[str, Any], *, citation: Optional[Dict[str, Any]] = None
) -> CrosswalkRecord:
    """
    Crosswalk MD.cff -> internal normalised record.
    Assumes MD.cff already validated by JSON Schema (Step A).
    If citation is provided, enrich optional fields.
    """
    fdo_type = str(md["fdo_type"]).strip()
    rid = str(md["id"]).strip()
    title = str(md["title"]).strip()

    pub_id, pub_label = require_id_label(md["publisher"], "publisher")

    creators_raw = md.get("creators") or []
    creators: List[Tuple[str, str]] = [
        require_id_label(c, f"creators[{i}]") for i, c in enumerate(creators_raw)
    ]

    repo_url: Optional[str] = None
    if citation and isinstance(citation, dict):
        repo_url = citation.get("repository-code") or citation.get("url") or None
        if not creators:
            creators = _citation_authors_to_idlabel(citation)

    return CrosswalkRecord(
        fdo_type=fdo_type,
        id=rid,
        title=title,
        publisher_id=pub_id,
        publisher_label=pub_label,
        creators=creators,
        repository_url=str(repo_url).strip() if repo_url else None,
    )
