# UI hand-off

## Readiness

The ingestion layer is ready to support a user interface. It provides a populated
Neo4j graph, a portable canonical JSON graph, stable IDs, point-in-time fields,
source text/locations and provenance on every asserted node and relationship.

The UI is a new read-only product layer. It does not change the locked ingestion
architecture, source scope or parser. In particular, it must not call the
Federal Register API as an alternative source and must not put Aura credentials
in browser code.

## Approved first UI slice

The initial interface should answer source-linked legal-structure questions, not
give migration advice or infer a person's outcome.

1. Search titles, provisions, visa classes, visa subclasses, PICs, SRCs and
   visa conditions.
2. Offer an **as-at date** control and display the version/effective interval
   used for every result.
3. Provide a visa-subclass page. Subclass 102 is the initial demonstration:
   Schedule 1 item 1108 / Child (Migrant) Class AH → Subclass 102 → Schedule 2
   grant provisions, with PIC, provision and instrument links.
4. Provide provision pages showing source text, structural parent, references,
   version links and exact Register provenance.
5. Provide a focused visual relationship view for the selected pathway. Avoid an
   unbounded whole-database graph as the default screen.

## Technical boundary

```text
Browser UI → read-only application/API → Neo4j Aura
                                  └── source links → Federal Register
```

The application/API owns the Neo4j driver and reads credentials from server-side
environment variables. It exposes allow-listed, parameterised queries only.
The browser receives view models and Register source links, never Cypher,
database credentials or raw connection information.

Use the graph identity fields rather than display strings:

- title: Register `title_id`;
- version: title ID + Register version/compilation ID;
- structural entities: their version-scoped canonical `id`;
- as-at results: filter by preserved effective intervals, not by Register
  `isCurrent`/`in force` labels.

## UI data rules

- Display provenance beside legal assertions: source title/version, document,
  location, retrieval time and a link to the Register where available.
- Clearly label unavailable or metadata-only source material.
- Preserve uncertainty: a low-confidence relationship is source-backed parser
  output, not legal advice or an inferred conclusion.
- Do not collapse historical and current provisions into a single mutable record.
- Apply result limits and pagination; graph expansion must be bounded by the
  selected title/version/path.

## Before public release

The ingestion data is ready for development of an internal or local UI. Before
publishing it to external users, decide and document authentication, rate limits,
logging/privacy policy, legal disclaimer, source attribution presentation,
accessibility and an incident/update process. Those are application decisions,
not blockers to starting the UI build.
