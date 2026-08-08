# Backup and recovery

## Purpose

The Federal Register remains authoritative, but this project must retain the
exact inputs it used. The local archive is therefore evidence and reproducible
ingestion state, not a cache that may be casually discarded.

Git is not the archive. It tracks code, tests and documentation. The archive is
currently about 139 MB and consists of thousands of binary/API objects; it grows
as new versions are published. Do not add `data/raw`, `data/backfill`, `.env` or
daily logs to ordinary Git. Git LFS is technically possible but is not the
recommended operational store for a growing document archive.

## What to back up

Back up the complete `data/` directory from the persistent runtime host:

| Path | Why it matters |
| --- | --- |
| `data/raw/` | Immutable content-addressed API metadata, EPUBs, PDFs and manifests. This is the evidence needed to reproduce extraction. |
| `data/backfill/` | Historic-title results, source catalogue and resumable checkpoint state. |
| `data/graph.json` | Current canonical graph export. It can be regenerated, but retaining it makes recovery quicker. |
| `data/source-registry.json` | Last successful current-source receipt used by daily change detection. |
| `data/daily-ingestion.log` | Useful operational history; retain according to the chosen log policy. |

Back up `.env` separately in a secrets manager or encrypted password vault. Do
not bundle it with the normal archive backup and never commit it.

## Recommended storage policy

Use encrypted, versioned object storage or a managed backup service. Amazon S3,
Google Cloud Storage and Backblaze B2 are suitable examples; the choice is an
operations decision, not an ingestion architecture change.

Configure the destination with:

1. encryption in transit and at rest;
2. object versioning, so accidental deletion or overwrites can be recovered;
3. a retention/lifecycle policy appropriate to the legal-research record;
4. access restricted to the runtime and recovery operators; and
5. a regular restore test, not merely a successful upload report.

Run an initial full backup before enabling unattended daily ingestion, then back
up changed data after each successful daily run. A nightly filesystem snapshot is
also acceptable when it protects the whole `data/` directory and is copied off
the runtime machine.

## Integrity check before and after backup

The raw archive uses SHA-256 content addressing. Preserve directory names and
file bytes exactly. A basic local inventory can be recorded before transfer:

```bash
find data -type f -print0 | sort -z | xargs -0 shasum -a 256 > data-backup-manifest.sha256
```

Store that manifest alongside the backup (not in Git). On restore, generate a
new manifest and compare it to the saved one. Do not rewrite archive files during
validation.

## Recovery procedures

### Restore the ingestion host

1. Restore the repository from Git at the desired released commit.
2. Recreate `.env` from the secrets manager and restrict its file permissions.
3. Restore the complete `data/` directory from the backup, preserving paths.
4. Install dependencies or build the container.
5. Run the test suite from the project environment:
   `.venv/bin/python -m pytest -q`.
6. Run the daily command once manually. If the Register has not changed, it
   should report no changes; if it has, it will append a new archive object and
   upsert Neo4j.
7. Reinstall the once-daily scheduler only after the manual run succeeds.

### Rebuild Neo4j from the retained archive

The portable canonical graph is written before the Neo4j upsert. If Aura data is
lost, first restore `data/`, then run the baseline and historical commands with
`--neo4j` on the recovered host. They merge by stable identity and are safe to
repeat. Do not attempt to rebuild solely from a screenshot or query export when
the raw archive is available.

### Resume interrupted historical ingestion

Do not delete `data/backfill/backfill-state.json`. Re-run the exact
`backfill-baseline` command; completed title IDs are skipped and incomplete work
resumes sequentially. Delete the state file only when intentionally requesting a
full historical reprocessing, after retaining a separate backup.

## Recovery acceptance checks

After any restore, run the tests and the Neo4j verification queries in
[Operations](operations.md). Confirm that Subclass 102 has both a validity path
and grant pathways, that every expected source title is present, and that no
node/relationship lacks provenance. Record the restore date, code commit and
result with the backup operation.
