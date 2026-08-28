# One image, two jobs: the ADK API server (a live URL) and the unattended sweep
# (a Cloud Run Job on a schedule). They share an image deliberately - the agent
# that answers an operator at 14:00 and the one that sweeps at 06:00 must be the
# same fleet, with the same gate, or the governance claim is only true for one.
FROM python:3.11-slim

WORKDIR /app

RUN adduser --disabled-password --gecos "" fleet
ENV PATH="/home/fleet/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FREIGHT_FIXTURES=/app/fixtures \
    FREIGHT_WORKSPACE_ROOT=/app/workspace \
    FREIGHT_LEDGER_PATH=/app/audit/ledger.jsonl \
    FREIGHT_APPROVALS_PATH=/app/data/approvals.json \
    FREIGHT_MODEL=gemini-3.7-flash

# Dependencies first: this layer is cached across code edits.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Content the fleet reads. eval/ is NOT copied - the answer keys stay out of
# the image entirely, so a deployed agent cannot recite them even in principle.
COPY fixtures/ ./fixtures/
COPY deploy/agents/ ./agents/

# Seed the workspace at build time: the sweep iterates workspace/shipments/ on
# start and the console's evidence pages read the same documents, so an empty
# workspace makes both dead on arrival. Fixtures hold no answer keys, so
# nothing leaks by baking them in.
COPY scripts/seed_workspace.py ./scripts/seed_workspace.py
RUN python scripts/seed_workspace.py --all --workspace /app/workspace

RUN mkdir -p /app/workspace /app/audit /app/data && chown -R fleet:fleet /app
USER fleet

EXPOSE 8080

# Default: the OPERATOR CONSOLE. A judge visiting the URL should land on the
# Desk, not on an API index — and the console needs no GOOGLE_API_KEY to render,
# so the live URL cannot 500 on credentials or burn quota.
# The sweep Job still overrides this with `--command`; `adk api_server` /
# `adk web` remain the local dev entry point for talking to the fleet.
CMD ["sh", "-c", "uvicorn freight_fleet.console:app --host 0.0.0.0 --port ${PORT:-8080}"]
