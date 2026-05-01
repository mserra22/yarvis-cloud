"""
YARVIS — Servidor Cloud (Railway)
Ejecutar: uvicorn server:app --host 0.0.0.0 --port $PORT
"""
import os, json, re, asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import anthropic

app = FastAPI()

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET  = os.environ.get("YARVIS_SECRET", "cambia-esto-por-algo-secreto")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# Historial por sesión (en memoria; se reinicia si el servidor se reinicia)
histories: dict[str, list] = {}

# Agentes Mac conectados por WebSocket
agents: dict[str, WebSocket] = {}

SYSTEM_PROMPT = """Eres Yarvis, asistente personal en español. Controlas el ordenador del usuario cuando está encendido.
Etiquetas especiales para controlar el Mac (solo cuando el usuario lo pida):
- [URL:https://...] abre una URL en el navegador del Mac
- [CMD:comando]     ejecuta un comando en la terminal del Mac
- [APP:nombre]      abre una aplicación del Mac

Reglas:
- Responde SIEMPRE en español, máximo 3-4 frases, sin markdown.
- Si el Mac no está conectado, aún puedes conversar, informar y ayudar.
- Para comandos del Mac usa las etiquetas exactas."""

def extract_actions(text: str) -> list[dict]:
    actions = []
    for url in re.findall(r"\[URL:(https?://[^\]]+)\]", text):
        actions.append({"type": "url", "value": url})
    for cmd in re.findall(r"\[CMD:([^\]]+)\]", text):
        actions.append({"type": "cmd", "value": cmd})
    for app in re.findall(r"\[APP:([^\]]+)\]", text):
        actions.append({"type": "app", "value": app})
    return actions

def clean_text(text: str) -> str:
    return re.sub(r"\[(?:URL|CMD|APP):[^\]]+\]", "", text).strip()

# ── API REST ──────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    secret: str = ""

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET:
        raise HTTPException(status_code=401, detail="Clave incorrecta")
    if not claude:
        raise HTTPException(status_code=500, detail="API key no configurada")

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

    # Enviar acciones al agente Mac si está conectado
    mac_online = bool(agents)
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

    return {
        "reply": display,
        "actions": actions,
        "mac_online": bool(agents)
    }

@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str, secret: str = ""):
    if secret != YARVIS_SECRET:
        raise HTTPException(status_code=401, detail="Clave incorrecta")
    histories.pop(session_id, None)
    return {"ok": True}

@app.get("/api/status")
async def status():
    return {"mac_online": bool(agents), "sessions": len(histories)}

# ── WebSocket para agente Mac ─────────────────────────────────────────────────

@app.websocket("/ws/agent")
async def agent_ws(websocket: WebSocket):
    secret = websocket.query_params.get("secret", "")
    if secret != YARVIS_SECRET:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    agent_id = f"mac-{id(websocket)}"
    agents[agent_id] = websocket
    print(f"[+] Mac agent connected: {agent_id}")

    try:
        while True:
            # El agente puede enviar mensajes de estado
            msg = await websocket.receive_text()
            print(f"[agent] {msg}")
    except WebSocketDisconnect:
        agents.pop(agent_id, None)
        print(f"[-] Mac agent disconnected: {agent_id}")

# ── Servir PWA ────────────────────────────────────────────────────────────────

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/{full_path:path}")
async def serve_pwa(full_path: str):
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"error": "PWA no encontrada"}
