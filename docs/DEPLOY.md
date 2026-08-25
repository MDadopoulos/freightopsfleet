# Deploying to Google Cloud

Two things get deployed, from **one image**:

| What | Cloud Run kind | Why it exists |
|---|---|---|
| **The operator console** | Service (a live URL) | The image's default `CMD`. A judge clicking the URL lands on the Desk — the pending count, the decision queue, the ledger, the catalog and the scoreboard — with **no `GOOGLE_API_KEY` required**, no model call, and therefore no way to burn quota or 500 on credentials. |
| **The morning sweep** | Job + Cloud Scheduler | The track's async requirement: it runs at 06:00 with nobody watching, finds discrepancies, and **holds** every draft. |

> **The default `CMD` changed.** It is now
> `uvicorn freight_fleet.console:app --host 0.0.0.0 --port ${PORT:-8080}`.
> To serve ADK's API instead, override it:
> `--command sh --args "-c,adk api_server --host 0.0.0.0 --port \${PORT:-8080} /app/agents"`.
> Both live in one image; only the entry point differs.
>
> **`FREIGHT_CONSOLE_READONLY=1`** disables both decision buttons and returns
> `403` for either POST, touching neither the approval store nor the ledger. It
> is the entire access model, and it is the right setting for a public
> submission URL: the console is then a pure exhibit. Approvals stay on the
> local CLI, where the durable store actually lives.

They share an image on purpose. The agent that answers an operator at 14:00 and
the one that sweeps at 06:00 must be the same fleet with the same gate, or the
governance claim is only true for one of them.

> **Time and cost.** Budget 45–60 minutes for a first deploy, plus 15 for the
> optional GCS mount in §4b. Cloud Run scales to zero, so the two services cost
> approximately nothing while nobody is looking at them; a GCS bucket holding a
> few hundred KB of ledger is rounding error. **Inference is the only line item
> that grows** — and note §7a: an AI Studio API key does *not* draw on Google
> Cloud credits, so if you were granted credits and want them to absorb the
> model calls, you need Vertex. Either way the amounts are small at this scale:
> a sweep is six cross-checks, and the whole eval is nine tasks. Check the
> current per-token price rather than trusting a number in a document — but a
> hackathon-sized credit grant is not the constraint here; forgetting to delete
> a `--min-instances 1` service afterwards would be.

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

## 4. Deploy the service

From the repo root:

```bash
gcloud run deploy "$SERVICE" \
  --source . \
  --service-account "$SA" \
  --set-env-vars "FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_CONSOLE_READONLY=1" \
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
- **No `--set-secrets` here, deliberately.** The read-only console renders four
  artifacts the fleet already produced and calls no model, so it has no use for
  the API key — and a credential a process cannot use is a credential that
  cannot leak from it. The key is mounted only where work actually happens: the
  sweep Job (§5) and the private ops console (§4a). Where the key *is* mounted,
  `--set-secrets` puts it in the environment at runtime. It never
  appears in config output. The console does not read it — keep it mounted only
  if you intend to override the `CMD` back to `adk api_server`.
- **`--timeout 600`** matters: a cross-check reads three documents and reasons
  over them, which can take 60–90 seconds. Cloud Run's 300s default will cut off
  a slow run mid-answer.
- **`--memory 1Gi`** — ADK plus the Gemini SDK is comfortable here; 512Mi is
  tight enough to OOM under concurrency.
- **`--allow-unauthenticated`** makes the URL clickable for judges, and
  **`FREIGHT_CONSOLE_READONLY=1` is what makes that safe.** Be precise about the
  risk, because an earlier draft of this document got it wrong: the console
  makes **no model calls at all** (`grep -c "LlmAgent\|Runner" src/freight_fleet/console.py`
  → 0), so an open URL cannot burn Gemini quota. What it *could* do without the
  read-only flag is worse. `POST /decision/{id}/approve` calls
  `execute_approved`, which replays the held call and **writes the file**. On an
  open URL, any stranger becomes the human in the loop, and the ledger records
  their click as `approved` — the precise failure this project exists to
  prevent. `FREIGHT_CONSOLE_READONLY=1` makes both decision routes return 403
  before they reach the store, so the public surface is structurally incapable
  of approving anything. See §4a for how you approve things yourself.

First build takes 3–6 minutes. When it finishes you get a URL:

```
Service URL: https://freight-ops-fleet-XXXXXXXX-ew.a.run.app
```

### Verify it

```bash
export URL=$(gcloud run services describe "$SERVICE" --format='value(status.url)')

