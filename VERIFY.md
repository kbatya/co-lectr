# Verifying Co-Lectr from scratch

A from-nothing reproducibility check — clone on a clean machine or network and confirm the project runs.
Nothing here needs anything already on the dev box; the credentialed steps say exactly what to supply.

Secrets are **not** in the repository (`.env` and `.secrets/` are git-ignored, by design). The layer-1 core
and the whole test suite need no credentials; layer 2, Firestore and deploy each need one, listed below.

## What each step needs

| Step | Needs credentials? |
|---|---|
| Install + run the tests (96 pass, 7 skip without a Firestore key) | No |
| Layer 1 over the samples (facts, the class digest) | No |
| Layer 2 — the questions (`--review`, `adk web`) | A Gemini API key |
| Firestore — persistence, the stored class digest | A service-account key |
| Deploy to Cloud Run | `gcloud` + a GCP project with billing |

## 0. Prerequisites

- **Python 3.12** (developed and tested on 3.12.3), `git`.
- For layer 2: a [Gemini API key](https://aistudio.google.com/apikey).
- For Firestore / deploy: a Google Cloud project and a service-account key with Firestore access.

## 1. Clone (the repo *is* the `co_lectr` package)

```bash
git clone <repo-url> co_lectr
```

Work from the **parent** of `co_lectr/` for every command below.

## 2. Install

```bash
python -m venv .venv && .venv/Scripts/activate   # macOS/Linux: source .venv/bin/activate
pip install -r co_lectr/requirements.txt
```

## 3. Run the tests — no credentials (the core check)

```bash
python -m pytest co_lectr/tests -q
```

Expect **96 passed, 7 skipped** — the 7 skips are the Firestore integration tests, which need a
service-account key. The tests are offline: the network and the model are faked, so this passes on any
machine with no keys and no internet access to Google.

## 4. Layer 1 over the samples — no credentials

```bash
python -m co_lectr.cli co_lectr/samples --require run_agent load_config --no-store
```

Expect a per-class digest, e.g. `4/6  ruff:E722` for class 12a — computed by `ruff`/`ast` alone.

## 5. Layer 2 — the questions (needs a Gemini API key)

Create `co_lectr/.env`:

```
GOOGLE_API_KEY=<your Gemini API key>
```

```bash
python -m co_lectr.cli co_lectr/samples --require run_agent load_config --no-store --review --chapter "ch1 basics" "ch2 functions and files" "ch3 exceptions"
```

Expect one question per finding, anchored to `file:line`, never a corrected snippet.

## 6. Firestore and the lecturer agent (needs a service-account key)

Copy your service-account key to `co_lectr/.secrets/` (never commit it) and add to `co_lectr/.env`:

```
GOOGLE_APPLICATION_CREDENTIALS=<absolute path to the key>.json
GOOGLE_CLOUD_PROJECT=<your project id>
```

```bash
adk web        # pick co_lectr; ask "review student_04", "what did 12-A get wrong?"
```

## 7. Deploy (needs gcloud + a GCP project)

See the **Deploy to Cloud Run** section in [README.md](README.md). In short:

```bash
gcloud run deploy co-lectr --source . --region us-central1 --allow-unauthenticated
```

The live health check answers at `GET /` with `{"service": "co-lectr", "status": "ok"}`.

---

If steps 3 and 4 pass on a clean machine with no keys and no Google network access, the core is
reproducible independently of the original dev box.
