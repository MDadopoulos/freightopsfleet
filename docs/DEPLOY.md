# Deploying to Google Cloud

Three kinds of thing get deployed, from **one image**:

| What | Cloud Run kind | Why it exists |
|---|---|---|
| **The operator console** | Service (a live URL) | The image's default `CMD`. A judge clicking the URL lands on the Desk — the pending count, the decision queue, the ledger, the catalog and the scoreboard — with **no `GOOGLE_API_KEY` required**, no model call, and therefore no way to burn quota or 500 on credentials. §4 deploys it read-only, §4a again behind IAM where the buttons work, §4c a third time as a public sandbox. |
| **The morning sweep** | Job + Cloud Scheduler | The track's async requirement: it runs at 06:00 with nobody watching, finds discrepancies, and **holds** every draft. |
| **The chat surface** | Service behind IAP | §4d. ADK's dev UI, so a judge can *ask* the fleet something instead of only reading what it already did — behind Google sign-in, with per-user sessions in Cloud SQL that survive the container. It is the only surface a stranger can spend model tokens on, which is why it is the only one that requires a sign-in. |

> **The default `CMD` changed.** It is now
> `uvicorn freight_fleet.console:app --host 0.0.0.0 --port ${PORT:-8080}`.
> To serve ADK's API instead, override it:
> `--command sh --args="-c,adk api_server --host 0.0.0.0 --port \${PORT:-8080} /app/agents"`
> (the `=` is load-bearing — see §5).
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
> a `--min-instances 1` service afterwards would be. The one exception is §4d's
> Cloud SQL instance: roughly $8-10 for a judging window, and it bills while
> idle, because a database does not scale to zero. §10 deletes it.

---

## 0. Before you start

### Yes, you need the repo on the machine you deploy from

Three commands in this guide use `gcloud run deploy --source .` (§4, §4a, §5).
That uploads **your current directory** to Cloud Build, which builds the image
from the repo's `Dockerfile`. There is no "deploy from GitHub" shortcut here —
if the working directory is not the repo, the build has nothing to build.

The steps that need the repo present: **§4, §4a, §5** (the three `--source .`
deploys), **§7a's local test**, and **§8 step 5**. Everything else — enabling
APIs, Secret Manager, IAM, the scheduler, and every `curl` in §8 — is pure
`gcloud`/HTTP and works from anywhere you are logged in.

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
  which is how you reach the authenticated ops console from §4a without a
  tunnel on your own machine.
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
builds do not use it, and `.dockerignore` keeps it out of the upload.

### Either way, you need

- A Google Cloud project with **billing enabled** (credits count as billing, but
  the account must have a billing account attached).
- Owner or Editor on that project — you will be creating service accounts and
  granting IAM roles.

Set these once per shell, and re-set them if your Cloud Shell session recycles:

```bash
export PROJECT_ID=neat-domain-494716-b3    # your project id
export REGION=europe-west1                 # pick one near you; must support Cloud Run
export SERVICE=freight-ops-fleet
export JOB=freight-ops-sweep
export OPS=freight-ops-console
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
  aiplatform.googleapis.com
```

This takes a minute or two the first time. `aiplatform` is only needed if you
switch to Vertex (§7); the rest are needed for every path below.

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

**`ingest` is a local operator step. Nothing runs it in a Cloud Run container.**
That is a choice, and its reasons are the reasons it is a command rather than a
tool:

- A container that ingested on start would make a paid model call on every cold
  start, for output nobody asked for.
- The output would then have to persist somewhere, and the only durable place
  the fleet has is the state bucket — which §5 already explains is the wrong
  filesystem for many small writes.
- The transcription is *derived* from documents already in the image. Spending
  money to regenerate them on a demo URL buys a judge nothing they can see.

So the deployed chat surface (§4d) demonstrates the two halves that need no paid
ingest: ask it to read `raw/inbox/scan_001.pdf` and watch `read_file` refuse with
`binary` and the ingest hint, then ask the intake desk to sort `inbox/` — the
five canonical scans plus both quotes — and watch it group them and name what is
missing. The full 26-document ingest run belongs on camera, locally, where the
`--dry-run` plan, the paid run, and the `<!-- transcribed ... -->` marker on the
resulting file are all visible in one terminal and cost a few cents.

---

