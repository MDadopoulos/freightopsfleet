# Deploying to Google Cloud

Two things get deployed, from **one image**:

| What | Cloud Run kind | Why it exists |
|---|---|---|
| **The fleet API** | Service (a live URL) | The judges can talk to it. `adk api_server` serves the coordinator and all five desks. |
| **The morning sweep** | Job + Cloud Scheduler | The track's async requirement: it runs at 06:00 with nobody watching, finds discrepancies, and **holds** every draft. |

They share an image on purpose. The agent that answers an operator at 14:00 and
the one that sweeps at 06:00 must be the same fleet with the same gate, or the
governance claim is only true for one of them.

> **Time and cost.** Budget 45–60 minutes for a first deploy. Cloud Run
> scale-to-zero means the service costs approximately nothing when idle; the
> Gemini calls are the real cost, and at `gemini-3.7-flash` introductory pricing
> a full sweep of six shipments is a few cents.

---

## 0. Before you start

You need:

- A Google Cloud project with **billing enabled**. Yours is `neat-domain-494716-b3`
  (that is what `GOOGLE_CLOUD_PROJECT` is already set to in this environment).
- The `gcloud` CLI, authenticated as a user with Owner or Editor on that project.
- The repo checked out locally.

Everything below is copy-pasteable. Set these once per shell:

```bash
export PROJECT_ID=neat-domain-494716-b3
export REGION=europe-west1          # pick one near you; must support Cloud Run
export SERVICE=freight-ops-fleet
export JOB=freight-ops-sweep
export REPO=freight-ops             # Artifact Registry repo name

gcloud config set project "$PROJECT_ID"
gcloud config set run/region "$REGION"
```

**Why `europe-west1`:** the demo is a Hamburg import desk, and keeping the
service near you keeps the demo responsive on camera. Any Cloud Run region works.

---

## 1. Enable the APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  aiplatform.googleapis.com
```

This takes a minute or two the first time. `aiplatform` is only needed if you
switch to Vertex (§7); the rest are needed for every path below.

---

## 2. Put the API key in Secret Manager

Never bake the key into the image or a `--set-env-vars` flag — it would then sit
in your deployment history and in `gcloud run services describe` output forever.

```bash
# paste the key when prompted, then Ctrl-D
gcloud secrets create gemini-api-key --replication-policy=automatic --data-file=-

# confirm it exists (does not print the value)
gcloud secrets versions list gemini-api-key
```

If the secret already exists, add a new version instead:

```bash
gcloud secrets versions add gemini-api-key --data-file=-
```

---

## 3. Create a service account with the least privilege that works

The default Compute service account is over-privileged. Make a dedicated one:

```bash
gcloud iam service-accounts create freight-fleet \
  --display-name="Freight Ops Fleet runtime"

export SA="freight-fleet@${PROJECT_ID}.iam.gserviceaccount.com"

# read ONLY the one secret it needs
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor"
```

That is the whole grant for the API-key path. The fleet writes only inside its
own container, so it needs no storage, no database, no Vertex roles. (§7 adds
one role if you switch to Vertex.)

---

## 4. Deploy the API service

From the repo root:

```bash
gcloud run deploy "$SERVICE" \
  --source . \
  --service-account "$SA" \
  --set-secrets "GOOGLE_API_KEY=gemini-api-key:latest" \
  --set-env-vars "FREIGHT_MODEL=gemini-3.7-flash" \
  --memory 1Gi \
  --cpu 1 \
  --timeout 600 \
  --max-instances 3 \
  --allow-unauthenticated
```

What each flag is doing, and why:

- **`--source .`** builds with Cloud Build using the repo's `Dockerfile`. No
  local Docker needed. `.dockerignore` keeps `eval/` — the answer keys — out of
  the build context entirely.
- **`--set-secrets`** mounts the key as `GOOGLE_API_KEY` at runtime. It never
  appears in config output.
- **`--timeout 600`** matters: a cross-check reads three documents and reasons
  over them, which can take 60–90 seconds. Cloud Run's 300s default will cut off
  a slow run mid-answer.
- **`--memory 1Gi`** — ADK plus the Gemini SDK is comfortable here; 512Mi is
  tight enough to OOM under concurrency.
- **`--allow-unauthenticated`** makes the URL clickable for judges. **This is a
  deliberate demo trade-off**: anyone with the URL can spend your Gemini quota.
  For anything beyond the submission window, drop this flag and use
  `gcloud run services proxy` instead (see §8).

First build takes 3–6 minutes. When it finishes you get a URL:

```
Service URL: https://freight-ops-fleet-XXXXXXXX-ew.a.run.app
```

### Verify it

```bash
export URL=$(gcloud run services describe "$SERVICE" --format='value(status.url)')

# the app is discoverable
curl -s "$URL/list-apps"
# expect: ["freight_ops"]
```

Then drive one real turn:

```bash
curl -s -X POST "$URL/run" \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "freight_ops",
    "user_id": "operator",
    "session_id": "demo-1",
    "new_message": {"role": "user", "parts": [{"text":
      "Cross-check the documents in shipments/shp-002-hero"}]}
  }' | python3 -m json.tool | tail -40
```

You should see the cross-check report with four discrepancies. **If you get a
session error**, create the session first:

```bash
curl -s -X POST "$URL/apps/freight_ops/users/operator/sessions/demo-1" \
  -H "Content-Type: application/json" -d '{}'
