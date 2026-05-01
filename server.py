"""
YARVIS — Servidor Cloud (Railway)
Variables de entorno necesarias:
  ANTHROPIC_API_KEY  — clave de Anthropic
  YARVIS_SECRET      — clave de acceso
  ELEVENLABS_API_KEY — clave de ElevenLabs (para voz tipo Jarvis)
"""
import os, json, re, base64
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import anthropic
import httpx

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET  = os.environ.get("YARVIS_SECRET", "yarvis-secret")
ELEVEN_KEY     = os.environ.get("ELEVENLABS_API_KEY", "")

# Voz "Daniel" de ElevenLabs — británica, grave, tipo Jarvis
# Puedes cambiarla por otra en: https://elevenlabs.io/voices
ELEVEN_VOICE   = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")
ELEVEN_MODEL   = "eleven_multilingual_v2"

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

histories: dict[str, list] = {}
agents:    dict[str, WebSocket] = {}

SYSTEM_PROMPT = """Eres Yarvis, asistente personal en español. Controlas el ordenador del usuario cuando está encendido.
Etiquetas disponibles:
- [URL:https://...] abre una URL en el Mac
- [CMD:comando]     ejecuta en terminal del Mac
- [APP:nombre]      abre una aplicación del Mac
- [IMG:keyword]     imagen de fondo en inglés (1-2 palabras, ej: [IMG:space nebula])

Responde en español, máximo 2-3 frases, sin markdown ni asteriscos.
Incluye siempre [IMG:...] con una imagen relevante al tema."""

def extract_actions(text):
    acts = []
    for url in re.findall(r"\[URL:(https?://[^\]]+)\]", text):
        acts.append({"type":"url","value":url})
    for cmd in re.findall(r"\[CMD:([^\]]+)\]", text):
        acts.append({"type":"cmd","value":cmd})
    for a in re.findall(r"\[APP:([^\]]+)\]", text):
        acts.append({"type":"app","value":a})
    return acts

def extract_image(text):
    m = re.search(r"\[IMG:([^\]]+)\]", text)
    return m.group(1).strip() if m else "technology futuristic"

def clean_text(t):
    return re.sub(r"\[(?:URL|CMD|APP|IMG):[^\]]+\]", "", t).strip()

async def generate_voice(text: str) -> str | None:
    """Genera audio con ElevenLabs y devuelve base64. None si no está configurado."""
    if not ELEVEN_KEY:
        return None
    clean = text.replace("*","").replace("_","").replace("#","")[:500]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={
                    "xi-api-key": ELEVEN_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg"
                },
                json={
                    "text": clean,
                    "model_id": ELEVEN_MODEL,
                    "voice_settings": {
                        "stability": 0.55,
                        "similarity_boost": 0.80,
                        "style": 0.15,
                        "use_speaker_boost": True
                    }
                }
            )
            if r.status_code == 200:
                return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        print(f"ElevenLabs error: {e}")
    return None

# ── API ───────────────────────────────────────────────────────────────────────

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
        r = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=hist[-20:]
        )
        reply = r.content[0].text
    except Exception as e:
        raise HTTPException(500, str(e))

    hist.append({"role":"assistant","content":reply})
    acts    = extract_actions(reply)
    image   = extract_image(reply)
    display = clean_text(reply)

    # Generar audio con ElevenLabs
    audio_b64 = await generate_voice(display)

    # Enviar acciones al agente Mac
    if acts and agents:
        payload = json.dumps({"actions":acts})
        dead = []
        for aid, ws in agents.items():
            try: await ws.send_text(payload)
            except: dead.append(aid)
        for aid in dead: agents.pop(aid, None)

    return {
        "reply":     display,
        "actions":   acts,
        "mac_online": bool(agents),
        "image":     image,
        "audio_b64": audio_b64   # None si no hay ElevenLabs
    }

@app.get("/api/status")
async def status():
    return {
        "mac_online": bool(agents),
        "sessions":   len(histories),
        "voice":      "elevenlabs" if ELEVEN_KEY else "browser"
    }

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

# ── Servir frontend ───────────────────────────────────────────────────────────

BASE = Path(__file__).parent

def find(name):
    for p in [BASE/"static"/name, BASE/name]:
        if p.exists(): return p
    return None

@app.get("/manifest.json")
async def manifest():
    p = find("manifest.json")
    if p: return FileResponse(str(p), media_type="application/manifest+json")
    raise HTTPException(404)

@app.get("/")
async def root():
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    return JSONResponse({"status":"Yarvis API online"})

@app.get("/{path:path}")
async def spa(path: str):
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    raise HTTPException(404)