## 2. Put the API key in Secret Manager

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
  --set-env-vars "FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_CONSOLE_READONLY=1,FREIGHT_SWEEP_SCHEDULE=weekdays 06:00 Europe/Athens" \
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
curl -s "$URL/reconcile.json"   # expect: {"diverged":false,...}
curl -s "$URL/" | head -5       # the Desk
```

Do not probe `/healthz` on the public URL: Google's frontend reserves that path
on `run.app` hosts and answers 404 itself — the request never reaches the
container, so the app's own `/healthz` route cannot answer there. It still
works where no Google frontend sits in front, e.g. through the §4a proxy.

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

### The optional dev UI — see §4d before you put it on an open URL

Adding `--with_ui` to an `adk deploy` (or changing the `CMD` to
`adk api_server --with_ui`) serves ADK's web console, which is a much better
demo surface than curl. ADK's own docs mark it **development-only**, and that
label is doing more work than it looks like: the same app also serves the eval
runner, the trace viewer and the agent builder, none of which authorises
anything on its own. So it is a fine local habit and **not** a thing to leave on
an unauthenticated URL. §4d deploys it properly — behind IAP, with the
`user_id` pinned to the verified sign-in — and that is the version to use.

---

## 4a. The private ops console — where the buttons actually work

The public service can show the queue and refuse every decision. You still need
somewhere the approve button *works*, and it must not be the same URL.

Deploy the same image a second time, authenticated, with the read-only flag off
(`$OPS` is set in §0):

```bash
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

**In Cloud Shell**, run the same command and then use **Web Preview → Preview on
port 8081** (the icon at the top right). Same tunnel, no local install; this is
the reason §0 suggests Cloud Shell if you have not already got `gcloud` set up.

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

## 4c. A public sandbox for visitors

The read-only URL shows judges everything and lets them decide nothing — and
its 403 is the argument. But a visitor who wants to *feel* the approve button
work should be able to, without a password (§4a says why not) and without
touching the governed record. Deploy the same image a third time on a
**disposable copy** of the state:

```bash
export SANDBOX="freight-ops-sandbox"
export SANDBOX_BUCKET="${PROJECT_ID}-freight-sandbox"
gcloud storage buckets create "gs://$SANDBOX_BUCKET" --location "$REGION" --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "gs://$SANDBOX_BUCKET" \
  --member "serviceAccount:$SA" --role "roles/storage.objectAdmin"

# seed it from a good sweep, and re-run this line whenever you want it reset
gcloud storage cp "gs://$BUCKET/ledger.jsonl" "gs://$BUCKET/approvals.json" "gs://$SANDBOX_BUCKET/"

IMAGE=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(spec.template.spec.containers[0].image)')
gcloud run deploy "$SANDBOX" \
  --image "$IMAGE" \
  --service-account "$SA" \
  --add-volume "name=state,type=cloud-storage,bucket=$SANDBOX_BUCKET" \
  --add-volume-mount "volume=state,mount-path=/state" \
  --set-env-vars "FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_CONSOLE_MODE=sandbox,FREIGHT_LEDGER_PATH=/state/ledger.jsonl,FREIGHT_APPROVALS_PATH=/state/approvals.json" \
  --memory 1Gi --cpu 1 --timeout 300 --max-instances 1 \
  --allow-unauthenticated

# tell the read-only console where it is, so its refusals point somewhere
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars "FREIGHT_SANDBOX_URL=$(gcloud run services describe "$SANDBOX" --region "$REGION" --format='value(status.url)')"
```

What the two env vars do, and what they do not. `FREIGHT_CONSOLE_MODE=sandbox`
paints a ribbon on every page saying the record is disposable; it changes
nothing about the gate — the sandbox is simply a deployment whose store is not
the governed one. `FREIGHT_SANDBOX_URL` on the read-only console adds one link
to its first-visit brief, its disabled buttons and its 403 page. No approve in
the sandbox calls a model (it replays a file write inside that container), so
an open sandbox exposes serving cost only, capped by `--max-instances 1`. Reuse
the **same image digest** the public console runs — one image, three surfaces,
and the difference between them is env vars you can read back.

---

## 4d. The chat surface — ADK's dev UI behind Google sign-in

§4 shows a judge what the fleet already did. §4c lets them click approve on a
disposable copy. Neither lets them **ask the fleet a question**, and that is the
one thing a judge most wants to do. This section deploys ADK's dev UI as a
fourth service, behind Google sign-in, with per-user conversations that survive
the container.

```bash
export CHAT=freight-ops-chat
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
```

**Why a fourth deployment of the same image, and not a route on the console.**
`adk web` does not serve only a chat box. The same FastAPI app carries the eval
runner, the trace viewer, the agent builder and the artifact endpoints, and none
of them has any authorisation of its own. On an open URL, a passer-by could run
the eval against a paid model, read another visitor's conversation, or edit the
agent. So it goes behind IAP and nothing else changes: `console.py` stays
JavaScript-free and imports neither `google.adk` nor `google.genai` (there is a
test that asserts exactly that — `tests/test_seals.py`), which is what keeps the
public URL incapable of spending money. Two surfaces, two threat models, one
image.

### Sessions: Cloud SQL for PostgreSQL

The chat surface is the first deployment where durable sessions are a
*judgeable* capability rather than an operational nicety — a judge signs in,
asks about `shp-002-hero`, closes the tab, comes back and the fleet still knows
what it found. That needs a session store outside the container.

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

