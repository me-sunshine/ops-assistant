# Intelligent Business Operations Assistant

AI-assisted project coordination and stakeholder management, with a human approving every AI output.

> **Independent portfolio project.** This is a demonstration prototype built by Rashmi Singh. It is not a commercial implementation and was not built for a client. All project data in it is synthetic.

## What it does

1. A user submits a project request through a form.
2. An n8n workflow sends the request to an AI model running on the same machine.
3. The model extracts the objective, requirements, tasks, suggested owners, dependencies, risks and missing information, then drafts a project summary and a stakeholder update.
4. Everything the AI produced waits in a review screen. A person accepts, edits or rejects each item.
5. Only accepted items appear in the confirmed project record. Every decision is written to an audit log.

The system has no email or messaging capability. A stakeholder update can be approved, but a person has to copy and send it themselves.

## Architecture

```mermaid
flowchart LR
  U[Requester] -->|project request form| A[Review app<br/>FastAPI + SQLite]
  A -->|webhook| N[n8n workflow]
  N -->|1 extract| O[Ollama<br/>local model]
  N -->|2 summarise| O
  N -->|3 draft update| O
  N -->|result, one-time token| A
  A -->|pending items| R[Human reviewer]
  R -->|accept / edit / reject| A
  A --> P[(Confirmed project record)]
  A --> L[(Append-only audit log)]
```

| Part | Role | Licence |
|---|---|---|
| Review app (`app/`) | Intake form, validation, review screen, records, audit log | This repository |
| n8n Community Edition (`n8n/workflows/`) | Runs the automation: three model calls, then hands the result back | Sustainable Use License. Free to self-host, **not open source** |
| Ollama | Runs the language model locally | MIT |
| Qwen2.5 7B (default model) | Extraction and drafting | Apache 2.0 |
| SQLite | Storage | Public domain |

No cloud AI service is used and no API key is needed. Nothing leaves your machine.

## How human oversight is enforced

| Control | Where it lives |
|---|---|
| AI output is stored as `pending` and is excluded from the project record until reviewed | `app/main.py`, `record_page` |
| Each item is labelled **stated**, **AI suggestion** or **system check** | `items.provenance` |
| A "stated" label is only kept if the model's quote is really found in the request text. Otherwise it is downgraded to "AI suggestion" | `evidence_found` |
| Risks and missing information are always labelled as suggestions | `store_ai_result` |
| Owners and due dates proposed by the AI are only applied when a reviewer accepts them; the original proposal is kept next to any edit | `items.ai_owner`, `items.owner` |
| Stakeholder updates stay drafts until approved, and approval sends nothing | `review_document` |
| The audit log cannot be updated or deleted; the database rejects both | triggers in `app/db.py` |
| The workflow can only write back with a one-time token issued for that request | `check_callback` |

## Run it with Docker Compose

Requirements: Docker Desktop, about 8 GB of free memory and 6 GB of disk for the default model.

```bash
git clone https://github.com/me-sunshine/ops-assistant.git
```

```bash
cd ops-assistant
```

```bash
docker compose up -d --build
```

The first start downloads the model (about 4.7 GB). Follow the download with:

```bash
docker compose logs -f ollama-pull
```

Then open <http://localhost:8000>. The n8n editor is at <http://localhost:5678> and asks you to create a local owner account the first time.

Stop everything with:

```bash
docker compose down
```

Your data stays in Docker volumes. Add `-v` to that command to delete it.

> The Compose file was written for this project but could not be run in the cloud environment where the prototype was developed, because container image downloads were cut off there. The prototype itself was developed and tested with the same three components running directly (see below). Treat the first Compose start on your machine as a test and report what you see.

## Run it without Docker

This is the setup the prototype was tested with.

1. Install [Ollama](https://ollama.com/download) and download the model:

   ```bash
   ollama pull qwen2.5:7b
   ```

2. Install n8n (needs Node.js 20 or newer), import the workflow and start it:

   ```bash
   npm install -g n8n@2.35.7
   ```

   ```bash
   n8n import:workflow --input=n8n/workflows/project-intake.json
   ```

   ```bash
   n8n publish:workflow --id=projectIntake01
   ```

   ```bash
   n8n start
   ```

3. In a second terminal, start the review app (needs Python 3.11 or newer):

   ```bash
   pip install -r app/requirements.txt
   ```

   ```bash
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

4. Open <http://localhost:8000> and submit a request. Sample requests are in `data/sample_requests.json`.

## Using a smaller or different model

Set `OLLAMA_MODEL` in a `.env` file (copy `.env.example`). Any Ollama model that supports structured output works. Smaller models are faster and less accurate. Check the model's licence on its Ollama library page before using it.

## Tests

```bash
pip install pytest && python -m pytest tests
```

The automated tests cover input validation, missing-information checks, handling of AI output (including malformed output), the approval rules and the audit log. They replace n8n with a stub, so they run in seconds without a model.

To measure the real workflow against the sample dataset (takes several minutes per request on a laptop CPU):

```bash
python eval/run_eval.py
```

Results and limitations are in [`docs/TEST_REPORT.md`](docs/TEST_REPORT.md).

## Security notes

- All ports are bound to `127.0.0.1`. Do not publish them to the internet: the review app has no login, because it is a single-user local demonstration.
- The Ollama port is not published at all in the Compose setup.
- There are no secrets in this repository and none are needed.
- Use synthetic data only.

## Repository layout

```
app/                  review app (FastAPI, Jinja templates, SQLite)
n8n/workflows/        the n8n workflow, importable JSON
data/                 synthetic sample requests with an answer key
eval/                 evaluation script and saved results
tests/                automated tests
docs/                 test report and screenshots
docker-compose.yml    local stack: ollama, n8n, app
```
