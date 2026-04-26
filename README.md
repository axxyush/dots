# Dots: Accessibility Agent Orchestration

Dots converts **floorplan images** into **tactile-friendly maps** and attaches a **QR link** so anyone can scan and ask map-specific questions (text or voice) like:

- “Where is the entrance?”
- “Where are the stairs?”
- “How many exits are there?”

This repo contains:

- **A Python backend** (FastAPI) intended to run on a public server (e.g. **Vultr**) that:
  - generates tactile maps
  - creates a per-map `map_id`
  - serves `/m/{map_id}` (text Q&A) and `/m/{map_id}/voice` (voice Q&A)
- **Fetch.ai uAgents** that can be hosted on **Agentverse** for:
  - room scan intake
  - payment (Dorado testFET)
  - floorplan → tactile generation
  - wayfinding Q&A

> Note: This project intentionally avoids OpenAI dependencies in the main tactile pipeline.

---

## Architecture (high level)

![Architecture Diagram](Architecture%20Diagram.png)

---

## What’s in this repo

### Backend (Vultr-hosted)
- `backend/main.py`: FastAPI server
- `backend/map_store.py`: SQLite registry storing:
  - `map_id`
  - `tactile_png_url` / `tactile_pdf_url`
  - `context_text` (map-specific grounding)
  - chat history keyed by `(map_id, session_id)`
- `backend/layout_brief.py`: prompt builders for map-grounded Q&A

### Tools / pipelines
- `agents/tools.py`: downloader, Gemini tactile generation, Gemini context extraction, uploader, optional ADA/CV pipeline
- `floorplan_parser/connectdots_pdf.py`: ConnectDots-style tactile PDF renderer (optional; supports “no numbering” mode)

### Fetch.ai Agents (Agentverse)
Depending on your deployment, you may run/host:
- **Room Scan**: intake RoomPlan scans / payloads
- **Dorado Pay**: testFET gating + payment verification
- **Floorplan → Tactile Map**: floorplan URL → tactile output
- **WayFind**: answers questions grounded in a specific map

---

## Quickstart (local)

### 1) Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Configure `.env`

Create `.env` at repo root (never commit it):

```bash
GEMINI_API_KEY=...
ASI_ONE_API_KEY=...          # or ASI_API_KEY=...
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
PUBLIC_BASE_URL=http://127.0.0.1:8000
```

### 3) Run backend

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## Create a broadcast map (QR → chat/voice)

### Option A: you already have a tactile PNG (fast)

```bash
curl -X POST "http://127.0.0.1:8000/maps/from-tactile-upload" \
  -F "file=@/path/to/tactile.png"
```

The response includes:
- `chat_url`: open in browser for text Q&A
- `voice_url`: open in browser for ElevenLabs voice Q&A

### Option B: from a floorplan URL

```bash
curl -X POST "http://127.0.0.1:8000/maps/from-floorplan-url" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"https://…/floorplan.png","model":"gemini-3-pro-image-preview"}'
```

---

## Deploy on Vultr (production)

- Run the same FastAPI backend on the server.
- Set `PUBLIC_BASE_URL` to your public origin, e.g.:
  - `http://<your-vultr-ip>:8000` or your HTTPS domain.
- Ensure `.env` is present on the server with:
  - Gemini key
  - ASI:One key (optional for text Q&A)
  - ElevenLabs key + agent id (required for voice)

---

## Notes / limitations

- `tmpfiles.org` links are temporary; for production, replace with durable storage (S3/R2).
- Tactile-only images may be hard to interpret perfectly; best results come from generating context from the original floorplan or storing a structured layout.

