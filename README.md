# Australian Migration Law Ingestion MVP

This repository ingests authoritative Commonwealth migration legislation into a
versioned legal graph.  Phase 1 is intentionally narrow:

- Federal Register of Legislation API only;
- Migration Act 1958 (`C1958A00062`), Migration Regulations 1994
  (`F1996B03551`), and registered migration legislative instruments;
- immutable archive of API metadata and EPUB/PDF source files;
- deterministic structure and reference extraction; and
- a canonical graph export, persisted to Neo4j Aura for the operational run.

It does not ingest policy, case law, Departmental guidance, or use an LLM.

## Documentation map

| Document | Purpose |
| --- | --- |
| [Foundation](docs/foundation.md) | Vision, ontology, relationship semantics, source architecture and update strategy. |
| [ADR-001](docs/adr/001-locked-ingestion-architecture.md) | The locked source, identity, provenance and parser decisions. |
| [Operations](docs/operations.md) | Commands, scheduling, verification and incident handling. |
| [Backup and recovery](docs/backup-and-recovery.md) | What to retain, where to retain it, and how to restore a working ingestion state. |
| [Verification record](docs/verification.md) | Baseline acceptance evidence and repeatable checks. |
| [UI hand-off](docs/ui-handoff.md) | The safe read-only boundary and initial product slice for a future interface. |

The source archive and runtime state are intentionally **not** in Git. Git holds
the implementation and documentation; the archive belongs in persistent,
versioned backup storage. See [Backup and recovery](docs/backup-and-recovery.md).

## Quick start

Requires Python 3.11+ and no third-party runtime packages. The repository includes
a project-local `.venv` configured with Python 3.14 on this machine.

```bash
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m migration_law_ingestion.cli ingest-regulations \
  --as-at 2026-08-08 --archive-root data/raw --output data/graph.json
.venv/bin/python -m pytest
```

The command obtains the version current at the requested point in time, archives
the Register title/version metadata and the primary EPUB and PDFs when published,
then writes a JSON graph. Re-running a download with identical bytes reuses the
object; a different byte stream is stored under its different SHA-256 digest.

The API client is deliberately rate-conscious: it makes one request at a time,
waits at least 0.75 seconds between requests by default, and backs off/retries on
429 and transient server errors. Increase the interval with `--request-interval`
for a slower run.

`--skip-pdf` is useful during parser development. The archive is deliberately
separate from the graph so extraction can be reproduced from immutable inputs.

To establish the full Phase 1 baseline, including the Act, Regulations and the
current principal migration instruments found in the Register catalogue:

```bash
.venv/bin/python -m migration_law_ingestion.cli ingest-baseline \
  --as-at 2026-08-08 --include-instruments \
  --archive-root data/raw --output data/graph.json
```

`--instrument-limit 10` makes a bounded development run. The daily scheduler
first calls `check-updates` with the same source flags, then runs
`ingest-baseline` only if the output reports a changed version. Discovery is
limited to current, principal `LegislativeInstrument` titles whose Register name
begins with “Migration”; the static Act and Regulations are always included.

For a scheduler, use the included wrapper (with the same arguments). It exits
without downloading or parsing again when the archived Register metadata is
unchanged:

```bash
.venv/bin/python scripts/daily_ingestion.py --include-instruments
```

Historical backfill is explicit and uses Register version IDs, never a later
compilation substituted for historic text. Start with a bounded run:

```bash
.venv/bin/python -m migration_law_ingestion.cli backfill-title \
  --title-id F1996B03551 --kind Regulations --version-limit 5 \
  --skip-pdf --request-interval 0.75
```

The product vision, ontology, relationship semantics, source architecture and
update strategy are captured in [the foundation document](docs/foundation.md).

## Locked decisions

The system identifies a law by Register **title ID**, not by display name. A graph
version is identified by title ID + Register version ID (compilation/register ID),
with start/end and retrospective start/end preserved. Register `status` is recorded
but is never treated as a substitute for the effective interval. Uncommenced and
unincorporated amendments remain source facts instead of being silently discarded.

Every graph node and relationship carries provenance: source title/version,
source document/location/text, effective interval, retrieval instant, source hash,
parser version, extraction method, and confidence. See
[ADR-001](docs/adr/001-locked-ingestion-architecture.md) for the decision record.

## Current acceptance test

The deterministic parser tests use a small source-faithful Subclass 102 fixture.
They verify the path:

`Schedule 1 item 1108 / Child (Migrant) (Class AH) → Subclass 102 → Schedule 2 grant provisions`

and verify PIC extraction plus the legislative-instrument hooks in the item and
grant text. The live CLI archives and parses the published EPUB; the fixture keeps
the normal test suite fast and offline.

## Layout

- `src/migration_law_ingestion/api.py` — Register API client
- `src/migration_law_ingestion/archive.py` — content-addressed raw archive
- `src/migration_law_ingestion/parser.py` — EPUB structure and reference parser
- `src/migration_law_ingestion/model.py` — portable canonical graph model
- `src/migration_law_ingestion/neo4j_sink.py` — batched Neo4j upsert
- `tests/` — deterministic acceptance and archive tests

Neo4j is an integration boundary rather than the canonical parser output. The
portable JSON graph is written before the Neo4j upsert, so retrieval, archival
and deterministic extraction remain independently reproducible.

## Neo4j persistence

The canonical JSON export is always written first. To also upsert the validated
graph into Neo4j, install the optional dependency and provide connection variables:

```bash
.venv/bin/python -m pip install -e '.[neo4j]'
.venv/bin/python -m migration_law_ingestion.cli ingest-baseline \
  --as-at 2026-08-08 --include-instruments --neo4j
```

Create a private `.env` file in the repository root by copying `.env.example`,
then enter the connection values from Aura. `.env` is ignored by Git and is loaded
only when `--neo4j` is used:

```bash
cp .env.example .env
```

The source archive and parser do not depend on these credentials. The writer uses
allow-listed labels and relationship types, batching up to 500 graph records per
Cypher transaction and storing provenance JSON on every persisted entity.

## Operating the complete pipeline

The current-source baseline is the daily operational graph. Run
`backfill-baseline` once to materialise every historic Register version (including
former authorised principal instruments) and resume safely after any interruption.
For a persistent daily deployment, use the supplied Docker package and scheduler
template. See [operations](docs/operations.md) for the exact commands and storage
requirements.
