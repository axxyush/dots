![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)

# BrailleMap — Fetch.ai Agent Pipeline

Four uAgents that turn an iPhone RoomPlan scan into a Braille-ready PDF and an audio walkthrough.

```
POST /trigger/{room_id}
        │
        ▼
  Agent 1 — Spatial Processor       (3D → 2D normalized layout)
        │                           Implements the Fetch.ai Chat Protocol
        ▼
  Agent 2 — Object Enricher         (Gemini Vision → useful labels)
        │
        ├──► Agent 3 — Map Generator    (ReportLab → PDF → Cloudinary)
        └──► Agent 4 — Narration Agent  (ASI1-Mini → ElevenLabs → Cloudinary)
```

Agents 3 and 4 run in parallel once enrichment completes.

## Setup

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.template .env
# fill in API keys in .env
```

## Run

Start the mock backend (in another terminal, from the project root):

```bash
python mock_backend.py
```

Start all four agents:

```bash
python run_all.py
```

Or run each in its own terminal (prints its address on startup):

```bash
python agent_spatial.py      # port 8001
python agent_enricher.py     # port 8002
python agent_map.py          # port 8003
python agent_narration.py    # port 8004
```

Trigger the pipeline for a real room:

```bash
python test_pipeline.py <room_id>
```

## Agents

### Agent 1 — Spatial Processor
Fetches the raw `CapturedRoom` export from the backend, drops the Y axis, normalizes so the room's min corner sits at the origin, and emits a clean 2D layout. Implements the Fetch.ai **Chat Protocol** so it is discoverable via ASI:One — natural-language triggers like `process room <id>` kick off the pipeline.

### Agent 2 — Object Enricher
Replaces generic RoomPlan categories (`Chair`, `Storage`, `Table`) with specific, blind-navigation-friendly labels (`office chair with armrests`, `filing cabinet`, `reception desk with monitor`) by showing Gemini Vision the room photos alongside the object list.

### Agent 3 — Map Generator
Renders the 2D layout as a dot-grid PDF — walls as continuous dot borders, doors and windows as distinct symbols, objects as numbered dot clusters with a legend on page 2. Uploads the PDF to Cloudinary and patches `pdf_url` back to the room document.

### Agent 4 — Narration Agent
Generates a 150-word walkthrough with **ASI1-Mini** (Gemini fallback if the endpoint is unreachable), synthesizes it to MP3 with **ElevenLabs**, uploads the audio to Cloudinary, and patches `audio_url` + `narration_text`.

## Addresses

Agent addresses are **deterministic from the seed** set in `.env`. Each agent computes the downstream agent's address by hashing the downstream seed — you never need to copy-paste addresses between files. If you change a seed, every agent that targets it picks up the new address on the next restart.

## Registering on Agentverse

1. Start each agent once so it registers with the Almanac contract.
2. Log into https://agentverse.ai.
3. For each agent: **Register Agent** → paste the address printed on startup, set name/description, and attach this README.
4. Verify via ASI:One search.

## Backend endpoints this pipeline expects

| Method | Path | Purpose |
|--------|------|---------|
| `GET`   | `/rooms/{id}/full`          | Full room including `scan_data` and `photos` |
| `PATCH` | `/rooms/{id}`               | Partial update (agents write `layout_2d`, `pdf_url`, etc.) |
| `POST`  | `/trigger/{id}`             | Kick off the pipeline for an already-uploaded room |

The included `mock_backend.py` (at the repo root) exposes all three. It reuses `trigger.py` from this folder, so the env that runs the backend needs `uagents`, `requests`, and `python-dotenv` available. Either install this folder's `requirements.txt` into that env or run `mock_backend.py` from the agents' venv (add `fastapi` + `uvicorn` to it). If the import fails, `POST /trigger/{id}` returns 500 with a clear message; all other endpoints still work.

## Troubleshooting

- **Address already in use**: `lsof -i :8001` then `kill -9 <pid>`. Ports are 8001–8004.
- **Agent can't reach another agent**: seeds differ between agents or the downstream agent isn't running. All four must be up for the pipeline to complete.
- **Gemini returns garbage JSON**: the code strips markdown fences and falls back to original labels on parse failure — check `ctx.logger` output for the raw response.
- **ASI1-Mini 404 / 5xx**: Agent 4 automatically falls back to Gemini for text generation; the log line prints which LLM was used.
- **ElevenLabs rate limit**: free tier is 10k characters/month; each narration is ~800 chars.
