from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal, Optional

import uuid

import requests
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# Ensure repo root importable when running from anywhere
REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PYTHONPATH", str(REPO_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO_ROOT / ".env", override=False)
except Exception:
    pass

from agents.tools import dispatch  # noqa: E402
from agents.roomplan_tools import tactile_map_from_roomplan_json, upload_artifact as upload_roomplan_artifact  # noqa: E402

from backend.layout_brief import build_system_prompt, build_system_prompt_from_context  # noqa: E402
from backend.map_store import MapStore  # noqa: E402

app = FastAPI(title="dots backend", version="0.1.0")

DB_PATH = Path(tempfile.gettempdir()) / "dots_backend" / "maps.db"
store = MapStore(DB_PATH)

BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ASI_API_URL = os.environ.get("ASI_API_URL", "https://api.asi1.ai/v1/chat/completions")
ASI_MODEL = os.environ.get("ASI_MODEL", "asi1-mini")

ELEVENLABS_TOKEN_URL = "https://api.elevenlabs.io/v1/convai/conversation/token"


def _asi_api_key() -> str:
    # Back-compat with older env naming in this repo.
    return (os.environ.get("ASI_API_KEY") or os.environ.get("ASI_ONE_API_KEY") or "").strip()


def _elevenlabs_api_key() -> str:
    return (os.environ.get("ELEVENLABS_API_KEY") or "").strip()


def _elevenlabs_agent_id() -> str:
    return (os.environ.get("ELEVENLABS_AGENT_ID") or "").strip()


class UrlRequest(BaseModel):
    image_url: str = Field(..., description="Public http(s) URL to a floorplan image.")
    model: str = Field("gemini-3-pro-image-preview", description="Gemini image model id.")


class TactileResponse(BaseModel):
    tactile_png_url: str
    tactile_png_path: str

class AdaResponse(BaseModel):
    ada_pdf_url: str
    ada_report_pdf_path: str
    ada_summary: dict[str, int] = Field(default_factory=dict)
    ada_report_text: str = ""
    ada_findings_count: int = 0


class FloorplanArtifactsResponse(BaseModel):
    tactile_png_url: str
    tactile_png_path: str
    ada_pdf_url: str
    ada_report_pdf_path: str
    ada_summary: dict[str, int] = Field(default_factory=dict)
    ada_report_text: str = ""
    ada_findings_count: int = 0

class TactileMapCreateResponse(BaseModel):
    map_id: str
    voice_url: str
    chat_url: str
    tactile_png_url: str


class RoomplanMapRequest(BaseModel):
    roomplan_json: dict = Field(..., description="RoomPlan JSON object (already parsed).")
    source_name: str = Field("roomplan.json", description="Label for artifacts / id generation.")


class MapCreateResponse(BaseModel):
    map_id: str
    chat_url: str
    qr_url: str
    tactile_pdf_url: str | None = None


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str


class VoiceSessionResponse(BaseModel):
    conversation_token: str
    agent_id: str
    overrides: dict


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "").suffix or ".png"
    out_dir = Path(tempfile.gettempdir()) / "dots_backend"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"upload_{os.urandom(6).hex()}{suffix}"
    data = upload.file.read()
    out_path.write_bytes(data)
    return out_path

@app.post("/tactile/from-url", response_model=TactileResponse)
def tactile_from_url(req: UrlRequest) -> TactileResponse:
    dl = dispatch("download_image", {"url": req.image_url})
    if "error" in dl:
        raise HTTPException(status_code=400, detail=str(dl["error"]))

def _generate_ada_response(image_path: Path | str) -> AdaResponse:
    parsed = dispatch("parse_floorplan", {"image_path": str(image_path)})
    if "error" in parsed:
        raise HTTPException(status_code=500, detail=str(parsed["error"]))

    pdf_path = parsed.get("ada_report_pdf_path")
    if not isinstance(pdf_path, str) or not pdf_path.strip():
        note = (
            parsed.get("ada_report_pdf_note")
            or parsed.get("ada_note")
            or "ADA report PDF was not generated."
        )
        raise HTTPException(status_code=500, detail=str(note))

    up = dispatch("upload_artifact", {"file_path": pdf_path})
    if "error" in up:
        raise HTTPException(status_code=502, detail=str(up["error"]))

    return AdaResponse(
        ada_pdf_url=up["url"],
        ada_report_pdf_path=pdf_path,
        ada_summary=parsed.get("ada_summary") or {},
        ada_report_text=str(parsed.get("ada_report_text") or ""),
        ada_findings_count=len(parsed.get("ada_findings") or []),
    )