- **`postgresql+asyncpg://`** — `asyncpg` is the one new runtime dependency this
  section adds. ADK's `DatabaseSessionService` opens the URI through SQLAlchemy's
  *async* engine, so a sync driver is not a slower option, it is a startup
  error. It is a runtime dependency rather than an extra because a missing
  driver would otherwise surface at the first session write on Cloud Run, after
  the deploy looked successful.
- **`@/sessions?host=/cloudsql/PROJECT:REGION:INSTANCE`** — empty host, database
  name, and the unix socket as a query parameter. That is the form Cloud Run's
  built-in Cloud SQL connector exposes, and it is why no VPC connector, no
  proxy sidecar and no IP allowlist appear anywhere in this section.
- **It must match the form `cli.py` uses.** `chat`, `sweep` and this UI all read
  `FREIGHT_SESSIONS_DB` and all open it through the same
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
only line item here that bills while nobody is looking — Cloud Run scales to
zero, Cloud SQL does not. Delete it when the window closes (§10):

```bash
gcloud sql instances delete freight-sessions
```

### Deploy it

```bash
IMAGE=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(spec.template.spec.containers[0].image)')

gcloud run deploy "$CHAT" \
  --image "$IMAGE" \
  --service-account "$SA" \
  --command sh \
  --args="-c,uvicorn freight_fleet.devui:app_factory --factory --host 0.0.0.0 --port \${PORT:-8080}" \
  --add-cloudsql-instances "$PROJECT_ID:$REGION:freight-sessions" \
  --set-secrets FREIGHT_SESSIONS_DB=freight-sessions-uri:latest \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_AGENTS_DIR=/app/agents,FREIGHT_IAP_AUDIENCE=/projects/$PROJECT_NUMBER/locations/$REGION/services/$CHAT" \
  --memory 1Gi \
  --cpu 1 \
  --timeout 600 \
  --max-instances 1 \
  --min-instances 0 \
  --concurrency 4 \
  --no-allow-unauthenticated
```

- **`--command sh --args="-c,uvicorn ... --factory"`** overrides the image's
  console `CMD` with the dev UI. The `=` is the same parser quirk §5 documents:
  the value starts with a dash, and without `=` gcloud reads it as another flag.
  `--factory` is required too — building ADK's app scans the agent directory and
  opens the session database, so that must happen when uvicorn starts the worker,
  not when the module is imported.
- **`--add-cloudsql-instances`** is what mounts `/cloudsql/...` into the
  container. Without it the URI's socket path does not exist and the first
  session write fails with a connection error that names a file, not a database.
- **`--set-secrets`** keeps the password out of `gcloud run services describe`
  output and out of your deployment history. `--set-env-vars` would put it in
  both, permanently.
- **`FREIGHT_IAP_AUDIENCE`** is the exact string
  `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE`, with the
  **numeric** project number. Getting it wrong is the one mistake that fails
  *open* in naive implementations — an unverified audience accepts a valid IAP
  token minted for somebody else's service. `devui.py` requires it explicitly
  rather than deriving it, so a wrong value fails closed and loudly. Unset means
  local development, where the middleware passes requests through untouched;
  never leave it unset in a deployment.
- **`--no-allow-unauthenticated`** — IAP invokes the service as its own service
  agent, so the service itself must not be public. The two are layered, not
  alternatives.
- **`--max-instances 1 --concurrency 4 --timeout 600`** — see *Spend* below.

### Put IAP in front of it

```bash
gcloud services enable iap.googleapis.com

# The IAP service agent must exist before it can be granted anything.
gcloud beta services identity create --service=iap.googleapis.com --project="$PROJECT_ID"

gcloud run services add-iam-policy-binding "$CHAT" --region="$REGION" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

**The one-time custom OAuth client — do not skip this on an org-less project.**
IAP's Google-managed OAuth client admits only identities *inside your
organization*. This project has no organization, so with the managed client
nobody can sign in at all: every judge gets a refusal and no amount of IAM
fixes it. You need your own OAuth client, once per project:

1. **Console path (the one to use).** APIs & Services → **OAuth consent screen /
   Branding**: set the audience to **External** and the publishing status to
   **In production** (in *Testing* only listed test users can sign in, which is
   the same wall by a different name). Then Credentials → **Create OAuth client
   ID** → *Web application*, and add the authorised redirect URI
   `https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect` —
   substituting the client id the dialog just gave you into its own redirect
   URI, which reads like a typo and is not.
2. **gcloud path.** `gcloud iap settings set iap-oauth.yaml --resource-type=cloud-run --service="$CHAT" --region="$REGION"`
   applies a YAML settings file. **Read the current settings back first** —
   `gcloud iap settings get --resource-type=cloud-run --service="$CHAT" --region="$REGION"` —
   and edit what it prints, rather than writing the file from memory: IAP's
   settings schema differs between resource types and a wrong key is accepted
   silently as "no change". **Do not commit `iap-oauth.yaml`**; it holds the
   client secret. `rm` it when the command succeeds.

Then turn IAP on and let anyone with a Google account in:

```bash
gcloud run services update "$CHAT" --region="$REGION" --iap

