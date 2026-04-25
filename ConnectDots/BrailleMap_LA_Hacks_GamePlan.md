# BrailleMap — LA Hacks 2026 Game Plan

---

## 1. Product Positioning

### One-Liner
**BrailleMap** turns a 5-minute iPhone scan into a print-ready tactile map with audio walkthrough — replacing a process that today costs $1,000–5,000 and takes weeks.

### The Problem
The ADA (Americans with Disabilities Act) requires public accommodations to be accessible, and tactile wayfinding maps are a key part of that. But producing them today requires hiring specialized accessibility consultants, manual CAD drafting, and custom fabrication. A single tactile map of a building floor can cost thousands and take weeks to produce.

Result: most public buildings simply don't have them. Hotels, hospitals, university buildings, shopping centers, government offices — the maps don't exist because the production pipeline is broken.

### The Solution
BrailleMap is an AI-powered multi-agent system that automates the entire pipeline:

1. A facility manager scans a room with an iPhone (LiDAR-equipped)
2. AI agents process the scan — structuring spatial data, enriching object labels, and generating a tactile map layout
3. The output is a Braille-ready PDF + a spoken audio walkthrough of the space
4. Maps are stored in a cloud database, accessible through a web dashboard for printing, sharing, and managing

**What used to take weeks and thousands of dollars now takes 5 minutes and costs nothing.**

### Who Is It For?

**Primary user: Facility / property operations staff**
- University campus facilities coordinators
- Hotel general managers and operations teams
- Hospital and clinic administrators
- Government building managers
- Shopping center / mall property management

These people are responsible for ADA compliance but rarely have budget for specialized accessibility vendors. They already carry iPhones for inspections and documentation.

**Secondary beneficiary: Blind and visually impaired individuals**
They receive the output (tactile maps + audio walkthrough) but don't operate the scanning tool themselves.

**Why this framing matters for the pitch:**
Don't lead with "an app for blind people" — lead with "a compliance and operations tool that makes buildings accessible at near-zero cost." The judges will find this more credible and scalable. The humanitarian impact is the *result*, not the *pitch*.

### Competitive Landscape
- **LightHouse for the Blind** and similar orgs produce tactile maps manually → expensive, slow, doesn't scale
- **Indoor mapping tools** (Matterport, etc.) create visual 3D scans → not designed for accessibility output
- **Apple's RoomPlan** provides raw spatial data → but no one has built the accessibility output layer on top

BrailleMap sits at the intersection: commodity hardware (iPhone) + AI agents to automate what currently requires human specialists.

---

## 2. Technical Architecture

### System Diagram

