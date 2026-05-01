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
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

histories: dict[str, list] = {}
agents:    dict[str, WebSocket] = {}

SYSTEM_PROMPT = """Eres Yarvis, asistente personal en español. Controlas el ordenador del usuario cuando está encendido.
Etiquetas para controlar el Mac:
- [URL:https://...] abre una URL
- [CMD:comando]     ejecuta en terminal
- [APP:nombre]      abre una aplicación

Responde en español, máximo 3 frases, sin markdown."""

def extract_actions(text):
    acts = []
    for url in re.findall(r"\[URL:(https?://[^\]]+)\]", text):
        acts.append({"type":"url","value":url})
    for cmd in re.findall(r"\[CMD:([^\]]+)\]", text):
        acts.append({"type":"cmd","value":cmd})
    for a in re.findall(r"\[APP:([^\]]+)\]", text):
        acts.append({"type":"app","value":a})
    return acts

def clean_text(t):
    return re.sub(r"\[(?:URL|CMD|APP):[^\]]+\]","",t).strip()

# ── Archivos estáticos ────────────────────────────────────────────────────────
BASE = Path(__file__).parent

def find(name):
    for p in [BASE/"static"/name, BASE/name]:
        if p.exists(): return p
    return None

# ── API POST (registrado primero, sin conflicto) ──────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    secret: str = ""

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET:
        raise HTTPException(401, "Clave incorrecta")
    if not claude:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en Railway")
    hist = histories.setdefault(req.session_id, [])
    hist.append({"role":"user","content":req.message})
    try:
        r = claude.messages.create(model="claude-sonnet-4-20250514",
            max_tokens=600, system=SYSTEM_PROMPT, messages=hist[-20:])
        reply = r.content[0].text
    except Exception as e:
        raise HTTPException(500, str(e))
    hist.append({"role":"assistant","content":reply})
    acts = extract_actions(reply)
    display = clean_text(reply)
    if acts and agents:
        payload = json.dumps({"actions":acts})
        dead = []
        for aid, ws in agents.items():
            try: await ws.send_text(payload)
            except: dead.append(aid)
        for aid in dead: agents.pop(aid, None)
    return {"reply":display, "actions":acts, "mac_online":bool(agents)}

@app.get("/api/status")
async def status():
    return {"mac_online":bool(agents), "sessions":len(histories)}

@app.delete("/api/history/{sid}")
async def clear(sid: str, secret: str = ""):
    if secret != YARVIS_SECRET: raise HTTPException(401, "Clave incorrecta")
    histories.pop(sid, None)
    return {"ok":True}

@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    if ws.query_params.get("secret","") != YARVIS_SECRET:
        await ws.close(1008); return
    await ws.accept()
    aid = f"mac-{id(ws)}"
    agents[aid] = ws
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        agents.pop(aid, None)

# ── Servir SPA — rutas GET explícitas, sin app.mount() ───────────────────────

@app.get("/manifest.json")
async def manifest():
    p = find("manifest.json")
    if p: return FileResponse(str(p), media_type="application/manifest+json")
    raise HTTPException(404)

@app.get("/")
async def root():
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    return JSONResponse({"status":"Yarvis API online — sube index.html al repo"})

@app.get("/{path:path}")
async def spa(path: str):
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    raise HTTPException(404)