gcloud iap web add-iam-policy-binding \
  --resource-type=cloud-run --service="$CHAT" --region="$REGION" \
  --member=allAuthenticatedUsers \
  --role=roles/iap.httpsResourceAccessor
```

`allAuthenticatedUsers` means *any signed-in Google account* — not anonymous
traffic, but not a list you curated either. Two honest costs: IAP's audit logs
stop identifying **who was authorised** (everyone is), and the binding carries
no expiry condition, so nothing removes it for you. Remove it when judging ends:

```bash
gcloud iap web remove-iam-policy-binding \
  --resource-type=cloud-run --service="$CHAT" --region="$REGION" \
  --member=allAuthenticatedUsers \
  --role=roles/iap.httpsResourceAccessor
```

**If the custom OAuth client is more than you want to do**, the ladder down, in
order:

1. **Bind the judges' addresses individually**: `--member="user:judge@example.com"`,
   repeated. Strictly better than `allAuthenticatedUsers` — real audit logs,
   real per-person access — and it needs the same custom OAuth client, so it
   saves you nothing except the open door. Use it if you know the addresses.
2. **Last resort: a throwaway Google account** whose password goes only in the
   submission form. It works, and it is worse in a way worth naming: every judge
   then shares one `user_id`, so "per-user durable sessions" degrades to one
   shared conversation, they see each other's history, and the IAP audit log
   records one identity for everybody. If you end up here, say so in the
   submission rather than letting a judge discover it.

### Per-user isolation, in three sentences

The middleware verifies the `x-goog-iap-jwt-assertion` header against IAP's key
set and the audience above, and refuses the request if it is missing or invalid
(HTTP `403` JSON; a websocket gets `close` code `4403` after the handshake).
It then **overwrites** the `user_id` in all three places ADK reads one — the
`/users/<id>/` path segment, the JSON body of `/run` and `/run_sse`, and the
`/run_live` query string — with the verified `email` claim. So the dev UI's
"Edit user ID" control is cosmetic: a visitor can type anything into it, and the
server pins the session to the account they signed in with regardless. It
rewrites unconditionally rather than checking that the claimed id matches,
because a check has a branch that can be wrong and an overwrite does not.

One naming note. The dev UI's app name is the agent directory, `freight_ops`,
while `cli.py` and the sweep use `freight_fleet`. Same database, separate
conversation namespaces — which is what you want: a judge's chat should not
appear in the operator's history.

### Governance state on this service is sandbox-class

`FREIGHT_LEDGER_PATH` and `FREIGHT_APPROVALS_PATH` stay at the image defaults,
on the container's own disk. No state bucket is mounted, deliberately: mounting
§4b's bucket read-write would put a ledger append behind every tool call through
GCS-FUSE, which is the concurrent-small-write failure this repo has already hit
once (§5).

So: **a hold raised in chat lands in that container's disposable ledger**
(`session_id="cloudrun"`), is reported in the reply the judge is reading, and
disappears with the instance. It is **not** the governed record the ops console
approves. That is a demonstration of the gate, not an entry in the audit trail —
and a judge who wants to click **approve** on a real held action should be
pointed at the §4c sandbox console, where the buttons work against a durable
(if disposable) store. Say which one they are looking at; the distinction is the
whole point of the project and it survives being explained out loud.

### Spend and abuse controls

This is the only surface where a stranger's click costs money, so:

- **IAP** stops anonymous traffic entirely — no sign-in, no request reaches the
  container.
- **`--max-instances 1`** caps the worst case to one container's worth of Gemini
  calls, however many people are signed in. **`--concurrency 4`** keeps that one
  container from queueing a crowd behind a 60-second cross-check.
  **`--timeout 600`** is the same reasoning as §4: a three-document cross-check
  takes 60–90 seconds and Cloud Run's 300s default would cut it off mid-answer.
- **A budget alert**, so a surprise is a surprise you hear about:

```bash
gcloud billing budgets create \
  --billing-account="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)' | sed 's#.*/##')" \
  --display-name=freight-ops \
  --budget-amount=50 \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

  **Budget alerts do not stop spend.** They send email at 50%, 90% and 100% of
  $50 and nothing else happens. The thing that actually caps this deployment is
  `--max-instances 1`; the budget is how you find out you were wrong about that.

### Reaching it

Open the service URL in a browser and sign in:

```bash
gcloud run services describe "$CHAT" --region "$REGION" --format='value(status.url)'
```

