# BrailleMap — 5-Day Execution Plan

This plan assumes 5 full days of work before/during LA Hacks, three people working in parallel, and nobody has used Fetch.ai before. It's built to be followed step-by-step. If a step is unclear, that's a bug in the plan — flag it.

---

## Team Assignments

**Person A — iOS + Mobile**
Already has the test app working. Will fork it into the production scanner, add photo capture, add backend upload, and build the voice interaction interface for the conversational agent.

**Person B — Agent Pipeline (Fetch.ai)**
Owns all 4 agents, Fetch.ai registration, Agentverse setup, Chat Protocol implementation, and the integrations with Gemini Vision, ASI1-Mini, and ElevenLabs.

**Person C — Backend + Dashboard**
Owns FastAPI server, Vultr deployment, MongoDB Atlas, the React dashboard with Cloudinary, and the HTTP layer that connects iOS → backend → agents.

**Shared:** Person D handles domain registration, README documentation, pitch deck, demo video, and sponsor submissions. Person D also helps with testing.

---

## Day 0 (Today) — Setup & Accounts

Before anyone writes code, everyone needs accounts and access. Do this today so Day 1 isn't blocked on signups.

### Everyone

- [ ] Create a shared GitHub org or shared repo list. All repos go here.
- [ ] Join a shared Slack/Discord channel for team coordination.
- [ ] Set up a shared Google Doc for API keys and environment variables (use 1Password or similar if you want to be more secure).

### Person A (iOS)

- [ ] Create a new private GitHub repo: `braillemap-ios`
- [ ] Push the existing TestRoomPlan code as the starting point (new branch: `main`)
- [ ] Ensure Xcode is working and can deploy to the physical iPhone Pro

### Person B (Agents)

- [ ] Sign up for Fetch.ai Agentverse: https://agentverse.ai
- [ ] Sign up for Google AI Studio: https://aistudio.google.com — generate a Gemini API key
- [ ] Sign up for ElevenLabs: https://elevenlabs.io — generate an API key (free tier gives you 10k characters/month which is plenty for testing)
- [ ] Install Python 3.11+ if not already installed
- [ ] Create a private GitHub repo: `braillemap-agents`

### Person C (Backend)

- [ ] Sign up for Vultr: https://www.vultr.com/ (use the MLH partner link when it's available for free credits, but for dev you can use a $6/month instance temporarily)
- [ ] Sign up for MongoDB Atlas: https://www.mongodb.com/cloud/atlas/register — create a free M0 cluster
- [ ] Sign up for Cloudinary: https://cloudinary.com (free tier)
- [ ] Create a private GitHub repo: `braillemap-backend` (for FastAPI)
- [ ] Create a private GitHub repo: `braillemap-dashboard` (for React)

### Person D (Logistics)

- [ ] Register domain on GoDaddy: `braillemap.app` or `braillemap.tech` (whichever is available)
- [ ] Create a shared Google Drive folder for demo assets (pitch deck, screenshots, demo video)

**End of Day 0 checkpoint:** Everyone has their accounts set up, repos exist, API keys are in the shared doc. Nobody has written code yet. That's fine.

---

## Day 1 — Core Pipelines Come Alive

Today's goal: Every person gets their piece of the system to a "hello world" state. No integration yet. Three parallel tracks.

### Person A — iOS: Backend Upload + Photo Capture

**Step 1: Fork TestRoomPlan into production scanner (30 min)**

Clone your TestRoomPlan repo into `braillemap-ios`. Rename the project to `BrailleMapScanner`. Keep the core scanning and RoomExporter logic — you're adding to it, not replacing.

**Step 2: Add photo capture after scan (1-2 hours)**

In `ResultsView.swift`, add a new section before the share sheet that prompts the user to capture photos.

Add state:
```swift
@State private var capturedPhotos: [UIImage] = []
@State private var showingCamera = false
```

Add a button after the Floor Plan / Report tabs:
```swift
if capturedPhotos.isEmpty {
    Button("Capture Room Photos") { showingCamera = true }
} else {
    Text("\(capturedPhotos.count) photos captured")
    Button("Retake") { 
        capturedPhotos.removeAll()
        showingCamera = true 
    }
}
```

Use `UIImagePickerController` wrapped in `UIViewControllerRepresentable` for the camera. When a photo is captured, append to `capturedPhotos`. Let the user capture 3-5 photos, then return to results.

Test: Can you complete a scan, then take 3 photos, and see them listed?

**Step 3: Build the upload function (2-3 hours)**

Create a new file `BackendClient.swift`:

```swift
import Foundation
import UIKit

struct UploadResponse: Codable {
    let roomId: String
    let status: String
}

class BackendClient {
    static let shared = BackendClient()
    
    // For dev, point this at your Vultr server IP or ngrok URL
    private let baseURL = URL(string: "https://YOUR_BACKEND_URL")!
    
    func uploadScan(
        scanData: ScanExportData,
        photos: [UIImage],
        roomName: String,
        buildingName: String
    ) async throws -> UploadResponse {
        let encoder = JSONEncoder()
        let scanJSON = try encoder.encode(scanData)
        let scanDict = try JSONSerialization.jsonObject(with: scanJSON) as! [String: Any]
        
        let photoStrings = photos.compactMap { image -> String? in
            guard let data = image.jpegData(compressionQuality: 0.7) else { return nil }
            return data.base64EncodedString()
        }
        
        let payload: [String: Any] = [
            "scan_data": scanDict,
            "photos": photoStrings,
            "metadata": [
                "room_name": roomName,
                "building_name": buildingName,
                "scanned_at": ISO8601DateFormatter().string(from: Date())
            ]
        ]
        
        let payloadData = try JSONSerialization.data(withJSONObject: payload)
        
        var request = URLRequest(url: baseURL.appendingPathComponent("scan"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = payloadData
        
        let (data, _) = try await URLSession.shared.data(for: request)
        return try JSONDecoder().decode(UploadResponse.self, from: data)
    }
}
```

