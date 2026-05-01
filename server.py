"""
YARVIS — Servidor Cloud con Claude + Groq + Memoria + Rutinas
Variables:
  GROQ_API_KEY       — conversación rápida (gratis, obligatorio)
  ANTHROPIC_API_KEY  — tareas complejas: código, apps, análisis (opcional)
  YARVIS_SECRET      — clave de acceso
  ELEVENLABS_API_KEY — voz tipo Jarvis (opcional)
"""
import os, json, re, base64, asyncio
from pathlib import Path
from datetime import datetime, timedelta
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
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")
ELEVEN_KEY    = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE  = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")

groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

# Claude solo se importa si hay API key
claude_client = None
if ANTHROPIC_KEY:
    try:
        import anthropic
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    except ImportError:
        pass

DATA_DIR     = Path("/data") if Path("/data").exists() else Path("/tmp")
MEMORY_FILE  = DATA_DIR / "yarvis_memory.json"

agents:           dict[str, WebSocket] = {}
session_histories: dict[str, list]     = {}

# ── Detección de tareas complejas ─────────────────────────────────────────────
COMPLEX_KEYWORDS = [
    "crea","crear","desarrolla","programa","escribe código","script","aplicación","app",
    "analiza","análisis","explica en detalle","diseña","arquitectura","base de datos",
    "función","clase","algoritmo","implementa","genera un","construye","html","python",
    "javascript","css","api","web","página","sistema","automatiza","macro"
]

def is_complex_task(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in COMPLEX_KEYWORDS)

# ── Memoria ───────────────────────────────────────────────────────────────────
def load_memory() -> dict:
    try:
        if MEMORY_FILE.exists():
            return json.loads(MEMORY_FILE.read_text())
    except Exception as e:
        print(f"Error cargando memoria: {e}")
    return {"user_facts":[],"pending_followups":[],"routines":[],
            "tasks":[],"location":None,"conversation_count":0}

def save_memory(mem: dict):
    try:
        MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Error guardando memoria: {e}")

memory = load_memory()

# ── Weather ───────────────────────────────────────────────────────────────────
WEATHER_CODES = {
    0:"despejado",1:"casi despejado",2:"parcialmente nublado",3:"nublado",
    45:"niebla",51:"llovizna ligera",61:"lluvia ligera",63:"lluvia moderada",
    65:"lluvia fuerte",71:"nieve ligera",80:"chubascos",95:"tormenta"
}

async def get_weather(lat: float, lon: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast",
                params={"latitude":lat,"longitude":lon,
                        "current":"temperature_2m,weather_code,wind_speed_10m","timezone":"auto"})
            if r.status_code == 200:
                d = r.json()["current"]
                return f"{round(d['temperature_2m'])}°C, {WEATHER_CODES.get(d['weather_code'],'variable')}, viento {round(d['wind_speed_10m'])} km/h"
    except: pass
    return "no disponible"

# ── Rutinas ───────────────────────────────────────────────────────────────────
def check_routine(text: str) -> dict | None:
    for r in memory.get("routines",[]):
        trigger = r.get("trigger","").lower()
        if trigger and trigger in text.lower():
            return r
    return None

async def routine_context(routine: dict) -> str:
    parts = [f"Rutina activa: {routine.get('description',routine.get('trigger'))}."]
    actions = routine.get("actions",[])
    if "weather" in actions and memory.get("location"):
        loc = memory["location"]
        w = await get_weather(loc["lat"],loc["lon"])
        parts.append(f"Tiempo en {loc.get('city','tu ciudad')}: {w}.")
    if "tasks" in actions:
        tasks = memory.get("tasks",[])
        parts.append(f"Tareas: {', '.join(tasks[:5])}" if tasks else "Sin tareas pendientes.")
    if "time" in actions:
        parts.append(f"Son las {datetime.now().strftime('%H:%M')} del {datetime.now().strftime('%A %d de %B')}.")
    return " ".join(parts)

