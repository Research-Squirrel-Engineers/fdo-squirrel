from pathlib import Path
from ingest.package_source import load_package_from_source, PACKAGE_SOURCE
from ingest import load_md_cff_schema, validate_against_schema
from crosswalks import md_cff_to_crosswalk
from crosswalks.citation_crosswalk_engine import CitationCrosswalkEngine
from fdo import crosswalk_to_rdf_turtle


def main():
    # --------------------------------------------------
    # Project root (absolute, stable)
    # --------------------------------------------------
    project_root = Path(__file__).resolve().parent
    output_dir = project_root / "output"
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / "fdo-metadata.ttl"

    # --------------------------------------------------
    # Load ZIP package (MD.cff + CITATION.cff + files)
    # --------------------------------------------------
    md, cff, info = load_package_from_source(PACKAGE_SOURCE)

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
    # Write RDF to file (ALWAYS deterministic)
    # --------------------------------------------------
    output_path.write_text(ttl, encoding="utf-8")

    print(f"\n✔ RDF written to {output_path}")


if __name__ == "__main__":
    main()
