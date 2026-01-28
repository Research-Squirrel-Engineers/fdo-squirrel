# FAIR Digital Object Squirrel Implementation (FDOSquirrel)

**v0.1 – Reference implementation (ZIP-based FDO → RDF)**

`fdo-squirrel` is a **reference implementation for modelling FAIR Digital Objects (FDOs)** from self-contained ZIP packages into **machine-readable RDF**.  
It demonstrates a **package-centric, reproducible crosswalk** from community-standard metadata files to interoperable knowledge graph representations.

---

## What it does

Given a **ZIP package as Source of Truth**, `fdo-squirrel`:

- reads **descriptive metadata** from `MD.cff`
- reads **citation metadata** from `CITATION.cff`
- inspects the **ZIP contents** (files, sizes, checksums)
- generates a **single RDF/Turtle representation** of the FDO
- records **provenance** for every mapped field

The result is a **self-describing FDO** that can be ingested into RDF-based infrastructures and knowledge graphs.

### Architecture overview

![ZIP-centric FDO modelling workflow](architecture.png)

---

## Input requirements

The input **must be a ZIP file** containing at least:

### 1. `CITATION.cff`
- Valid according to the **Citation File Format (CFF)** specification  
  https://citation-file-format.github.io/
- Used for:
  - creators / authors
  - citation-related metadata

### 2. `MD.cff`
- Valid according to the **project-specific MD.cff schema**
- Used for:
  - FDO type (`fdo:SoftwareFDO`, `fdo:AnalysisFDO`, `fdo:3DDataFDO`)
  - title, description, version
  - licence, publisher
  - spatial and temporal metadata

### 3. Arbitrary package content
- data, software, models, documentation, etc.
- each file becomes a `dcat:Distribution`
- roles are assigned via rule-based classification

---

## Output

Running the pipeline produces:

- **`fdo-metadata.ttl`**  
  RDF/Turtle representation of the FDO, combining:
  - DCAT
  - FDO vocabulary
  - CIDOC CRM / CRMdig
  - GeoSPARQL (if applicable)

- **`rdf_modelling_report.json`**  
  A machine-readable provenance report documenting:
  - which field came from which source
  - how many triples were generated per mapping step

- **`rdf_modelling_report.html`**  
  Human-readable HTML version of the modelling report

---

## How to run

### Option A – via local config (recommended for development)

Create a local file **`config.local.json`** (not committed):

```json
{
  "package_source": "C:/tmp/fdo/ogham-analysis.zip"
}
```

Then run:

```bash
python main.py
```

---

### Option B – via command line (one-off runs)

```bash
python main.py --package "C:/tmp/fdo/GEARS_1.zip"
```

This overrides any local configuration.

---

## Requirements

- Python ≥ 3.10
- Install dependencies:
  ```bash
  pip install -r requirements.txt
  ```

---

## Status

This is a **v0.1 reference implementation**.

- ✔ stable RDF output
- ✔ valid Turtle
- ✔ deterministic identifiers
- ✔ explicit provenance
- ✔ suitable for documentation and scientific publication

The focus is **correctness, transparency, and explainability**, not performance or completeness.

---

## Why this matters

`fdo-squirrel` shows how **FAIR Digital Objects can be modelled as self-contained, package-based entities**, where:

- metadata and data stay together
- RDF is derived, not manually curated
- provenance is explicit and reproducible

This makes FDOs suitable for **long-term reuse, federation, and knowledge graph integration**.