# ── Prompt del sistema ────────────────────────────────────────────────────────
def build_system(mac_online: bool, use_claude: bool = False) -> str:
    now = datetime.now().strftime("%A %d de %B de %Y, %H:%M")
    if use_claude:
        base = f"""Eres Yarvis, asistente personal con capacidades avanzadas de programación e IA.
Fecha: {now}. Puedes crear aplicaciones completas, escribir código, analizar problemas complejos y diseñar sistemas.
Cuando crees código, hazlo completo y funcional. Responde en español."""
    else:
        base = f"""Eres Yarvis, asistente personal en español con voz tipo Jarvis de Iron Man.
Fecha: {now}. Directo, conciso, ligeramente formal. Máximo 2-3 frases. Sin markdown."""

    parts = [base]
    facts = memory.get("user_facts",[])
    if facts:
        parts.append("Conoces al usuario:\n" + "\n".join(f"- {f}" for f in facts[-15:]))

    followups = [f["text"] for f in memory.get("pending_followups",[])
                 if datetime.now() >= datetime.fromisoformat(f.get("due_after","2099-01-01"))]
    if followups:
        parts.append(f"Pregunta si es natural: {'; '.join(followups[:2])}")

    if mac_online:
        parts.append("Mac conectado. Usa [URL:https://...] [CMD:comando] [APP:nombre] cuando el usuario pida acciones en el Mac.")
    else:
        parts.append("Mac NO conectado. No uses etiquetas de acción. Si piden ejecutar algo en el Mac, diles que el agente no está activo.")

    return "\n\n".join(parts)

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

async def generate_voice(text: str):
    if not ELEVEN_KEY: return None
    clean = re.sub(r"[*_`#\[\]]","",text)[:500]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={"xi-api-key":ELEVEN_KEY,"Content-Type":"application/json","Accept":"audio/mpeg"},
                json={"text":clean,"model_id":"eleven_multilingual_v2",
                      "voice_settings":{"stability":0.55,"similarity_boost":0.80,"style":0.1,"use_speaker_boost":True}})
            if r.status_code == 200:
                return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        print(f"ElevenLabs: {e}")
    return None

# ── API Chat ──────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    secret: str = ""

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET:
        raise HTTPException(401, "Clave incorrecta")
    if not groq_client:
        raise HTTPException(500, "GROQ_API_KEY no configurada")

    mac_online = bool(agents)
    hist = session_histories.setdefault(req.session_id, [])

    # Contexto de rutina
    routine = check_routine(req.message)
    user_msg = req.message
    if routine:
        ctx = await routine_context(routine)
        user_msg = f"{req.message}\n[Contexto: {ctx}]"

    hist.append({"role":"user","content":user_msg})

    # Decidir qué modelo usar
    use_claude = claude_client and is_complex_task(req.message)
    ai_used = "claude" if use_claude else "groq"

    try:
        if use_claude:
            # Claude para tareas complejas (código, apps, análisis)
            r = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                system=build_system(mac_online, use_claude=True),
                messages=hist[-20:]
            )
            reply = r.content[0].text
        else:
            # Groq para conversación rápida
            r = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role":"system","content":build_system(mac_online)}] + hist[-20:],
                max_tokens=300,
                temperature=0.7
            )
            reply = r.choices[0].message.content
    except Exception as e:
        print(f"AI error ({ai_used}): {e}")
        raise HTTPException(500, f"Error IA: {str(e)}")

    hist.append({"role":"assistant","content":reply})
    acts    = extract_actions(reply)
    display = clean_text(reply)

    if acts and agents:
        payload = json.dumps({"actions":acts})
        dead = []
        for aid, ws in agents.items():
            try: await ws.send_text(payload)
            except: dead.append(aid)
        for aid in dead: agents.pop(aid, None)

    asyncio.create_task(update_memory(hist[-6:]))
    audio_b64 = await generate_voice(display)

    return {"reply":display,"actions":acts if mac_online else [],
            "mac_online":mac_online,"audio_b64":audio_b64,"ai_used":ai_used}

