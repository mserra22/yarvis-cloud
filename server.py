"""
YARVIS — Servidor Cloud (Railway)
Variables de entorno:
  GROQ_API_KEY       — clave de Groq (gratis en console.groq.com)
  YARVIS_SECRET      — clave de acceso que tú eliges
  ELEVENLABS_API_KEY — opcional, para voz tipo Jarvis
"""
import os, json, re, base64
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from groq import Groq
import httpx

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

GROQ_KEY      = os.environ.get("GROQ_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")
ELEVEN_KEY    = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE  = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")

groq = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

histories: dict[str, list] = {}
agents:    dict[str, WebSocket] = {}

SYSTEM_PROMPT = """Eres Yarvis, asistente personal en español. Controlas el ordenador del usuario.
Etiquetas disponibles:
- [URL:https://...] abre una URL en el Mac
- [CMD:comando]     ejecuta en terminal del Mac
- [APP:nombre]      abre una aplicación del Mac

Responde en español, máximo 2-3 frases, sin markdown ni asteriscos."""

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
    return re.sub(r"\[(?:URL|CMD|APP):[^\]]+\]", "", t).strip()

async def generate_voice(text: str):
    if not ELEVEN_KEY:
        return None
    clean = re.sub(r"[*_`#\[\]]", "", text)[:500]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={"xi-api-key": ELEVEN_KEY,
                         "Content-Type": "application/json",
                         "Accept": "audio/mpeg"},
                json={"text": clean,
                      "model_id": "eleven_multilingual_v2",
                      "voice_settings": {"stability": 0.55,
                                         "similarity_boost": 0.80,
                                         "style": 0.1,
                                         "use_speaker_boost": True}}
            )
            if r.status_code == 200:
                return base64.b64encode(r.content).decode("utf-8")
            print(f"ElevenLabs {r.status_code}: {r.text[:200]}")
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
    if not groq:
        raise HTTPException(500, "GROQ_API_KEY no configurada en Railway")

    hist = histories.setdefault(req.session_id, [])
    hist.append({"role": "user", "content": req.message})

    try:
        r = groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + hist[-20:],
            max_tokens=300,
            temperature=0.7
        )
        reply = r.choices[0].message.content
    except Exception as e:
        print(f"Groq error: {e}")
        raise HTTPException(500, f"Error IA: {str(e)}")

    hist.append({"role": "assistant", "content": reply})
    acts    = extract_actions(reply)
    display = clean_text(reply)

    audio_b64 = await generate_voice(display)

    if acts and agents:
        payload = json.dumps({"actions": acts})
        dead = []
        for aid, ws in agents.items():
            try: await ws.send_text(payload)
            except: dead.append(aid)
        for aid in dead: agents.pop(aid, None)

    return {"reply": display, "actions": acts,
            "mac_online": bool(agents), "audio_b64": audio_b64}

@app.get("/api/status")
async def status():
    return {"mac_online": bool(agents), "sessions": len(histories),
            "voice": "elevenlabs" if ELEVEN_KEY else "browser",
            "ai": "groq" if GROQ_KEY else "none"}

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

# ── Manifest ──────────────────────────────────────────────────────────────────

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "Yarvis", "short_name": "Yarvis",
        "start_url": "/", "display": "standalone",
        "background_color": "#000000", "theme_color": "#000000",
        "icons": []
    })

# ── Frontend ──────────────────────────────────────────────────────────────────

BASE = Path(__file__).parent

def find(name):
    for p in [BASE/"static"/name, BASE/name]:
        if p.exists(): return p
    return None

@app.get("/")
async def root():
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    return JSONResponse({"status": "Yarvis API online"})

@app.get("/{path:path}")
async def spa(path: str):
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    raise HTTPException(404)