**`gcloud run services proxy` no longer works once IAP is on.** The proxy
authenticates you to *Cloud Run*, and IAP now sits in front of Cloud Run
expecting a browser sign-in flow it cannot get from a CLI tunnel. Browser
sign-in is the path; that is the point of this section, and it is worth knowing
before you try the §4a habit and read the refusal as a broken deployment.

Allow **several minutes** after `--iap` and after the IAM binding before the
first successful sign-in. IAP configuration propagates; a 403 in the first two
minutes is not yet evidence of anything.

---

### The invite code — who was invited, not only who they are

Once the consent screen is published, `allAuthenticatedUsers` means *any* Google
account, and every signed-in visitor can make the fleet spend money. IAP cannot
express "invited"; `freight_fleet.access` can. It sits inside the IAP layer and
asks, once per browser, for a code the operator hands out in the submission form:

```bash
export CODE="FLEET-$(openssl rand -hex 3 | tr a-f A-F)-$(openssl rand -hex 3 | tr a-f A-F)"
printf '%s' "$CODE" | gcloud secrets create freight-chat-access-code --replication-policy=automatic --data-file=-
gcloud secrets add-iam-policy-binding freight-chat-access-code   --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor
gcloud run services update "$CHAT" --region "$REGION"   --update-secrets FREIGHT_CHAT_ACCESS_CODE=freight-chat-access-code:latest
echo "$CODE"   # goes in the submission form and nowhere else
```

What it does: a signed-in visitor without the cookie is sent to `/access`, a
plain HTML form; API calls and the websocket are refused outright (403 / 4403).
The right code sets a `HttpOnly; Secure; SameSite=Lax` cookie for seven days,
signed with a key derived from the code itself — so **rotating the code
(`gcloud secrets versions add` + the `update-secrets` line again) invalidates
every cookie** with no second secret to manage. Five wrong guesses from one
address lock it out for fifteen minutes. Whitespace and case are ignored: judges
type these by hand.

What it does not do: it is not an identity. The IAP layer still pins every
`user_id` to the signed-in email, so two judges sharing one code still get their
own sessions and their own history. Unset, the gate passes everything through —
the same convention `FREIGHT_IAP_AUDIENCE` follows for local development.

---

### The demo login — for visitors who would rather not sign in

Some judges will not want their Google account in a stranger's database, and
that is a reasonable position. So the same gate has a second mode and the same
image runs a second time, **without IAP**, where the username is the identity:

```bash
# mint the credentials: prints the JSON for the secret AND the passwords, once
python -m freight_fleet.cli chat-users judge1 judge2 judge3 > chat-users.txt
# store the JSON half as the secret; keep the passwords half for the submission form
sed -n '/^{/,/^}/p' chat-users.txt | gcloud secrets create freight-chat-users --replication-policy=automatic --data-file=-
gcloud secrets add-iam-policy-binding freight-chat-users --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor

export CHAT_DEMO=freight-ops-chat-demo
gcloud run deploy "$CHAT_DEMO" --region "$REGION" --image "$IMAGE" --service-account "$SA"   --command sh --args="-c,uvicorn freight_fleet.devui:app_factory --factory --host 0.0.0.0 --port \${PORT:-8080}"   --add-cloudsql-instances "$PROJECT_ID:$REGION:freight-sessions"   --set-secrets FREIGHT_SESSIONS_DB=freight-sessions-uri:latest,FREIGHT_CHAT_USERS=freight-chat-users:latest   --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_AGENTS_DIR=/app/agents"   --memory 1Gi --cpu 1 --timeout 600 --max-instances 1 --min-instances 0 --concurrency 4   --allow-unauthenticated
gcloud run services update "$SERVICE" --region "$REGION"   --update-env-vars "FREIGHT_CHAT_DEMO_URL=$(gcloud run services describe "$CHAT_DEMO" --region "$REGION" --format='value(status.url)')"
```

No `FREIGHT_IAP_AUDIENCE` and no access code: `FREIGHT_CHAT_USERS` alone selects
the mode. Passwords are scrypt hashes in the secret and nowhere else; the login
pins `user_id` to the username through the same helper IAP uses, so `judge1`
and `judge2` never see each other's sessions, and `--allow-unauthenticated` is
safe because the gate refuses everything but the form until a login succeeds.
Rotate by minting again — a new table is a new signing key, so every cookie
dies with the old passwords. Both chat services share the database: an email
and a username are different identities, so nothing collides.

Why not only this, then? Because it is a shared secret handed around, and the
project's own §4a says what that costs. Google sign-in remains the surface with
real identities and an audit log; the demo login is the courtesy exit for a
visitor who declines it. Offer both, say which is which, and retire the demo
credentials when judging ends.

---

## 5. Deploy the sweep as a Job