# the console is up (no credentials involved)
curl -s "$URL/healthz"          # expect: {"ok":true}
curl -s "$URL/reconcile.json"   # expect: {"diverged":false,...}
curl -s "$URL/" | head -5       # the Desk
```

Open `$URL` in a browser: that is the demo surface. If you overrode the `CMD`
back to `adk api_server`, check discoverability instead:

```bash
curl -s "$URL/list-apps"
# expect: ["freight_ops"]
```

Then, if you are serving the ADK API, drive one real turn (the console has no
such endpoint — it never calls a model):

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

## 4a. The private ops console — where the buttons actually work

The public service can show the queue and refuse every decision. You still need
somewhere the approve button *works*, and it must not be the same URL.

Deploy the same image a second time, authenticated, with the read-only flag off:

```bash
export OPS=freight-ops-console

gcloud run deploy "$OPS" \
  --source . \
  --service-account "$SA" \
  --set-secrets "GOOGLE_API_KEY=gemini-api-key:latest" \
  --set-env-vars "FREIGHT_MODEL=gemini-3.7-flash" \
  --memory 1Gi --cpu 1 --timeout 600 --max-instances 1 \
  --no-allow-unauthenticated
```

`--no-allow-unauthenticated` means Cloud Run rejects any request without a valid
Google identity token. Grant yourself the invoker role and nobody else:

```bash
gcloud run services add-iam-policy-binding "$OPS" \
  --region "$REGION" \
  --member "user:$(gcloud config get-value account)" \
  --role "roles/run.invoker"
```

Reach it by opening an authenticated tunnel — `gcloud` mints and refreshes the
token for you, so the browser needs no plugin and you paste no credentials:

```bash
gcloud run services proxy "$OPS" --region "$REGION" --port 8081
# now open http://localhost:8081 — buttons live, everything else identical
```

**Why two services and not one with a password.** A shared password in an env
var is a secret that leaks into shell history, screen shares and screenshots —
on a demo call, especially. IAM is the access model Cloud Run already has, and
splitting the deployment means the public surface is not "trusted not to write",
it is **configured so it cannot**. That is the same argument the gate makes about
tools, applied to the deployment: make the unsafe thing unreachable rather than
asking a human to avoid it. It also demos well — you can show a judge the
disabled buttons on the public URL and the live ones on localhost, from one
image, and the difference is two flags.

---

## 4b. Making the Job and the console share one ledger

**Read this before you demo.** Every Cloud Run container gets its own
filesystem. The sweep Job writes its holds to `audit/ledger.jsonl` and
`data/approvals.json` *inside the Job's container*, which is destroyed when the
job exits. The console Service reads those paths *inside its own container* and
sees nothing. Left alone, the deployed sweep and the deployed console never meet
— the sweep reports "5 draft(s) held", the console says the desk is clear, and
the demo has nothing to click.

Mount one GCS bucket into both so they share state:

```bash
export BUCKET="${PROJECT_ID}-freight-state"
gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA" --role "roles/storage.objectAdmin"
```

Then add the same three flags to the Job (§5) and to the ops console (§4a):

```bash
  --add-volume "name=state,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=state,mount-path=/state" \
  --set-env-vars "FREIGHT_LEDGER_PATH=/state/ledger.jsonl,FREIGHT_APPROVALS_PATH=/state/approvals.json"
```

(`--set-env-vars` replaces the whole set, so include `FREIGHT_MODEL` in the same
flag rather than as a second one.) Both paths are already env-driven in the code
— `cli.py:52` and `console.py:96` read `FREIGHT_LEDGER_PATH`, `cli.py:54` and
`console.py:100` read `FREIGHT_APPROVALS_PATH` — so nothing needs changing to
support this.

Give the **public** console the same mount read-only, so judges see the real
sweep's output rather than an empty desk:

```bash
  --add-volume "name=state,type=cloud-storage,bucket=$BUCKET,readonly=true" \
  --add-volume-mount "volume=state,mount-path=/state" \
```

Two honest caveats. GCS-FUSE is not a POSIX filesystem: appends and renames work
but are not atomic across writers, so this is sound for one Job plus one console
and is **not** a concurrency design. And the store's re-read-before-mutate
(added in `FileApprovalStore._reload`) is what keeps the console and the sweep
from clobbering each other's view here — it was written for the local
two-process case and applies unchanged.

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
  --max-retries 0 \
  --region "$REGION"
```

- **`--command` / `--args`** override the image's `CMD`, so the same image runs
  the CLI instead of the API server.
