## dots — SenseGrid + RoomPlan tactile maps

This repo contains two Fetch.ai `uagents` that generate **accessible tactile maps** from:
- **floorplan images** (URL → JSON → reconstruction + tactile PDF)
- **Apple RoomPlan JSON** (paste/URL → converted JSON → tactile output)

Both agents can run locally while still being reachable on **Agentverse** via `mailbox=True`.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` (repo root):

```bash
GEMINI_API_KEY=...
ASI_ONE_API_KEY=...
SENSEGRID_AGENT_SEED=...
ROOMPLAN_BRAILLE_AGENT_SEED=...
```

### Agent 1: `sensegrid` (floorplan images)

- **Run**:

```bash
.venv/bin/python agents/sensegrid_agent.py
```

- **Use**: send one or more **http(s) image URLs** in chat. The agent returns:
  - a reconstructed PNG + parsed JSON
  - a tactile PDF (ConnectDots-style) after payment flow

### Agent 2: `roomplan-tactile` (RoomPlan JSON)

- **Run**:

```bash
.venv/bin/python agents/roomplan_braille_agent.py
```

- **Use**:
  - say `help` to see instructions
  - paste RoomPlan JSON directly, or send a public URL to a `.json`

### Quick experiment: “Nano Banana Pro” tactile image

This is a **prompting experiment** (not deterministic) that asks Gemini image models
to generate a tactile-plaque-style raster map from a floorplan image:

```bash
.venv/bin/python scripts/test_nanobanana_tactile.py --image /path/to/floorplan.png
```