The sweep is not a web request — it is a scheduled batch run that must exit.
Cloud Run **Jobs**, not Services. This and §4a are the only two deployments that
call a model, so they are the only two that need model credentials — swap the
`--set-secrets` line below for the Vertex variables if you are following §7a:

```bash
gcloud run jobs deploy "$JOB" \
  --source . \
  --service-account "$SA" \
  --set-secrets "GOOGLE_API_KEY=gemini-api-key:latest" \
  --set-env-vars "FREIGHT_MODEL=gemini-3.7-flash" \
  --command sh \
  --args="-c,python -m freight_fleet.cli sweep; ec=\$?; cp /app/audit/ledger.jsonl /state/ledger.jsonl && cp /app/data/approvals.json /state/approvals.json; exit \$ec" \
  --memory 1Gi \
  --task-timeout 1800 \
  --max-retries 0 \
  --region "$REGION"
```

- **`--command` / `--args`** override the image's `CMD`, so the same image runs
  the CLI instead of the API server. The `=` in `--args=` is load-bearing: the
  value starts with a dash, and without `=` gcloud's parser reads it as another
  flag and fails with "expected one argument" — in every shell, not just
  PowerShell.
- **Why `sh -c` and a copy at the end, not `python` straight onto `/state`.**
  The sweep's agents append ledger rows concurrently and rewrite the approval
  store on every hold. Through GCS-FUSE those small writes are not atomic, and
  a real run died on `OSError: [Errno 116] Stale file handle` with its holds
  lost from the store. So the Job writes to the container's local disk (the
  image's default `FREIGHT_LEDGER_PATH` / `FREIGHT_APPROVALS_PATH`) and copies
  the two finished files to the bucket once — GCS-FUSE's happy path. The
  `exit $ec` keeps the sweep's own honest exit code. Consequence: each run
  **replaces** the shared state, so decisions taken on the ops console before
  a re-run are overwritten. Run the sweep once, then decide; do not schedule
  it while a decided queue matters.
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
  !! 1 of 6 shipment(s) were NOT checked: shp-005-air-dg
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

**The time is yours, not the app's.** Nothing in the code knows about 06:00 —
the sweep is a command, and *when* it runs is operator policy expressed here,
in Cloud Scheduler. Change the cadence by editing this one job
(`gcloud scheduler jobs update http freight-ops-morning-sweep --schedule "..."`),
per deployment, per customer. There is deliberately no schedule setting inside
the console: a screen that could edit the schedule would need credentials that
mutate infrastructure, and the console's safety claim is that it holds none.
The desk *displays* the cadence read-only from `FREIGHT_SWEEP_SCHEDULE` (set in
§4) — if you change the cron here, update that env var to match, because the
console repeats what you tell it and cannot check.

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

**Recommendation for the submission: skip both — unless you deploy §4d.**
The kill-restart-resume property is already proven locally and on camera, and
for the sweep and the CLI neither option adds a judgeable capability; they add
operational durability the video cannot show. Spend the day on the recording
instead.

That reasoning turns over exactly where this section said it would. **§4d's chat
surface makes durable sessions judgeable**: a judge signs in, asks about a
shipment, comes back after the instance has been recycled, and the fleet either
remembers or it does not — and they can see which. So §4d does option (a),
concretely, with the Cloud SQL instance, the `freight-sessions-uri` secret and
the exact URI form. Option (b) stays skipped: Agent Engine would add
`google-cloud-aiplatform` plus a regional resource to provision and delete, for
the same durability §4d gets from a database this repo's code already opens.

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

The sweep Job (§5) and the private ops console (§4a) are the only two that call
models. Swap `--set-secrets` for the Vertex variables in both:

```bash
gcloud run jobs update "$JOB" --region "$REGION" \
  --clear-secrets \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FREIGHT_MODEL=gemini-3.7-flash,FREIGHT_LEDGER_PATH=/app/audit/ledger.jsonl,FREIGHT_APPROVALS_PATH=/app/data/approvals.json"
```

(The Job keeps the *local* paths — §5 explains the copy-at-end.)

```bash
gcloud run services update "$OPS" --region "$REGION" \
  --clear-secrets \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FREIGHT_MODEL=gemini-3.7-flash"
```

Two things to watch. `--set-env-vars` **replaces the entire set**, so anything
you set earlier (including the `FREIGHT_LEDGER_PATH` pair from §4b) must be
repeated in the same flag or it is dropped — use `--update-env-vars` instead if
you would rather merge. And `--clear-secrets` is what actually removes the AI
Studio key; without it the key stays mounted and you are back to two billing
paths.

The **public** console (§4) needs none of this. It calls no model, so it takes
no credentials of either kind.

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
to, so do not skip ahead when one is red.

**1. The service is up and the container is healthy.**

