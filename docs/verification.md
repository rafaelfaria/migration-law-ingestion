# Verification record

## Completed Phase 1 baseline

The full historical and current-source ingestion was live-verified on
2026-08-09 against the Federal Register API and the configured Neo4j Aura
database. Register API requests were sequential with a minimum 0.75-second
interval and retry/backoff for rate limiting and transient failures.

| Check | Result |
| --- | --- |
| Historical authority-qualified source titles archived | 486 |
| Current-source receipt date | 2026-08-09 |
| Current titles in receipt | 144 |
| Neo4j `Entity` nodes | 9,956 |
| Neo4j relationships | 24,768 |
| Expected historical title nodes present | 486 / 486 |
| Nodes without provenance | 0 |
| Relationships without provenance | 0 |
| Subclass 102 validity paths | 1 |
| Subclass 102 grant paths | 23 |
| Automated tests | 17 passed |
| Container configuration | validated |
| Daily operational run | passed; no Register changes detected |

There are additional titled graph entities beyond the 486 source titles because
the graph retains cited and authorising titles discovered in source-backed
relationships. Those are not claimed to enlarge the authoritative ingestion
catalogue.

## Repeatable verification

Run the test suite:

```bash
.venv/bin/python -m pytest -q
```

Validate the Compose configuration:

```bash
docker compose config
```

Run the operational update once:

```bash
.venv/bin/python scripts/daily_ingestion.py \
  --include-instruments --neo4j --request-interval 0.75
```

Then use Aura Query:

```cypher
MATCH (n:Entity) WHERE n.provenance_json IS NULL
RETURN count(n) AS nodes_missing_provenance;

MATCH ()-[r]->() WHERE r.provenance_json IS NULL
RETURN count(r) AS relationships_missing_provenance;

MATCH (v:VisaSubclass {number: '102'})-[:HAS_VALIDITY_PROVISION]->(p:Provision)
RETURN v.number, p.number;

MATCH (v:VisaSubclass {number: '102'})-[:HAS_GRANT_PROVISION]->(p:Provision)
RETURN v.number, count(p) AS grant_provisions;
```

Expected results for the supplied baseline are zero missing-provenance records,
at least one validity result for Subclass 102, and at least one grant result. The
absolute counts will increase as the Register publishes new law.

## Known evidence limitation

Some legacy Register entries have metadata but no downloadable version document.
The pipeline preserves them as provenance-rich metadata-only records; it does not
invent legal text or fabricate parsed provisions. Document-backed versions are
deterministically parsed from the archived EPUB, with PDFs retained where the
Register publishes them.