def _generate_tactile_response(image_path: Path | str, model: str) -> TactileResponse:
    t = dispatch(
        "tactile_map_from_image_nanobanana",
        {"image_path": str(image_path), "model": model},
    )
    if "error" in t:
        raise HTTPException(status_code=500, detail=str(t["error"]))

    up = dispatch("upload_artifact", {"file_path": t["png_path"]})
    if "error" in up:
        raise HTTPException(status_code=502, detail=str(up["error"]))

    return TactileResponse(tactile_png_url=up["url"], tactile_png_path=t["png_path"])

@app.post("/maps/from-floorplan-url", response_model=TactileMapCreateResponse)
def create_map_from_floorplan_url(req: UrlRequest, request: Request) -> TactileMapCreateResponse:
    # Download original
    dl = dispatch("download_image", {"url": req.image_url})
    if "error" in dl:
        raise HTTPException(status_code=400, detail=str(dl["error"]))

    # Generate tactile PNG
    t = dispatch(
        "tactile_map_from_image_nanobanana",
        {"image_path": dl["image_path"], "model": req.model},
    )
    if "error" in t:
        raise HTTPException(status_code=500, detail=str(t["error"]))

    up = dispatch("upload_artifact", {"file_path": t["png_path"]})
    if "error" in up:
        raise HTTPException(status_code=502, detail=str(up["error"]))

    # Generate map-specific Q&A context from the original floorplan image
    ctx_res = dispatch(
        "qa_context_from_floorplan_image_gemini",
        {"image_path": dl["image_path"], "model": "gemini-2.5-pro"},
    )
    if "error" in ctx_res:
        raise HTTPException(status_code=500, detail=str(ctx_res["error"]))

    map_id = uuid.uuid4().hex[:16]
    metadata = {"room_name": "Floorplan", "space_name": map_id}
    store.put_map(
        map_id=map_id,
        layout_2d=None,
        metadata=metadata,
        tactile_png_url=up["url"],
        context_text=str(ctx_res.get("context_text") or ""),
    )

    public_base = _public_base(request)
    return TactileMapCreateResponse(
        map_id=map_id,
        voice_url=f"{public_base}/m/{map_id}/voice",
        chat_url=f"{public_base}/m/{map_id}",
        tactile_png_url=up["url"],
    )


@app.post("/maps/from-floorplan-upload", response_model=TactileMapCreateResponse)
def create_map_from_floorplan_upload(
    request: Request,
    file: UploadFile = File(...),
    model: str = "gemini-3-pro-image-preview",
) -> TactileMapCreateResponse:
    # Save upload (this should be the *original* floorplan image)
    p = _save_upload(file)

    # Generate map-specific Q&A context from the original floorplan image
    ctx_res = dispatch(
        "qa_context_from_floorplan_image_gemini",
        {"image_path": str(p), "model": "gemini-2.5-pro"},
    )
    if "error" in ctx_res:
        raise HTTPException(status_code=500, detail=str(ctx_res["error"]))

    map_id = uuid.uuid4().hex[:16]
    metadata = {"room_name": (file.filename or "Floorplan"), "space_name": map_id}
    # Optional: upload the original image so the QR page can link to it
    up_src = dispatch("upload_artifact", {"file_path": str(p)})
    if "error" in up_src:
        raise HTTPException(status_code=502, detail=str(up_src["error"]))
    store.put_map(
        map_id=map_id,
        layout_2d=None,
        metadata=metadata,
        tactile_png_url=up_src["url"],
        context_text=str(ctx_res.get("context_text") or ""),
    )

    public_base = _public_base(request)
    return TactileMapCreateResponse(
        map_id=map_id,
        voice_url=f"{public_base}/m/{map_id}/voice",
        chat_url=f"{public_base}/m/{map_id}",
        tactile_png_url=up_src["url"],
    )


