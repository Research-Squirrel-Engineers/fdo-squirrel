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
Optional:
    pip install pyyaml
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
            temp = inner.replace('\\"', '__ESCAPED_QUOTE__')
            if '"' in temp:
                temp = temp.replace('"', '\\"')
                inner = temp.replace('__ESCAPED_QUOTE__', '\\"')
                line = f'{m.group(1)}"{inner}"{m.group(3)}'
        lines.append(line)
    return "".join(lines)


def _normalize_license(value: str) -> str:
    if not value or value == "?":
        return "?"
    s = str(value).strip()
    if s.startswith("https://spdx.org/licenses/") or s.startswith("http://spdx.org/licenses/"):
        s = s.rstrip("/").split("/")[-1]
        s = re.sub(r"\.html?$", "", s, flags=re.IGNORECASE)
    if s in {"CC-BY-4.0", "CC_BY_4.0", "CC-BY-4.0.html"}:
        return "CC-BY-4.0"
    return s


def _append_unique(items: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in items:
        items.append(value)


def _extract_from_ttl(ttl_path: Path, meta: FDOMetadata) -> None:
    raw_ttl = ttl_path.read_text(encoding="utf-8")

    def grab(pattern: str) -> str | None:
        m = re.search(pattern, raw_ttl, re.DOTALL)
        return m.group(1).strip() if m else None

    ds = grab(r'(?m)^(<[^>]+>)\s+a\s+dcat:Dataset')
    if ds:
        meta.doi = ds.strip('<>')

    title = grab(r'dct:title\s+"([^"]+)"')
    if title:
        meta.title = title

    version = grab(r'dct:hasVersion\s+"([^"]+)"')
    if version:
        meta.version = version

    created = grab(r'dct:created\s+"([^"]+)"')
    if created:
        meta.date_created = created

    license_val = grab(r'dct:license\s+<([^>]+)>') or grab(r'dct:license\s+"([^"]+)"')
    if license_val:
        meta.license = _normalize_license(license_val)

    # creators and publishers from IRIs are often resolved better from MD.cff later
    creators = re.findall(r'dct:creator\s+<([^>]+)>', raw_ttl)
    if creators and meta.creator == '?':
        meta.creator = ", ".join(creators)

    publishers = re.findall(r'dct:publisher\s+<([^>]+)>', raw_ttl)
    if publishers and meta.publisher == '?':
        meta.publisher = ", ".join(publishers)

    for kw in re.findall(r'dcat:keyword\s+(.*?);', raw_ttl, re.DOTALL):
        for lit in re.findall(r'"([^"]+)"', kw):
            _append_unique(meta.keywords, lit)

    # distributions
    dist_blocks = re.findall(
        r'<urn:fdo-squirrel:dist/[^>]+>\s+a\s+dcat:Distribution.*?fdo:sha256\s+"[^"]*"\s*\.',
        raw_ttl,
        re.DOTALL,
    )
    EXCLUDE_ROLES = {"data"}
    EXCLUDE_EXTS = {".ttl", ".html", ".json"}
    seen: set[tuple[str, str]] = set()
    for block in dist_blocks:
        path_m = re.search(r'fdo:path\s+"([^"]+)"', block)
        role_m = re.search(r'fdo:role\s+"([^"]+)"', block)
        mime_m = re.search(r'dcat:mediaType\s+"([^"]+)"', block)
        if path_m and role_m and mime_m:
            path, role, mime = path_m.group(1), role_m.group(1), mime_m.group(1)
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if role in EXCLUDE_ROLES or f".{ext}" in EXCLUDE_EXTS:
                continue
            if (role, path) not in seen:
                seen.add((role, path))
                meta.distributions[role].append((path, mime))

def _extract_from_md_cff(md_path: Path, meta: FDOMetadata) -> None:
    try:
        import yaml  # type: ignore
    except Exception:
        return

    try:
        data = yaml.safe_load(md_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return

    if meta.title == "?":
        meta.title = str(data.get("title", "?")).strip() or "?"
    if meta.version == "?":
        meta.version = str(data.get("version", "?")).strip() or "?"
    if meta.date_created == "?":
        meta.date_created = str(data.get("date_created", "?")).strip() or "?"

    if meta.license == "?":
        lic = data.get("license")
        if isinstance(lic, dict):
            meta.license = _normalize_license(str(lic.get("label") or lic.get("id") or "?"))
        elif lic:
            meta.license = _normalize_license(str(lic))

    pubs = data.get("publishers") or []
    labels = [str(p.get("label", "")).strip() for p in pubs if isinstance(p, dict) and p.get("label")]
    labels = [x for x in labels if x]
    if labels:
        meta.publisher = ", ".join(dict.fromkeys(labels))

    creators = data.get("creators") or []
    labels = [str(c.get("label", "")).strip() for c in creators if isinstance(c, dict) and c.get("label")]
    labels = [x for x in labels if x]
    if labels:
        meta.creator = ", ".join(dict.fromkeys(labels))

    if not meta.keywords:
        for kw in data.get("keywords") or []:
            if isinstance(kw, dict) and kw.get("label"):
                _append_unique(meta.keywords, str(kw["label"]))
            elif isinstance(kw, str):
                _append_unique(meta.keywords, kw)

    if meta.technique == "?":
        tech = data.get("technique") or {}
        langs = tech.get("programming_languages") if isinstance(tech, dict) else None
        repo = tech.get("repository") if isinstance(tech, dict) else None
        parts = []
        if isinstance(langs, list) and langs:
            parts.append("Languages: " + ", ".join(str(x) for x in langs))
        if isinstance(repo, dict):
            repo_type = str(repo.get("type", "")).strip()
            status = str(repo.get("development_status", "")).strip()
            repo_bits = [x for x in [repo_type, status] if x]
            if repo_bits:
                parts.append("Repository: " + " / ".join(repo_bits))
        if parts:
            meta.technique = "; ".join(parts)

    spatial = data.get("spatial") or {}
    if isinstance(spatial, dict):
        if meta.spatial_osm == "?" and spatial.get("id"):
            s = str(spatial.get("id", "")).strip()
            m = re.search(r"(relation|node|way)/(\d+)", s)
            meta.spatial_osm = f"OSM {m.group(1)}/{m.group(2)}" if m else s
        if meta.longitude == "?" and spatial.get("lon") is not None:
            meta.longitude = str(spatial.get("lon"))
        if meta.latitude == "?" and spatial.get("lat") is not None:
            meta.latitude = str(spatial.get("lat"))

    temporal = data.get("temporal") or {}
    if isinstance(temporal, dict):
        if meta.temporal_id == "?" and temporal.get("id"):
            tid = str(temporal.get("id", "")).strip()
            m = re.search(r"period/(\w+)", tid)
            meta.temporal_id = f"ChronOntology {m.group(1)}" if m else tid
        if meta.temporal_start == "?" and temporal.get("start") is not None:
            meta.temporal_start = str(temporal.get("start"))
        if meta.temporal_end == "?" and temporal.get("end") is not None:
            meta.temporal_end = str(temporal.get("end"))

    heritage = data.get("heritage_object") or {}
    if isinstance(heritage, dict):
        otype = heritage.get("object_type") or {}
        material = heritage.get("material") or {}
        if meta.object_label == "?" and isinstance(otype, dict) and otype.get("label"):
            meta.object_label = str(otype.get("label"))
        if not meta.object_wikidata and isinstance(otype, dict) and otype.get("id"):
            meta.object_wikidata = str(otype.get("id")).replace("http://www.wikidata.org/entity/", "wd:")
        if meta.material_label == "?" and isinstance(material, dict) and material.get("label"):
            meta.material_label = str(material.get("label"))
        if not meta.material_wikidata and isinstance(material, dict) and material.get("id"):
            meta.material_wikidata = str(material.get("id")).replace("http://www.wikidata.org/entity/", "wd:")


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
            if kw:
                _append_unique(meta.keywords, kw)

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


ROLE_STYLES_3D = {
    "model": ("MODEL", "**fdo:role = model**", "#2b6cb0", "#2c5282"),
    "metadata": ("META", "**fdo:role = metadata**", "#276749", "#22543d"),
    "documentation": ("DOCS", "**fdo:role = documentation**", "#744210", "#5f370e"),
}

ROLE_STYLES_SOFTWARE = {
    "software": ("SOFT", "**fdo:role = software**", "#2b6cb0", "#2c5282"),
    "script": ("SCRIPT", "**fdo:role = script**", "#805ad5", "#6b46c1"),
    "metadata": ("META", "**fdo:role = metadata**", "#276749", "#22543d"),
    "documentation": ("DOCS", "**fdo:role = documentation**", "#744210", "#5f370e"),
    "container": ("CONT", "**fdo:role = container**", "#2c7a7b", "#285e61"),
    "notebook": ("NB", "**fdo:role = notebook**", "#b7791f", "#975a16"),
    "workflow": ("WF", "**fdo:role = workflow**", "#c05621", "#9c4221"),
    "environment": ("ENV", "**fdo:role = environment**", "#718096", "#4a5568"),
}


def _wd(label: str, wdid: str) -> str:
    return f"{label} ({wdid})" if wdid else label


def _is_software_fdo(meta: FDOMetadata) -> bool:
    software_roles = {
        "software",
        "script",
        "container",
        "notebook",
        "workflow",
        "environment",
    }
    roles = set(meta.distributions.keys())
    return bool(roles & software_roles)


def _dedup_files(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(files))


def _render_role_body(role: str, files: list[tuple[str, str]]) -> str:
    files = _dedup_files(files)
    count = len(files)
    unit = "file" if count == 1 else "files"

    if role == "metadata":
        names = "\n    ".join(dict.fromkeys(p for p, _ in files))
        return f"    {names}\n    *({count} {unit})*"

    mimes = list(dict.fromkeys(m for _, m in files))
    primary_mime = mimes[0] if mimes else "?"
    return f"    {primary_mime}\n    *({count} {unit})*"


def generate_mermaid_3d(meta: FDOMetadata) -> str:
    doi = meta.doi.replace("https://doi.org/", "")
    kws = ", ".join(meta.keywords) if meta.keywords else "?"
    total = sum(len(v) for v in meta.distributions.values())

    lines = [
        "flowchart TD",
        f'    FDO["`🗂️ **FDO: {meta.title}**',
        f"    DOI: {doi}",
        f'    {total} files · {meta.license} · v{meta.version}`"]',
        "",
        "    FDO --- PROVMETA",
        "",
        '    PROVMETA["`📋 **Core Metadata**',
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
        f'    ⚙️ Technique: {meta.technique}`"]',
        "",
    ]

    role_order = ["model", "metadata", "documentation"]
    present = [r for r in role_order if r in meta.distributions]

    for role in present:
        lines.append(f"    FDO --> {ROLE_STYLES_3D[role][0]}")
    lines.append("")

    for role in present:
        node_id, label, _, _ = ROLE_STYLES_3D[role]
        body = _render_role_body(role, meta.distributions[role])

        lines += [
            f'    {node_id}["`{label}',
            "    ────────────────",
            f'{body}`"]',
            "",
        ]

    lines += [
        "    style PROVMETA fill:#44337a,color:#fff,stroke:#322659",
        "    style FDO fill:#2d3748,color:#fff,stroke:#4a5568",
    ]
    for role in present:
        node_id, _, fill, stroke = ROLE_STYLES_3D[role]
        lines.append(f"    style {node_id} fill:{fill},color:#fff,stroke:{stroke}")

    return "\n".join(lines) + "\n"


def generate_mermaid_software(meta: FDOMetadata) -> str:
    doi = meta.doi.replace("https://doi.org/", "")
    kws = ", ".join(meta.keywords) if meta.keywords else "?"
    total = sum(len(v) for v in meta.distributions.values())

    lines = [
        "flowchart TD",
        f'    FDO["`🗂️ **FDO: {meta.title}**',
        f"    DOI: {doi}",
        f'    {total} files · {meta.license} · v{meta.version}`"]',
        "",
        "    FDO --- PROVMETA",
        "",
        '    PROVMETA["`📋 **Core Metadata**',
        "    ────────────────────────────",
        f"    🏷️ Keywords: {kws}",
        f"    📅 Created: {meta.date_created}",
        f"    👤 Creator: {meta.creator}",
        f"    🏢 Publisher: {meta.publisher}",
        f'    ⚙️ Technical Stack: {meta.technique}`"]',
        "",
    ]

    role_order = [
        "software",
        "script",
        "metadata",
        "documentation",
        "container",
        "notebook",
        "workflow",
        "environment",
    ]
    present = [r for r in role_order if r in meta.distributions]

    for role in present:
        lines.append(f"    FDO --> {ROLE_STYLES_SOFTWARE[role][0]}")
    lines.append("")

    for role in present:
        node_id, label, _, _ = ROLE_STYLES_SOFTWARE[role]
        body = _render_role_body(role, meta.distributions[role])

        lines += [
            f'    {node_id}["`{label}',
            "    ────────────────",
            f'{body}`"]',
            "",
        ]

    lines += [
        "    style PROVMETA fill:#44337a,color:#fff,stroke:#322659",
        "    style FDO fill:#2d3748,color:#fff,stroke:#4a5568",
    ]
    for role in present:
        node_id, _, fill, stroke = ROLE_STYLES_SOFTWARE[role]
        lines.append(f"    style {node_id} fill:{fill},color:#fff,stroke:{stroke}")

    return "\n".join(lines) + "\n"


def generate_mermaid(meta: FDOMetadata) -> str:
    if _is_software_fdo(meta):
        return generate_mermaid_software(meta)
    return generate_mermaid_3d(meta)


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
            md_path = self.ttl_path.with_name("MD.cff")
            if md_path.exists():
                _extract_from_md_cff(md_path, meta)
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
