from pathlib import Path

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
PACKAGE_SOURCE = "C:/git/fdo-squirrel/example_fdo.zip"


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

    print(f"✔ RDF written to {output_path}")
    print(f"✔ Package source: {PACKAGE_SOURCE}")


if __name__ == "__main__":
    main()