```
┌─────────────┐       ┌──────────────────────────────────────────────────┐
│  iPhone App  │       │              VULTR CLOUD (Backend)               │
│  (Swift)     │       │                                                  │
│              │       │  ┌─────────────────────────────────────────────┐ │
│  RoomPlan    │──────▶│  │  FastAPI Server (API Gateway)               │ │
│  LiDAR Scan  │ POST  │  │  - Receives scan data + images              │ │
│              │ JSON  │  │  - Triggers agent pipeline                  │ │
│  + Capture   │       │  │  - Returns results to dashboard             │ │
│    photos    │       │  └──────────────┬──────────────────────────────┘ │
└─────────────┘       │                 │                                │
                      │                 ▼                                │
                      │  ┌─────────────────────────────────────────────┐ │
                      │  │  FETCH.AI AGENT PIPELINE (uAgents)          │ │
                      │  │                                             │ │
                      │  │  Agent 1: Spatial Processor                 │ │
                      │  │    - Normalizes RoomPlan JSON               │ │
                      │  │    - Projects 3D → 2D top-down floor plan   │ │
                      │  │    - Outputs: room boundary + object coords │ │
                      │  │              │                               │ │
                      │  │              ▼                               │ │
                      │  │  Agent 2: Object Enricher                   │ │
                      │  │    - Receives object list + captured photos  │ │
                      │  │    - Calls Gemini Vision API                │ │
                      │  │    - "table" → "check-in counter w/ monitor"│ │
                      │  │              │                               │ │
                      │  │              ▼                               │ │
                      │  │  Agent 3: Map Generator          ──────┐    │ │
                      │  │    - 2D layout → Braille dot grid      │    │ │
                      │  │    - Generates PDF + legend        parallel  │ │
                      │  │    - Stores PDF via Cloudinary      runs │   │ │
                      │  │                                         │   │ │
                      │  │  Agent 4: Narration Agent          ─────┘   │ │
                      │  │    - Generates room description text        │ │
                      │  │    - Calls ElevenLabs TTS API               │ │
                      │  │    - Outputs: MP3 audio walkthrough         │ │
                      │  └─────────────────────────────────────────────┘ │
                      │                 │                                │
                      │                 ▼                                │
                      │  ┌─────────────────────────────────────────────┐ │
                      │  │  MongoDB Atlas                              │ │
                      │  │  - Scan metadata, room geometry,            │ │
                      │  │    enriched labels, PDF URL, audio URL      │ │
                      │  └─────────────────────────────────────────────┘ │
                      └──────────────────────────────────────────────────┘
                                        │
                                        ▼
                      ┌──────────────────────────────────────────────────┐
                      │  REACT WEB DASHBOARD (Cloudinary Starter Kit)   │
                      │  - View all scanned rooms                       │
                      │  - Download Braille PDF                         │
                      │  - Play audio walkthrough                       │
                      │  - Manage building/room inventory               │
                      └──────────────────────────────────────────────────┘
```

---

## 3. Component Breakdown

### 3A. iPhone App (The Scanner)

**What it does:** Scans a room using Apple's RoomPlan API and sends structured data to cloud backend.

**Tech:** Swift, RoomPlan framework, ARKit

**Key details:**
- RoomPlan requires an iPhone/iPad with LiDAR (iPhone Pro 12+)
- API outputs a `CapturedRoom` object containing:
  - Walls (position, dimensions, orientation)
  - Doors and windows (position, type)
  - Objects (category, position, dimensions) — categories include: table, chair, sofa, bed, sink, toilet, bathtub, oven, refrigerator, TV, storage, staircase, and more
- Export `CapturedRoom` as JSON (serialize positions, dimensions, categories)
- Also capture 3-5 reference photos of the room during scanning (ARSession frames)
- POST the JSON + images to the FastAPI backend

**Why RoomPlan instead of raw video + YOLO:**
RoomPlan gives you structured 3D spatial data out of the box — object positions in real-world coordinates, wall boundaries, door locations. Trying to reconstruct this from a video feed with object detection would take weeks, not hours.

**Implementation priority:** HIGH — this is the data source for everything else.

**Estimated time:** 4-6 hours

**Docs:**
- RoomPlan: https://developer.apple.com/documentation/roomplan
- CapturedRoom: https://developer.apple.com/documentation/roomplan/capturedroom

---

### 3B. Backend API Server

**What it does:** Receives scan data, orchestrates agents, serves results to dashboard.

**Tech:** Python, FastAPI, hosted on Vultr

**Endpoints:**
```
POST /scan              — Upload scan JSON + images, triggers agent pipeline
GET  /rooms             — List all scanned rooms
GET  /rooms/{id}        — Room details + PDF URL + audio URL
GET  /rooms/{id}/pdf    — Download Braille PDF
GET  /rooms/{id}/audio  — Stream audio walkthrough
```

**Why Vultr:** Free cloud credits for hackathon participants. You need compute somewhere — Vultr gives you a VM with Python, and it qualifies for the "Best Use of Vultr" prize (portable screens per team member). Spin up an Ubuntu instance, deploy via SSH. If Vultr gives you trouble on the day, fall back to any VPS but you lose that prize.

**Estimated time:** 2-3 hours

---

### 3C. Fetch.ai Agent Pipeline (Core Product Logic)

**Tech:** Fetch.ai uAgents framework, Python, registered on Agentverse

**Mandatory requirements from Fetch.ai:**
- Use uAgents framework OR Fetch.ai SDK
- Register ALL agents on Agentverse
- Implement Chat Protocol (agents must be discoverable via ASI:One)
- Include `innovationlab` badge in README
- Follow their README structure

