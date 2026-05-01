"""
YARVIS — Servidor Cloud
"""
import os, json, re
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import anthropic

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

histories: dict[str, list] = {}
agents:    dict[str, WebSocket] = {}

SYSTEM_PROMPT = """Eres Yarvis, asistente personal en español que puede controlar el Mac del usuario.
Etiquetas para acciones en el Mac:
- [URL:https://...] → abre URL en el navegador
- [CMD:comando]     → ejecuta en terminal
- [APP:nombre]      → abre aplicación

Responde siempre en español, máximo 3 frases, sin markdown ni asteriscos."""

def get_actions(text):
    out=[]
    for u in re.findall(r"\[URL:(https?://[^\]]+)\]",text): out.append({"type":"url","value":u})
    for c in re.findall(r"\[CMD:([^\]]+)\]",text):          out.append({"type":"cmd","value":c})
    for a in re.findall(r"\[APP:([^\]]+)\]",text):          out.append({"type":"app","value":a})
    return out

def clean(text): return re.sub(r"\[(?:URL|CMD|APP):[^\]]+\]","",text).strip()

class Msg(BaseModel):
    message: str
    session_id: str = "default"
    secret: str = ""

@app.post("/api/chat")
async def chat(msg: Msg):
    if msg.secret != YARVIS_SECRET:
        raise HTTPException(401, "Clave incorrecta")
    if not claude:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en Railway Variables")
    hist = histories.setdefault(msg.session_id, [])
    hist.append({"role":"user","content":msg.message})
    try:
        r = claude.messages.create(model="claude-sonnet-4-20250514",
            max_tokens=600, system=SYSTEM_PROMPT, messages=hist[-20:])
        reply = r.content[0].text
    except Exception as e:
        raise HTTPException(500, str(e))
    hist.append({"role":"assistant","content":reply})
    actions = get_actions(reply)
    display = clean(reply)
    if actions and agents:
        dead=[]
        for aid,ws in agents.items():
            try: await ws.send_text(json.dumps({"actions":actions}))
            except: dead.append(aid)
        for aid in dead: agents.pop(aid,None)
    return {"reply":display,"actions":actions,"mac_online":bool(agents)}

@app.get("/api/status")
async def status():
    return {"mac_online":bool(agents),"ok":True}

@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    if ws.query_params.get("secret","")!=YARVIS_SECRET:
        await ws.close(1008); return
    await ws.accept()
    aid=f"mac-{id(ws)}"; agents[aid]=ws
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        agents.pop(aid,None)

BASE = Path(__file__).parent
def find(name):
    for p in [BASE/"static"/name, BASE/name]:
        if p.exists(): return p
    return None

@app.get("/manifest.json")
async def manifest_route():
    p=find("manifest.json")
    if p: return FileResponse(str(p))
    raise HTTPException(404)

@app.get("/")
async def root():
    p=find("index.html")
    if p: return FileResponse(str(p),media_type="text/html")
    return JSONResponse({"status":"Yarvis running"})

@app.get("/{path:path}")
async def fallback(path: str):
    p=find("index.html")
    if p: return FileResponse(str(p),media_type="text/html")
    return JSONResponse({"status":"Yarvis running"})
