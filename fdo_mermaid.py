"""
fdo_mermaid.py
==============
Generate a compact Mermaid flowchart from a FAIR Digital Object (FDO).

Usage as module:
    from fdo_mermaid import FDOMermaidGenerator

    gen = FDOMermaidGenerator(
        ttl_path="output/fdo-metadata.ttl",
        html_path="output/rdf_modelling_report.html",  # optional
    )
    gen.save("output/fdo_overview.mermaid")

Dependencies:
    pip install rdflib beautifulsoup4
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class FDOMetadata:
    doi: str = "?"
    title: str = "?"
    version: str = "?"
    license: str = "?"
    date_created: str = "?"
    creator: str = "?"
    publisher: str = "?"
    object_label: str = "?"
    object_wikidata: str = ""
    material_label: str = "?"
    material_wikidata: str = ""
    keywords: list[str] = field(default_factory=list)
    condition: str = "?"
    urgency: str = "?"
    spatial_osm: str = "?"
    latitude: str = "?"
    longitude: str = "?"
    temporal_id: str = "?"
    temporal_start: str = "?"
    temporal_end: str = "?"
    technique: str = "?"
    distributions: dict[str, list[tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )


def _sanitize_ttl(ttl: str) -> str:
    """Fix unescaped double quotes inside Turtle string literals."""
    lines = []
    for line in ttl.splitlines(keepends=True):
        m = re.match(r'(\s*\S+\s+)"(.*)"(\s*[;,.]?\s*)$', line, re.DOTALL)
        if m:
            inner = m.group(2)
            temp = inner.replace('\\"', "\x00")
            if '"' in temp:
                temp = temp.replace('"', '\\"')
                inner = temp.replace("\x00", '\\"')
                line = f'{m.group(1)}"{inner}"{m.group(3)}'
        lines.append(line)
    return "".join(lines)


def _extract_from_ttl(ttl_path: Path, meta: FDOMetadata) -> None:
    from rdflib import Graph, Namespace, RDF, OWL
    from rdflib.namespace import DCTERMS

    SCHEMA = Namespace("https://schema.org/")
    FDO = Namespace("https://w3id.org/fdo-squirrel/")
    DCAT_NS = Namespace("http://www.w3.org/ns/dcat#")

    g = Graph()
    raw_ttl = ttl_path.read_text(encoding="utf-8")
    try:
        g.parse(data=_sanitize_ttl(raw_ttl), format="turtle")
    except Exception:
        try:
            g.parse(data=raw_ttl, format="turtle")
        except Exception:
            return

    datasets = list(g.subjects(RDF.type, DCAT_NS.Dataset))
    if not datasets:
        return
    ds = datasets[0]

    def _s(val) -> str:
        return str(val).strip() if val is not None else "?"

    meta.doi = _s(ds)
    meta.title = _s(g.value(ds, DCTERMS.title))
    meta.version = _s(g.value(ds, DCTERMS.hasVersion))
    meta.date_created = _s(g.value(ds, DCTERMS.created))
    meta.license = _s(g.value(ds, DCTERMS.license))
    if "CC-BY-4.0" in meta.license or "CC_BY_4.0" in meta.license:
        meta.license = "CC-BY-4.0"

    creator_node = g.value(ds, DCTERMS.creator)
    if creator_node:
        meta.creator = _s(g.value(creator_node, SCHEMA.name))
    publisher_node = g.value(ds, DCTERMS.publisher)
    if publisher_node:
        meta.publisher = _s(g.value(publisher_node, SCHEMA.name))

    obj_type = g.value(ds, DCTERMS.type)
    if obj_type:
        meta.object_wikidata = _s(obj_type).replace(
            "http://www.wikidata.org/entity/", "wd:"
        )
    material = g.value(ds, DCTERMS.subject)
    if material:
        meta.material_wikidata = _s(material).replace(
            "http://www.wikidata.org/entity/", "wd:"
        )

    spatial = g.value(ds, DCTERMS.spatial)
    if spatial:
        s = _s(spatial)
        m = re.search(r"(relation|node|way)/(\d+)", s)
        meta.spatial_osm = f"OSM {m.group(1)}/{m.group(2)}" if m else s
    lat = g.value(ds, SCHEMA.latitude)
    lon = g.value(ds, SCHEMA.longitude)
    if lat:
        meta.latitude = _s(lat)
    if lon:
        meta.longitude = _s(lon)

    temporal_node = g.value(ds, DCTERMS.temporal)
    if temporal_node:
        same_as = g.value(temporal_node, OWL.sameAs)
        if same_as:
            m = re.search(r"period/(\w+)", _s(same_as))
            meta.temporal_id = f"ChronOntology {m.group(1)}" if m else _s(same_as)
        start = g.value(temporal_node, DCAT_NS.startDate)
        end = g.value(temporal_node, DCAT_NS.endDate)
        if start:
            meta.temporal_start = _s(start)
        if end:
            meta.temporal_end = _s(end)

    for prov in g.objects(ds, DCTERMS.provenance):
        m = re.search(r'"software":\s*"([^"]+)"', _s(prov))
        if m:
            meta.technique = m.group(1) + " (Photogrammetry)"
            break

    CONDITION_VALS = {"good", "fair", "poor", "bad", "excellent"}
    URGENCY_VALS = {"low", "medium", "high", "critical"}
    for d in g.objects(ds, DCTERMS.description):
        dl = _s(d).lower()
        if dl in CONDITION_VALS:
            meta.condition = _s(d)
        if dl in URGENCY_VALS:
            meta.urgency = _s(d)

    # Distributions — block-level regex on raw TTL (immune to rdflib double-parsing)
    import re as _re

    dist_blocks = _re.findall(
        r'<urn:fdo-squirrel:dist/[^>]+>\s+a\s+dcat:Distribution.*?fdo:sha256\s+"[^"]*"\s*\.',
        raw_ttl,
        _re.DOTALL,
    )
    # Exclude pipeline output artifacts (not part of the original FDO payload)
    EXCLUDE_ROLES = {"data"}
    EXCLUDE_EXTS = {".ttl", ".html", ".json"}

    seen: set[tuple[str, str]] = set()
    for block in dist_blocks:
        path_m = _re.search(r'fdo:path\s+"([^"]+)"', block)
        role_m = _re.search(r'fdo:role\s+"([^"]+)"', block)
        mime_m = _re.search(r'dcat:mediaType\s+"([^"]+)"', block)
        if path_m and role_m and mime_m:
            path, role, mime = path_m.group(1), role_m.group(1), mime_m.group(1)
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if role in EXCLUDE_ROLES or f".{ext}" in EXCLUDE_EXTS:
                continue
            if (role, path) not in seen:
                seen.add((role, path))
                meta.distributions[role].append((path, mime))


def _extract_from_html(html_path: Path, meta: FDOMetadata) -> None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    rows: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) == 2:
            rows[tds[0].get_text(strip=True)] = tds[1].get_text(strip=True)

    def _get(*keys) -> str:
        for k in keys:
            if k in rows and rows[k] not in ("", "?"):
                return rows[k].strip()
        return "?"

    if meta.title == "?":
        meta.title = _get("dataset.title")
    if meta.version == "?":
        meta.version = _get("dataset.version")
    if meta.date_created == "?":
        meta.date_created = _get("dataset.date_created")
    if meta.creator == "?":
        meta.creator = _get("agent.creator")
    if meta.publisher == "?":
        meta.publisher = _get("dataset.publisher")

    obj_raw = _get("dataset.heritage_object.object_type")
    if obj_raw != "?":
        m = re.search(r"'label':\s*'([^']+)'", obj_raw)
        if m:
            meta.object_label = m.group(1)
        m = re.search(r"'id':\s*'([^']+)'", obj_raw)
        if m:
            meta.object_wikidata = m.group(1).replace(
                "http://www.wikidata.org/entity/", "wd:"
            )

    mat_raw = _get("dataset.heritage_object.material")
    if mat_raw != "?":
        m = re.search(r"'label':\s*'([^']+)'", mat_raw)
        if m:
            meta.material_label = m.group(1)
        m = re.search(r"'id':\s*'([^']+)'", mat_raw)
        if m:
            meta.material_wikidata = m.group(1).replace(
                "http://www.wikidata.org/entity/", "wd:"
            )

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) == 2 and tds[0].get_text(strip=True) == "keywords":
            kw = tds[1].get_text(strip=True).strip('"')
            if kw and kw not in meta.keywords:
                meta.keywords.append(kw)

    if meta.condition == "?":
        meta.condition = _get("dataset.heritage_object.overall_condition")
    if meta.urgency == "?":
        meta.urgency = _get("dataset.heritage_object.conservation_urgency")

    if meta.technique == "?":
        tech_raw = _get("dataset.technique.processing")
        if tech_raw != "?":
            m = re.search(r"'software':\s*'([^']+)'", tech_raw)
            meta.technique = (
                m.group(1) if m else tech_raw.split(",")[0]
            ) + " (Photogrammetry)"

    if meta.doi.startswith("https://doi.org/"):
        meta.doi = meta.doi.replace("https://doi.org/", "")

    # Distributions — only if TTL didn't populate them
    if not meta.distributions:
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            if "fdo:path" in headers and "fdo:role" in headers:
                pi = headers.index("fdo:path")
                ri = headers.index("fdo:role")
                mi = headers.index("mediaType") if "mediaType" in headers else None
                seen: set[tuple[str, str]] = set()
                for tr in table.find_all("tr")[1:]:
                    tds = tr.find_all("td")
                    if len(tds) > max(pi, ri):
                        path = tds[pi].get_text(strip=True)
                        role = tds[ri].get_text(strip=True)
                        mime = tds[mi].get_text(strip=True) if mi is not None else "?"
                        if path and role and (path, role) not in seen:
                            seen.add((path, role))
                            meta.distributions[role].append((path, mime))


ROLE_STYLES = {
    "model": ("MODEL", "**fdo:role = model**", "#2b6cb0", "#2c5282"),
    "metadata": ("META", "**fdo:role = metadata**", "#276749", "#22543d"),
    "documentation": ("DOCS", "**fdo:role = documentation**", "#744210", "#5f370e"),
}


def generate_mermaid(meta: FDOMetadata) -> str:
    def _wd(label: str, wdid: str) -> str:
        return f"{label} ({wdid})" if wdid else label

    doi = meta.doi.replace("https://doi.org/", "")
    kws = ", ".join(meta.keywords) if meta.keywords else "?"
    total = sum(len(v) for v in meta.distributions.values())

    lines = [
        "flowchart TD",
        f'    FDO["🗂️ **FDO: {meta.title}**',
        f"    DOI: {doi}",
        f'    {total} files · {meta.license} · v{meta.version}"]',
        "",
        "    FDO --- PROVMETA",
        "",
        '    PROVMETA["📋 **Core Metadata**',
        "    ────────────────────────────",
        f"    🏛️ Object: {_wd(meta.object_label, meta.object_wikidata)}",
        f"    🪨 Material: {_wd(meta.material_label, meta.material_wikidata)}",
        f"    🏷️ Keywords: {kws}",
        f"    📅 Created: {meta.date_created}",
        f"    👤 Creator: {meta.creator}",
        f"    🏢 Publisher: {meta.publisher}",
        f"    🩺 Condition: {meta.condition} · Urgency: {meta.urgency}",
        f"    📍 Spatial: {meta.spatial_osm}",
        f"    lat: {meta.latitude} · lon: {meta.longitude}",
        f"    🕐 Temporal: {meta.temporal_id}",
        f"    start: {meta.temporal_start} · end: {meta.temporal_end}",
        f'    ⚙️ Technique: {meta.technique}"]',
        "",
    ]

    role_order = ["model", "metadata", "documentation"]
    present = [r for r in role_order if r in meta.distributions]

    for role in present:
        lines.append(f"    FDO --> {ROLE_STYLES[role][0]}")
    lines.append("")

    for role in present:
        node_id, label, _, _ = ROLE_STYLES[role]
        files = list(dict.fromkeys(meta.distributions[role]))
        count = len(files)
        unit = "file" if count == 1 else "files"
        mimes = list(dict.fromkeys(m for _, m in files))
        primary_mime = mimes[0] if mimes else "?"

        if role == "metadata":
            names = "\n    ".join(dict.fromkeys(p for p, _ in files))
            body = f"    {names}\n    *({count} {unit})*"
        else:
            body = f"    {primary_mime}\n    *({count} {unit})*"

        lines += [
            f'    {node_id}["{label}',
            f"    ────────────────",
            f'{body}"]',
            "",
        ]

    lines += [
        "    style PROVMETA fill:#44337a,color:#fff,stroke:#322659",
        "    style FDO fill:#2d3748,color:#fff,stroke:#4a5568",
    ]
    for role in present:
        node_id, _, fill, stroke = ROLE_STYLES[role]
        lines.append(f"    style {node_id} fill:{fill},color:#fff,stroke:{stroke}")

    return "\n".join(lines) + "\n"


class FDOMermaidGenerator:
    def __init__(
        self,
        ttl_path: Optional[str | Path] = None,
        html_path: Optional[str | Path] = None,
    ) -> None:
        if ttl_path is None and html_path is None:
            raise ValueError("Provide at least one of ttl_path or html_path.")
        self.ttl_path = Path(ttl_path) if ttl_path else None
        self.html_path = Path(html_path) if html_path else None
        self._meta: Optional[FDOMetadata] = None

    def extract(self) -> FDOMetadata:
        meta = FDOMetadata()
        if self.ttl_path and self.ttl_path.exists():
            _extract_from_ttl(self.ttl_path, meta)
        if self.html_path and self.html_path.exists():
            _extract_from_html(self.html_path, meta)
        self._meta = meta
        return meta

    def generate(self) -> str:
        if self._meta is None:
            self.extract()
        return generate_mermaid(self._meta)

    def save(self, output_path: str | Path = "fdo_overview.mermaid") -> Path:
        out = Path(output_path)
        out.write_text(self.generate(), encoding="utf-8")
        return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Mermaid flowchart for an FDO."
    )
    parser.add_argument("ttl", nargs="?", help="Path to fdo-metadata.ttl")
    parser.add_argument("html", nargs="?", help="Path to rdf_modelling_report.html")
    parser.add_argument("-o", "--output", default="fdo_overview.mermaid")
    args = parser.parse_args()
    gen = FDOMermaidGenerator(ttl_path=args.ttl, html_path=args.html)
    print(f"✅ Saved: {gen.save(args.output)}")
