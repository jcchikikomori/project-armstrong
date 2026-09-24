# Runtime needs nothing but the standard library. pytest is installed so the
# same image runs the tests, which keeps `docker compose run` the only way
# anyone has to invoke this.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    # pytest and coverage need a writable home; /app is root-owned.
    COVERAGE_FILE=/tmp/.coverage

RUN pip install --no-cache-dir pytest==8.3.3 pytest-cov==5.0.0

WORKDIR /app
COPY medikeep_import/ ./medikeep_import/
COPY tests/ ./tests/
COPY pyproject.toml ./

# The bind-mounted datasets/ is owned by uid 1000 on the host, matching PUID
# in deploy/.env.
RUN useradd --uid 1000 --create-home importer
USER importer

ENTRYPOINT ["python", "-m", "medikeep_import"]
CMD ["plan"]