---

**Agent 1 — Spatial Processor**

| | |
|---|---|
| **Input** | Raw CapturedRoom JSON (3D positions, dimensions, categories) |
| **Process** | Extract floor-plane projection (drop Y-axis, keep X and Z). Normalize coordinates to a standard grid (e.g., 200x200 unit grid). Identify room boundaries from wall segments. Place objects as rectangular footprints. Handle edge cases: overlapping objects, objects against walls. |
| **Output** | 2D room layout JSON: room boundary polygon, list of objects with `{label, x, y, width, depth}`, door positions with orientation |

---

**Agent 2 — Object Enricher**

| | |
|---|---|
| **Input** | Object list from Agent 1 + reference photos from scan |
| **Process** | For each object, send the cropped image region + RoomPlan category to Gemini Vision API. Prompt: *"This object was detected as [category] in a room scan. Based on this image, provide a more specific label useful for a visually impaired person. Respond with only the label, e.g., 'reception desk with computer' or 'water fountain'."* Replace generic labels with enriched ones. |
| **Output** | Enriched object list with descriptive labels |
| **Sponsor** | This is your **Gemini API** integration |

---

**Agent 3 — Map Generator**

| | |
|---|---|
| **Input** | 2D room layout with enriched labels |
| **Process** | Define a Braille-compatible dot grid. Map room boundary to dot borders (solid dots for walls). Map objects to simplified dot shapes. Generate a legend section mapping symbols to labels. Render as PDF: Page 1 = dot map, Page 2 = legend. Upload PDF to Cloudinary. |
| **Output** | PDF URL (stored in Cloudinary) |
| **Sponsor** | This is your **Cloudinary** integration |

**On the dot mapping algorithm:** Start simple. Walls = continuous dot lines along boundary. Objects = filled dot rectangles at their position. Doors = gaps in the wall dot line. Each object gets a small number/letter label that maps to the legend. Don't over-engineer the Braille grid — for the hackathon, the visual representation of "here's what would be raised bumps" is sufficient.

---

**Agent 4 — Narration Agent**

