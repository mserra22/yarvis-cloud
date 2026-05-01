"""
YARVIS — Servidor Cloud (Railway)
"""
import os, json, re
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import anthropic

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

histories: dict[str, list] = {}
agents:    dict[str, WebSocket] = {}

SYSTEM_PROMPT = """Eres Yarvis, asistente personal en español. Controlas el ordenador del usuario cuando está encendido.
Etiquetas especiales para controlar el Mac:
- [URL:https://...] abre una URL en el navegador del Mac
- [CMD:comando]     ejecuta un comando en la terminal del Mac
- [APP:nombre]      abre una aplicación del Mac

Reglas:
- Responde SIEMPRE en español, máximo 3-4 frases, sin markdown.
- Si el Mac no está conectado, conversa igualmente sin mencionar las etiquetas.
- Usa las etiquetas solo cuando el usuario pida algo que requiera el ordenador."""

def extract_actions(text):
    actions = []
    for url in re.findall(r"\[URL:(https?://[^\]]+)\]", text):
        actions.append({"type": "url", "value": url})
    for cmd in re.findall(r"\[CMD:([^\]]+)\]", text):
        actions.append({"type": "cmd", "value": cmd})
    for a in re.findall(r"\[APP:([^\]]+)\]", text):
        actions.append({"type": "app", "value": a})
    return actions

def clean_text(text):
    return re.sub(r"\[(?:URL|CMD|APP):[^\]]+\]", "", text).strip()

# ── API ───────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    secret: str = ""

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET:
        raise HTTPException(status_code=401, detail="Clave incorrecta")
    if not claude:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY no configurada en Railway")

    hist = histories.setdefault(req.session_id, [])
    hist.append({"role": "user", "content": req.message})

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=hist[-20:]
        )
        reply = response.content[0].text
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    hist.append({"role": "assistant", "content": reply})
    actions = extract_actions(reply)
    display = clean_text(reply)

    if actions and agents:
        payload = json.dumps({"actions": actions})
        dead = []
        for aid, ws in agents.items():
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(aid)
        for aid in dead:
            agents.pop(aid, None)

    return {"reply": display, "actions": actions, "mac_online": bool(agents)}

@app.get("/api/status")
async def status():
    return {"mac_online": bool(agents), "sessions": len(histories)}

@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str, secret: str = ""):
    if secret != YARVIS_SECRET:
        raise HTTPException(status_code=401, detail="Clave incorrecta")
    histories.pop(session_id, None)
    return {"ok": True}

# ── WebSocket agente Mac ──────────────────────────────────────────────────────

@app.websocket("/ws/agent")
async def agent_ws(websocket: WebSocket):
    if websocket.query_params.get("secret", "") != YARVIS_SECRET:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    aid = f"mac-{id(websocket)}"
    agents[aid] = websocket
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        agents.pop(aid, None)

# ── Servir PWA ────────────────────────────────────────────────────────────────

BASE = Path(__file__).parent

def find_file(name):
    for p in [BASE / "static" / name, BASE / name]:
        if p.exists():
            return p
    return None

@app.get("/manifest.json")
async def manifest():
    p = find_file("manifest.json")
    if p:
        return FileResponse(str(p))
    raise HTTPException(404)

@app.get("/")
@app.get("/{full_path:path}")
async def serve_pwa(full_path: str = ""):
    p = find_file("index.html")
    if p:
        return FileResponse(str(p), media_type="text/html")
    return JSONResponse({"status": "Yarvis running — sube index.html al repo"})
