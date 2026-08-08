# Operations

## Runtime boundary

The Federal Register remains the authority. This service retains a local,
content-addressed raw archive under `data/raw`, writes a canonical graph export
under `data/`, and upserts the validated graph to Neo4j Aura. The `data` folder
is operational state and must live on persistent storage; it is deliberately not
committed to Git.

The container receives its connection credentials from the local `.env` file.
That file is ignored by Git. No credential belongs in a command line, commit,
log, or scheduler definition.

## First production load

From the repository root:

```bash
docker compose build
docker compose run --rm ingestion python -m migration_law_ingestion.cli ingest-baseline \
  --as-at 2026-08-08 --include-instruments --neo4j --request-interval 0.75
```

The API client is sequential, waits at least the requested interval between
requests, and backs off on 429 and transient server failures. Do not run more
than one ingestion container against the same archive at once.

## Complete historical materialisation

The historical command includes the Act, Regulations, and former as well as
current principal Migration instruments when they are authorised by either root.
It writes a checkpoint after each completed title, so interruption is safe:

```bash
docker compose run --rm ingestion python -m migration_law_ingestion.cli backfill-baseline \
  --neo4j --request-interval 0.75
```

Its checkpoint is `data/backfill/backfill-state.json`. A rerun reuses raw source
objects and starts at the first incomplete title. Remove that state file only
when intentionally requesting a whole-catalogue reprocessing.

## Daily updates

The daily command first compares Register title and selected-version metadata to
the raw archive. If nothing changed it exits without re-downloading or parsing.
If a title changed, it creates a new immutable archive snapshot and upserts the
validated graph:

```bash
docker compose run --rm ingestion python scripts/daily_ingestion.py \
  --include-instruments --neo4j --request-interval 0.75
```

Use a scheduler on the persistent host. A cron template is supplied at
`deploy/cron.daily.example`; substitute the repository's absolute path before
installing it. The scheduler should run once daily and never overlap a historical
backfill.

## Verification

After a load, use Aura Query to run:

```cypher
MATCH (n) RETURN count(n) AS nodes;
MATCH ()-[r]->() RETURN count(r) AS relationships;
MATCH (v:VisaSubclass {number: '102'})-[:HAS_VALIDITY_PROVISION]->(p:Provision)
RETURN v.number, p.number;
```

The ingestion writer gives every canonical node a technical `Entity` label and
an `id` uniqueness constraint. This is only an indexed persistence identity;
the legal labels and relationship types remain the canonical ontology.
