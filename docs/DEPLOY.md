# Deploying to Google Cloud

**One image, two deployments.**

| What | Cloud Run kind | Why it exists |
|---|---|---|
| **The fleet** | Service (a live URL) | §4. One door for everything a visitor can do: the homepage, the login, the operator desk, the chat, document upload, and the button that starts the sweep. One process, one URL, one ledger. |
| **The morning sweep** | Job + Cloud Scheduler | §5 and §6. The track's async requirement: it runs at 06:00 with nobody watching, cross-checks every open shipment, drafts the correction emails and **holds** every one of them. |

They share an image on purpose. The agent that answers an operator at 14:00 and
the one that sweeps at 06:00 must be the same fleet with the same gate, or the
governance claim is only true for one of them. They now also share one **state
bucket**, which is what makes the claim end to end: the sweep's hold at 06:00 is
the row the desk approves at 09:00, and a hold a judge raises in chat at 14:00
lands in the same queue with their name on it.

> **History, so an old command in your shell history does not confuse you.**
> This used to be four Cloud Run services — a read-only console, a public
> sandbox, a chat behind IAP, and a chat with a demo login. Each was defensible
> on its own and together they were a maze; worse, they could not close the
> loop, because a hold raised in chat landed in the chat container's disposable
> ledger while the only desk that could approve anything was a different URL
> over a different bucket. One service over one shared bucket closes it. **IAP,
> `FREIGHT_CONSOLE_READONLY`, `FREIGHT_CONSOLE_MODE`, `FREIGHT_SANDBOX_URL`,
> `FREIGHT_CHAT_DEMO_URL`, `FREIGHT_PUBLIC_URL` and `FREIGHT_IAP_AUDIENCE` are
> all retired.** §10 has the teardown lines for anything you already deployed.

> **The `=` in `--args=` is load-bearing.** Both deploys below override the entry
> point with `--command sh --args='-c,…'`. The value starts with a dash, and
> without the `=` gcloud's parser reads it as another flag and fails with
> "expected one argument" — in every shell, not just PowerShell. The image's
> default `CMD` is already the Service's command
> (`uvicorn freight_fleet.webapp:app_factory --factory`); the Service passes it
> explicitly anyway, so that what runs is visible in
> `gcloud run services describe` rather than buried in a layer. The Job must
> pass it, because it runs something else entirely.

> **Time and cost.** Budget 45–60 minutes for a first deploy. Cloud Run scales to
> zero, so the service costs approximately nothing while nobody is looking at
> it; a GCS bucket holding a few hundred KB of ledger, a spool of demo mail and
> a handful of uploads is rounding error. **Inference is the only line item that
> grows** — and note §7a: an AI Studio API key does *not* draw on Google Cloud
> credits, so if you were granted credits and want them to absorb the model
> calls, you need Vertex. The shape deployed below is Vertex-only for exactly
> that reason. Either way the amounts are small at this scale: a sweep is six
> cross-checks, and the whole eval is nine tasks. Check the current per-token
> price rather than trusting a number in a document — but a hackathon-sized
> credit grant is not the constraint here; forgetting to delete a
> `--min-instances 1` service afterwards would be. The one exception is §4.2's
> Cloud SQL instance: roughly $8–10 for a judging window, and it bills while
> idle, because a database does not scale to zero. §10 deletes it.

---

## 0. Before you start

### Yes, you need the repo on the machine you deploy from

Two commands in this guide use `gcloud run deploy --source .` (§4.3 and §5).
That uploads **your current directory** to Cloud Build, which builds the image
from the repo's `Dockerfile`. There is no "deploy from GitHub" shortcut here —
if the working directory is not the repo, the build has nothing to build.

The steps that need the repo present: **§4.3 and §5** (the two `--source .`
deploys), **§4.4's `chat-users` command**, **§7a's local test**, and **§8
step 5**. Everything else — enabling APIs, Secret Manager, IAM, the scheduler,
and every `curl` in §8 — is pure `gcloud`/HTTP and works from anywhere you are
logged in.

### Option A — Cloud Shell (recommended; installs nothing)

Open <https://shell.cloud.google.com>. It is free, browser-based, and comes with
`gcloud`, `git` and Python already installed **and already authenticated as
you** — which removes the whole "install the CLI, then log in" step, and is the
fastest way to get from zero to a deployed service.

```bash
git clone https://github.com/MDadopoulos/freightopsfleet.git
cd freightopsfleet
python3 --version        # needs 3.11+; Cloud Shell is current
```

Three things worth knowing before you rely on it:

- **Web Preview** (the icon top-right) exposes ports 8080–8084 in the browser,
  which is how you run the app *locally* in Cloud Shell —
  `uvicorn freight_fleet.webapp:app_factory --factory --port 8080` — without
  installing anything. The deployed service needs no tunnel at all: it is a
  public URL with its own login in front of it.
- `$HOME` persists (5 GB), but the VM is recycled after inactivity — a session
  idles out after ~20 minutes and is capped at ~12 hours. Your clone survives;
  a long-running process does not. Do not start a `--repeat 3` eval and walk
  away.
- For §7a, if the ADC check complains about credentials, run
  `gcloud auth application-default login` in Cloud Shell too. Being logged in
  for `gcloud` and having ADC for *client libraries* are related but not always
  the same thing, and that command settles it either way.

### Option B — your own machine