# ── Memoria en background ─────────────────────────────────────────────────────
async def update_memory(recent_hist: list):
    global memory
    if not groq_client or len(recent_hist) < 2: return
    try:
        conv = "\n".join([f"{m['role'].upper()}: {m['content'][:300]}" for m in recent_hist])
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":"""Extrae del diálogo en JSON:
{"facts":["hecho1"],"followups":[{"text":"¿tal el X?","hours_later":6}],"location":{"city":"","lat":0,"lon":0},"routine_request":{"trigger":"","description":"","actions":["weather","tasks","time"]},"tasks":["tarea"]}
Solo incluye campos con datos reales. Responde SOLO JSON."""},
            {"role":"user","content":conv}],
            max_tokens=300, temperature=0.2)
        text = re.sub(r"```json|```","",r.choices[0].message.content).strip()
        extracted = json.loads(text)
    except: return

    changed = False
    for f in extracted.get("facts",[]):
        if f and f not in memory["user_facts"]:
            memory["user_facts"].append(f); changed=True
    memory["user_facts"] = memory["user_facts"][-50:]

    for fu in extracted.get("followups",[]):
        try:
            due = (datetime.now()+timedelta(hours=float(fu.get("hours_later",6)))).isoformat()
            memory["pending_followups"].append({"text":fu["text"],"due_after":due}); changed=True
        except: pass
    cutoff = (datetime.now()-timedelta(days=7)).isoformat()
    memory["pending_followups"] = [f for f in memory["pending_followups"] if f.get("due_after","")>cutoff]

    loc = extracted.get("location")
    if loc and loc.get("lat") and loc.get("lon"):
        memory["location"]=loc; changed=True

    rr = extracted.get("routine_request")
    if rr and rr.get("trigger"):
        trigger = rr["trigger"].lower()
        if trigger not in [r.get("trigger","").lower() for r in memory["routines"]]:
            memory["routines"].append({"trigger":trigger,"description":rr.get("description",trigger),"actions":rr.get("actions",[])}); changed=True

    for t in extracted.get("tasks",[]):
        if t and t not in memory["tasks"]:
            memory["tasks"].append(t); changed=True

    memory["conversation_count"] = memory.get("conversation_count",0)+1
    if changed: save_memory(memory)

# ── WebSocket Mac agent ───────────────────────────────────────────────────────
@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    if ws.query_params.get("secret","") != YARVIS_SECRET:
        await ws.close(1008); return
    await ws.accept()
    aid = f"mac-{id(ws)}"
    agents[aid] = ws
    print(f"[+] Mac conectado: {aid}")
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        agents.pop(aid, None)
        print(f"[-] Mac desconectado: {aid}")

@app.get("/api/status")
async def status():
    return {"mac_online":bool(agents),"memories":len(memory.get("user_facts",[])),"routines":len(memory.get("routines",[])),"tasks":len(memory.get("tasks",[])),"claude_available":bool(claude_client),"voice":"elevenlabs" if ELEVEN_KEY else "browser"}

@app.get("/api/memory")
async def get_mem(secret: str=""):
    if secret!=YARVIS_SECRET: raise HTTPException(401)
    return memory

@app.delete("/api/memory")
async def clear_mem(secret: str=""):
    global memory
    if secret!=YARVIS_SECRET: raise HTTPException(401)
    memory={"user_facts":[],"pending_followups":[],"routines":[],"tasks":[],"location":None,"conversation_count":0}
    save_memory(memory); return {"ok":True}

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({"name":"Yarvis","short_name":"Yarvis","start_url":"/","display":"standalone","background_color":"#000","theme_color":"#000","icons":[]})

BASE = Path(__file__).parent
def find(name):
    for p in [BASE/"static"/name, BASE/name]:
        if p.exists(): return p
    return None

@app.get("/")
async def root():
    p=find("index.html")
    if p: return FileResponse(str(p),media_type="text/html")
    return JSONResponse({"status":"Yarvis API online","claude":bool(claude_client),"memories":len(memory.get("user_facts",[]))})

@app.get("/{path:path}")
async def spa(path: str):
    p=find("index.html")
    if p: return FileResponse(str(p),media_type="text/html")
    raise HTTPException(404)

