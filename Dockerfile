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

RUN mkdir -p /app/workspace /app/audit /app/data && chown -R fleet:fleet /app
USER fleet

EXPOSE 8080

# Default: the API server. The sweep job overrides this with `--command`.
CMD ["sh", "-c", "adk api_server --host 0.0.0.0 --port ${PORT:-8080} /app/agents"]