```bash
export URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
curl -s -o /dev/null -w '%{http_code}\n' "$URL/"   # expect: 200
```

(Not `/healthz` — Google's frontend swallows that path on `run.app` hosts; see
Troubleshooting.)

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
  !! 1 of 6 shipment(s) were NOT checked: shp-005-air-dg
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

**8. The chat surface refuses an anonymous caller** (only if you deployed §4d).
The thing to prove here is the *refusal*, because the failure that matters fails
open — an unauthenticated 200 means IAP is not actually in front:

```bash
export CHAT_URL=$(gcloud run services describe "$CHAT" --region "$REGION" --format='value(status.url)')
curl -s -o /dev/null -w '%{http_code}\n' "$CHAT_URL/"
# expect 302 (IAP redirecting to the Google sign-in) — never 200
```

A `403` here is also fine and means the same thing. A `200` means either `--iap`
did not take or `--no-allow-unauthenticated` was dropped; go back to §4d before
the URL goes anywhere near a submission form. Then open the URL in a browser,
sign in, send one message, sign out and back in, and confirm the conversation is
still listed — that round trip is the only check that proves `FREIGHT_SESSIONS_DB`
reached the container rather than ADK falling back to memory in silence.

---

## 9. Running the demo

The five minutes, in the order that makes the argument. Have two windows open:
the **public URL** in a browser, and a terminal.

**Before you start recording**

- Run the sweep once (§8 step 5) so the desk has real holds. A demo that begins
  with an empty queue spends its first minute creating one.
- Open the authenticated tunnel and leave it running: `gcloud run services proxy "$OPS" --region "$REGION" --port 8081`.
- `curl -s -o /dev/null "$URL/"` — a cold start takes a few seconds and you do
  not want that pause on camera.

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

**If the cut allows more than five minutes**, two additions earn their seconds
and neither belongs in the six above — the run of show is an argument, and both
of these are supporting evidence rather than a step in it:

- **The front door (§1a, local terminal, ~40s).** `ingest --dry-run` to show 26
  PDFs and scans planned, the real run on one file, and the resulting
  `inbox/*.md` with its `<!-- transcribed ... -->` first line. This is the one
  paid step an operator runs by hand, which is why it is a terminal and not a
  URL.
- **The chat surface (§4d, browser, ~60s).** Sign in, ask it to read
  `raw/inbox/scan_001.pdf` and let it refuse with `binary`, then ask a real
  question. Say plainly that a hold raised here lands in that container's
  disposable ledger and is not the record you approved in step 3 — the
  distinction is worth more on camera than the extra feature is.

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

### The rest of the teardown

§4d and §4c add things that either stay open or keep billing, so they need
deleting rather than tightening. In this order:

```bash
# 1. Close the chat surface's open door first — before deleting anything else,
#    so there is no window where the service is up and unlisted.
gcloud iap web remove-iam-policy-binding \
  --resource-type=cloud-run --service="$CHAT" --region="$REGION" \
  --member=allAuthenticatedUsers --role=roles/iap.httpsResourceAccessor

# 2. The chat service itself.
gcloud run services delete "$CHAT" --region "$REGION"

# 3. The session database. THIS is the line item that bills while nobody is
#    looking — Cloud Run scales to zero, Cloud SQL does not.
gcloud sql instances delete freight-sessions

# 4. The secret that pointed at it, now a live password for nothing.
gcloud secrets delete freight-sessions-uri

# 5. The public sandbox (§4c) and its disposable bucket.
gcloud run services delete "$SANDBOX" --region "$REGION"
gcloud storage rm --recursive "gs://$SANDBOX_BUCKET"

# 6. Stop the morning sweep from spending money on an audience of nobody.
gcloud scheduler jobs delete freight-ops-morning-sweep --location "$REGION"
```

Leave `$BUCKET` (§4b) alone if you want to keep the ledger the demo was built
on — it holds a few hundred KB and costs approximately nothing. Delete the
custom OAuth client too if you are done with IAP on this project; an unused
OAuth client with a live secret is the same category of thing as the unused API
key §7a tells you to revoke.

