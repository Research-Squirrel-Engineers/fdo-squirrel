from pathlib import Path
import json
import html
from datetime import datetime


from ingest.package_source import load_package_from_source
from ingest import load_md_cff_schema, validate_against_schema
from crosswalks import md_cff_to_crosswalk
from crosswalks.citation_crosswalk_engine import CitationCrosswalkEngine
from fdo import crosswalk_to_rdf_turtle


# --------------------------------------------------
# HARDCODED FDO PACKAGE (VARIANT C)
# --------------------------------------------------
# This can be a DOI, landing page, or direct ZIP URL.
# Example (DOI):
# PACKAGE_SOURCE = "https://doi.org/10.1234/fdo.demo.3d.001"
# Example (direct ZIP):
# PACKAGE_SOURCE = "C:/git/fdo-squirrel/example_fdo.zip" #example

# PACKAGE_SOURCE = "C:/tmp/fdo/o3d-epidoc-extractor.zip"
PACKAGE_SOURCE = "C:/tmp/fdo/ogham-analysis.zip"
# PACKAGE_SOURCE = "C:/tmp/fdo/CHUIS_1.zip"
# PACKAGE_SOURCE = "C:/tmp/fdo/GEARS_1.zip"


def _normalise_report(report: dict) -> list[dict]:
    """
    Returns a list of rows with keys:
      field, sources(list[str]), primary_source(str), count, examples(list)
    Supports both historic and current report shapes.
    """
    rows: list[dict] = []

    def _row(field: str, meta: dict) -> dict:
        sources = meta.get("sources")
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list):
            # Backwards compat: some variants used "source"
            src = meta.get("source", "unknown")
            sources = [src] if src else ["unknown"]
        sources = [str(s) for s in sources if s is not None] or ["unknown"]
        return {
            "field": field,
            "sources": sources,
            "primary_source": sources[0],
            "count": meta.get("count", 0),
            "examples": meta.get("examples", []) or [],
        }

    # Shape: {"summary": {"dataset.temporal": {...}, ...}, ...}
    if isinstance(report.get("summary"), dict):
        for field, meta in report["summary"].items():
            if isinstance(meta, dict):
                rows.append(_row(field, meta))
        return rows

    # Older shape: {"fields": {"dataset.temporal": {...}, ...}, ...}
    if isinstance(report.get("fields"), dict):
        for field, meta in report["fields"].items():
            if isinstance(meta, dict):
                rows.append(_row(field, meta))
        return rows

    return rows

    # Older shape: {"fields": {"dataset.temporal": {...}, ...}, ...}
    if isinstance(report.get("fields"), dict):
        for field, meta in report["fields"].items():
            if not isinstance(meta, dict):
                continue
            rows.append(
                {
                    "field": field,
                    "source": meta.get("source", "unknown"),
                    "count": meta.get("count", 0),
                    "examples": meta.get("examples", []) or [],
                    "detail": meta.get("detail"),
                }
            )
        return rows

    return rows