@app.post("/maps/from-tactile-upload", response_model=TactileMapCreateResponse)
def create_map_from_tactile_upload(
    request: Request,
    file: UploadFile = File(...),
) -> TactileMapCreateResponse:
    """
    Fast path: user already has a tactile PNG (NanoBanana output).
    We skip image generation and only create per-map Q&A context + broadcast URLs.
    """
    p = _save_upload(file)

    up_tactile = dispatch("upload_artifact", {"file_path": str(p)})
    if "error" in up_tactile:
        raise HTTPException(status_code=502, detail=str(up_tactile["error"]))

    ctx_res = dispatch(
        "qa_context_from_tactile_image_gemini",
        {"image_path": str(p), "model": "gemini-2.5-pro"},
    )
    if "error" in ctx_res:
        raise HTTPException(status_code=500, detail=str(ctx_res["error"]))

    map_id = uuid.uuid4().hex[:16]
    metadata = {"room_name": (file.filename or "Tactile Map"), "space_name": map_id}
    store.put_map(
        map_id=map_id,
        layout_2d=None,
        metadata=metadata,
        tactile_png_url=up_tactile["url"],
        context_text=str(ctx_res.get("context_text") or ""),
    )

    public_base = _public_base(request)
    return TactileMapCreateResponse(
        map_id=map_id,
        voice_url=f"{public_base}/m/{map_id}/voice",
        chat_url=f"{public_base}/m/{map_id}",
        tactile_png_url=up_tactile["url"],
    )
def _generate_floorplan_artifacts_response(
    image_path: Path | str, model: str
) -> FloorplanArtifactsResponse:
    tactile = _generate_tactile_response(image_path, model)
    ada = _generate_ada_response(image_path)
    return FloorplanArtifactsResponse(
        tactile_png_url=tactile.tactile_png_url,
        tactile_png_path=tactile.tactile_png_path,
        ada_pdf_url=ada.ada_pdf_url,
        ada_report_pdf_path=ada.ada_report_pdf_path,
        ada_summary=ada.ada_summary,
        ada_report_text=ada.ada_report_text,
        ada_findings_count=ada.ada_findings_count,
    )


@app.post("/tactile/from-url", response_model=TactileResponse)
def tactile_from_url(req: UrlRequest) -> TactileResponse:
    dl = dispatch("download_image", {"url": req.image_url})
    if "error" in dl:
        raise HTTPException(status_code=400, detail=str(dl["error"]))
    return _generate_tactile_response(dl["image_path"], req.model)


@app.post("/tactile/from-upload", response_model=TactileResponse)
def tactile_from_upload(
    file: UploadFile = File(...),
    model: str = "gemini-3-pro-image-preview",
) -> TactileResponse:
    p = _save_upload(file)
    return _generate_tactile_response(p, model)

@app.post("/maps/from-roomplan", response_model=MapCreateResponse)
def create_map_from_roomplan(req: RoomplanMapRequest, request: Request) -> MapCreateResponse:
    # Generate tactile PDF + layout
    res = tactile_map_from_roomplan_json(
        roomplan_json=json_dumps_compact(req.roomplan_json),
        source_name=req.source_name,
    )
    if "error" in res:
        raise HTTPException(status_code=400, detail=str(res["error"]))

    up = upload_roomplan_artifact(res["pdf_path"])
    if "error" in up:
        raise HTTPException(status_code=502, detail=str(up["error"]))

    map_id = uuid.uuid4().hex[:16]
    store.put_map(
        map_id=map_id,
        layout_2d=res["layout_2d"],
        metadata=res["metadata"],
        tactile_pdf_url=up["url"],
    )

    public_base = _public_base(request)
    chat_url = f"{public_base}/m/{map_id}"
    qr_url = f"{public_base}/m/{map_id}/qr"
    return MapCreateResponse(
        map_id=map_id,
        chat_url=chat_url,
        qr_url=qr_url,
        tactile_pdf_url=up["url"],
    )