**Step 4: Wire upload button into ResultsView (1 hour)**

Add an "Upload to BrailleMap" button. When tapped, call `BackendClient.shared.uploadScan(...)`, show a loading state, and on success display the returned roomId.

For today, you don't have a backend URL yet — that's fine. Hardcode `http://localhost:8000` or a placeholder. You'll fix this tomorrow when Person C has a real endpoint.

**End of Day 1 for Person A:** Scanner captures room + photos, upload function exists (even if it can't reach a real backend yet). Tested on device.

---

### Person B — Agents: Fetch.ai Hello World

**Step 1: Read the Fetch.ai docs (1 hour — don't skip this)**

Don't try to skim or jump to code. Read these in order:
- https://fetch.ai/docs/guides/agents/getting-started/create-a-uagent (the fundamental agent concept)
- https://fetch.ai/docs/guides/agents/intermediate/communicating-with-other-agents (how agents talk to each other)
- https://fetch.ai/docs/guides/agents/intermediate/register-in-almanac (how to register on Agentverse)
- https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol-examples (Chat Protocol — required for Fetch.ai submission)

As you read, write down questions. It's fine not to understand everything yet.

**Step 2: Install and set up (30 min)**

```bash
mkdir braillemap-agents
cd braillemap-agents
python3.11 -m venv venv
source venv/bin/activate
pip install uagents google-generativeai elevenlabs python-dotenv pydantic requests
```

Create `.env` file with your keys (don't commit this):
```
GEMINI_API_KEY=your_gemini_key
ELEVENLABS_API_KEY=your_elevenlabs_key
AGENT_SEED_1=any_random_string_1
AGENT_SEED_2=any_random_string_2
AGENT_SEED_3=any_random_string_3
AGENT_SEED_4=any_random_string_4
```

**Step 3: Build a single "hello world" agent (2 hours)**

Create `hello_agent.py`:

```python
from uagents import Agent, Context, Model
from dotenv import load_dotenv
import os

load_dotenv()

# Message schema — what this agent receives
class HelloRequest(Model):
    name: str

class HelloResponse(Model):
    greeting: str

# The agent itself
hello_agent = Agent(
    name="hello_agent",
    seed=os.getenv("AGENT_SEED_1"),
    port=8001,
    endpoint=["http://localhost:8001/submit"]
)

@hello_agent.on_message(model=HelloRequest)
async def handle_hello(ctx: Context, sender: str, msg: HelloRequest):
    ctx.logger.info(f"Received hello from {sender}: {msg.name}")
    greeting = f"Hello, {msg.name}! I'm your BrailleMap agent."
    await ctx.send(sender, HelloResponse(greeting=greeting))

if __name__ == "__main__":
    print(f"Agent address: {hello_agent.address}")
    hello_agent.run()
```

Run it: `python hello_agent.py`

You should see the agent start and print its address. Save this address.

**Step 4: Build a client that calls the agent (1 hour)**

Create `test_client.py`:

```python
from uagents import Agent, Context, Model
from dotenv import load_dotenv
import os

load_dotenv()

class HelloRequest(Model):
    name: str

class HelloResponse(Model):
    greeting: str

# Put the address you got from hello_agent.py here
HELLO_AGENT_ADDRESS = "agent1q..."

client = Agent(
    name="test_client",
    seed=os.getenv("AGENT_SEED_2"),
    port=8002,
    endpoint=["http://localhost:8002/submit"]
)

@client.on_event("startup")
async def send_message(ctx: Context):
    await ctx.send(HELLO_AGENT_ADDRESS, HelloRequest(name="Aryan"))

@client.on_message(model=HelloResponse)
async def handle_response(ctx: Context, sender: str, msg: HelloResponse):
    ctx.logger.info(f"Got response: {msg.greeting}")

if __name__ == "__main__":
    client.run()
```

Run this in a separate terminal. You should see the greeting printed. This proves agent-to-agent communication works.

**Step 5: Test Gemini Vision API directly (1 hour, separate from Fetch.ai)**

In a separate file `test_gemini.py`:

```python
import google.generativeai as genai
import os
from dotenv import load_dotenv
from PIL import Image

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel('gemini-2.0-flash-exp')

# Test with any image of a room
img = Image.open("test_room.jpg")  # put any room photo here
response = model.generate_content([
    "Identify the objects in this room. For each object, provide a specific label that would help a blind person navigate (e.g., 'reception desk with computer' instead of just 'table').",
    img
])
print(response.text)
```

This verifies your Gemini key works and you understand the API shape before wrapping it in an agent.

**Step 6: Test ElevenLabs directly (30 min)**

```python
from elevenlabs import ElevenLabs, play
import os
from dotenv import load_dotenv

load_dotenv()
client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

audio = client.text_to_speech.convert(
    text="Entering through the main door, the reception desk is directly ahead at three meters.",
    voice_id="JBFqnCBsd6RMkjVDRZzb",  # default "George" voice
    model_id="eleven_multilingual_v2"
)

# Save audio to file
with open("test_narration.mp3", "wb") as f:
    for chunk in audio:
        f.write(chunk)
print("Saved test_narration.mp3")
```

**End of Day 1 for Person B:** Single agent works. Two agents can communicate. Gemini and ElevenLabs APIs are tested. You're not building the real agents yet — you're proving the primitives work.

---

### Person C — Backend: FastAPI + MongoDB Hello World

**Step 1: Set up FastAPI locally (1 hour)**

```bash
mkdir braillemap-backend
cd braillemap-backend
python3.11 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn pymongo python-dotenv pydantic python-multipart cloudinary
```

Create `main.py`:

```python
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
import uuid

app = FastAPI()

class ScanUpload(BaseModel):
    scan_data: Dict[str, Any]
    photos: List[str]  # base64 encoded
    metadata: Dict[str, Any]

class UploadResponse(BaseModel):
    room_id: str
    status: str

@app.get("/")
def root():
    return {"status": "BrailleMap backend is alive"}

@app.post("/scan", response_model=UploadResponse)
async def upload_scan(payload: ScanUpload):
    room_id = str(uuid.uuid4())
    # For now, just acknowledge receipt — we'll add MongoDB next
    print(f"Received scan for room: {payload.metadata.get('room_name')}")
    print(f"Scan data keys: {list(payload.scan_data.keys())}")
    print(f"Number of photos: {len(payload.photos)}")
    return UploadResponse(room_id=room_id, status="received")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

Run it: `uvicorn main:app --reload --host 0.0.0.0 --port 8000`

Test with curl:
```bash
curl http://localhost:8000/
```

**Step 2: Expose your local server to Person A via ngrok (30 min)**

Person A needs to hit your backend from their iPhone. Install ngrok:
```bash
brew install ngrok  # or download from ngrok.com
ngrok http 8000
```

Give the ngrok URL (like `https://abc123.ngrok.io`) to Person A. They plug it into their `BackendClient.swift` as the baseURL. Now the iPhone can hit your local backend.

**Step 3: Connect MongoDB Atlas (2 hours)**

In your MongoDB Atlas dashboard:
1. Create a database called `braillemap`
2. Add your current IP to the IP access list (for dev — allow 0.0.0.0/0 temporarily if needed)
3. Create a database user with read/write permissions
4. Get the connection string (looks like `mongodb+srv://user:pass@cluster...`)

Add to `.env`:
```
MONGODB_URI=mongodb+srv://...
```

Update `main.py`:
```python
from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()
mongo_client = MongoClient(os.getenv("MONGODB_URI"))
db = mongo_client.braillemap
rooms_collection = db.rooms

@app.post("/scan", response_model=UploadResponse)
async def upload_scan(payload: ScanUpload):
    room_id = str(uuid.uuid4())
    
    document = {
        "_id": room_id,
        "scan_data": payload.scan_data,
        "photos": payload.photos,  # store base64 — or upload to Cloudinary first
        "metadata": payload.metadata,
        "status": "received",
        "created_at": datetime.utcnow(),
        "pdf_url": None,
        "audio_url": None,
        "enriched_objects": None,
        "layout_2d": None
    }
    
    rooms_collection.insert_one(document)
    
    return UploadResponse(room_id=room_id, status="received")

@app.get("/rooms/{room_id}")
async def get_room(room_id: str):
    room = rooms_collection.find_one({"_id": room_id})
    if not room:
        return {"error": "not found"}
    # Don't return raw photos (too big) — just metadata
    room.pop("photos", None)
    room.pop("scan_data", None)
    return room
```

Test: upload a scan from the iPhone, then curl `http://localhost:8000/rooms/{id}` — you should see the stored metadata.

**Step 4: Skeleton React dashboard (2 hours)**

```bash
cd ..
npx create-cloudinary-react braillemap-dashboard
cd braillemap-dashboard
npm install
npm start
```

This gives you a running React app with Cloudinary integration. For today, just get it to a page that says "BrailleMap Dashboard — rooms will appear here." Tomorrow you'll connect it to the backend.

**End of Day 1 for Person C:** FastAPI server runs locally, accepts scan uploads, stores them in MongoDB. Person A can hit it via ngrok. React dashboard is running (empty).

---

### Day 1 Checkpoint (End of Day)

Before going to bed, do a 20-minute team sync:

- Person A: Can you scan, capture photos, and hit the upload endpoint (even if it's still ngrok)?
- Person B: Can two local agents talk to each other? Do Gemini and ElevenLabs APIs work?
- Person C: Does MongoDB receive the uploaded data?

**If any of these are red, fix before starting Day 2.** The rest of the plan depends on these foundations.

---

## Day 2 — Build the Real Agents

Today Person B does the heavy lifting. Person A and Person C support them and polish their own pieces.

### Person B — Build Agent 1, 2, and 3

**Step 1: Create the message schemas (30 min)**

Create `schemas.py`:

```python
from uagents import Model
from typing import List, Optional, Dict, Any

class SpatialProcessingRequest(Model):
    room_id: str
    scan_data: Dict[str, Any]  # raw ScanExportData from iPhone

class Layout2D(Model):
    room_id: str
    room_width: float
    room_depth: float
    walls: List[Dict[str, Any]]
    doors: List[Dict[str, Any]]
    objects: List[Dict[str, Any]]  # each has index, category, x, y, width, depth

class EnrichmentRequest(Model):
    room_id: str
    layout: Layout2D
    photos_base64: List[str]

class EnrichedLayout(Model):
    room_id: str
    layout: Layout2D  # but with enriched category labels

class MapGenerationRequest(Model):
    room_id: str
    layout: EnrichedLayout

class MapGenerationResult(Model):
    room_id: str
    pdf_url: str

class NarrationRequest(Model):
    room_id: str
    layout: EnrichedLayout

class NarrationResult(Model):
    room_id: str
    audio_url: str
    narration_text: str
```

**Step 2: Build Agent 1 — Spatial Processor (2 hours)**

Create `agent_spatial.py`:

```python
from uagents import Agent, Context
from schemas import SpatialProcessingRequest, Layout2D, EnrichmentRequest
from dotenv import load_dotenv
import os
import requests

load_dotenv()

spatial_agent = Agent(
    name="spatial_processor",
    seed=os.getenv("AGENT_SEED_1"),
    port=8001,
    endpoint=["http://localhost:8001/submit"]
)

ENRICHER_ADDRESS = "REPLACE_WITH_ENRICHER_ADDRESS_AFTER_STEP_3"
BACKEND_URL = "http://localhost:8000"

def project_to_2d(scan_data: dict) -> dict:
    """Converts 3D RoomPlan data to 2D floor layout."""
    walls = scan_data.get("walls", [])
    doors = scan_data.get("doors", [])
    objects = scan_data.get("objects", [])
    
    # Compute room bounding box
    xs = []
    zs = []
    for wall in walls:
        xs.append(wall["positionX"])
        zs.append(wall["positionZ"])
    
    if not xs:
        return {"error": "no walls detected"}
    
    min_x, max_x = min(xs), max(xs)
    min_z, max_z = min(zs), max(zs)
    room_width = max_x - min_x
    room_depth = max_z - min_z
    
    # Normalize all positions relative to min_x, min_z (so origin is corner)
    def normalize(x, z):
        return {"x": x - min_x, "y": z - min_z}
    
    walls_2d = []
    for i, w in enumerate(walls):
        pos = normalize(w["positionX"], w["positionZ"])
        walls_2d.append({
            "index": i,
            "x": pos["x"],
            "y": pos["y"],
            "width": w["widthMeters"]
        })
    
    doors_2d = []
    for i, d in enumerate(doors):
        pos = normalize(d["positionX"], d["positionZ"])
        doors_2d.append({
            "index": i,
            "x": pos["x"],
            "y": pos["y"],
            "width": d["widthMeters"]
        })
    
    objects_2d = []
    for i, o in enumerate(objects):
        pos = normalize(o["positionX"], o["positionZ"])
        objects_2d.append({
            "index": i,
            "category": o["category"],
            "x": pos["x"],
            "y": pos["y"],
            "width": o["widthMeters"],
            "depth": o["depthMeters"]
        })
    
    return {
        "room_width": room_width,
        "room_depth": room_depth,
        "walls": walls_2d,
        "doors": doors_2d,
        "objects": objects_2d
    }

@spatial_agent.on_message(model=SpatialProcessingRequest)
async def process_scan(ctx: Context, sender: str, msg: SpatialProcessingRequest):
    ctx.logger.info(f"Processing scan for room {msg.room_id}")
    
    layout_dict = project_to_2d(msg.scan_data)
    
    layout = Layout2D(
        room_id=msg.room_id,
        room_width=layout_dict["room_width"],
        room_depth=layout_dict["room_depth"],
        walls=layout_dict["walls"],
        doors=layout_dict["doors"],
        objects=layout_dict["objects"]
    )
    
    # Store intermediate result in backend
    requests.patch(f"{BACKEND_URL}/rooms/{msg.room_id}", json={
        "layout_2d": layout_dict,
        "status": "spatial_processed"
    })
    
    # Fetch photos from backend to pass to enricher
    photos_resp = requests.get(f"{BACKEND_URL}/rooms/{msg.room_id}/photos")
    photos = photos_resp.json().get("photos", [])
    
    await ctx.send(ENRICHER_ADDRESS, EnrichmentRequest(
        room_id=msg.room_id,
        layout=layout,
        photos_base64=photos
    ))

if __name__ == "__main__":
    print(f"Spatial Processor address: {spatial_agent.address}")
    spatial_agent.run()
```

Note: You'll need Person C to add PATCH `/rooms/{id}` and GET `/rooms/{id}/photos` endpoints. Coordinate with them.

**Step 3: Build Agent 2 — Object Enricher (2-3 hours)**

Create `agent_enricher.py`:

```python
from uagents import Agent, Context
from schemas import EnrichmentRequest, EnrichedLayout, MapGenerationRequest, NarrationRequest
import google.generativeai as genai
import os
import base64
from PIL import Image
from io import BytesIO
import requests
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

enricher_agent = Agent(
    name="object_enricher",
    seed=os.getenv("AGENT_SEED_2"),
    port=8002,
    endpoint=["http://localhost:8002/submit"]
)

MAP_GEN_ADDRESS = "REPLACE_WITH_MAP_GEN_ADDRESS"
NARRATION_ADDRESS = "REPLACE_WITH_NARRATION_ADDRESS"
BACKEND_URL = "http://localhost:8000"

def base64_to_image(b64_string: str) -> Image.Image:
    image_data = base64.b64decode(b64_string)
    return Image.open(BytesIO(image_data))

async def enrich_objects_with_gemini(objects: list, photos: list) -> list:
    model = genai.GenerativeModel('gemini-2.0-flash-exp')
    
    if not photos:
        # No photos — return objects as-is
        return objects
    
    # Convert first 3 photos to PIL images
    images = [base64_to_image(p) for p in photos[:3]]
    
    # Build the prompt with the object list
    object_summary = "\n".join([
        f"- Object {o['index']}: currently labeled '{o['category']}', located at position ({o['x']:.1f}m, {o['y']:.1f}m)"
        for o in objects
    ])
    
    prompt = f"""You are helping a blind person navigate an unfamiliar room. Here are the objects detected by a LiDAR scanner in this room:

{object_summary}

Below are photos of the room. For each object, provide a more specific, descriptive label that would help a blind person understand what it is (e.g., 'reception desk with computer monitor' instead of just 'table', or 'hand dryer' instead of just 'storage').

Respond in this exact JSON format:
{{
  "enriched": [
    {{"index": 0, "label": "specific label here"}},
    {{"index": 1, "label": "specific label here"}}
  ]
}}

If you can't identify an object from the photos, use the original label. Respond with ONLY the JSON, no other text."""
    
    content = [prompt] + images
    response = model.generate_content(content)
    
    # Parse JSON from response
    import json
    text = response.text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    
    try:
        enriched_data = json.loads(text)
        enriched_map = {e["index"]: e["label"] for e in enriched_data["enriched"]}
    except Exception as e:
        print(f"Failed to parse Gemini response: {e}")
        print(f"Raw response: {response.text}")
        return objects
    
    # Update objects with enriched labels
    for obj in objects:
        if obj["index"] in enriched_map:
            obj["category"] = enriched_map[obj["index"]]
    
    return objects

@enricher_agent.on_message(model=EnrichmentRequest)
async def enrich_scan(ctx: Context, sender: str, msg: EnrichmentRequest):
    ctx.logger.info(f"Enriching objects for room {msg.room_id}")
    
    objects_dict_list = [dict(o) for o in msg.layout.objects]
    enriched = await enrich_objects_with_gemini(objects_dict_list, msg.photos_base64)
    
    # Update the layout
    msg.layout.objects = enriched
    
    # Store in backend
    requests.patch(f"{BACKEND_URL}/rooms/{msg.room_id}", json={
        "enriched_objects": enriched,
        "status": "enriched"
    })
    
    enriched_layout = EnrichedLayout(room_id=msg.room_id, layout=msg.layout)
    
    # Fan out to both map generator and narration agent
    await ctx.send(MAP_GEN_ADDRESS, MapGenerationRequest(
        room_id=msg.room_id, layout=enriched_layout
    ))
    await ctx.send(NARRATION_ADDRESS, NarrationRequest(
        room_id=msg.room_id, layout=enriched_layout
    ))

if __name__ == "__main__":
    print(f"Object Enricher address: {enricher_agent.address}")
    enricher_agent.run()
```

**Step 4: Build Agent 3 — Map Generator (2-3 hours)**

Create `agent_map.py`:

```python
from uagents import Agent, Context
from schemas import MapGenerationRequest, MapGenerationResult
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import os
import requests
import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

map_agent = Agent(
    name="map_generator",
    seed=os.getenv("AGENT_SEED_3"),
    port=8003,
    endpoint=["http://localhost:8003/submit"]
)

BACKEND_URL = "http://localhost:8000"

def generate_braille_pdf(room_id: str, layout: dict) -> str:
    """Generate a dot-grid PDF representing the room layout."""
    filename = f"/tmp/braille_map_{room_id}.pdf"
    c = canvas.Canvas(filename, pagesize=A4)
    page_width, page_height = A4
    
    # Leave margins
    margin = 50
    usable_width = page_width - 2 * margin
    usable_height = page_height - 2 * margin - 100  # leave room for legend
    
    room_width = layout["room_width"]
    room_depth = layout["room_depth"]
    
    if room_width <= 0 or room_depth <= 0:
        c.drawString(100, 500, "Invalid room dimensions")
        c.save()
        return filename
    
    # Scale factor — fit room to page
    scale = min(usable_width / room_width, usable_height / room_depth)
    
    def to_page(x, y):
        return (margin + x * scale, margin + 100 + y * scale)
    
    # Draw room boundary with dots
    # Simple approach: dots along each wall
    c.setFillColorRGB(0, 0, 0)
    
    # Dot grid for walls
    dot_spacing = 8  # points between dots
    wall_points = [
        ((0, 0), (room_width, 0)),  # bottom wall
        ((room_width, 0), (room_width, room_depth)),  # right wall
        ((room_width, room_depth), (0, room_depth)),  # top wall
        ((0, room_depth), (0, 0))  # left wall
    ]
    
    for start, end in wall_points:
        sx, sy = to_page(start[0], start[1])
        ex, ey = to_page(end[0], end[1])
        length = ((ex-sx)**2 + (ey-sy)**2) ** 0.5
        num_dots = int(length / dot_spacing)
        for i in range(num_dots + 1):
            t = i / num_dots if num_dots > 0 else 0
            x = sx + t * (ex - sx)
            y = sy + t * (ey - sy)
            c.circle(x, y, 2, fill=1)
    
    # Draw doors as gaps (skip — simplified: mark doors with a different symbol)
    c.setFillColorRGB(0, 0, 1)
    for door in layout["doors"]:
        dx, dy = to_page(door["x"], door["y"])
        c.rect(dx - 6, dy - 6, 12, 12, fill=1)
    
    # Draw objects as dot clusters with numbered labels
    c.setFillColorRGB(0.8, 0.4, 0)
    legend_entries = []
    for obj in layout["objects"]:
        ox, oy = to_page(obj["x"], obj["y"])
        obj_width_pts = obj["width"] * scale
        obj_depth_pts = obj["depth"] * scale
        
        # Draw a small rectangle of dots for the object
        dots_x = max(2, int(obj_width_pts / dot_spacing))
        dots_y = max(2, int(obj_depth_pts / dot_spacing))
        for ix in range(dots_x):
            for iy in range(dots_y):
                dx = ox - obj_width_pts/2 + ix * dot_spacing
                dy = oy - obj_depth_pts/2 + iy * dot_spacing
                c.circle(dx, dy, 1.5, fill=1)
        
        # Label
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(ox + 5, oy + 5, str(obj["index"] + 1))
        c.setFillColorRGB(0.8, 0.4, 0)
        
        legend_entries.append(f"{obj['index'] + 1}. {obj['category']}")
    
    # Draw legend at bottom
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, 80, "Legend:")
    c.setFont("Helvetica", 10)
    y_pos = 65
    for entry in legend_entries:
        c.drawString(margin, y_pos, entry)
        y_pos -= 12
        if y_pos < 30:
            break
    
    # Title
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, page_height - margin, f"BrailleMap — Room Layout")
    c.setFont("Helvetica", 10)
    c.drawString(margin, page_height - margin - 15, 
                 f"Dimensions: {room_width:.1f}m x {room_depth:.1f}m")
    
    c.save()
    return filename

@map_agent.on_message(model=MapGenerationRequest)
async def generate_map(ctx: Context, sender: str, msg: MapGenerationRequest):
    ctx.logger.info(f"Generating map for room {msg.room_id}")
    
    layout_dict = {
        "room_width": msg.layout.layout.room_width,
        "room_depth": msg.layout.layout.room_depth,
        "walls": msg.layout.layout.walls,
        "doors": msg.layout.layout.doors,
        "objects": msg.layout.layout.objects
    }
    
    pdf_path = generate_braille_pdf(msg.room_id, layout_dict)
    
    # Upload to Cloudinary
    result = cloudinary.uploader.upload(
        pdf_path,
        resource_type="raw",
        folder="braillemap/pdfs"
    )
    pdf_url = result["secure_url"]
    
    # Update backend
    requests.patch(f"{BACKEND_URL}/rooms/{msg.room_id}", json={
        "pdf_url": pdf_url,
        "status_map_done": True
    })
    
    ctx.logger.info(f"PDF uploaded: {pdf_url}")

if __name__ == "__main__":
    print(f"Map Generator address: {map_agent.address}")
    map_agent.run()
```

**Step 5: Test the chain locally (1 hour)**

Run all three agents in separate terminals. Then trigger the chain manually by sending a fake `SpatialProcessingRequest` to Agent 1. Verify:
- Agent 1 logs "processing"
- Agent 2 logs "enriching" and calls Gemini
- Agent 3 logs "generating map" and PDF appears in Cloudinary

**End of Day 2 for Person B:** 3 of 4 agents working end-to-end from mock data. Agent 4 (narration + conversational) is tomorrow.

---

### Person A — Polish + Test Against Real Backend (Day 2)

- [ ] Switch `BackendClient.swift` baseURL to Person C's ngrok URL
- [ ] Test end-to-end: scan → photos → upload → verify room appears in MongoDB
- [ ] Add UI state: "Uploading... Processing... Ready!" based on polling `/rooms/{id}` status
- [ ] Add a room name input field (so the user can name the room being scanned)
- [ ] Build a simple "My Scans" screen that lists previously scanned rooms by fetching `GET /rooms`

---

### Person C — Backend Polish + Deploy to Vultr (Day 2)

**Step 1: Add endpoints Person B needs (2 hours)**

```python
@app.patch("/rooms/{room_id}")
async def update_room(room_id: str, updates: dict):
    rooms_collection.update_one({"_id": room_id}, {"$set": updates})
    return {"status": "updated"}

@app.get("/rooms/{room_id}/photos")
async def get_room_photos(room_id: str):
    room = rooms_collection.find_one({"_id": room_id}, {"photos": 1})
    return {"photos": room.get("photos", []) if room else []}

@app.get("/rooms")
async def list_rooms():
    rooms = list(rooms_collection.find({}, {"photos": 0, "scan_data": 0}))
    return {"rooms": rooms}

@app.post("/rooms/{room_id}/trigger")
async def trigger_processing(room_id: str):
    """Kicks off the agent pipeline by sending the scan to Agent 1."""
    room = rooms_collection.find_one({"_id": room_id})
    if not room:
        return {"error": "not found"}
    
    # Send message to Agent 1 via its HTTP endpoint
    SPATIAL_AGENT_URL = os.getenv("SPATIAL_AGENT_URL")  # from Person B
    # Use uAgents' HTTP protocol to send the message
    # (Person B will tell you the exact format)
    
    return {"status": "triggered"}
```

Make sure `/scan` automatically calls `/rooms/{id}/trigger` at the end.

**Step 2: Deploy backend to Vultr (2 hours)**

1. Spin up an Ubuntu 22.04 instance on Vultr (smallest tier is fine)
2. SSH in, install Python 3.11, clone your repo
3. Install dependencies in a venv
4. Set environment variables
5. Use `tmux` or `systemd` to keep uvicorn running
6. Open port 8000 in Vultr's firewall
7. Optional: use nginx + certbot for HTTPS

Give Person A the public URL. They swap out ngrok for this.

**Step 3: Start on React dashboard (3 hours)**

Build the room list view:
- Fetch `GET /rooms`
- Display as cards with room name, scan date, status
- Each card links to a detail view

Room detail view:
- Fetch `GET /rooms/{id}`
- Show 2D visualization (reuse whatever rendering approach — can be simple divs)
- Embed PDF preview (once `pdf_url` is set)
- Audio player (once `audio_url` is set)

---

### Day 2 Checkpoint

End of day sync:
- Can a scan go from iPhone → Vultr backend → MongoDB → trigger Agent 1 → Agent 2 → Agent 3 → PDF in Cloudinary? 
- If yes, you're ahead of schedule.
- If the chain breaks anywhere, debug before Day 3.

---

## Day 3 — Conversational Agent (The "Wow" Factor)

Today is Person B's heaviest day. Everyone else is polishing.

### Person B — Build the Conversational Agent (Agent 4)

The approach: instead of making Agent 4 a one-shot narration generator, make it a conversational endpoint that maintains the room layout as context and responds to voice queries.

**Step 1: Generate the initial narration text with ASI1-Mini (2 hours)**

First, get the static narration working. Create `agent_narration.py`:

```python
from uagents import Agent, Context
from schemas import NarrationRequest, NarrationResult
import requests
import os
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
import cloudinary.uploader

load_dotenv()

elevenlabs_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

narration_agent = Agent(
    name="narration_agent",
    seed=os.getenv("AGENT_SEED_4"),
    port=8004,
    endpoint=["http://localhost:8004/submit"]
)

BACKEND_URL = "http://localhost:8000"
ASI_API_URL = "https://api.asi1.ai/v1/chat/completions"  # verify current URL
ASI_API_KEY = os.getenv("ASI_API_KEY")

def generate_narration_with_asi1(layout: dict) -> str:
    """Use ASI1-Mini to generate natural language room description."""
    object_list = "\n".join([
        f"- {o['category']} at position ({o['x']:.1f}m, {o['y']:.1f}m)"
        for o in layout["objects"]
    ])
    
    door_info = ""
    if layout["doors"]:
        d = layout["doors"][0]
        door_info = f"The entrance door is at position ({d['x']:.1f}m, {d['y']:.1f}m)."
    
    prompt = f"""Generate a spoken walkthrough of this room for a blind person. The room is {layout['room_width']:.1f}m wide and {layout['room_depth']:.1f}m deep. {door_info}

Objects in the room:
{object_list}

Describe the room as if the person is entering through the door. Use directions like "to your left", "ahead of you", "to your right". Mention distances in meters. Keep it under 150 words. Start with "Welcome. You are entering..."."""
    
    response = requests.post(
        ASI_API_URL,
        headers={
            "Authorization": f"Bearer {ASI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "asi1-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 300
        }
    )
    
    return response.json()["choices"][0]["message"]["content"]

def generate_audio(text: str, room_id: str) -> str:
    audio = elevenlabs_client.text_to_speech.convert(
        text=text,
        voice_id="JBFqnCBsd6RMkjVDRZzb",
        model_id="eleven_multilingual_v2"
    )
    
    filepath = f"/tmp/narration_{room_id}.mp3"
    with open(filepath, "wb") as f:
        for chunk in audio:
            f.write(chunk)
    
    result = cloudinary.uploader.upload(
        filepath,
        resource_type="video",  # audio uses video resource type in Cloudinary
        folder="braillemap/audio"
    )
    return result["secure_url"]

@narration_agent.on_message(model=NarrationRequest)
async def generate_narration(ctx: Context, sender: str, msg: NarrationRequest):
    ctx.logger.info(f"Generating narration for room {msg.room_id}")
    
    layout_dict = {
        "room_width": msg.layout.layout.room_width,
        "room_depth": msg.layout.layout.room_depth,
        "walls": msg.layout.layout.walls,
        "doors": msg.layout.layout.doors,
        "objects": msg.layout.layout.objects
    }
    
    narration_text = generate_narration_with_asi1(layout_dict)
    audio_url = generate_audio(narration_text, msg.room_id)
    
    requests.patch(f"{BACKEND_URL}/rooms/{msg.room_id}", json={
        "audio_url": audio_url,
        "narration_text": narration_text,
        "status_audio_done": True
    })

if __name__ == "__main__":
    print(f"Narration Agent address: {narration_agent.address}")
    narration_agent.run()
```

Test: trigger the pipeline from a real scan, verify MP3 appears in Cloudinary and plays correctly.

**Step 2: Build conversational endpoint (4 hours)**

Now the interactive layer. Add a new HTTP endpoint on the backend (not a uAgent message — direct HTTP for low latency):

In `main.py` (Person C's backend):

```python
@app.post("/rooms/{room_id}/ask")
async def ask_about_room(room_id: str, question: dict):
    """Given a question about a room, return a spoken answer."""
    room = rooms_collection.find_one({"_id": room_id})
    if not room:
        return {"error": "not found"}
    
    layout = room.get("layout_2d", {})
    if not layout:
        return {"error": "room not processed yet"}
    
    user_question = question.get("question", "")
    
    # Build context from layout
    object_context = "\n".join([
        f"- {o['category']} at ({o['x']:.1f}m from left wall, {o['y']:.1f}m from entrance wall), size: {o['width']:.1f}m x {o['depth']:.1f}m"
        for o in layout.get("objects", [])
    ])
    
    system_prompt = f"""You are a navigation assistant for a visually impaired person exploring a room. 

Room dimensions: {layout.get('room_width', 0):.1f}m wide, {layout.get('room_depth', 0):.1f}m deep.
Entrance is at position (0, 0). Directions are relative to a person standing at the entrance facing into the room.
- "ahead" = positive Y direction (into the room)
- "left" = negative X direction
- "right" = positive X direction

Objects in the room:
{object_context}

Answer the person's question clearly and concisely, using meters and relative directions. Keep answers under 40 words."""
    
    # Call ASI1-Mini
    response = requests.post(
        "https://api.asi1.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {os.getenv('ASI_API_KEY')}",
            "Content-Type": "application/json"
        },
        json={
            "model": "asi1-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_question}
            ],
            "temperature": 0.5,
            "max_tokens": 150
        }
    )
    
    answer_text = response.json()["choices"][0]["message"]["content"]
    
    # Generate audio for the answer
    audio = elevenlabs_client.text_to_speech.convert(
        text=answer_text,
        voice_id="JBFqnCBsd6RMkjVDRZzb",
        model_id="eleven_flash_v2_5"  # faster model for low latency
    )
    
    # Stream audio back as base64 (simpler than uploading for quick Q&A)
    audio_bytes = b"".join(audio)
    audio_b64 = base64.b64encode(audio_bytes).decode()
    
    return {
        "question": user_question,
        "answer_text": answer_text,
        "answer_audio_base64": audio_b64
    }
```

**Why HTTP instead of a uAgent for the Q&A loop?** Latency. Voice conversations need sub-2-second response times. Adding uAgent message-passing for each Q&A adds overhead that makes the conversation feel laggy. The conversational endpoint is HTTP, but you can still describe it as "powered by Agent 4" in your pitch — because it calls ASI1-Mini and ElevenLabs, which are wrapped in your agent architecture.

**Step 3: iOS — Voice conversation interface (4 hours, Person A)**

Person A adds a "Talk to Room" button on each scanned room's detail view.

When tapped, it opens a voice chat view. Use `SFSpeechRecognizer` to transcribe the user's speech, send the text to `/rooms/{id}/ask`, receive the audio response, and play it via `AVAudioPlayer`.

Basic flow:
1. User taps a big microphone button
2. Speech recognizer listens, transcribes
3. User stops → transcribed text is sent to backend
4. Backend returns audio response
5. Audio plays automatically
6. Repeat

Keep the UI dead simple: big mic button, transcribed text visible, "thinking..." state, audio plays. Done.

---

### Person C — React Dashboard Polish (Day 3)

- Room detail page: show PDF embedded (iframe), audio player, layout visualization
- "Download PDF" button
- "Delete room" (optional)

### Person D — Pitch Deck v1 (Day 3)

Rough draft of 10 slides. Don't worry about design yet, just structure:
1. Title + team names
2. Problem (blind navigation, ADA compliance, cost barrier)
3. Solution (5-minute iPhone scan → Braille map + audio)
4. Demo (video placeholder)
5. Technical architecture (the big diagram)
6. Multi-agent system (Fetch.ai emphasis)
7. Tech stack (Gemini, ElevenLabs, Vultr, MongoDB, Cloudinary)
8. Market / impact
9. What's next
10. Thank you + contact

---

## Day 4 — Integration, Testing, Polish

Today nobody adds new features. You debug, polish, and test end-to-end.

### Full Pipeline Test (3 hours, whole team)

1. Person A scans a real room (their apartment, a classroom, a bathroom)
2. Upload happens
3. All 4 agents fire
4. PDF lands in Cloudinary
5. Audio lands in Cloudinary
6. Dashboard displays everything
7. Someone tries the conversational interface and asks 10 different questions
8. Log what breaks. Fix it.

### Register Agents on Agentverse (2 hours, Person B)

This is mandatory for Fetch.ai prize eligibility.

- Follow: https://fetch.ai/docs/guides/agents/intermediate/register-in-almanac
- Register all 4 agents with the Almanac contract
- Implement the Chat Protocol per https://innovationlab.fetch.ai/resources/docs/chat-protocol/chat-protocol-overview
- Verify agents appear in Agentverse search
- Test that agents are discoverable via ASI:One

### README (2 hours, Person D)

Write the project README with:
- `![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)` badge (mandatory)
- Architecture diagram
- Agent addresses (for Fetch.ai judges to verify)
- Setup instructions
- Demo video link (coming Day 5)

### Scan Multiple Demo Rooms (2 hours, Person A)

Scan 3-4 rooms you'll use in the demo:
- An empty classroom (expected best quality)
- A restroom (clearly bounded, useful real-world example)
- A hallway (linear navigation example)
- Optional: your apartment for testing

For each, verify the full pipeline works and the conversational responses are accurate.

---

## Day 5 — Demo Prep

### Record Backup Demo Video (3 hours, Person A + D)

Screen-record the full flow on iPhone and dashboard. Include:
- Scanning a room (30 sec)
- Upload and processing (20 sec)
- PDF download (10 sec)
- Audio walkthrough playing (40 sec)
- Live conversational Q&A (60 sec with 4-5 questions)

Edit to 3 minutes max. This is your safety net if live demo fails.

### Final Pitch Deck (2 hours, Person D)

Polish visuals. Add architecture diagram. Add live demo QR code (links to video).

### Rehearse the Demo (2 hours, whole team)

Each person practices their role:
- Who operates the iPhone?
- Who hands the judge earbuds?
- Who walks through the architecture?
- Who handles questions?

Time the demo: 3 minutes max. If you run long, cut.

### Sponsor Submissions (2 hours, Person D)

Submit to every eligible track on the Devpost (or LA Hacks submission platform):
- Fetch.ai (Most Impactful Vertical + Best Multi-Agent + Best ASI1-Mini)
- Gemini API
- ElevenLabs
- Vultr
- MongoDB
- Cloudinary
- Arista Networks
- GoDaddy (domain)

Each submission needs: project title, description, demo video link, GitHub repo links.

---

## Daily Standup Template

Each morning, 15 min:
1. What I finished yesterday
2. What I'm doing today
3. What's blocking me
4. Do I need help from another team member?

---

## Red Flags — When to Cut Scope

If by end of Day 3 any of these are still broken, cut features:

| Broken | Cut This |
|--------|----------|
| Agent chain doesn't complete | Remove ASI1-Mini from Agent 4, use Gemini instead (simpler) |
| Conversational interface too laggy | Cut to pre-generated audio walkthrough only |
| PDF generation buggy | Simplify to just wall outlines + numbered object markers, no fancy dot patterns |
| RoomPlan accuracy terrible in demo rooms | Pre-record demo video, don't do live scan at expo |
| Cloudinary integration broken | Store PDFs/audio directly on Vultr server, serve via FastAPI |

**The must-haves (don't cut these):**
- Scan → upload → MongoDB storage
- At least one agent processing (even if it's just spatial processing)
- PDF generated (even if simple)
- Audio narration (even if not conversational)
- Dashboard that shows the room

Everything else is polish.

---

## Final Checklist (Morning of LA Hacks)

- [ ] All repos pushed, no uncommitted changes
- [ ] Vultr backend running and responding
- [ ] Agents running on Agentverse (verify discoverable)
- [ ] iPhone app deployed to demo device (with developer trust set)
- [ ] 3-4 pre-scanned demo rooms in MongoDB
- [ ] Backup demo video on laptop + cloud
- [ ] Pitch deck ready (PDF + Keynote/PowerPoint backup)
- [ ] Domain registered, pointing to dashboard
- [ ] All sponsor submissions ready to fire
- [ ] Team has slept