`eval.yml`'s `schedule:` also keeps spending after the window closes. Comment it
out (§11) or the Monday-morning eval bills you for a repository nobody is
judging any more.

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
`roles/storage.objectAdmin` on the ledger buckets (§4b, §4c). CI must not be able
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
| `/healthz` returns Google's HTML "404!!1" page while other routes work | Google's frontend reserves `/healthz` on `run.app` hosts — the request never reaches the container | Probe `/` or `/reconcile.json` instead. The route itself is fine and answers through the §4a proxy. |
| `Service URL returns 404 on /list-apps` | `adk api_server` was pointed at the wrong folder | The `CMD` must end with `/app/agents` — the folder *containing* `freight_ops/`, not the agent folder itself. |
| `No module named freight_fleet` | `pip install .` ran before `src/` was copied | Keep the Dockerfile's COPY order: `pyproject.toml` + `src/`, then install. |
| `ValueError: No API key was provided` | Secret not mounted, or the SA lacks `secretAccessor` | `gcloud run services describe $SERVICE --format='value(spec.template.spec.containers[0].env)'` and re-check §3. |
| Request dies at ~5 minutes | Cloud Run default 300s timeout | `--timeout 600` (§4). |
| `gcloud run deploy` fails with no Dockerfile / nothing to build | Not run from the repo root | `--source .` uploads the current directory — `cd` into the clone first. See §0. |
| Console shows an empty desk right after the sweep held drafts | Job and Service have separate filesystems | Mount one bucket into both — §4b. This is the most common cloud-only failure. |
| Sweep dies with `FileNotFoundError: .../workspace/shipments` | Image built before the Dockerfile's seed step — `/app/workspace` is empty | Rebuild with the current Dockerfile (it runs `seed_workspace.py --all` at build time), then redeploy the Job and both consoles from the same source. |
| `POST /decision/.../approve` returns 302 on the public URL | `FREIGHT_CONSOLE_READONLY` not set | Redeploy §4 with the flag; verify with §8 step 4 before demoing. |
| `reconcile.json` says `diverged: true` on a fresh deploy | Console and Job reading different paths | Check both carry the same `FREIGHT_LEDGER_PATH` / `FREIGHT_APPROVALS_PATH`. |
| 404 or `model not found` after switching to Vertex | Model id or endpoint differs on Vertex | See §7a — set `FREIGHT_MODEL` from `gcloud ai models list`; try `GOOGLE_CLOUD_LOCATION=global` before a region. |
| `Could not resolve project using application default credentials` | No ADC on the machine | Locally: `gcloud auth application-default login`. On Cloud Run: the service was deployed without `--service-account`. |
| `403 Permission denied` on a Vertex call | Service account lacks the role | `roles/aiplatform.user` on `$SA` — §7a. Also check `aiplatform.googleapis.com` is enabled. |
| SDK logs that it chose one credential over another | Both `GOOGLE_API_KEY` and Vertex vars are set | Expected precedence, but ambiguous billing. `--clear-secrets` on the Job and ops console. |
| Sandbox or console shows old state up to a minute after a copy into the bucket | GCS-FUSE metadata cache (60s TTL) on the reader | Wait a minute, or restart the revision. Not a data loss. |
| Env vars you set earlier vanished after an update | `--set-env-vars` replaces the whole set | Repeat them all in one flag, or use `--update-env-vars` to merge. |
| Agent answers "not found" for every document | Workspace never seeded | The image must contain `fixtures/`; check `.dockerignore` does not exclude it. |
| `Failed to create database engine` | A sync SQLite URL | Async driver required: `sqlite+aiosqlite:///...`, not `sqlite:///...`. |
| Sweep job succeeds but writes files | Gate bypassed — **stop and investigate** | This must be impossible; `outbox/` should be empty after a sweep. Read the ledger before deploying further. |
| `ingest` prints "nothing to ingest" | Workspace seeded without `--all`, so there is no `raw/` | `python scripts/seed_workspace.py --all` — §1a. `--dry-run` needs no credentials, so this is free to re-check. |
| Agent answers `{"status": "binary"}` for a document | Correct behaviour: `read_file` reads only `.md`, `.csv`, `.txt` | Transcribe it first (§1a), or point the agent at the canonical markdown. This is a refusal, not a failure. |
| Eval score moved after an `ingest --force` | The eval grades whatever the workspace holds and never seeds | `python scripts/seed_workspace.py --all --clean`, then re-run — §1a. |
| Nobody can sign in to the chat service; IAP refuses every account | IAP's Google-managed OAuth client admits only in-organization identities, and this project has no organization | Create the custom OAuth client — §4d. No IAM binding fixes this one. |
| Chat sessions vanish between visits | `FREIGHT_SESSIONS_DB` unset, so ADK fell back to in-memory sessions with no warning | Mount `freight-sessions-uri` (§4d) and confirm with `gcloud run services describe "$CHAT" --format='value(spec.template.spec.containers[0].env)'`. |
| Chat 403s every request with a valid Google sign-in | `FREIGHT_IAP_AUDIENCE` does not match this service | It is `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE` with the **numeric** project number — §4d. Failing closed here is the intended behaviour. |
| `gcloud run services proxy` on the chat service returns 403 | IAP expects a browser sign-in flow a CLI tunnel cannot perform | Open the service URL in a browser instead — §4d. |
| The eval workflow fails at `google-github-actions/auth` | The WIF provider condition did not match the run's OIDC claims | Check `repository_id`, `repository_owner_id` and `event_name` against §11. A fork, or a `pull_request` run, is *supposed* to fail here. |

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