@app.get("/m/{map_id}", response_class=HTMLResponse)
def map_chat_page(map_id: str) -> str:
    rec = store.get_map(map_id)
    if not rec:
        raise HTTPException(status_code=404, detail="map not found")

    links = ""
    if rec.tactile_pdf_url:
        links += f'<p><a href="{rec.tactile_pdf_url}">Download tactile PDF</a></p>'
    if rec.tactile_png_url:
        links += f'<p><a href="{rec.tactile_png_url}">Download tactile PNG</a></p>'

    # Tiny no-build chat UI.
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Map Q&A</title>
    <style>
      body {{ font-family: system-ui, -apple-system, sans-serif; margin: 24px; max-width: 720px; }}
      #log {{ border: 1px solid #ddd; padding: 12px; border-radius: 10px; min-height: 240px; white-space: pre-wrap; }}
      .row {{ display: flex; gap: 8px; margin-top: 12px; }}
      input {{ flex: 1; padding: 10px; border-radius: 10px; border: 1px solid #ccc; }}
      button {{ padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; background: #fafafa; }}
    </style>
  </head>
  <body>
    <h2>Ask about this tactile map</h2>
    {links}
    <p><a href="/m/{map_id}/voice">Open voice mode</a></p>
    <div id="log"></div>
    <div class="row">
      <input id="msg" placeholder="e.g., where is the nearest table?" />
      <button id="send">Send</button>
    </div>
    <script>
      const mapId = "{map_id}";
      let sessionId = localStorage.getItem("dots_session_" + mapId) || "";
      const log = document.getElementById("log");
      const msg = document.getElementById("msg");
      const btn = document.getElementById("send");

      function append(who, text) {{
        log.textContent += `\\n${{who}}: ${{text}}\\n`;
        log.scrollTop = log.scrollHeight;
      }}

      async function send() {{
        const text = (msg.value || "").trim();
        if (!text) return;
        msg.value = "";
        append("you", text);
        const resp = await fetch(`/m/${{mapId}}/chat`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ message: text, session_id: sessionId || null }})
        }});
        const data = await resp.json();
        if (!resp.ok) {{
          append("error", data.detail || "request failed");
          return;
        }}
        sessionId = data.session_id;
        localStorage.setItem("dots_session_" + mapId, sessionId);
        append("agent", data.reply);
      }}

      btn.addEventListener("click", send);
      msg.addEventListener("keydown", (e) => {{ if (e.key === "Enter") send(); }});
      append("agent", "Hi — ask me anything about this map (objects, directions, distances).");
    </script>
  </body>