- **`--task-timeout 1800`** — six shipments at ~60s each, with headroom.
- **`--max-retries 0`** — the sweep is **not idempotent**: every run that reaches
  a shipment drafts a notice and holds it, so a second run holds a second copy of
  the same draft under a second approval id. It also now exits non-zero when it
  *skips* a shipment rather than only when it dies — which is the honest signal,
  but it means Cloud Run would retry a run that already held five of six drafts
  and hand the operator five duplicates. Retrying is the wrong response to a
  partial sweep. A failed execution is visible in the logs; re-run it by hand
  after reading them, once you know which shipments actually got through.

Run it once by hand, on camera if you like:

```bash
gcloud run jobs execute "$JOB" --region "$REGION" --wait
gcloud run jobs executions logs read \
  "$(gcloud run jobs executions list --job="$JOB" --region="$REGION" \
      --limit=1 --format='value(name)')" --region "$REGION"
```

Two lines of the logs matter. The tally:

```
  5 draft(s) held for approval; nothing sent, nothing written.
```

And, if the run did not reach every shipment, the line naming what it missed —
a sweep that silently skipped work is not a successful sweep:

```
  !! 1 of 6 shipment(s) were NOT checked: shp-004-air-dg
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

## 7a. Spending your GCP credits on inference (Vertex instead of AI Studio)

This matters if you were granted Cloud credits. A `GOOGLE_API_KEY` from AI
Studio bills through **Google AI Studio / the Gemini Developer API**, which is a
different billing path from Google Cloud — your Cloud credits do not pay for it.
Cloud Run, Secret Manager and GCS *are* covered; the model calls are not.

To route inference through Vertex AI, where the credits apply, the fleet needs
no code change — `google-genai` picks its backend from the environment. On the
Job and the ops console, drop the secret and set three variables instead:

```bash
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,FREIGHT_MODEL=gemini-3.7-flash"
```

and give the service account Vertex access:

```bash
gcloud services enable aiplatform.googleapis.com
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role "roles/aiplatform.user"
```

**Verify this with one real call before you rely on it.** Two things differ
between the backends and neither is guaranteed by this document: the exact model
identifier Vertex publishes for Gemini 3.7 Flash may not be the bare
`gemini-3.7-flash` string the Developer API accepts, and the model must be
available in the region you picked. Run the smoke test in §8
after switching; if it fails on the model name, set `FREIGHT_MODEL` to whatever
`gcloud ai models list --region "$REGION"` reports rather than guessing.

Keeping the AI Studio key is a perfectly reasonable choice for a hackathon — the
whole eval suite costs cents. Switch only if you actually want the credits to
absorb it.

---

## 8. Smoke-testing the deployment

Run these in order. Each one fails loudly and tells you which step to go back
to, so do not skip ahead when one is red.

**1. The service is up and the container is healthy.**

```bash
export URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
curl -s "$URL/healthz"                 # expect: {"ok":true}
```

**2. The record and the queue agree.** This is the governance healthcheck, and
it is the one worth watching:

```bash
curl -s "$URL/reconcile.json" | python3 -m json.tool
# expect "diverged": false on a fresh deploy
```

`diverged: true` on a *fresh* deploy means the mount in §4b is wrong — usually
the console reading a different path from the Job. It is not a code failure.

**3. Every screen renders.** A 500 here is almost always a missing artifact, not
a bug:

```bash
for path in / /ledger /fleet /evidence; do
  printf '%-12s %s\n' "$path" "$(curl -s -o /dev/null -w '%{http_code}' "$URL$path")"
done
# expect 200 200 200 200
```

**4. The public surface really cannot decide.** Prove the read-only flag rather
than trusting it — take any id from `/reconcile.json` and try to approve it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL/decision/any-id-here/approve"
# expect 403 — if this returns 302 or 200, FREIGHT_CONSOLE_READONLY is not set
```

**5. The fleet actually reasons.** This is the first step that spends money and
the first that needs the API key, so a failure here is credentials or model
name, not plumbing:

```bash
gcloud run jobs execute "$JOB" --region "$REGION" --wait
gcloud run jobs executions logs read \
  "$(gcloud run jobs executions list --job="$JOB" --region="$REGION" \
      --limit=1 --format='value(name)')" --region "$REGION"
```

Two lines in that output matter — the tally, and the skip line if there is one:

```
  5 draft(s) held for approval; nothing sent, nothing written.
  !! 1 of 6 shipment(s) were NOT checked: shp-004-air-dg
```

