"""
YARVIS — Servidor Cloud (Railway)
"""
import os, json, re, asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import anthropic

app = FastAPI()

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "cambia-esto-por-algo-secreto")

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
- Si el Mac no está conectado, puedes conversar e informar igualmente.
- Para comandos del Mac usa las etiquetas exactas."""

def extract_actions(text):
    actions = []
    for url in re.findall(r"\[URL:(https?://[^\]]+)\]", text):
        actions.append({"type": "url", "value": url})
    for cmd in re.findall(r"\[CMD:([^\]]+)\]", text):
        actions.append({"type": "cmd", "value": cmd})
    for app_ in re.findall(r"\[APP:([^\]]+)\]", text):
        actions.append({"type": "app", "value": app_})
    return actions

def clean_text(text):
    return re.sub(r"\[(?:URL|CMD|APP):[^\]]+\]", "", text).strip()

# ── Buscar index.html en raíz o en static/ ────────────────────────────────────
BASE = Path(__file__).parent
def find_index():
    for p in [BASE / "static" / "index.html", BASE / "index.html"]:
        if p.exists(): return p
    return None

def find_manifest():
    for p in [BASE / "static" / "manifest.json", BASE / "manifest.json"]:
        if p.exists(): return p
    return None

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
        raise HTTPException(status_code=500, detail="API key no configurada en Railway")

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

@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str, secret: str = ""):
    if secret != YARVIS_SECRET:
        raise HTTPException(status_code=401, detail="Clave incorrecta")
    histories.pop(session_id, None)
    return {"ok": True}

@app.get("/api/status")
async def status():
    return {"mac_online": bool(agents), "sessions": len(histories)}

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

# ── Servir archivos estáticos ─────────────────────────────────────────────────
static_dir = BASE / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/manifest.json")
async def manifest():
    p = find_manifest()
    if p: return FileResponse(str(p))
    raise HTTPException(404)

@app.get("/{full_path:path}")
async def serve_pwa(full_path: str):
    p = find_index()
    if p: return FileResponse(str(p))
    return {"status": "Yarvis server running — sube index.html al repo"}
