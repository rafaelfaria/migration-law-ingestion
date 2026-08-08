FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
RUN python -m pip install --no-cache-dir '.[neo4j]'

CMD ["python", "-m", "migration_law_ingestion.cli", "--help"]