| | |
|---|---|
| **Input** | 2D room layout with enriched labels + door position |
| **Process** | Use ASI1-Mini (Fetch.ai's LLM) or Gemini to generate a natural language walkthrough: *"Entering through the main door, you are facing a rectangular room approximately 8 meters wide and 6 meters deep. Immediately to your left is a reception desk. Along the far wall are three chairs. The restroom entrance is on your right, approximately 4 meters from the entry door."* Send text to ElevenLabs TTS API. Use a calm, clear voice preset. |
| **Output** | MP3 audio file URL |
| **Sponsor** | This is your **ElevenLabs** integration. Also where **ASI1-Mini** gets used (Fetch.ai "Best ASI-1 Mini Implementation" sub-prize). |

---

**Inter-agent communication:**
- Agents communicate via Fetch.ai's message protocol
- Agent 1 → Agent 2 → (Agent 3 + Agent 4 in parallel)
- The FastAPI server triggers Agent 1 and collects final outputs from Agents 3 and 4

**Estimated time:** 8-12 hours total (this is the bulk of the hackathon)

---

### 3D. MongoDB Atlas (Data Layer)

**Schema:**
```json
{
  "buildings": {
    "_id": "ObjectId",
    "name": "Pauley Pavilion",
    "address": "301 Westwood Plaza, LA",
    "manager_id": "user_123",
    "created_at": "timestamp"
  },
  "rooms": {
    "_id": "ObjectId",
    "building_id": "ref → buildings",
    "name": "Room 201 - Restroom",
    "raw_scan": {},
    "layout_2d": {},
    "enriched_objects": [],
    "pdf_url": "https://res.cloudinary.com/...",
    "audio_url": "https://...",
    "scanned_at": "timestamp",
    "scanned_by": "user_123"
  }
}
```

**Why MongoDB:** Free Atlas tier. JSON scan data stores natively. Also claims the "Best Use of MongoDB Atlas" prize with minimal extra effort.

**Estimated time:** 1-2 hours

---

### 3E. React Dashboard (Cloudinary Starter Kit)

**Tech:** React via `create-cloudinary-react`, Cloudinary for media

**Key screens:**
1. **Building Overview** — list of all scanned rooms
2. **Room Detail** — 2D visual preview, PDF download, audio playback, scan metadata
3. **Upload/Scan Status** — shows pipeline progress

PDFs and room preview images are stored/transformed/served through Cloudinary. Use their image transformations for thumbnails. Built with their required starter kit.

**Estimated time:** 4-6 hours

---

## 4. Sponsor Prize Targeting

| Sponsor | Track | Prize | Your Integration | Extra Effort |
|---------|-------|-------|-----------------|-------------|
| **Fetch.ai** | Most Impactful Vertical Solution | **$2,500** | Core agent architecture, all 4 agents on Agentverse | Primary focus |
| **Fetch.ai** | Best Multi-Agent System | **$1,000** | Same submission — emphasize 4-agent orchestration | None (same work) |
| **Fetch.ai** | Best ASI-1 Mini Implementation | **$1,500** | Agent 4 uses ASI1-Mini for narration text generation | Minimal |
| **Gemini API** | Best Use of Gemini API | Google Swag Kit | Agent 2 uses Gemini Vision for object enrichment | Already built in |
| **ElevenLabs** | Best Use of ElevenLabs | Wireless Earbuds | Agent 4 uses ElevenLabs TTS for audio walkthrough | Already built in |
| **Vultr** | Best Use of Vultr | Portable Screens | Backend hosted on Vultr cloud compute | Just deploy there |
| **MongoDB** | Best Use of MongoDB Atlas | M5Stack IoT Kit | Room/scan data stored in Atlas | Already built in |
| **Cloudinary** | Cloudinary Challenge | **$500/person** | Dashboard built with starter kit, PDFs served via Cloudinary | Already built in |
| **Arista** | Connect the Dots | Bose QC + Claude Pro | Submit as "connecting physical spaces to accessible information" | Just submit |
| **GoDaddy** | Best Domain Name | Digital Gift Card | Register braillemap.app or braillemap.xyz | 5 minutes |

**You are eligible for 10 prize categories from one unified product.**

---

## 5. Task Allocation

### Person A — Mobile / Scanner Lead
- Build Swift iPhone app with RoomPlan
- LiDAR scanning, CapturedRoom serialization, photo capture
- Upload flow to backend API
- Demo scanning at venue (hours 20-28)

### Person B — Agent Pipeline Lead
- Set up Fetch.ai uAgents framework
- Build all 4 agents
- Register on Agentverse, implement Chat Protocol
- Integrate Gemini Vision API + ElevenLabs API + ASI1-Mini
- **This is the hardest role — pick your strongest backend dev**

### Person C — Backend + Dashboard Lead
- FastAPI server setup, deploy on Vultr
- MongoDB Atlas setup and schemas
- React dashboard with Cloudinary starter kit
- PDF generation support (coordinate with Person B)

### Person D (4th member)
- GoDaddy domain registration
- README documentation (Fetch.ai badge, structure)
- Pitch deck and demo video preparation
- Testing, demo rehearsal, sponsor submission logistics

---

## 6. Timeline (36 Hours)

### Hours 0-2: Setup Sprint
- [ ] Everyone reads Fetch.ai starter pack docs
- [ ] Person A: Create Xcode project, test RoomPlan on iPhone Pro
- [ ] Person B: Set up uAgents boilerplate, Agentverse account, API keys (Gemini, ElevenLabs)
- [ ] Person C: Spin up Vultr instance, init FastAPI + MongoDB Atlas cluster

### Hours 2-8: Core Build
- [ ] Person A: Working scanner → JSON export → POST to backend
- [ ] Person B: Agent 1 (Spatial Processor) + Agent 2 (Object Enricher)
- [ ] Person C: API endpoints + MongoDB CRUD + basic data flow

### Hours 8-14: Full Pipeline
- [ ] Person A: Refine scanning, edge cases, reference photo capture
- [ ] Person B: Agent 3 (Map Generator — PDF) + Agent 4 (Narration — ElevenLabs)
- [ ] Person C: Start React dashboard with Cloudinary kit
- [ ] **MILESTONE: End-to-end flow works (scan → agents → PDF + audio)**

### Hours 14-20: Integration & Polish
- [ ] Person B: Register agents on Agentverse, Chat Protocol
- [ ] Person C: Complete dashboard (room list, detail, download, playback)
- [ ] Everyone: Integration testing with real scans

### Hours 20-28: Hardening
- [ ] Run demo scans at Pauley Pavilion (hallway, restroom, classroom)
- [ ] Fix bugs from real-world testing
- [ ] Person D: Pitch deck, README with Fetch.ai badge, demo video
- [ ] Register GoDaddy domain

### Hours 28-36: Demo Prep
- [ ] Rehearse demo (3-4 min max)
- [ ] Record backup demo video (in case live demo fails)
- [ ] Submit to ALL eligible sponsor tracks (10 submissions)
- [ ] Final README and documentation polish

---

## 7. Demo Script (3 Minutes)

**[0:00-0:30] The Problem**
"There are 7 million blind or visually impaired Americans. When they enter an unfamiliar building — a hotel, hospital, campus building — they have no spatial map. Tactile wayfinding maps exist, but producing one costs thousands and takes weeks. Most buildings don't have them."

**[0:30-1:00] The Solution**
"BrailleMap changes that. A facility manager scans a room with their iPhone in under 5 minutes. Our AI agent pipeline automatically produces a print-ready Braille map and a spoken audio walkthrough."

**[1:00-2:00] Live Demo**
- Show iPhone scanning (or show pre-recorded scan)
- Show data flowing to dashboard
- Show generated PDF with dot-pattern map and legend
- Play ElevenLabs audio walkthrough describing the room

**[2:00-2:30] Architecture**
"Four Fetch.ai agents on Agentverse orchestrate the pipeline: a Spatial Processor structures the LiDAR data, an Object Enricher uses Gemini Vision to identify specific objects, a Map Generator produces the Braille PDF, and a Narration Agent uses ElevenLabs to speak the room aloud."

**[2:30-3:00] Impact**
"What used to cost $3,000 and take 3 weeks now takes 5 minutes at near-zero cost. We scanned [X rooms] here at Pauley Pavilion to prove it. Every hotel, hospital, and university can now make their spaces accessible."

---

## 8. Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| RoomPlan scan quality poor in crowded space | Pre-scan rooms early (hours 20-24). Also find a nearby empty classroom. |
| Fetch.ai uAgents learning curve too steep | Person B starts docs at hour 0. If uAgents is painful by hour 6, fall back to Fetch.ai SDK (still eligible). |
| Braille dot mapping algorithm is complex | Start simple: walls = dot borders, objects = dot rectangles, doors = gaps. Refine only if time allows. |
| Gemini Vision returns bad/generic labels | Keep RoomPlan's original label as fallback. Enrichment is additive — system works without it. |
| Live demo fails on stage | Record backup video at hour 28. Have dashboard screenshots as static fallback. |
| ASI1-Mini access issues | Agent 4 can use Gemini for text generation as backup. Note ASI1-Mini usage in README regardless. |

---

## 9. Key Links

| Resource | URL |
|----------|-----|
| RoomPlan docs | https://developer.apple.com/documentation/roomplan |
| CapturedRoom reference | https://developer.apple.com/documentation/roomplan/capturedroom |
| Fetch.ai starter pack | Link in challenge doc (Google Doc) |
| Agentverse | https://agentverse.ai/ |
| Gemini API | https://ai.google.dev/ |
| ElevenLabs API | https://elevenlabs.io/docs |
| Cloudinary React Kit | https://www.npmjs.com/package/create-cloudinary-react |
| MongoDB Atlas (free tier) | https://www.mongodb.com/atlas |
| Vultr signup | MLH partner link for free credits |