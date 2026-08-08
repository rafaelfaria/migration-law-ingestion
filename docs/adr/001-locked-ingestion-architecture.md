# ADR-001: Authoritative, reproducible migration-law ingestion

**Status:** Accepted — Phase 1

## Decision

Use the Federal Register of Legislation API as the only Phase 1 source. The
pipeline is:

```text
Federal Register API → immutable filesystem archive → deterministic parser
                     → canonical legal graph → optional Neo4j sink
```

The canonical identifier for a law is the Register `titleId`; a compilation or
other point-in-time view retains its `registerId`, `compilationNumber`, `start`,
`end`, `retrospectiveStart`, and `retrospectiveEnd`. We retain the title and
version Register statuses separately from legal effectiveness.

Raw API JSON and each acquired document are retained with their retrieval time,
request URL, MIME type (where supplied), filename and SHA-256 digest. Binary
objects are content-addressed and never overwritten.

The initial graph vocabulary is:

- Nodes: `LegislationTitle`, `LegislationVersion`, `Schedule`, `Part`,
  `Division`, `Subdivision`, `Provision`, `VisaClass`, `VisaSubclass`, `PIC`,
  `SRC`, `VisaCondition`, `LegislativeInstrument`.
- Relationships: `HAS_VERSION`, `CONTAINS`, `PART_OF`, `HAS_SUBCLASS`,
  `HAS_VALIDITY_PROVISION`, `HAS_GRANT_PROVISION`, `REFERENCES`,
  `REFERENCES_PIC`, `REFERENCES_SRC`, `REFERENCES_CONDITION`, `AUTHORISED_BY`,
  `SPECIFIED_BY`, `AMENDED_BY`, `SUPERSEDED_BY`.

All node/relationship instances must have provenance: `source_title_id`,
`source_version_id`, `source_document`, `source_location`, `source_text`,
`effective_from`, `effective_to`, `retrieved_at`, `source_hash`,
`parser_version`, `extraction_method`, and `confidence`.

## Consequences

- EPUB is the primary parsing source because it provides deterministic structured
  text. PDF is retained for authoritative visual evidence, not parsed in this MVP.
- The parser emits explicit but unresolved legislative-instrument hooks where the
  Regulations say an instrument specifies a form, place, manner or country. Those
  become resolved `LegislativeInstrument` nodes as matching registered instruments
  are ingested.
- The initial instrument catalogue selector is deterministic and conservative:
  current principal Register `LegislativeInstrument` titles whose names begin with
  `Migration`. The Act and Regulations are static roots. This is a transparent
  MVP boundary, not a claim that title text alone proves legal authorisation.
- No LLM participates in acquisition or extraction. Low-confidence or unknown
  citation forms stay as explicit references for later deterministic rules.
- Neo4j is configurable only after canonical output is validated. No alternative
  database, UI, policy, case-law, or guidance work is in scope.