**6. The holds reached the console.** Refresh the public URL. If §4b is wired,
the desk now shows the drafts the Job just held. If it still says the desk is
clear, the Job and the console are not sharing a bucket — go back to §4b.

**7. A decision executes, once.** In the authenticated tunnel
(`gcloud run services proxy "$OPS" --port 8081`), approve one draft, then check
the record:

```bash
curl -s "$URL/ledger.jsonl" | tail -3
```

You should see `held` then `executed` for that id — and approving the same id a
second time must refuse, because the grant is single-use.

---

## 9. Running the demo

The five minutes, in the order that makes the argument. Have two windows open:
the **public URL** in a browser, and a terminal.

**Before you start recording**

- Run the sweep once (§8 step 5) so the desk has real holds. A demo that begins
  with an empty queue spends its first minute creating one.
- Open the authenticated tunnel and leave it running: `gcloud run services proxy "$OPS" --region "$REGION" --port 8081`.
- `curl -s "$URL/healthz"` — a cold start takes a few seconds and you do not
  want that pause on camera.

**The run of show**

1. **The desk (public URL, ~40s).** Open `/`. Point at the pending count. "Five
   drafts are waiting. Nobody was watching when they were written — a scheduled
   job at 06:00 checked six shipments and stopped at the gate."
2. **One decision (~60s).** Open a held action. Show the draft, and the
   *evidence* — the documents the agent read before drafting. Then try to
   approve **on the public URL** and let it refuse. "This surface can't decide.
   The buttons aren't disabled by CSS; the route returns 403."
3. **The approval (~40s).** Switch to `localhost:8081` — same image, one flag
   different — and approve it. Show the file appearing.
4. **The record (~50s).** Open `/ledger`. Every call the fleet made, with the
   verdict and the outcome. Point at the sha256 of the file as served: "you can
   recompute this with `shasum -a 256`."
5. **The scoreboard (~60s).** Open `/evidence`. 7/7, three runs of three, and
   the clean control — the shipment with nothing wrong with it, graded with zero
   tolerance. "A missed discrepancy costs a correction. A fabricated one costs
   trust."
6. **The close (~30s).** `/fleet` — five desks, and the tool each is allowed. "One
   gate, one ledger, one eval. The governance isn't five prompts asking nicely;
   it's one code path every tool call goes through."

**What to say if something breaks on camera.** The honest line is the strong
one: the URL is the demo surface, not the proof. The ledger and the scoreboard
run identically on a laptop, and both are in the repo. A cold start that takes
eight seconds does not weaken the argument.

---

## 10. Locking it down after the hackathon

If you followed §4 and §4a, the public service is already read-only and the
decision routes already live behind IAM, so there is less to do here than there
would otherwise be — the open URL exposes rendered artifacts and nothing that
acts. What it still is, is a permanent public endpoint you are no longer
watching. When the judging window closes, close it:

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
| Console shows an empty desk right after the sweep held drafts | Job and Service have separate filesystems | Mount one bucket into both — §4b. This is the most common cloud-only failure. |
| `POST /decision/.../approve` returns 302 on the public URL | `FREIGHT_CONSOLE_READONLY` not set | Redeploy §4 with the flag; verify with §8 step 4 before demoing. |
| `reconcile.json` says `diverged: true` on a fresh deploy | Console and Job reading different paths | Check both carry the same `FREIGHT_LEDGER_PATH` / `FREIGHT_APPROVALS_PATH`. |
| 404 or `model not found` after switching to Vertex | Model id or region differs on Vertex | See §7a — set `FREIGHT_MODEL` from `gcloud ai models list`, do not guess. |
| Agent answers "not found" for every document | Workspace never seeded | The image must contain `fixtures/`; check `.dockerignore` does not exclude it. |
| `Failed to create database engine` | A sync SQLite URL | Async driver required: `sqlite+aiosqlite:///...`, not `sqlite:///...`. |
| Sweep job succeeds but writes files | Gate bypassed — **stop and investigate** | This must be impossible; `outbox/` should be empty after a sweep. Read the ledger before deploying further. |

---

## What deploying does *not* prove

Worth saying plainly in the submission, because judges notice when a live URL is
doing less work than it appears:

- A deployed URL proves the fleet **runs** in a container. It does not prove the
  governance property — that is proven by the ledger and the scoreboard, both of
  which run identically on a laptop. The console makes both *legible* at the URL;
  it does not make either more true.
- The container's workspace is **ephemeral**. Approvals granted against the
  deployed service do not survive a cold start unless you mount durable storage.
  The local CLI is the honest approval surface; the URL is the demo surface.