```bash
git clone https://github.com/MDadopoulos/freightopsfleet.git
cd freightopsfleet
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

You will also need the `gcloud` CLI installed
(<https://cloud.google.com/sdk/docs/install>) and authenticated:

```bash
gcloud auth login
gcloud auth application-default login   # separate command, needed for §7a
```

Those two are genuinely different: `auth login` authenticates the `gcloud`
command, `auth application-default login` writes the credentials Python client
libraries read. Skipping the second is the most common reason §7a's test fails
with "could not resolve project using application default credentials".

The local venv is only needed for the §7a test and the eval — the Cloud Run
builds do not use it, and `.gcloudignore` keeps it out of the upload. That
file exists on purpose: without it gcloud derives the upload list from
`.gitignore`, which hides `eval/runs/*.json` (the run records are committed
with `git add -f`), and the Scoreboard page deploys empty.

### Either way, you need

- A Google Cloud project with **billing enabled** (credits count as billing, but
  the account must have a billing account attached).
- Owner or Editor on that project — you will be creating service accounts and
  granting IAM roles.

Set these once per shell, and re-set them if your Cloud Shell session recycles:

```bash
export PROJECT_ID=neat-domain-494716-b3    # your project id
export REGION=europe-west1                 # pick one near you; must support Cloud Run
export SERVICE=freight-ops-fleet           # the one service
export JOB=freight-ops-sweep               # the one job
export BUCKET="${PROJECT_ID}-freight-state"   # the shared state, created in §4.1
export REPO=freight-ops                    # Artifact Registry repo name

gcloud config set project "$PROJECT_ID"
gcloud config set run/region "$REGION"
```

**Why `europe-west1`:** the demo is a Hamburg import desk, and keeping the
service near you keeps it responsive on camera. Any Cloud Run region works — but
if you switch to Vertex in §7a, note that the *model* endpoint is set separately
by `GOOGLE_CLOUD_LOCATION`, and `global` is usually the right value there
regardless of which region you run the container in.

---

## 1. Enable the APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  sqladmin.googleapis.com \
  aiplatform.googleapis.com
```

This takes a minute or two the first time. `aiplatform` is what every model call
in the deployed shape goes through (§7a), and `sqladmin` is the session database
(§4.2). The rest are needed for every path below.

---

## 1a. Document ingestion — from paper to the inbox

A freight desk does not receive markdown. It receives a PDF from a carrier's
system and a crooked photo of a printout that someone laid on a flatbed at an
angle. `fixtures/raw/` is that arrival surface, and `ingest` is the operator
step that turns it into something the fleet can read.

### What `fixtures/raw/` holds

26 committed originals — **23 PDFs and 3 scan-like PNGs** — mirroring the
canonical tree: `raw/inbox/scan_001.pdf`, `raw/quotes/quote_baltic.pdf`,
`raw/shipments/shp-002-hero/waybill.pdf`, and so on. They are *rendered from*
the canonical markdown and CSV by `scripts/render_documents.py`, so the two can
never disagree about a figure — a rendered original that added a fact the
canonical file lacks would break AGENTS.md #8, and rendering from the source is
what makes that impossible rather than merely discouraged.

Three of them (`inbox/scan_002.png`, `inbox/scan_004.png`,
`shipments/shp-005-air-dg/air_waybill.png`) go through a second pass — seeded
noise, a ±1.2° skew, a Gaussian blur, greyscale — so at least one document in
each shape the fleet handles arrives as pixels rather than as extractable text.

**They are committed, not built.** Two reasons, both about honesty:

- Rendering at image-build time would pull ~45 MB of PDF and raster libraries
  into `python:3.11-slim` for files that never change, and would make the bytes
  a judge sees depend on whichever library versions the builder happened to
  resolve that morning.
- A committed artefact is reviewable. You can open it, and a diff tells you it
  moved. `python scripts/render_documents.py --check` is the seal, and `ci.yml`
  runs it on every push — byte-exact for the PDFs (fpdf2 is pure Python and the
  script fixes the creation date and the producer string, so the same source
  gives the same bytes on Windows and on the ubuntu runner) and
  rendering-invariant for the PNGs (dimensions, a size band, and the source
  PDF's text still extractable). A rasteriser's anti-aliasing is not sealable
  across platforms, and a seal that pretended otherwise would report nothing.

The render libraries live in a pinned optional extra —
`pip install -e ".[render]"`. They are *not* runtime dependencies, so the
deployed image never carries them. The pins are what make `--check` mean
something: fpdf2 writes its own version into the PDF's `/Producer`, and Pillow's
resampling changes between releases.

### How `raw/` reaches a workspace

- `python scripts/seed_workspace.py --all` copies `fixtures/raw/` to
  `workspace/raw/`. A plain `seed_workspace.py` (no `--all`) copies **no**
  `raw/`, so `ingest` finds nothing to plan — that is the documented default,
  not a bug.
- The **image** already has it: the Dockerfile runs `seed_workspace.py --all` at
  build time, and its existing `COPY fixtures/` line carries the binaries in.
  `eval/` is still excluded, so no answer key rides along.
- A container that starts with an **empty** workspace (a fresh volume mount)
  gets `raw/` from `deploy/agents/freight_ops/agent.py`'s fallback seed, which
  copies the same four directories.

Carrying it into the container matters because the deployed fleet should show
the same arrival surface a local one does. `read_file` reads only `.md`, `.csv`
and `.txt`; anything else comes back as

```json
{"status": "binary", "path": "raw/inbox/scan_001.pdf",
 "hint": "not a text document; run `python -m freight_fleet.cli ingest` to transcribe raw/ into inbox/"}
```

which is the honest answer. A tool that silently returned mojibake for a PDF
would have the model reasoning over its own decoding errors.

### Running it

```bash
python -m freight_fleet.cli ingest --dry-run           # the plan; no credentials needed
python -m freight_fleet.cli ingest --only 'inbox/*'    # one subtree
python -m freight_fleet.cli ingest                     # all of it
python -m freight_fleet.cli ingest --force             # overwrite existing inbox/ targets
```

**The naming rule.** Everything lands flat in `inbox/`, because that is where
`doc_intake` and the rest of the fleet already look. The first path segment (the
source directory) is dropped; every segment after it is joined with `__`:

| Source | Target |
|---|---|
| `raw/inbox/scan_001.pdf` | `inbox/scan_001.md` |
| `raw/quotes/quote_baltic.pdf` | `inbox/quote_baltic.md` |
| `raw/shipments/shp-002-hero/waybill.pdf` | `inbox/shp-002-hero__waybill.md` |

Flattening rather than nesting is deliberate: `doc_intake`'s whole job is to work
out which shipment a loose document belongs to, and handing it the answer in a
directory name would grade the wrong thing. The `__` prefix is only what keeps
six different `waybill.pdf` files from colliding on one filename.

**Every transcription is marked.** Line one of every written file is

```
<!-- transcribed from raw/shipments/shp-002-hero/waybill.pdf by gemini-3.7-flash -->
```

so a reader can always tell a model's reading of a scan from a hand-written
canonical fixture. Nothing is ever deleted, and an existing target is skipped
unless `--force` — a re-run after a partial failure is safe by default. The
command exits 1 if anything failed, because an ingest that transcribed 25 of 26
documents and returned 0 would be a silent hole in the inbox.

### The environment it needs

The same variables as §7a. This is a paid Vertex call — the only one in the repo
that is not made by an agent:

```bash
export FREIGHT_WORKSPACE_ROOT=$PWD/workspace
python scripts/seed_workspace.py --all

unset GOOGLE_API_KEY                            # only one billing path at a time
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GOOGLE_CLOUD_LOCATION=global
export FREIGHT_MODEL=gemini-3.7-flash

gcloud auth application-default login           # if you have not already — §0
.venv/bin/python -m freight_fleet.cli ingest --dry-run
```

`--dry-run` builds no client and reads no credentials; it prints the plan and
marks which targets already exist. Run it first — it is the cheapest way to find
out your workspace was seeded without `--all`.

**Cost.** 26 documents, one call each, one or two pages in and a page of
markdown out. On `gemini-3.7-flash` that is **cents, not dollars** — the whole
tree costs less than one eval run. Check the current per-token price rather than
trusting that sentence; the point is the order of magnitude. Vertex has no Files
API, so every byte travels inline in the request; the per-file cap is 6 MB and a
larger file is refused rather than truncated, because half a waybill transcribed
as if it were whole is the worst possible output.

**Determinism, stated honestly.** The call pins `seed=20260722` and
`thinking_level="LOW"`, and in practice two ingests of the same PDF produce the
same markdown. Vertex documents `seed` as *best effort*, so that is an
observation, not a guarantee — and it is exactly why **the eval never runs
`ingest`**. The scoreboard grades the canonical markdown that ships in
`fixtures/`, which does not move. Ingestion is the realistic front door;
the graded path stays deterministic behind it.

**Reseed before you trust a score.** The eval grades whatever
`FREIGHT_WORKSPACE_ROOT` holds and never seeds itself, so after any
`ingest --force` over the canonical inbox:

```bash
python scripts/seed_workspace.py --all --clean
```

`--clean` wipes the workspace first. Without it, a transcription left in
`inbox/` is graded as if it were canonical, and a score measured that way is a
score of a different set of documents than the answer keys describe. `eval.yml`
runs the same command as an explicit step for the same reason — §11.

### What the deployed demo does with it, and what it does not

**The 26-document bulk `ingest` is a local operator step. Nothing runs it in a
Cloud Run container.** That is a choice, and its reasons are the reasons it is a
command rather than a tool:

- A container that ingested on start would make 26 paid model calls on every
  cold start, for output nobody asked for.
- The transcription is *derived* from documents already in the image. Spending
  money to regenerate them on a demo URL buys a judge nothing they can see.

**One document at a time is a different question, and the deployed service does
do that.** `POST /upload` — the upload control on `/chat` — writes the file into
the workspace jail under `raw/uploads/<who>/`, runs exactly the same
transcription on that one file, and puts the result in `inbox/` with the same
`<!-- transcribed ... -->` marker. It is bounded where a bulk run is not: PDF,
PNG or JPEG only, 6 MB (the model's inline cap), ten uploads an hour per
identity, and a durable copy under `FREIGHT_UPLOADS_DIR` on the state bucket
that is restored into the workspace on container start, so a restart forgets
nothing a judge handed it. A judge uploading their own scan is worth the cents;
regenerating fixtures already in the image is not.

The deployed chat also demonstrates the two halves that need no paid ingest at
all: ask it to read `raw/inbox/scan_001.pdf` and watch `read_file` refuse with
`binary` and the ingest hint, then ask the intake desk to sort `inbox/` — the
five canonical scans plus both quotes — and watch it group them and name what is
missing. The full 26-document run still belongs on camera, locally, where the
`--dry-run` plan, the paid run, and the marker on the resulting file are all
visible in one terminal and cost a few cents.

---

## 2. Put the API key in Secret Manager

> **Optional — the deployed shape does not use this.** §4 and §5 both run the
> model through Vertex and mount no API key at all. Skip to §3 unless you
> specifically want the AI Studio path.
>
> **If you have Cloud credits, read §7a first.** This section sets up an AI
> Studio API key, which bills through the Gemini Developer API and **does not
> draw on Google Cloud credits**. §7a routes the model through Vertex AI
> instead, using the service account you are about to create and no key file at
> all. You can do §2 now and switch later — nothing here is wasted — but if you
> already know you want the credits to pay for inference, skip to §7a and come
> back.

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
```

One identity runs both deployments, and it collects exactly six grants. Each is
made in the section that needs it rather than all at once here, so that reading
a section tells you what that section costs in privilege:

| Role | Scope | What it buys | Granted in |
|---|---|---|---|
| `roles/aiplatform.user` | project | call the model | §7a |
| `roles/storage.objectAdmin` | **the state bucket only** | the ledger, the approval store, the mail spool, the uploads | §4.1 |
| `roles/cloudsql.client` | project | open the session database | §4.2 |
| `roles/secretmanager.secretAccessor` | **each secret, one binding each** | the session URI, the user table, the invite code, the OAuth client secret, the SMTP password | §4.2, §4.4, §4.5 |
| `roles/run.invoker` | **the sweep Job only** | the desk's "Run the sweep now" button, and Cloud Scheduler | §4.6, §6 |
| `roles/run.viewer` | project | see whether a sweep is already running | §4.6 |

Two of those are worth naming out loud. `secretAccessor` is bound **per secret**
and never at the project — a service that can read every secret in a project is
not least privilege, it is a shorter command. And `run.viewer` is a
project-level *read* role because there is no per-job viewer; it is the smallest
thing that can list a job's executions, which is what stops the desk button from
starting a second sweep on top of a running one.

Nothing on that list can create or delete infrastructure, and none of it is an
owner or editor role. The service account can call a model, read five named
secrets, write one bucket, open one database and start one job — and that is the
complete list of things the deployed URL can cause to happen.

If you are taking the AI Studio path in §2 rather than Vertex, add that secret
too:

```bash
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor"
```

---

## 4. Deploy the service — one door

Everything a visitor can do lives on one URL behind one login: the homepage, the
operator desk, the chat, document upload, and the button that starts the sweep.
One process means one ledger, and one ledger is the point — a hold raised in
chat is the same row the desk approves, and the sweep's 06:00 holds are waiting
on the same screen.

Six things in order — the state bucket, the session database, the deploy itself,
the login, the mail transport, the IAM behind the sweep button — and then a
verification pass.

### 4.1 The state bucket — the one filesystem all the writers share

**Read this before you demo.** Every Cloud Run container gets its own
filesystem. The sweep Job writes its holds to `audit/ledger.jsonl` and
`data/approvals.json` *inside the Job's container*, which is destroyed when the
job exits. The Service reads those paths *inside its own container* and sees
nothing. Left alone, the deployed sweep and the deployed desk never meet — the
sweep reports "5 draft(s) held", the desk says the queue is clear, and the demo
has nothing to click.

One GCS bucket, mounted into both:

```bash
export BUCKET="${PROJECT_ID}-freight-state"
gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA" --role "roles/storage.objectAdmin"
```

**The Service mounts it read-write**, which is the change that made the
four-service shape unnecessary. Four different things write there: the desk when
a human approves (the approval store, then two ledger rows), the gate when chat
holds something, `/state/sent` when an approved message is spooled, and
`/state/uploads` when a judge uploads a document. A read-only mount would make
all four either impossible or a lie.

Both governance paths are env-driven in the code — `console.py` and `cli.py`
both read `FREIGHT_LEDGER_PATH` and `FREIGHT_APPROVALS_PATH` — so nothing needs
changing to support this.

**Two honest caveats, and one flag that follows from them.** GCS-FUSE is not a
POSIX filesystem: appends and renames work but are not atomic across writers. So
**`--max-instances 1` on the Service is load-bearing**, not a cost control — two
containers appending small ledger rows through GCS-FUSE lose writes, and this
repo has watched that happen once already (§5). And the store's
re-read-before-mutate (`FileApprovalStore._reload`) is what keeps the desk and
the sweep from clobbering each other's view; it was written for the local
two-process case and applies here unchanged. The sweep still does its many small
writes on local disk and publishes once at the end, for the same reason.

A third consequence worth knowing before you demo: GCS-FUSE caches metadata for
about 60 seconds on the reader, so a file copied into the bucket from *outside*
can take a minute to appear. Writes the container makes itself are visible to it
immediately; this only bites after a `gcloud storage cp`.

### 4.2 Sessions that survive the container — Cloud SQL for PostgreSQL

A judge signs in, asks about `shp-002-hero`, closes the tab, comes back and the
fleet still knows what it found. That needs a session store outside the
container, and on the chat it is a *judgeable* capability rather than an
operational nicety — a judge can see whether it remembers.

```bash
gcloud services enable sqladmin.googleapis.com

gcloud sql instances create freight-sessions \
  --database-version=POSTGRES_16 \
  --tier=db-f1-micro \
  --region="$REGION"

gcloud sql databases create sessions --instance=freight-sessions

# A URL-safe password on purpose: the URI below is parsed, not escaped, so a
# password containing @ / : or ? would silently split it in the wrong place.
export DBPASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
gcloud sql users create fleet --instance=freight-sessions --password="$DBPASS"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" --role="roles/cloudsql.client"

printf 'postgresql+asyncpg://fleet:%s@/sessions?host=/cloudsql/%s:%s:freight-sessions' \
  "$DBPASS" "$PROJECT_ID" "$REGION" \
  | gcloud secrets create freight-sessions-uri --replication-policy=automatic --data-file=-

gcloud secrets add-iam-policy-binding freight-sessions-uri \
  --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
```

Four things about that URI are load-bearing:

- **`postgresql+asyncpg://`** — `asyncpg` is a runtime dependency of this repo,
  not an extra. ADK's `DatabaseSessionService` opens the URI through
  SQLAlchemy's *async* engine, so a sync driver is not a slower option, it is a
  startup error — and a missing driver would otherwise surface at the first
  session write on Cloud Run, after the deploy looked successful.
- **`@/sessions?host=/cloudsql/PROJECT:REGION:INSTANCE`** — empty host, database
  name, and the unix socket as a query parameter. That is the form Cloud Run's
  built-in Cloud SQL connector exposes, and it is why no VPC connector, no proxy
  sidecar and no IP allowlist appear anywhere in this guide.
- **It must match the form `cli.py` uses.** `chat`, `sweep` and the deployed app
  all read `FREIGHT_SESSIONS_DB` and all open it through the same
  `DatabaseSessionService`, so one URI gives you one durable store shared by
  three entry points and no new code. The local default stays
  `sqlite+aiosqlite:///./data/sessions.db`. **Never a bare `sqlite://`** — ADK
  selects a *different* service for that scheme with a colliding table layout,
  so the failure presents as "my sessions are gone" rather than as a
  configuration error.
- **Without a URI, ADK silently falls back to in-memory sessions.** No warning,
  no error, and the demo works right up until the instance is recycled between
  the judge's two visits. The secret is not an optimisation; it is the feature.

**Cost.** `db-f1-micro` is roughly **$8–10 for a judging window**, and it is the
only line item in this guide that bills while nobody is looking — Cloud Run
scales to zero, Cloud SQL does not. §10 deletes it.

### 4.3 The deploy

From the repo root. Every flag is explained under it:

```bash
gcloud run deploy "$SERVICE" --region "$REGION" --source . \
  --service-account "$SA" \
  --command sh \
  --args='-c,uvicorn freight_fleet.webapp:app_factory --factory --host 0.0.0.0 --port ${PORT:-8080}' \
  --add-volume "name=state,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=state,mount-path=/state" \
  --add-cloudsql-instances "$PROJECT_ID:$REGION:freight-sessions" \
  --set-secrets "FREIGHT_SESSIONS_DB=freight-sessions-uri:latest,FREIGHT_CHAT_USERS=freight-chat-users:latest,FREIGHT_CHAT_ACCESS_CODE=freight-chat-access-code:latest" \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_AGENTS_DIR=/app/agents,FREIGHT_LEDGER_PATH=/state/ledger.jsonl,FREIGHT_APPROVALS_PATH=/state/approvals.json,FREIGHT_MAIL_SPOOL=/state/sent,FREIGHT_UPLOADS_DIR=/state/uploads,FREIGHT_MAIL_SINK=freightops.demo@gmail.com,FREIGHT_CHAT_URL=/chat,FREIGHT_GATED=1,FREIGHT_SWEEP_JOB=projects/$PROJECT_ID/locations/$REGION/jobs/$JOB,FREIGHT_SWEEP_SCHEDULE=weekdays 06:00 Europe/Athens,FREIGHT_CONTACT_EMAIL=freightops.demo@gmail.com,FREIGHT_REPO_URL=https://github.com/MDadopoulos/freightopsfleet,FREIGHT_GOOGLE_REDIRECT_URI=https://SERVICE-URL/auth/google/callback" \
  --memory 1Gi --cpu 1 --timeout 600 --max-instances 1 --min-instances 0 --concurrency 8 \
  --allow-unauthenticated
```

**Two chicken-and-egg problems, both harmless.** The two login secrets in
`--set-secrets` are created in §4.4, and `FREIGHT_GOOGLE_REDIRECT_URI` needs the
service URL that this command is about to print. On a first pass, either create
the secrets first, or drop them from the flag and add them afterwards with
`--update-secrets`; then come back and set the real redirect URI with
`--update-env-vars` once you know the URL. §4.4 has both commands.

What the flags are doing:

- **`--source .`** builds with Cloud Build using the repo's `Dockerfile`. No
  local Docker needed. `.dockerignore` keeps `eval/answer_keys/` — the answer
  keys — out of the build context entirely; only `eval/runs/`, the committed
  and already-graded run records, is copied in so the Scoreboard page renders
  (it lives outside the workspace jail, where no agent tool can reach it).
- **`--command sh --args='-c,uvicorn …'`** is the image's own default, stated
  explicitly so a `describe` shows what runs. See the note at the top of this
  document about the `=`.
- **`--add-volume` / `--add-volume-mount`** mount §4.1's bucket at `/state`,
  read-write.
- **`--add-cloudsql-instances`** is what mounts `/cloudsql/…` into the
  container. Without it the session URI's socket path does not exist and the
  first session write fails with a connection error that names a file, not a
  database.
- **`--set-secrets`** keeps the database password, the password hashes and the
  invite code out of `gcloud run services describe` output and out of your
  deployment history. `--set-env-vars` would put them in both, permanently.
- **No `GOOGLE_API_KEY`.** The model runs on Vertex through the attached service
  account (§7a): the container gets tokens from the metadata server, and there
  is no key to mount, leak or rotate.
- **`--timeout 600`** matters: a cross-check reads three documents and reasons
  over them, which can take 60–90 seconds. Cloud Run's 300s default would cut
  off a slow answer mid-stream.
- **`--memory 1Gi`** — ADK plus the Gemini SDK is comfortable here; 512Mi is
  tight enough to OOM under concurrency.
- **`--max-instances 1`** is the correctness flag, not the cost flag (§4.1), and
  it doubles as the spend cap: one container's worth of Gemini calls, however
  many people are signed in. **`--concurrency 8`** keeps that one container from
  queueing a crowd behind a 60-second cross-check. `access.py` also counts login
  failures per address *in this process*, which is the whole service precisely
  because there is only one instance.
- **`--allow-unauthenticated`** makes the URL clickable, and **the login is what
  makes that safe**. Cloud Run IAM cannot express "any judge with the code"; the
  app's own door can, and it is the same door for the desk, the chat and the
  uploads. `/`, `/privacy`, `/reconcile.json`, `/robots.txt` and `/healthz` are
  the only paths reachable without it.

The environment variables, since there are a lot of them:

| Variable | What it does |
|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | route every model call through Vertex on the attached service account — §7a |
| `FREIGHT_MODEL` | the model id every agent uses |
| `FREIGHT_AGENTS_DIR` | `/app/agents` — the folder *containing* `freight_ops/`, which ADK scans |
| `FREIGHT_LEDGER_PATH`, `FREIGHT_APPROVALS_PATH` | the governed record, on the bucket |
| `FREIGHT_MAIL_SPOOL` | where every delivered message is written; the **Sent** page reads it |
| `FREIGHT_UPLOADS_DIR` | the durable copy of uploads, restored into the workspace on start |
| `FREIGHT_MAIL_SINK` | where approved mail is actually delivered — §4.5 |
| `FREIGHT_CHAT_URL` | makes the desk link to "Ask the fleet"; `/chat` on this same host |
| `FREIGHT_GATED` | tells the console a login sits in front of it, so the nav offers **Sign out**. Display only — the console never checks a cookie itself |
| `FREIGHT_SWEEP_JOB` | the Job's full resource name; setting it is what renders the **Run the sweep now** button — §4.6 |
| `FREIGHT_SWEEP_SCHEDULE` | the cadence, verbatim, for display. The app cannot check it; §6 is where it is actually set |
| `FREIGHT_CONTACT_EMAIL` | the address on `/privacy` |
| `FREIGHT_REPO_URL` | the "Source" link on the homepage |
| `FREIGHT_GOOGLE_REDIRECT_URI` | the OAuth callback — §4.4 |

Two more, set on the live service with `--update-env-vars` (no rebuild):
`FREIGHT_PRICE_IN_PER_M=0.75` and `FREIGHT_PRICE_OUT_PER_M=3.75`, USD per
million tokens — Gemini 3.7 Flash's introductory Vertex AI rates, valid through
2026-12-31 (the standard rates from 2027-01-01 are 1.50 / 7.50). With both set,
the chat page turns its token counter into a dollar estimate at *your* rates;
with either unset it shows tokens only and says so. The page never invents a
price, which is why this is an env var and not a constant — a hardcoded price
would be wrong the week after it was written, and this one is wrong on New
Year's Day.

### 4.4 The login — who is allowed to spend the fleet's money

`access.py` is the whole access model, and it is what replaced IAP. Everything
behind `/` needs a name attached to it, because everything behind `/` can
approve a send, ask the fleet to spend money, or upload a document. There are
two ways to get a name, and a deployment switches each on by which variables it
sets:

| Set | What `/access` shows |
|---|---|
| `FREIGHT_CHAT_USERS` | the **demo login** panel — username and password |
| `FREIGHT_GOOGLE_CLIENT_ID` + `FREIGHT_GOOGLE_CLIENT_SECRET` + `FREIGHT_GOOGLE_REDIRECT_URI` | the **Google sign-in** panel |
| `FREIGHT_CHAT_ACCESS_CODE` *with* Google | an invite-code field **inside** the Google panel, checked before the redirect |
| `FREIGHT_CHAT_ACCESS_CODE` *alone* | a bare code form. This is the old IAP-era mode, where something in front already knew who you were; it pins no identity and is **not** the deployed shape |
| nothing at all | local development: the gate passes everything through and the desk is at `/desk` |

The deployed shape sets all three groups: two panels side by side, with the code
in front of the Google one.

#### Mint the demo users

```bash
# prints the JSON for the secret AND the passwords, once — there is no second chance
python -m freight_fleet.cli chat-users judge1 judge2 judge3 > chat-users.txt

# the JSON half becomes the secret; the passwords half goes in the submission form
sed -n '/^{/,/^}/p' chat-users.txt \
  | gcloud secrets create freight-chat-users --replication-policy=automatic --data-file=-
gcloud secrets add-iam-policy-binding freight-chat-users \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor
```

Some judges will not want their Google account in a stranger's database, and
that is a reasonable position — this is the courtesy exit for them. The secret
holds scrypt hashes and nothing else; the passwords exist only in
`chat-users.txt`, which you delete once the submission form has them. Rotate by
minting again: the cookie's signing key is *derived* from the user table, so a
new table kills every outstanding cookie with no second secret to manage.

The username **is** the identity here. It is pinned into every ADK `user_id` and
stamped on every ledger row that visitor decides, so `judge1` and `judge2` never
see each other's conversations and the record says which of them approved what.

#### Create the OAuth client — this is what replaced IAP

IAP is gone. It was a per-service switch that could not share a page with a
password form, its Google-managed OAuth client admits only identities *inside an
organization* (and this project has no organization, so nobody could sign in at
all), and it broke `gcloud run services proxy`. Our own OAuth client does the
same job on one page, and the page can offer both ways in.

1. Google Cloud console → **APIs & Services → Credentials → Create credentials →
   OAuth client ID → Web application**.
2. Authorised redirect URI: `https://<service-url>/auth/google/callback` —
   exactly, including the scheme, with no trailing slash. Google matches this
   string literally.
3. The consent screen already exists on this project: audience **External**,
   publishing status **In production**, support email
   `freightops.demo@gmail.com`, homepage `/` and privacy policy `/privacy`. Both
   of those pages are served by this app and reachable **without a login**, which
   is what makes the published state honest rather than a form filled in with a
   URL nobody can open.

Then store the secret half and hand the service all three values:

```bash
printf '%s' 'THE-CLIENT-SECRET' \
  | gcloud secrets create freight-google-client-secret --replication-policy=automatic --data-file=-
gcloud secrets add-iam-policy-binding freight-google-client-secret \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor

export URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')

gcloud run services update "$SERVICE" --region "$REGION" \
  --update-secrets FREIGHT_GOOGLE_CLIENT_SECRET=freight-google-client-secret:latest \
  --update-env-vars "FREIGHT_GOOGLE_CLIENT_ID=THE-CLIENT-ID.apps.googleusercontent.com,FREIGHT_GOOGLE_REDIRECT_URI=$URL/auth/google/callback"
```

The client **secret** goes through Secret Manager and the client **id** does
not: an OAuth client id is public by construction — it travels in the URL of
every sign-in — and hiding it would only make it harder to read back what is
deployed.

**Use the `--update-` forms for every change after the first deploy.**
`--set-secrets` and `--set-env-vars` replace the entire set; `--update-secrets`
and `--update-env-vars` merge. The Service carries three secrets and seventeen
environment variables, and retyping all of them correctly to change one is how
this deployment gets broken.

What the callback does when it comes back: the code is exchanged for an ID
token over TLS, and the token is checked for issuer, audience (it must have been
minted for *this* client — somebody else's client is still a valid Google
signature) and expiry. The verified, `email_verified` address is the identity.

#### The invite code

```bash
export CODE="FLEET-$(openssl rand -hex 3 | tr a-f A-F)-$(openssl rand -hex 3 | tr a-f A-F)"
printf '%s' "$CODE" \
  | gcloud secrets create freight-chat-access-code --replication-policy=automatic --data-file=-
gcloud secrets add-iam-policy-binding freight-chat-access-code \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor
echo "$CODE"   # goes in the submission form and nowhere else
```

Any Google account can sign in with Google; only an invited one should get to
put the fleet to work. The code is asked for **before** the redirect, so a
stranger cannot even make Google render a consent screen for this app. Five
wrong guesses from one address lock it out for fifteen minutes. Whitespace and
case are ignored — judges type these by hand.

What it is not: an identity. The verified email is the identity, so two judges
sharing one code still get their own sessions, their own history and their own
name on the rows they approve.

Rotating any of the three — the user table, the code, the client id — changes
the derived cookie key and invalidates every outstanding cookie. One rotation
mechanism for the whole door, and no second secret to keep in step.

### 4.5 Mail — where an approved send actually goes

`send_email` is the one tool whose mistake cannot be undone, so the model never
chooses where mail goes. The drafted `to` — a carrier, a shipper's agent — is
recorded as the **intended** recipient and is *never used as a delivery
address*. Delivery is to `FREIGHT_MAIL_SINK`, plus a copy to the approving human
when they signed in with Google and therefore have an address. A chat box that
could email an arbitrary address after one click would be a spam relay with a
nice UI.

Two transports, chosen with `FREIGHT_MAIL_TRANSPORT`:

- **`spool`** (the default, and what §4.3 deploys). Nothing leaves the project.
  Every approved message is written as one JSON file under `FREIGHT_MAIL_SPOOL`
  — `/state/sent` on the bucket — and the console's **Sent** page is the mailbox.
  Honest and free; it is what a deployment runs until somebody hands it SMTP
  credentials.
- **`smtp`** — real delivery, *then* the spool. Every transport spools, because
  the spool is the evidence: the ledger row says *that* a send ran, the spool
  says *what* left, to whom, approved by whom.

Real mail through Gmail, if you want an approval to land in an inbox on camera:

```bash
# On the sending Google account, 2-Step Verification must be ON; then create an
# App Password (Google Account → Security → App passwords). The account's normal
# password will not authenticate here.
printf '%s' 'THE-16-CHAR-APP-PASSWORD' \
  | gcloud secrets create freight-smtp-password --replication-policy=automatic --data-file=-
gcloud secrets add-iam-policy-binding freight-smtp-password \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor

gcloud run services update "$SERVICE" --region "$REGION" \
  --update-secrets FREIGHT_SMTP_PASSWORD=freight-smtp-password:latest \
  --update-env-vars "FREIGHT_MAIL_TRANSPORT=smtp,FREIGHT_SMTP_USER=freightops.demo@gmail.com"
```

`FREIGHT_SMTP_HOST` defaults to `smtp.gmail.com` and `FREIGHT_SMTP_PORT` to
`587` (STARTTLS), so neither needs setting for Gmail. `FREIGHT_MAIL_FROM`
defaults to the SMTP user.

**A missing sink or a missing password is an error *result*, not a silent
downgrade to `spool`.** The decision page then says `⚠ APPROVED, NOT DELIVERED`,
the grant is spent, and the ledger records the failure — because a send the
operator believed went out and did not is the worst outcome this module has.
Everything that does leave carries the `[Freight Ops demo]` subject prefix, the
intended recipient in the body and in an `X-Freight-Demo-Intended-To` header,
and a line saying every party in it is fictional. A real carrier must never
receive one of these, and a judge's inbox must never mistake one for real.

### 4.6 Letting the desk start the sweep

The desk renders a **Run the sweep now** button whenever `FREIGHT_SWEEP_JOB` is
set — §4.3 sets it to the Job's full resource name. `POST /sweep/run` then calls
the Cloud Run Jobs REST API with the service's *own* credentials, which is the
split that keeps the console's seal meaningful: `console.py` renders the button
and holds no credential, `webapp.py` holds the token.

Two grants, and the second is the one people forget:

```bash
# start the job
gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" \
  --member "serviceAccount:$SA" --role roles/run.invoker

# list its executions, so a second sweep is refused while one is running
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role roles/run.viewer
```

Without `run.viewer` the service cannot see that a sweep is already in flight,
and the button either fails or starts a second run that holds every draft twice
under a second set of approval ids. Two guards sit in the code as well: an
execution without a completion time means the POST refuses with `· ALREADY
RUNNING`, and a ten-minute cooldown means an impatient judge clicking four times
starts one sweep. Anything else comes back as `⚠ NOT STARTED`, with the reason
in the deployment log and nothing changed — the button never half-works.

The Job itself is §5. Deploy that before you press this.

### Verify it

The first build takes 3–6 minutes. When it finishes you get a URL:

```
Service URL: https://freight-ops-fleet-XXXXXXXX-ew.a.run.app
```

```bash
export URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')

curl -s -o /dev/null -w '%{http_code}\n' "$URL/"           # 200 — the homepage, public
curl -s "$URL/reconcile.json"                              # {"diverged":false,...}, public
curl -s -o /dev/null -w '%{http_code}\n' "$URL/privacy"    # 200 — the consent screen links here
curl -s -o /dev/null -w '%{http_code}\n' "$URL/desk"       # 303 — to /access?next=/desk
```

`/`, `/privacy`, `/reconcile.json`, `/robots.txt` and `/healthz` need no login —
the first two because Google's consent screen points at them, the rest because
they are probes. Everything else sends a logged-out browser to
`/access?next=<where they were going>` and refuses an API call with `403` JSON;
a websocket is closed with code `4403` after the handshake rather than left
hanging.

**Do not probe `/healthz` on the public URL.** Google's frontend reserves that
path on `run.app` hosts and answers its own 404, so the request never reaches
the container. The route is fine; the hostname is the problem.

Then open `$URL` in a browser. That is the demo surface: the homepage, **Sign in
→**, and the desk. §8 is the full smoke test.

---

## 5. Deploy the sweep as a Job

The sweep is not a web request — it is a scheduled batch run that must exit.
Cloud Run **Jobs**, not Services:

```bash
gcloud run jobs deploy "$JOB" --region "$REGION" --source . --service-account "$SA" \
  --command sh \
  --args="-c,cp /state/ledger.jsonl /app/audit/ledger.jsonl 2>/dev/null || true; cp /state/approvals.json /app/data/approvals.json 2>/dev/null || true; python -m freight_fleet.cli sweep; ec=\$?; cp /app/audit/ledger.jsonl /state/ledger.jsonl && cp /app/data/approvals.json /state/approvals.json; exit \$ec" \
  --add-volume "name=state,type=cloud-storage,bucket=$BUCKET" --add-volume-mount "volume=state,mount-path=/state" \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_LEDGER_PATH=/app/audit/ledger.jsonl,FREIGHT_APPROVALS_PATH=/app/data/approvals.json" \
  --memory 1Gi --cpu 1 --task-timeout 1800 --max-retries 0
```

**Read the wrapper left to right. It is four steps and each is there for a
reason.**

1. **Seed from the shared state.** The two leading `cp /state/… /app/…` lines
   pull the current ledger and approval store *into* the container before the
   sweep starts, so the run begins from what everyone else has already done.
2. **Sweep on local disk.** The Job's `FREIGHT_LEDGER_PATH` and
   `FREIGHT_APPROVALS_PATH` stay on `/app`, not on `/state`, deliberately: the
   sweep's agents append ledger rows concurrently and rewrite the approval store
   on every hold, and through GCS-FUSE those small writes are not atomic. A real
   run died on `OSError: [Errno 116] Stale file handle` with its holds lost from
   the store. Local disk is where many small writes belong.
3. **Publish once at the end.** Two copies back to `/state` — GCS-FUSE's happy
   path, and the only moment the shared record moves.
4. **`exit $ec`** keeps the sweep's own honest exit code, so a partial run is
   still reported as a failure even though the publish succeeded.

**The seed step is new, and it is a bug fix rather than a refinement.** The old
wrapper started from the image's empty ledger and *replaced* the shared state at
the end, so every sweep silently wiped whatever a judge had approved, rejected
or raised in chat since the last one. This guide used to carry that as a warning
— "run the sweep once, then decide; do not schedule it while a decided queue
matters" — which is a documented landmine, not a fixed one. Seeding first makes
a run additive: it reads what is already there, appends its own holds, and
publishes the union. The `|| true` on each seed copy is for the very first run,
when the bucket is empty and the files do not exist yet.

- **The `=` in `--args=` is load-bearing** — see the note at the top of this
  document. So are the backslashes in `\$?` and `\$ec`: those dollar signs must
  reach `sh` inside the container as literals, not be expanded by the shell you
  are typing into.
- **`--task-timeout 1800`** — six shipments at ~60s each, with headroom.
- **`--max-retries 0`** — the sweep is **not idempotent**: every run that reaches
  a shipment drafts a notice and holds it, so a second run holds a second copy
  of the same draft under a second approval id. It also exits non-zero when it
  *skips* a shipment rather than only when it dies — which is the honest signal,
  but it means Cloud Run would retry a run that already held five of six drafts
  and hand the operator five duplicates. Retrying is the wrong response to a
  partial sweep. A failed execution is visible in the logs; re-run it by hand
  after reading them, once you know which shipments actually got through.
- **No `--set-secrets`.** Like the Service, the Job is Vertex-only: the attached
  service account *is* the credential (§7a), so there is no key to mount.

**What the sweep drafts now is email.** It cross-checks each open shipment and,
where the documents disagree, drafts the correction notice and calls
`send_email` — which is CRITICAL with an external side effect in
`governance.policy`, so the gate holds every one of them and nothing is sent. It
no longer writes drafts to `outbox/`; a held file was a weaker demonstration
than a held email, because a file has no recipient to get wrong. Those holds are
what the desk approves, and §4.5 is where an approved one actually goes.

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
  !! 1 of 6 shipment(s) were NOT checked: shp-005-air-dg
```

Once §4.6's two IAM bindings are in place, exactly this run is also one click
from the desk — which is the version a judge can drive themselves.

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

That is the same binding §4.6 makes for the desk's **Run the sweep now** button
— one service account, one grant, two callers — so if you have already done §4.6
this is a no-op.

`0 6 * * 1-5` is 06:00 on weekdays. Freight desks do not sweep on Sunday.

**The time is yours, not the app's.** Nothing in the code knows about 06:00 —
the sweep is a command, and *when* it runs is operator policy expressed here,
in Cloud Scheduler. Change the cadence by editing this one job
(`gcloud scheduler jobs update http freight-ops-morning-sweep --schedule "..."`),
per deployment, per customer. There is deliberately no schedule setting inside
the console: a screen that could edit the schedule would need credentials that
mutate infrastructure, and the console's safety claim is that it holds none.
The desk *displays* the cadence read-only from `FREIGHT_SWEEP_SCHEDULE` (set in
§4.3) — if you change the cron here, update that env var to match, because the
console repeats what you tell it and cannot check. The desk's button (§4.6) is
the manual complement to this schedule, not a second one: it starts the same
Job, and the Job holds the same drafts either way.

Force a firing to prove the wiring without waiting for morning:

```bash
gcloud scheduler jobs run freight-ops-morning-sweep --location "$REGION"
```

---

## 7. Optional: durable conversations (Cloud SQL or Agent Engine)

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

**The deployment takes option (a), and §4.2 has it concretely** — the Cloud SQL
instance, the `freight-sessions-uri` secret and the exact URI form. Durable
sessions are worth their cost where they are *judgeable*, and the chat is where
they are: a judge signs in, asks about a shipment, comes back after the instance
has been recycled, and the fleet either remembers or it does not — and they can
see which. For the sweep and the CLI alone, neither option would add a
capability a video can show; the kill-restart-resume property is already proven
locally.

Option (b) stays skipped. Agent Engine would add `google-cloud-aiplatform` plus
a regional resource to provision and delete, for the same durability §4.2 gets
from a database this repo's code already opens.

---

## 7a. Run the model through Google Cloud (Vertex AI) instead of AI Studio

**Do this if you were granted Cloud credits.** An AI Studio `GOOGLE_API_KEY`
bills through the Gemini Developer API, which is a **separate billing path from
Google Cloud** — your Cloud credits do not pay for it. Cloud Run, Secret
Manager and GCS are covered by credits; the model calls are not, until you move
them to Vertex.

### You do not need a JSON key. Please do not create one.

This is the part worth being clear about, because a service-account JSON key is
the intuitive answer and it is the wrong one in both places you need
credentials:

- **On Cloud Run**, the service account you already attach with
  `--service-account "$SA"` *is* the identity. The container gets tokens from
  the metadata server automatically. A key file would add a long-lived secret
  that has to be stored, mounted and rotated, in exchange for nothing.
- **On your laptop**, `gcloud auth application-default login` writes
  short-lived user credentials that the SDK finds the same way. Also no file to
  guard.

A JSON key is a permanent credential that works from anywhere it is copied to,
including a public repo, a Docker layer or a screen-shared terminal. It is the
single most common way cloud projects get compromised, and nothing in this
deployment needs one. `GOOGLE_APPLICATION_CREDENTIALS` stays unset.

One place sets that variable and does not break the rule: GitHub Actions. The
file `google-github-actions/auth` writes there is an *external account*
configuration holding no key and no secret, worthless off the runner and expired
with the job — §11 spells out why that is federation rather than a key.

### How the SDK decides — verified against the installed library

`google-genai` picks its backend from the environment, so **no code in this repo
changes**; `GOOGLE_API_KEY` appears nowhere in `src/` except one docstring. The
resolution order below is read from `google/genai/_api_client.py` in this repo's
own venv, not from memory:

| Variable | Effect |
|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | `true`/`1` (case-insensitive) switches to Vertex |
| `GOOGLE_CLOUD_PROJECT` | the project; if unset, taken from ADC |
| `GOOGLE_CLOUD_LOCATION` | the endpoint; **if unset, the SDK defaults to `global`** |

With Vertex on and no api_key, the client calls
`google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])`
— that is ADC, and that is why the attached service account is enough.

**Unset `GOOGLE_API_KEY` when you switch.** With both set, the SDK applies a
precedence rule (env project/location wins over env api_key) and logs which one
it chose. It will probably do what you want, but "probably" is a bad property
for a billing path — remove the key so there is only one answer.

### One-time project setup

```bash
gcloud services enable aiplatform.googleapis.com

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA" \
  --role "roles/aiplatform.user"
```

`roles/aiplatform.user` is what lets the service account call models. It does
not grant it the ability to create or delete Vertex resources.

### Test it locally first

Do this before redeploying anything — it is a 60-second check that separates
"my flags are wrong" from "my deployment is wrong":

```bash
gcloud auth application-default login          # ADC on your laptop
gcloud config set project "$PROJECT_ID"

unset GOOGLE_API_KEY                            # only one billing path at a time
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GOOGLE_CLOUD_LOCATION=global

.venv/bin/python -c "
from google import genai
c = genai.Client()
print('backend vertexai =', c._api_client.vertexai)
print(c.models.generate_content(model='gemini-3.7-flash',
      contents='Reply with exactly: VERTEX OK').text)
"
```

If that prints `VERTEX OK`, the whole fleet works — every agent goes through the
same client. Then prove it end to end on one shipment:

```bash
FREIGHT_WORKSPACE_ROOT=$PWD/workspace .venv/bin/python eval/run_eval.py
```

**If it fails on the model name**, that is the one thing this document cannot
promise, because the identifier Vertex publishes can differ from the Developer
API's and can vary by endpoint. Do not guess — ask:

```bash
gcloud ai models list --region us-central1 | grep -i gemini
```

and set `FREIGHT_MODEL` to what it reports. If a regional endpoint is required
for the model, set `GOOGLE_CLOUD_LOCATION` to that region instead of `global`.
Whatever you end up with, **re-run the eval before the demo** — a model swap is
a behaviour change, and the 7/7 in the README was measured on the Developer API.
If the score moves, that is a finding to report, not a number to quietly update.

### Deploy with it

**Both deployments in this guide are already Vertex-only.** §4.3 and §5 set
`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION`, and neither mounts an API key — so there is nothing to
swap. This section is the *why*; §3's `roles/aiplatform.user` is the grant that
makes it work.

If you deployed the AI Studio path first and are switching now, the Job is
simple, because its whole environment fits in one flag:

```bash
gcloud run jobs update "$JOB" --region "$REGION" \
  --clear-secrets \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_LEDGER_PATH=/app/audit/ledger.jsonl,FREIGHT_APPROVALS_PATH=/app/data/approvals.json"
```

(The Job keeps the *local* paths — §5 explains the seed-and-publish wrapper.)

The Service is not simple, and this is where people break it. `--clear-secrets`
would also unmount the session URI, the user table and the invite code, and
`--set-env-vars` would drop the seventeen variables §4.3 set. Remove only the key,
and merge:

```bash
gcloud run services update "$SERVICE" --region "$REGION" \
  --remove-secrets GOOGLE_API_KEY \
  --update-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global"
```

`--set-*` replaces the entire set; `--update-*` merges. That one distinction is
the most common way this deployment loses its login halfway through an unrelated
change.

### Then delete the key

Once the eval passes on Vertex, the AI Studio key is an unused credential with
live billing attached:

```bash
gcloud secrets delete gemini-api-key            # after the eval passes, not before
```

and revoke it in AI Studio. An unused key that still works is the one nobody
notices has leaked.

---

## 8. Smoke-testing the deployment

Run these in order. Each one fails loudly and tells you which step to go back
to, so do not skip ahead when one is red. Steps 1–2 and 5 are `curl`; the rest
need a browser, because the thing being tested is a login.

**1. The service is up, and the door is a door.**

```bash
export URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')

curl -s -o /dev/null -w '%{http_code}\n' "$URL/"                 # 200 — the homepage
curl -s -o /dev/null -w '%{http_code}\n' "$URL/desk"             # 303 — to /access?next=/desk
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL/upload"   # 403 — JSON "login required"
```

(Not `/healthz` — Google's frontend swallows that path on `run.app` hosts; see
Troubleshooting.) A **200 on `/desk`** from a cookieless `curl` means no
credentials reached the container, so the gate is in its pass-everything local
mode. Look at what is actually mounted:

```bash
gcloud run services describe "$SERVICE" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].env)'
```

**2. The record and the queue agree.** This is the governance healthcheck, and
it is the one worth watching:

```bash
curl -s "$URL/reconcile.json" | python3 -m json.tool
# expect "diverged": false
```

`diverged: true` right after a deploy means the mount is wrong — usually the
Service and the Job pointed at different paths. It is not a code failure.

**3. Both ways in work.** Open `$URL`, click **Sign in →**, and use one of the
demo usernames from §4.4. Then, in a private window, use the Google panel with
the invite code. Both should land on `/desk`, and the nav should offer **Sign
out** — that is `FREIGHT_GATED=1` doing its one job. A missing panel means its
variables are not all set; a `redirect_uri_mismatch` means the callback URL
registered on the OAuth client is not byte-identical to
`FREIGHT_GOOGLE_REDIRECT_URI`.

**4. Every screen renders.** Signed in, walk `/desk`, `/ledger`, `/fleet`,
`/evidence`, `/sent` and `/chat`. A 500 here is almost always a missing
artifact, not a bug.

**5. The fleet actually reasons, unattended.** The first step that spends money,
so a failure here is credentials or model name, not plumbing:

```bash
gcloud run jobs execute "$JOB" --region "$REGION" --wait
gcloud run jobs executions logs read \
  "$(gcloud run jobs executions list --job="$JOB" --region="$REGION" \
      --limit=1 --format='value(name)')" --region "$REGION"
```

Two lines in that output matter — the tally, and the skip line if there is one:

```
  5 draft(s) held for approval; nothing sent, nothing written.
  !! 1 of 6 shipment(s) were NOT checked: shp-005-air-dg
```

**6. The holds reached the desk.** Refresh `/desk`: the drafts the Job just held
are in the queue. If it is still clear, the Job and the Service are not sharing
a bucket — go back to §4.1. This is the most common cloud-only failure there is.

**7. A decision executes, once, and something actually leaves.** Open a held
`send_email`, read the draft and the evidence — the documents the agent read
before drafting — and approve it. You want:

- the strip saying `✓ APPROVED — send_email executed`, with a link to **Sent**;
- `/sent` showing that message with **intended** and **delivered** recipients as
  two different things;
- `/ledger` showing `held`, then `approved`/`executed`, carrying **your**
  identity, not the word "operator";
- and a second approval of the same id refusing with `· ALREADY DECIDED`,
  because the grant is single-use.

`⚠ APPROVED, NOT DELIVERED` means the SMTP transport is on and its credentials
or sink are wrong — §4.5. On the default `spool` transport this cannot happen.

**8. A chat hold lands on the same desk.** Open `/chat`, ask the fleet for
something that ends in a notice being sent, and watch the gate stop it. Then go
back to `/desk`: the hold is in the same queue as the sweep's, with your name on
it. **This is the check the old four-service shape could not pass**, and it is
the single best thirty seconds of the demo.

**9. An upload survives a restart.** Upload a PDF from `fixtures/raw/` through
`/chat`, watch it get transcribed, and ask the fleet to read it back. Then force
a new revision and confirm it is still there — the durable copy under
`/state/uploads` is restored into the workspace on container start:

```bash
# any env-var change makes a new revision; the app ignores this one entirely
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars "FREIGHT_DEPLOY_NONCE=$(date +%s)"
```

**10. The button starts the job.** Press **Run the sweep now** on the desk. You
want `✓ SWEEP STARTED`; press it again immediately and you want `· ALREADY
RUNNING`. A `⚠ NOT STARTED` is IAM — §4.6 — and the reason is in the log:

```bash
gcloud run services logs read "$SERVICE" --region "$REGION" --limit 50
```

**11. Sessions survive the container.** After step 9's restart, sign back in and
confirm the conversation from step 8 is still listed. That round trip is the
only check that proves `FREIGHT_SESSIONS_DB` reached the container rather than
ADK falling back to in-memory sessions in silence.

---

## 9. Running the demo

The five minutes, in the order that makes the argument. One browser window on
the service URL is all you need now — there is no second URL and no tunnel.

**Before you start recording**

- Run the sweep once (§8 step 5, or the desk button) so the queue has real
  holds. A demo that begins with an empty queue spends its first minute creating
  one.
- Sign in beforehand and leave the tab open. The login is worth twenty seconds
  on camera, not sixty.
- `curl -s -o /dev/null "$URL/"` — a cold start takes a few seconds and you do
  not want that pause on camera.

**The run of show**

1. **The front door (~20s).** `/` — what this is in five sentences, and one
   button. Sign in with a demo username. "No Google account needed. The username
   is the identity here, and it goes on every row I decide."
2. **The desk (~40s).** Point at the pending count. "Five drafts are waiting.
   Nobody was watching when they were written — a scheduled job at 06:00
   cross-checked six shipments and stopped at the gate."
3. **One decision (~60s).** Open a held `send_email`. Show the drafted notice,
   the contract that held it, and the *evidence* — the documents the agent read
   before drafting. Approve it.
4. **What actually left (~30s).** `/sent`. The message, with its **intended**
   recipient and its **delivered** recipient side by side. "The model drafted a
   carrier's address. It was never used as one. Delivery goes to the demo
   mailbox and to the person who approved it."
5. **Ask the fleet (~60s).** `/chat`. Ask something that ends in a send, and
   watch the gate hold it — then go back to `/desk` and show that hold sitting
   in the same queue as the sweep's. "Same gate, same ledger, whether a schedule
   raised it at 06:00 or I raised it just now."
6. **The record (~40s).** `/ledger`. Every call the fleet made, with the verdict,
   the outcome and who decided. Point at the sha256 of a written file: "you can
   recompute this with `shasum -a 256`."
7. **The scoreboard (~40s).** `/evidence`. 7/7, three runs of three, and the
   clean control — the shipment with nothing wrong with it, graded with zero
   tolerance. "A missed discrepancy costs a correction. A fabricated one costs
   trust."
8. **The close (~30s).** `/fleet` — five desks, and the tool each is allowed.
   "One gate, one ledger, one eval. The governance isn't five prompts asking
   nicely; it's one code path every tool call goes through."

**If the cut allows more than five minutes**, three additions earn their seconds
and none belongs in the eight above — the run of show is an argument, and these
are supporting evidence rather than steps in it:

- **Upload a document (~30s).** Drop a scan into `/chat`, watch it get
  transcribed on the spot, and ask a question about it. It is the fastest way to
  show the fleet reading something the *judge* chose.
- **Run the sweep now (~20s).** Press the desk button and let the flash say
  `✓ SWEEP STARTED`. It makes the unattended half something a judge can start
  themselves, which is worth more than a screenshot of Cloud Scheduler.
- **The front door in a terminal (§1a, ~40s).** `ingest --dry-run` to show 26
  PDFs and scans planned, the real run on one file, and the resulting
  `inbox/*.md` with its `<!-- transcribed ... -->` first line. This is the one
  paid step an operator runs by hand, which is why it is a terminal and not a
  URL.

**What to say if something breaks on camera.** The honest line is the strong
one: the URL is the demo surface, not the proof. The ledger and the scoreboard
run identically on a laptop, and both are in the repo. A cold start that takes
eight seconds does not weaken the argument.

---

## 10. Locking it down after the hackathon

What is left running is a public URL with a login in front of it and a live
model behind it, a database that bills while idle, and a sweep that spends money
on a schedule. When the judging window closes, close it.

### The complete shutdown

Deleting the two deployments stops every recurring cost except the bucket:

```bash
gcloud run services delete "$SERVICE" --region "$REGION"
gcloud run jobs delete "$JOB" --region "$REGION"
gcloud scheduler jobs delete freight-ops-morning-sweep --location "$REGION"

# THIS is the line item that bills while nobody is looking — Cloud Run scales
# to zero, Cloud SQL does not.
gcloud sql instances delete freight-sessions
gcloud secrets delete freight-sessions-uri      # now a live password for nothing
```

Then the credentials that outlive their service. Delete the OAuth client in
**APIs & Services → Credentials**: an unused OAuth client with a live secret is
the same category of thing as the unused API key §7a tells you to revoke. Same
for `freight-smtp-password` if you set up SMTP — a Gmail App Password that still
works is a credential nobody is watching.

### Or: keep it up, and shut the door

Revoke the credentials rather than the service. Rotating or removing any one of
the three invalidates every outstanding cookie, because the cookie's signing key
is derived from all of them:

```bash
# retire the demo logins
gcloud run services update "$SERVICE" --region "$REGION" --remove-secrets FREIGHT_CHAT_USERS

# and/or retire Google sign-in
gcloud run services update "$SERVICE" --region "$REGION" \
  --remove-secrets FREIGHT_GOOGLE_CLIENT_SECRET \
  --remove-env-vars FREIGHT_GOOGLE_CLIENT_ID,FREIGHT_GOOGLE_REDIRECT_URI
```

Removing **both** leaves the invite code alone in front of the app, which is the
old IAP-era mode and pins no identity — so remove the code too, or the service
becomes a shared-password box. Deleting the Cloud Scheduler job and unsetting
`FREIGHT_SWEEP_JOB` is what stops it spending; the login stops strangers, not
the schedule.

Leave `$BUCKET` alone if you want to keep the ledger the demo was built on — it
holds a few hundred KB and costs approximately nothing, and it is the only copy
of what happened.

`eval.yml`'s `schedule:` also keeps spending after the window closes. Comment it
out (§11) or the Monday-morning eval bills you for a repository nobody is
judging any more.

### Tearing down the old four-service shape

If you deployed an earlier version of this guide, these still exist and some of
them are still open to the internet. They are no longer part of the deployment.
Do IAP's IAM removal **first**, before the service it protects is deleted, so
there is no window where a binding outlives the thing you were watching:

```bash
gcloud iap web remove-iam-policy-binding \
  --resource-type=cloud-run --service=freight-ops-chat --region="$REGION" \
  --member=allAuthenticatedUsers --role=roles/iap.httpsResourceAccessor

gcloud run services delete freight-ops-chat       --region "$REGION"   # the chat behind IAP
gcloud run services delete freight-ops-chat-demo  --region "$REGION"   # the chat with a demo login
gcloud run services delete freight-ops-sandbox    --region "$REGION"   # the public sandbox
gcloud run services delete freight-ops-console    --region "$REGION"   # the private ops console

gcloud storage rm --recursive "gs://${PROJECT_ID}-freight-sandbox"     # the sandbox's disposable bucket
```

Then delete **IAP's** OAuth client — the one whose authorised redirect URI was
`https://iap.googleapis.com/v1/oauth/clientIds/…:handleRedirect`. It is a
different client from the one §4.4 creates, and leaving it is leaving a live
secret behind. `allAuthenticatedUsers` bindings carry no expiry condition;
nothing removes them for you.

---

## 11. CI and the eval in GitHub Actions

Two workflows, and the split between them is the security design.

| File | Triggers | Costs money | What it does |
|---|---|---|---|
| `.github/workflows/ci.yml` | `push` to `main`, `pull_request` | no | ruff, pytest on Python 3.11 and 3.12, and `scripts/render_documents.py --check` |
| `.github/workflows/eval.yml` | `workflow_dispatch`, `schedule` | **yes** | seeds a workspace and runs `eval/run_eval.py` against Gemini on Vertex |

**Why the eval is a separate file rather than a job with an `if`.** A workflow a
pull request can trigger must never be able to reach a service account, and the
cheapest way to guarantee that is for the two never to share a file. `ci.yml`
has `permissions: contents: read` and no `id-token: write` — it cannot mint an
OIDC token at all, so there is nothing for a malicious PR to steal. `eval.yml`
has no `pull_request` trigger and no `pull_request_target`; its absence is the
enforcement. (Both files say so in comments, which means a naive
`grep pull_request .github/workflows/eval.yml` *hits* — on the comment
explaining there is no such trigger. Check the `on:` keys, not the text.)

`ci.yml` also runs the fixture seal. If `render_documents.py --check` goes red
on the ubuntu runner while it is green on your machine, the fix is a **pin** in
`pyproject.toml`'s `render` extra, not a looser comparison — a seal that
tolerates drift reports nothing.

### One-time Workload Identity Federation setup

The eval authenticates to Google Cloud with **no JSON key anywhere**. GitHub
mints a short-lived OIDC token for the run; STS exchanges it for a Google access
token, but only if the token's claims match a condition you set here.

```bash
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  aiplatform.googleapis.com

# Its own identity, with exactly one role.
gcloud iam service-accounts create freight-eval \
  --display-name="Freight Ops eval (GitHub Actions)"
export EVAL_SA="freight-eval@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$EVAL_SA" \
  --role="roles/aiplatform.user"

gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc freightopsfleet \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.event_name=assertion.event_name" \
  --attribute-condition="assertion.repository_id == '1339530558' && assertion.repository_owner_id == '71187766' && assertion.event_name in ['workflow_dispatch','schedule']"

export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

gcloud iam service-accounts add-iam-policy-binding "$EVAL_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository_id/1339530558"

# Paste this exact string into eval.yml's `workload_identity_provider:`.
gcloud iam workload-identity-pools providers describe freightopsfleet \
  --location=global --workload-identity-pool=github --format='value(name)'
```

**Why a second service account and not `freight-fleet@`.** The runtime SA holds
`roles/storage.objectAdmin` on the state bucket (§4.1). CI must not be able
to touch the audit record — an eval that could rewrite the ledger it is
measuring is not evidence of anything. `freight-eval@` holds
`roles/aiplatform.user` and nothing else, which is precisely the permission "may
call a model" and no other.

**Why the condition names numeric ids and not `MDadopoulos/freightopsfleet`.**
This repository was created after GitHub's 2026-07-15 cutoff, so its OIDC
`sub` claim uses the immutable-id format; conditions written against the
*name* claims are the ones that quietly fail to match. `repository_id` and
`repository_owner_id` are also the claims that cannot be spoofed by renaming
anything: a fork carries a different `repository_id` and is refused at the STS
exchange, before any Vertex call happens. Pinning `event_name` to
`workflow_dispatch` and `schedule` means that even if a `pull_request` trigger
were ever added by accident, the token it produced would still be refused.

Find the two numbers with:

```bash
gh api repos/MDadopoulos/freightopsfleet --jq '{repo: .id, owner: .owner.id}'
```

`eval.yml` also hardcodes `GOOGLE_CLOUD_PROJECT` and the
`freight-eval@...iam.gserviceaccount.com` address, and guards the job with
`if: github.repository == 'MDadopoulos/freightopsfleet'`. That last line is
belt-and-braces — the provider condition already refuses a fork — but it makes a
fork's run fail instantly and legibly instead of failing halfway through an auth
step.

### `GOOGLE_APPLICATION_CREDENTIALS` in CI, and why §7a still says "stays unset"

`google-github-actions/auth` writes a credential file into the runner's
workspace and exports `GOOGLE_APPLICATION_CREDENTIALS` pointing at it. That is
not a contradiction of §7a. The file is an **external account** configuration:
it contains the provider path, the service account address, and instructions for
where to fetch the runner's OIDC token from. It holds **no key and no secret**,
it is worthless off that runner, and it expires with the job. §7a's rule — no
service-account JSON key, ever, on a laptop or in an image — is unchanged, and
this is the mechanism that makes keeping it cheap.

### Before the repository goes public

Three settings and one edit, in the order they matter:

1. **Settings → Actions → General → "Require approval for all external
   contributors."** The default lets a first-time contributor's PR run workflows
   after one approval; this makes every fork PR wait for a maintainer. `ci.yml`
   has no credentials to steal, but a fork PR can still burn runner minutes and
   read whatever the workflow prints.
2. **Pin the actions to commit SHAs.** `actions/checkout@v7` is a *tag*, and a
   tag can be moved. `uses: actions/checkout@<sha>  # v7.0.0` cannot. Do this
   before the repo is public, not after — a supply-chain change to a popular
   action is exactly the kind of thing that lands between a demo and judging.
3. **Comment out `eval.yml`'s `schedule:` block until the recording is done.**
   A weekday-morning eval is the right steady state — a regression shows up as a
   red run on a date rather than as a surprise the night before judging — but it
   spends real money on a cadence you are not watching yet. Put it back
   afterwards; a commented-out schedule that never returns is just a deleted one.

### The run records stay hand-committed

`eval.yml` uploads its record as an artifact (90 days) and writes a scoreboard
into the job summary. It does **not** commit anything to `eval/runs/` — its
`contents: read` permission could not, and that is deliberate. A bot appending
run records would turn the committed evidence into a log nobody reads, and the
README's "7/7, three runs of three" is a claim a human decided to make. Download
the artifact, look at it, and commit the record if it is one you want to stand
behind.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/healthz` returns Google's HTML "404!!1" page while other routes work | Google's frontend reserves `/healthz` on `run.app` hosts — the request never reaches the container | Probe `/` or `/reconcile.json` instead. The route itself is fine and answers anywhere the hostname is not `*.run.app`. |
| `404` on `/list-apps`, `/run_sse` or `/dev-ui/` | `FREIGHT_AGENTS_DIR` points at the wrong folder | It must be `/app/agents` — the folder *containing* `freight_ops/`, not the agent folder itself. |
| `No module named freight_fleet` | `pip install .` ran before `src/` was copied | Keep the Dockerfile's COPY order: `pyproject.toml` + `src/`, then install. |
| The **Google sign-in** panel is missing from `/access` | The panel renders only when all three of `FREIGHT_GOOGLE_CLIENT_ID`, `FREIGHT_GOOGLE_CLIENT_SECRET` and `FREIGHT_GOOGLE_REDIRECT_URI` are set — one missing and it silently does not appear | `gcloud run services describe "$SERVICE" --format='value(spec.template.spec.containers[0].env)'`, then §4.4. The same is true of the demo-login panel and `FREIGHT_CHAT_USERS`. |
| Google answers `redirect_uri_mismatch` | The callback registered on the OAuth client is not byte-identical to `FREIGHT_GOOGLE_REDIRECT_URI` | Register `https://<service-url>/auth/google/callback` exactly — scheme, host, no trailing slash — and set the env var to the same string. Nothing else produces this error. |
| The desk opens with no sign-in at all | Nothing is configured, so the gate is in `off` mode and passes everything through | That is the local-development shape, not a deployment. Mount `freight-chat-users` and/or the Google client — §4.4. |
| `/access` shows only a bare "enter the access code" form | `FREIGHT_CHAT_ACCESS_CODE` is set but neither users nor Google are | Code-alone is the old IAP-era mode, where something in front already knew who you were. It pins no identity, so ledger rows lose their name. Add a login — §4.4. |
| A decision page says `⚠ APPROVED, NOT DELIVERED` | `FREIGHT_MAIL_TRANSPORT=smtp` and the credentials or the sink are wrong. The grant is spent and the message did not leave — deliberately not a silent downgrade to `spool` | Check `FREIGHT_SMTP_USER`, the `freight-smtp-password` secret (a Gmail **App Password**, which requires 2-Step Verification on that account) and `FREIGHT_MAIL_SINK` — §4.5. |
| **Run the sweep now** flashes `⚠ NOT STARTED` | The service cannot start or cannot list the Job | Both bindings are needed: `roles/run.invoker` on the Job **and** `roles/run.viewer` on the project — §4.6. The reason is in `gcloud run services logs read "$SERVICE" --region "$REGION"`. |
| **Run the sweep now** flashes `· ALREADY RUNNING` when nothing looks like it is | The ten-minute cooldown, or an execution that has not reported completion yet | Wait. `gcloud run jobs executions list --job="$JOB" --region="$REGION"` says which of the two it is. |
| A sweep wiped decisions a judge had already taken | The **old** wrapper started from the image's empty ledger and *replaced* the shared state at the end | Redeploy the Job with the seeded wrapper in §5 — the two `cp /state/… /app/…` lines at the front are the entire fix. |
| Desk shows an empty queue right after the sweep held drafts | Job and Service have separate filesystems | Mount one bucket into both — §4.1. This is the most common cloud-only failure. |
| `reconcile.json` says `diverged: true` on a fresh deploy | Service and Job reading different paths | The Service uses `/state/...`; the Job uses `/app/...` and publishes to `/state` at the end. Check both env sets against §4.3 and §5. |
| Sweep dies with `FileNotFoundError: .../workspace/shipments` | Image built before the Dockerfile's seed step — `/app/workspace` is empty | Rebuild with the current Dockerfile (it runs `seed_workspace.py --all` at build time), then redeploy both the Service and the Job from the same source. |
| Uploads vanish after a cold start | `FREIGHT_UPLOADS_DIR` unset, so nothing was copied to the bucket | Set it to `/state/uploads` — §4.3. The workspace itself is always ephemeral; the durable copy is what gets restored on start. |
| Chat sessions vanish between visits | `FREIGHT_SESSIONS_DB` unset, so ADK fell back to in-memory sessions with no warning | Mount `freight-sessions-uri` — §4.2 — and confirm with `gcloud run services describe`. |
| `Failed to create database engine` | A sync database URL | An async driver is required: `postgresql+asyncpg://…` in the cloud, `sqlite+aiosqlite:///…` locally. Never a bare `sqlite://`. |
| `ValueError: No API key was provided` | The container has neither the Vertex variables nor a key | The deployed shape sets `GOOGLE_GENAI_USE_VERTEXAI=TRUE` and `GOOGLE_CLOUD_PROJECT` — §7a. Check them with `gcloud run services describe`. |
| 404 or `model not found` after switching to Vertex | Model id or endpoint differs on Vertex | §7a — set `FREIGHT_MODEL` from `gcloud ai models list`; try `GOOGLE_CLOUD_LOCATION=global` before a region. |
| `403 Permission denied` on a Vertex call | Service account lacks the role | `roles/aiplatform.user` on `$SA` — §7a. Also check `aiplatform.googleapis.com` is enabled. |
| `Could not resolve project using application default credentials` | No ADC on the machine | Locally: `gcloud auth application-default login`. On Cloud Run: the service was deployed without `--service-account`. |
| SDK logs that it chose one credential over another | Both `GOOGLE_API_KEY` and the Vertex variables are set | Expected precedence, but ambiguous billing. Remove the key — §7a. |
| Env vars or secrets you set earlier vanished after an update | `--set-env-vars` / `--set-secrets` replace the whole set | Use `--update-env-vars` / `--update-secrets` to merge. The Service carries three secrets and seventeen env vars; retyping them all to change one is how this breaks. |
| The desk shows old state for up to a minute after a `gcloud storage cp` into the bucket | GCS-FUSE metadata cache (60s TTL) on the reader | Wait a minute, or deploy a new revision. Writes the container makes itself are visible to it immediately. Not data loss. |
| Request dies at ~5 minutes | Cloud Run's default 300s timeout | `--timeout 600` — §4.3. |
| `gcloud run deploy` fails with no Dockerfile / nothing to build | Not run from the repo root | `--source .` uploads the current directory — `cd` into the clone first. See §0. |
| Agent answers "not found" for every document | Workspace never seeded | The image must contain `fixtures/`; check `.dockerignore` does not exclude it. |
| Agent answers `{"status": "binary"}` for a document | Correct behaviour: `read_file` reads only `.md`, `.csv` and `.txt` | Transcribe it first (§1a), or upload it through `/chat`, which transcribes on the spot. This is a refusal, not a failure. |
| `ingest` prints "nothing to ingest" | Workspace seeded without `--all`, so there is no `raw/` | `python scripts/seed_workspace.py --all` — §1a. `--dry-run` needs no credentials, so this is free to re-check. |
| Eval score moved after an `ingest --force` | The eval grades whatever the workspace holds and never seeds | `python scripts/seed_workspace.py --all --clean`, then re-run — §1a. |
| Sweep job succeeds but writes files or sends mail | Gate bypassed — **stop and investigate** | This must be impossible: a sweep holds every `send_email` and writes nothing. Read the ledger before deploying anything further. |
| The eval workflow fails at `google-github-actions/auth` | The WIF provider condition did not match the run's OIDC claims | Check `repository_id`, `repository_owner_id` and `event_name` against §11. A fork, or a `pull_request` run, is *supposed* to fail here. |

---

## What deploying does *not* prove

Worth saying plainly in the submission, because judges notice when a live URL is
doing less work than it appears:

- A deployed URL proves the fleet **runs** in a container. It does not prove the
  governance property — that is proven by the ledger and the scoreboard, both of
  which run identically on a laptop. The console makes both *legible* at the URL;
  it does not make either more true.
- The container's **workspace** is still ephemeral: the documents the fleet
  reads are baked into the image and reset with every container, and a judge's
  uploads survive only because §4 copies them to the state bucket and restores
  them on start. What *is* durable is the part that matters — the ledger, the
  approval store and the mail spool all live on that bucket, so a decision taken
  at the URL survives the cold start after it. The local CLI and the deployed
  desk are now the same approval surface over the same record, which is the one
  thing the four-service shape could not say.