</html>
""".strip()


@app.get("/m/{map_id}/voice", response_class=HTMLResponse)
def map_voice_page(map_id: str) -> str:
    rec = store.get_map(map_id)
    if not rec:
        raise HTTPException(status_code=404, detail="map not found")

    # This page expects the user to have mic permission. It uses the ElevenLabs
    # web SDK from their CDN and mints a per-map conversation token via our backend.
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Map Voice Q&A</title>
    <style>
      body {{ font-family: system-ui, -apple-system, sans-serif; margin: 24px; max-width: 720px; }}
      .card {{ border: 1px solid #ddd; padding: 14px; border-radius: 12px; }}
      button {{ padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; background: #fafafa; }}
      #status {{ margin-top: 10px; white-space: pre-wrap; color: #333; }}
    </style>
  </head>
  <body>
    <h2>Voice Q&A for this tactile map</h2>
    <div class="card">
      <p>This uses ElevenLabs Conversational AI. Allow microphone access when prompted.</p>
      <p><a href="/m/{map_id}">Back to text chat</a></p>
      <button id="start">Start voice</button>
      <button id="stop" disabled>Stop</button>
      <div id="status"></div>
    </div>

    <script type="module">
      const mapId = "{map_id}";
      const status = document.getElementById("status");
      const startBtn = document.getElementById("start");
      const stopBtn = document.getElementById("stop");
      let conv = null;

      function setStatus(msg) {{
        status.textContent = msg;
      }}

      startBtn.onclick = async () => {{
        try {{
          setStatus("Loading ElevenLabs web client…");
          // ElevenLabs web SDK exports `Conversation` (not `ElevenLabs`).
          // Use esm.sh which serves browser-friendly ESM.
          const mod = await import("https://esm.sh/@elevenlabs/client@1.2.1");
          const Conversation = mod?.Conversation;
          if (!Conversation?.startSession) {{
            setStatus("ElevenLabs web SDK failed to load (Conversation.startSession missing).");
            return;
          }}
          setStatus("Minting conversation token…");
          const r = await fetch(`/m/${{mapId}}/voice_session`, {{ method: "POST" }});
          const data = await r.json();
          if (!r.ok) {{
            setStatus(data.detail || "Failed to mint token");
            return;
          }}

          setStatus("Starting conversation… (allow microphone)");
          conv = await Conversation.startSession({{
            conversationToken: data.conversation_token,
            connectionType: "webrtc",
            // The overrides come from our backend (map-specific prompt).
            overrides: data.overrides,
            onConnect: () => setStatus("Listening. Ask your question out loud."),
            onDisconnect: () => setStatus("Disconnected."),
            onError: (msg) => setStatus("Error: " + (msg?.message || String(msg))),
          }});

          startBtn.disabled = true;
          stopBtn.disabled = false;
        }} catch (e) {{
          setStatus("Error: " + (e?.message || String(e)));
        }}
      }};

      stopBtn.onclick = async () => {{
        try {{
          if (conv?.endSession) await conv.endSession();
        }} catch (e) {{
          // ignore
        }}
        conv = null;
        startBtn.disabled = false;
        stopBtn.disabled = true;
        setStatus("Stopped.");
      }};
    </script>
  </body>
</html>
""".strip()


@app.get("/m/{map_id}/qr")
def map_qr(map_id: str, request: Request):
    # Returns a PNG QR that points to /m/{map_id}
    rec = store.get_map(map_id)
    if not rec:
        raise HTTPException(status_code=404, detail="map not found")
    try:
        import qrcode
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"qrcode not installed: {exc}")

    public_base = BASE_URL or str(request.base_url).rstrip("/")
    url = f"{public_base}/m/{map_id}"
    img = qrcode.make(url)
    out_dir = Path(tempfile.gettempdir()) / "dots_backend" / "qr"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{map_id}.png"
    img.save(out_path)
    return JSONResponse({"map_id": map_id, "url": url, "qr_png_path": str(out_path)})


@app.post("/m/{map_id}/voice_session", response_model=VoiceSessionResponse)
def map_voice_session(map_id: str) -> VoiceSessionResponse:
    """
    Mint a short-lived ElevenLabs Conversational AI token for *this map*.

    This keeps contexts separated by generating a per-map system prompt override
    (built from that map's layout_2d) while using a single ElevenLabs agent_id.
    """
    rec = store.get_map(map_id)
    if not rec:
        raise HTTPException(status_code=404, detail="map not found")
    eleven_key = _elevenlabs_api_key()
    eleven_agent = _elevenlabs_agent_id()
    if not eleven_key:
        raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY not set on server")
    if not eleven_agent:
        raise HTTPException(status_code=500, detail="ELEVENLABS_AGENT_ID not set on server")

    if rec.context_text:
        system_prompt = build_system_prompt_from_context(rec.context_text, rec.metadata)
    elif rec.layout_2d:
        system_prompt = build_system_prompt(rec.layout_2d, rec.metadata)
    else:
        raise HTTPException(status_code=500, detail="map has no context")
    first_message = "Hi — ask me anything about this tactile map."

    resp = requests.get(
        ELEVENLABS_TOKEN_URL,
        params={"agent_id": eleven_agent},
        headers={"xi-api-key": eleven_key},
        timeout=10,
    )
    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail=f"ElevenLabs token mint failed: {resp.status_code} {resp.text[:200]}",
        )
    body = resp.json()
    token = body.get("token") or body.get("conversation_token")
    if not token:
        raise HTTPException(status_code=502, detail="ElevenLabs token response missing token")

    # This matches the pattern in ConnectDots/braillemap-agents/voice_session.py:
    # client passes these overrides into ElevenLabs.startConversation(...).
    overrides = {
        "agent": {
            "prompt": {"prompt": system_prompt},
            "first_message": first_message,
        }
    }

    return VoiceSessionResponse(
        conversation_token=str(token),
        agent_id=str(eleven_agent),
        overrides=overrides,
    )