```

### The optional dev UI

Adding `--with_ui` to an `adk deploy` (or changing the `CMD` to
`adk api_server --with_ui`) serves ADK's web console, which is a much better
demo surface than curl. ADK's own docs mark it **development-only** — fine for a
hackathon URL, not for anything real.

---

## 5. Deploy the sweep as a Job

The sweep is not a web request — it is a scheduled batch run that must exit.
Cloud Run **Jobs**, not Services:

```bash
gcloud run jobs deploy "$JOB" \
  --source . \
  --service-account "$SA" \
  --set-secrets "GOOGLE_API_KEY=gemini-api-key:latest" \
  --set-env-vars "FREIGHT_MODEL=gemini-3.7-flash" \
  --command python \
  --args "-m,freight_fleet.cli,sweep" \
  --memory 1Gi \
  --task-timeout 1800 \
  --max-retries 1 \
  --region "$REGION"
```

- **`--command` / `--args`** override the image's `CMD`, so the same image runs
  the CLI instead of the API server.
- **`--task-timeout 1800`** — six shipments at ~60s each, with headroom.
- **`--max-retries 1`** — a retried sweep re-drafts notices that are already
  held. One retry is a network hiccup; more is duplicate work for the operator.

Run it once by hand, on camera if you like:

```bash
gcloud run jobs execute "$JOB" --region "$REGION" --wait
gcloud run jobs executions logs read \
  "$(gcloud run jobs executions list --job="$JOB" --region="$REGION" \
      --limit=1 --format='value(name)')" --region "$REGION"
```

The closing line of the logs is the one that matters:

```
  5 draft(s) held for approval; nothing sent, nothing written.
```

---

## 6. Schedule it for 06:00

```bash
gcloud scheduler jobs create http freight-ops-morning-sweep \
  --location "$REGION" \
  --schedule "0 6 * * 1-5" \
  --time-zone "Europe/Athens" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run" \
  --http-method POST \
  --oauth-service-account-email "$SA"
```

The scheduler's service account needs permission to run the job:

```bash
gcloud run jobs add-iam-policy-binding "$JOB" \
  --region "$REGION" \
  --member="serviceAccount:${SA}" \
  --role="roles/run.invoker"
```

`0 6 * * 1-5` is 06:00 on weekdays. Freight desks do not sweep on Sunday.

Force a firing to prove the wiring without waiting for morning:

```bash
gcloud scheduler jobs run freight-ops-morning-sweep --location "$REGION"
```

---

## 7. Optional: switch sessions to Vertex

The local default is SQLite, which lives inside the container — good enough for
the demo, but a Cloud Run instance is ephemeral, so sessions die with it. Two
ways to make them genuinely durable in the cloud:

**(a) Postgres (Cloud SQL).** The fleet's session URL is one env var:

```bash
--set-env-vars "FREIGHT_SESSIONS_DB=postgresql+asyncpg://USER:PASS@/DB?host=/cloudsql/INSTANCE"
```

plus `--add-cloudsql-instances INSTANCE`. Nothing in the code changes — the
`chat` command already reads `FREIGHT_SESSIONS_DB`.

**(b) Agent Engine sessions.** ADK's API server accepts
`--session_service_uri=agentengine://<resource-id>`. This needs a provisioned
Agent Engine and one extra IAM role:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/aiplatform.user"
```

**Recommendation for the submission: skip both.** The kill-restart-resume
property is already proven locally and on camera, and neither option adds a
judgeable capability — they add operational durability the video cannot show.
Spend the day on the recording instead.

---

## 8. Locking it down after the hackathon

The `--allow-unauthenticated` flag is a submission convenience with a real cost:
an open URL burns your Gemini quota and, because the fleet writes into its own
container, gives strangers a scratchpad. When the judging window closes:

```bash
gcloud run services update "$SERVICE" --no-allow-unauthenticated
```

and reach it through an authenticated proxy instead:

```bash
gcloud run services proxy "$SERVICE" --region "$REGION"   # then use localhost:8080
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Service URL returns 404 on /list-apps` | `adk api_server` was pointed at the wrong folder | The `CMD` must end with `/app/agents` — the folder *containing* `freight_ops/`, not the agent folder itself. |
| `No module named freight_fleet` | `pip install .` ran before `src/` was copied | Keep the Dockerfile's COPY order: `pyproject.toml` + `src/`, then install. |
| `ValueError: No API key was provided` | Secret not mounted, or the SA lacks `secretAccessor` | `gcloud run services describe $SERVICE --format='value(spec.template.spec.containers[0].env)'` and re-check §3. |
| Request dies at ~5 minutes | Cloud Run default 300s timeout | `--timeout 600` (§4). |
| Agent answers "not found" for every document | Workspace never seeded | The image must contain `fixtures/`; check `.dockerignore` does not exclude it. |
| `Failed to create database engine` | A sync SQLite URL | Async driver required: `sqlite+aiosqlite:///...`, not `sqlite:///...`. |
| Sweep job succeeds but writes files | Gate bypassed — **stop and investigate** | This must be impossible; `outbox/` should be empty after a sweep. Read the ledger before deploying further. |

---

## What deploying does *not* prove

Worth saying plainly in the submission, because judges notice when a live URL is
doing less work than it appears:

- A deployed URL proves the fleet **runs** in a container. It does not prove the
  governance property — that is proven by the ledger and the scoreboard, both of
  which run identically on a laptop.
- The container's workspace is **ephemeral**. Approvals granted against the
  deployed service do not survive a cold start unless you mount durable storage.
  The local CLI is the honest approval surface; the URL is the demo surface.