def write_html_report(json_path: Path, html_path: Path, context: dict):
    """
    Render rdf_modelling_report.json as a standalone HTML report.
    The report is designed to make provenance explicit: which RDF statements came from which inputs.
    """
    if not json_path.exists():
        print(
            f"⚠ No JSON modelling report found at {json_path} – skipping HTML report."
        )
        return

    report = json.loads(json_path.read_text(encoding="utf-8"))
    rows = _normalise_report(report)

    # Group by source for clearer narrative sections
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(str(r.get("primary_source", "unknown")), []).append(r)

    # Stable ordering: MD.cff first, CITATION.cff second, then everything else
    preferred = ["MD.cff", "CITATION.cff", "ZIP", "static", "unknown"]
    sources_sorted = sorted(
        by_source.keys(),
        key=lambda s: (preferred.index(s) if s in preferred else 99, s.lower()),
    )

    package_source = html.escape(str(context.get("package_source", "")))
    generated_at = html.escape(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ"))

    def esc(x: str) -> str:
        return html.escape(x if x is not None else "")

    def render_examples(examples) -> str:
        if not examples:
            return ""
        # show up to 5 examples
        show = examples[:5]
        lis = "".join(f"<li><code>{esc(str(e))}</code></li>" for e in show)
        more = ""
        if len(examples) > 5:
            more = f"<div class='muted'>… +{len(examples)-5} more</div>"
        return f"<ul class='examples'>{lis}</ul>{more}"

    section_help = {
        "MD.cff": "Values derived from <code>MD.cff</code> inside the FDO package. These describe the dataset/software/data object semantically (e.g., title, spatial/temporal extent, methods).",
        "CITATION.cff": "Values derived from <code>CITATION.cff</code> (citation metadata). These are crosswalked into RDF citation-related statements.",
        "ZIP": "Values derived from the ZIP package structure itself (e.g., distributions, paths, checksums), providing packaging and distribution provenance.",
        "static": "Statements added statically by the reference implementation to ensure interoperability profiles (e.g., CRM/CRMdig typing) regardless of input fields.",
        "unknown": "Items where the source could not be determined reliably from the modelling report.",
    }

    # Build HTML
    css = """
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;margin:24px;line-height:1.35}
    h1{font-size:22px;margin:0 0 10px}
    h2{font-size:18px;margin-top:28px;border-top:1px solid #ddd;padding-top:18px}
    .meta{color:#444;margin:8px 0 18px}
    .muted{color:#666;font-size:12px}
    table{border-collapse:collapse;width:100%;margin-top:10px}
    th,td{border:1px solid #ddd;padding:8px;vertical-align:top}
    th{background:#f7f7f7;text-align:left}
    code{background:#f2f2f2;padding:1px 4px;border-radius:4px}
    .examples{margin:6px 0 0 16px;padding:0}
    .examples li{margin:2px 0}
    .search{margin:12px 0}
    input[type="search"]{padding:8px;width:min(640px,100%);border:1px solid #bbb;border-radius:8px}
    """
    js = """
    function filterRows(){
      const q = document.getElementById('q').value.toLowerCase();
      document.querySelectorAll('tbody tr').forEach(tr=>{
        const txt = tr.innerText.toLowerCase();
        tr.style.display = txt.includes(q) ? '' : 'none';
      });
    }
    """

    html_parts = []
    html_parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    html_parts.append(
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    )
    html_parts.append("<title>RDF Modelling Report</title>")
    html_parts.append(f"<style>{css}</style>")
    html_parts.append(f"<script>{js}</script>")
    html_parts.append("</head><body>")
    html_parts.append("<h1>RDF Modelling Report</h1>")
    html_parts.append("<div class='meta'>")
    html_parts.append(
        f"<div><strong>Generated:</strong> <code>{generated_at}</code> (UTC)</div>"
    )
    if package_source:
        html_parts.append(
            f"<div><strong>Package source:</strong> <code>{package_source}</code></div>"
        )
    html_parts.append(
        f"<div class='muted'>This report explains which RDF statements were generated from which input sources (MD.cff, CITATION.cff, ZIP, static rules).</div>"
    )
    html_parts.append("</div>")

    html_parts.append(
        "<div class='search'><input id='q' type='search' placeholder='Filter fields, sources, examples…' oninput='filterRows()'></div>"
    )

    for src_name in sources_sorted:
        rows_src = sorted(by_source[src_name], key=lambda r: r["field"])
        help_txt = section_help.get(src_name, section_help["unknown"])
        html_parts.append(f"<h2>{esc(src_name)}</h2>")
        html_parts.append(f"<div class='muted'>{help_txt}</div>")
        html_parts.append(
            "<table><thead><tr>"
            "<th>Field</th><th>Sources</th><th>Count</th><th>Examples</th>"
            "</tr></thead><tbody>"
        )
        for r in rows_src:
            field = esc(str(r.get("field", "")))
            count = esc(str(r.get("count", 0)))
            examples_html = render_examples(r.get("examples", []))
            html_parts.append(
                f"<tr><td><code>{field}</code></td><td>{count}</td><td>{examples_html}</td></tr>"
            )
        html_parts.append("</tbody></table>")

    html_parts.append("</body></html>")

    html_path.write_text("".join(html_parts), encoding="utf-8")
    print(f"✔ HTML modelling report written to {html_path}")


def main():
    # --------------------------------------------------
    # Project root and output
    # --------------------------------------------------
    project_root = Path(__file__).resolve().parent
    output_dir = project_root / "output"
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / "fdo-metadata.ttl"

    # --------------------------------------------------
    # Load FDO package (ZIP via URL / DOI)
    # --------------------------------------------------
    md, cff, info = load_package_from_source(PACKAGE_SOURCE)

    # Centralised provenance: remember exactly which package was used
    info["package_source"] = PACKAGE_SOURCE

    # --------------------------------------------------
    # Validate MD.cff against schema
    # --------------------------------------------------
    schema = load_md_cff_schema(project_root / "schemas/md_cff/MD.cff-schema.yaml")
    validate_against_schema(md, schema)

    # --------------------------------------------------
    # Crosswalk: MD.cff → internal FDO record
    # --------------------------------------------------
    cw = md_cff_to_crosswalk(md, citation=cff)

    # --------------------------------------------------
    # Crosswalk: CITATION.cff → RDF triples
    # --------------------------------------------------
    engine = CitationCrosswalkEngine(
        project_root / "crosswalks/crosswalk.fdo-metadata.yaml",
        project_root / "crosswalks",
    )
    citation_triples = engine.crosswalk(cff, cw.id)

    # --------------------------------------------------
    # Aggregate RDF (MD.cff + CITATION.cff)
    # --------------------------------------------------
    ttl = crosswalk_to_rdf_turtle(cw, citation_triples, info=info)

    # --------------------------------------------------
    # Write RDF output
    # --------------------------------------------------
    output_path.write_text(ttl, encoding="utf-8")

    # --------------------------------------------------
    # Write HTML modelling report (from rdf_modelling_report.json)
    # --------------------------------------------------
    json_report_path = output_dir / "rdf_modelling_report.json"
    if not json_report_path.exists():
        # Fallback for older runs / alternative locations
        alt = project_root / "rdf_modelling_report.json"
        if alt.exists():
            json_report_path = alt

    write_html_report(json_report_path, output_dir / "rdf_modelling_report.html", info)
    print(f"✔ RDF written to {output_path}")
    print(f"✔ Package source: {PACKAGE_SOURCE}")


if __name__ == "__main__":
    main()