@app.post("/m/{map_id}/chat", response_model=ChatResponse)
def map_chat(map_id: str, req: ChatRequest) -> ChatResponse:
    rec = store.get_map(map_id)
    if not rec:
        raise HTTPException(status_code=404, detail="map not found")

    asi_key = _asi_api_key()
    if not asi_key:
        raise HTTPException(status_code=500, detail="ASI_API_KEY not set on server")

    session_id = (req.session_id or "").strip() or uuid.uuid4().hex[:16]
    prior = store.get_chat_messages(map_id=map_id, session_id=session_id)

    if rec.context_text:
        system = build_system_prompt_from_context(rec.context_text, rec.metadata)
    elif rec.layout_2d:
        system = build_system_prompt(rec.layout_2d, rec.metadata)
    else:
        raise HTTPException(status_code=500, detail="map has no context")
    messages = [{"role": "system", "content": system}]

    # Keep a short window for cost and latency.
    for m in prior[-8:]:
        if isinstance(m, dict) and m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})

    resp = requests.post(
        ASI_API_URL,
        headers={"Authorization": f"Bearer {asi_key}", "Content-Type": "application/json"},
        json={"model": ASI_MODEL, "messages": messages, "temperature": 0.2, "max_tokens": 400},
        timeout=60,
    )
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"ASI request failed: {resp.status_code} {resp.text[:200]}")

    reply = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    new_hist = prior + [{"role": "user", "content": req.message}, {"role": "assistant", "content": reply}]
    store.upsert_chat_messages(map_id=map_id, session_id=session_id, messages=new_hist[-40:])

    return ChatResponse(session_id=session_id, reply=reply)


def json_dumps_compact(obj) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _public_base(request: Request) -> str:
    """
    Prefer request.base_url during local testing even if PUBLIC_BASE_URL is set.
    This prevents returning links to a remote server that doesn't have the local map_id.
    """
    req_base = str(request.base_url).rstrip("/")
    host = (request.url.hostname or "").lower() if getattr(request, "url", None) else ""
    if host in {"127.0.0.1", "localhost"}:
        return req_base
    return BASE_URL or req_base

@app.post("/ada/from-url", response_model=AdaResponse)
def ada_from_url(req: UrlRequest) -> AdaResponse:
    dl = dispatch("download_image", {"url": req.image_url})
    if "error" in dl:
        raise HTTPException(status_code=400, detail=str(dl["error"]))
    return _generate_ada_response(dl["image_path"])


@app.post("/ada/from-upload", response_model=AdaResponse)
def ada_from_upload(file: UploadFile = File(...)) -> AdaResponse:
    p = _save_upload(file)
    return _generate_ada_response(p)


@app.post(
    "/floorplan-artifacts/from-url",
    response_model=FloorplanArtifactsResponse,
)
def floorplan_artifacts_from_url(req: UrlRequest) -> FloorplanArtifactsResponse:
    dl = dispatch("download_image", {"url": req.image_url})
    if "error" in dl:
        raise HTTPException(status_code=400, detail=str(dl["error"]))
    return _generate_floorplan_artifacts_response(dl["image_path"], req.model)


@app.post(
    "/floorplan-artifacts/from-upload",
    response_model=FloorplanArtifactsResponse,
)
def floorplan_artifacts_from_upload(
    file: UploadFile = File(...),
    model: str = "gemini-3-pro-image-preview",
) -> FloorplanArtifactsResponse:
    p = _save_upload(file)
    return _generate_floorplan_artifacts_response(p, model)
