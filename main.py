from pathlib import Path
import argparse
import json
import html
import re
from datetime import datetime

from ingest.package_source import load_package_from_source
from ingest import load_md_cff_schema, validate_against_schema
from crosswalks import md_cff_to_crosswalk
from crosswalks.citation_crosswalk_engine import CitationCrosswalkEngine
from fdo import crosswalk_to_rdf_turtle, build_generated_distributions_ttl
from fdo_mermaid import FDOMermaidGenerator
from fdo_finalize import render_mermaid_to_jpg, build_finished_bundle

# --------------------------------------------------
# HARDCODED FDO PACKAGE
# --------------------------------------------------
# This can be a DOI, landing page, or direct ZIP URL.

PACKAGE_SOURCE = ""  # Fallback


def resolve_package_source(default_value: str) -> str:
    """
    Resolve package source in this order:
      1) CLI: --package / -p
      2) Local config: config.local.json  →  { "package_source": "path/or/url" }
      3) Fallback: hardcoded PACKAGE_SOURCE constant (leave as "" to force config)

    Raises SystemExit with a clear message if no source could be found.
    """
    parser = argparse.ArgumentParser(description="fdo-squirrel runner")
    parser.add_argument(
        "--package", "-p", default=None, help="Path or URL to FDO ZIP package"
    )
    args, _ = parser.parse_known_args()  # ignore unknown args (e.g. pytest flags)

    if args.package:
        return args.package

    cfg_path = Path(__file__).resolve().parent / "config.local.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            ps = cfg.get("package_source")
            if isinstance(ps, str) and ps.strip():
                print(f"ℹ Using package_source from config.local.json: {ps.strip()}")
                return ps.strip()
        except Exception as e:
            print(f"⚠ Could not read config.local.json: {e}")
    else:
        print(f"ℹ No config.local.json found at {cfg_path}")

    if default_value and default_value.strip():
        return default_value.strip()

    raise SystemExit(
        "❌ No package source configured.\n"
        "   Set it in one of these ways:\n"
        '   1) config.local.json  →  { "package_source": "path/to/package.zip" }\n'
        "   2) CLI argument       →  python main.py --package path/to/package.zip\n"
        '   3) Hardcoded constant →  PACKAGE_SOURCE = "path/to/package.zip" in main.py'
    )


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
    Three sections: MD.cff fields, CITATION.cff fields, ZIP distributions (one row per file).
    """
    if not json_path.exists():
        print(
            f"⚠ No JSON modelling report found at {json_path} – skipping HTML report."
        )
        return

    report = json.loads(json_path.read_text(encoding="utf-8"))
    summary = report.get("summary", {})

    package_source = html.escape(str(context.get("package_source", "")))
    generated_at = html.escape(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ"))

    def esc(x) -> str:
        return html.escape(str(x) if x is not None else "")

    # --------------------------------------------------
    # Classify fields into MD.cff / CITATION.cff sections
    # Skip internal/technical fields and all distribution.* / ZIP sub-fields
    # --------------------------------------------------
    SKIP_FIELDS = {
        "file roles rules",
        "md.raw",
        "citation.raw",
        "zip.source",
        "zip.members",
        "distribution.id",
        "distribution.byteSize",
        "distribution.sha256",
        "distribution.mediaType",
        "distribution.role",
        "dataset.distributions",
        "citation.triples.input",
        "citation.triples.output",
        "citation.triples.used",
        "distributions",
    }

    md_rows, cff_rows = [], []
    for field, meta in summary.items():
        if field in SKIP_FIELDS:
            continue
        sources = meta.get("sources", [])
        if any(s.startswith("ZIP") or s.startswith("classification") for s in sources):
            continue
        if "CITATION.cff" in sources:
            cff_rows.append((field, meta))
        else:
            md_rows.append((field, meta))

    # --------------------------------------------------
    # Render a metadata field table (MD.cff / CITATION.cff)
    # --------------------------------------------------
    def render_meta_table(rows):
        if not rows:
            return "<p class='muted'>No fields recorded.</p>"
        out = ["<table><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>"]
        for field, meta in sorted(rows, key=lambda x: x[0]):
            examples = meta.get("examples", [])
            val = ""
            if examples:
                ex = examples[0]
                if isinstance(ex, dict):
                    # Prefer "value" (actual TTL value) over other keys
                    for k in (
                        "value",
                        "title",
                        "description",
                        "version",
                        "publisher_label",
                        "creator_name",
                        "keyword",
                        "spdx",
                        "sf_type",
                        "id",
                        "label",
                        "license",
                    ):
                        if k in ex and ex[k] not in (None, "", "None"):
                            val = f"<code>{esc(ex[k])}</code>"
                            break
                    if not val:
                        # fallback: skip value_type and predicate (not useful for users)
                        filtered = {
                            k: v
                            for k, v in ex.items()
                            if k not in ("value_type", "predicate", "count", "sf_type")
                        }
                        if filtered:
                            val = f"<code>{esc(str(next(iter(filtered.values()))))}</code>"
                else:
                    val = f"<code>{esc(str(ex))}</code>"
            count = meta.get("count", 0)
            display = val if val else f"<span class='muted'>{count} statement(s)</span>"
            out.append(f"<tr><td><code>{esc(field)}</code></td><td>{display}</td></tr>")
        out.append("</tbody></table>")
        return "".join(out)

    # --------------------------------------------------
    # Render ZIP distributions table (one row per file)
    # --------------------------------------------------
    def render_dist_table(summary):
        dist_entries = summary.get("distributions", {}).get("examples", [])
        total = summary.get("distribution.id", {}).get("count", 0)

        if not dist_entries:
            return f"<p class='muted'>{total} distributions recorded (no file details available).</p>"

        out = [
            "<table><thead><tr>"
            "<th>fdo:path</th><th>fdo:role</th><th>mediaType</th>"
            "<th>byteSize</th><th>sha256 (prefix)</th>"
            "</tr></thead><tbody>"
        ]
        for e in dist_entries:
            if not isinstance(e, dict):
                continue
            path = esc(e.get("path", ""))
            role = esc(e.get("role", ""))
            mt = esc(e.get("mediaType", ""))
            size = (
                esc(e.get("byteSize", ""))
                if e.get("byteSize") is not None
                else "<span class='muted'>—</span>"
            )
            sha_pre = (
                esc(e.get("sha256", ""))
                if e.get("sha256")
                else "<span class='muted'>—</span>"
            )
            out.append(
                f"<tr><td><code>{path}</code></td><td><code>{role}</code></td>"
                f"<td><code>{mt}</code></td><td>{size}</td><td><code>{sha_pre}</code></td></tr>"
            )
        if total > len(dist_entries):
            out.append(
                f"<tr><td colspan='5' class='muted'>… +{total - len(dist_entries)} more files</td></tr>"
            )
        out.append("</tbody></table>")
        return "".join(out)

    # --------------------------------------------------
    # CSS + JS
    # --------------------------------------------------
    css = """
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,sans-serif;margin:24px;line-height:1.4;max-width:1200px}
    h1{font-size:22px;margin:0 0 10px}
    h2{font-size:17px;margin-top:32px;border-top:2px solid #e0e0e0;padding-top:14px;color:#222}
    .meta{color:#555;margin:8px 0 18px;font-size:14px}
    .muted{color:#999;font-size:12px}
    table{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px}
    th,td{border:1px solid #e0e0e0;padding:6px 10px;vertical-align:top}
    th{background:#f5f5f5;font-weight:600;text-align:left}
    tr:hover td{background:#fafafa}
    code{background:#f0f0f0;padding:1px 5px;border-radius:3px;font-size:12px}
    .search{margin:14px 0}
    input[type="search"]{padding:8px 12px;width:min(560px,100%);border:1px solid #ccc;border-radius:8px;font-size:14px}
    """
    js = """
    function filterRows(){
      const q = document.getElementById('q').value.toLowerCase();
      document.querySelectorAll('tbody tr').forEach(tr=>{
        tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';
      });
    }
    """

    # --------------------------------------------------
    # Assemble
    # --------------------------------------------------
    dist_count = summary.get("distribution.id", {}).get("count", 0)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>RDF Modelling Report</title>",
        f"<style>{css}</style><script>{js}</script>",
        "</head><body>",
        "<h1>RDF Modelling Report</h1>",
        "<div class='meta'>",
        f"<div><strong>Generated:</strong> <code>{generated_at}</code> (UTC)</div>",
    ]
    if package_source:
        parts.append(
            f"<div><strong>Package source:</strong> <code>{package_source}</code></div>"
        )
    parts += [
        "<div class='muted'>Provenance: which RDF statements were generated from which input source.</div>",
        "</div>",
        "<div class='search'><input id='q' type='search' placeholder='Filter…' oninput='filterRows()'></div>",
        "<h2>MD.cff</h2>",
        "<div class='muted'>Semantic metadata from <code>MD.cff</code> — title, spatial/temporal extent, heritage object properties, technique.</div>",
        render_meta_table(md_rows),
        "<h2>CITATION.cff</h2>",
        "<div class='muted'>Citation metadata from <code>CITATION.cff</code> — creators, license, keywords.</div>",
        render_meta_table(cff_rows),
        f"<h2>ZIP — Distributions ({dist_count} files)</h2>",
        "<div class='muted'>Each file in the ZIP becomes a <code>dcat:Distribution</code>. All properties below are written into the TTL.</div>",
        render_dist_table(summary),
        "</body></html>",
    ]

    html_path.write_text("".join(parts), encoding="utf-8")
    print(f"✔ HTML modelling report written to {html_path}")


def main():
    # --------------------------------------------------
    # Project root and output
    # --------------------------------------------------
    project_root = Path(__file__).resolve().parent
    output_dir = project_root / "output"
    import shutil

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    output_path = output_dir / "fdo-metadata.ttl"

    # --------------------------------------------------
    # Load FDO package (ZIP via URL / DOI)
    # --------------------------------------------------
    package_source = resolve_package_source(PACKAGE_SOURCE)
    md, cff, info = load_package_from_source(package_source)

    # Centralised provenance: remember exactly which package was used
    info["package_source"] = package_source

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

    html_report_path = output_dir / "rdf_modelling_report.html"
    write_html_report(json_report_path, html_report_path, info)

    # --------------------------------------------------
    # Write Mermaid overview diagram
    # --------------------------------------------------
    mermaid_path = output_dir / "fdo_overview.mermaid"
    try:
        gen = FDOMermaidGenerator(
            ttl_path=output_path,
            html_path=html_report_path,
            md_dict=md,
        )
        gen.save(mermaid_path)
        print(f"✔ Mermaid diagram written to {mermaid_path}")
    except Exception as e:
        print(f"⚠ Mermaid generation skipped: {e}")

    # --------------------------------------------------
    # Render the Mermaid diagram as a high-resolution JPG (best effort -
    # see fdo_finalize.render_mermaid_to_jpg for the Node/Pillow fallback)
    # --------------------------------------------------
    jpg_path = output_dir / "fdo_overview.jpg"
    render_mermaid_to_jpg(mermaid_path, jpg_path)

    # --------------------------------------------------
    # Describe the generated companion files as additional
    # dcat:Distribution entries, so the finished bundle below is fully
    # self-describing (every file that ends up in it is modelled in the
    # RDF, not just the original ZIP's members).
    #
    # Read these files from disk *before* overwriting fdo-metadata.ttl with
    # the appended block below - the ttl's own byteSize/sha256 refer to its
    # pre-append content, since a manifest cannot include a hash of its own
    # final bytes.
    # --------------------------------------------------
    generated_files = [
        p
        for p in (output_path, json_report_path, html_report_path, mermaid_path, jpg_path)
        if p.exists()
    ]
    extra_ttl = build_generated_distributions_ttl(
        f"<{cw.id}>", cw.fdo_type, [(p, None) for p in generated_files]
    )
    if extra_ttl:
        ttl = ttl.rstrip("\n") + "\n\n" + extra_ttl + "\n"
        output_path.write_text(ttl, encoding="utf-8")
        print(
            f"✔ RDF updated with {len(generated_files)} generated-file "
            f"distribution(s)"
        )

    print(f"✔ RDF written to {output_path}")
    print(f"✔ Package source: {package_source}")

    # --------------------------------------------------
    # Bundle the original package + every generated file into one
    # self-contained, ready-to-(re)publish ZIP.
    # --------------------------------------------------
    original_zip = info.get("package_local_path")
    bundle_stem = Path(package_source.rstrip("/")).name
    if bundle_stem.lower().endswith(".zip"):
        bundle_stem = bundle_stem[: -len(".zip")]
    bundle_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", bundle_stem) or "fdo-package"
    bundle_path = output_dir / f"{bundle_stem}-fdo-bundle.zip"

    result = build_finished_bundle(
        original_zip_path=Path(original_zip) if original_zip else None,
        generated_files=generated_files,
        output_zip_path=bundle_path,
    )
    if result:
        print(f"✔ Finished FDO bundle written to {result}")


if __name__ == "__main__":
    main()
