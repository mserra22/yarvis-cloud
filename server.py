"""
YARVIS — Servidor Cloud con Memoria, Rutinas y Aprendizaje
Variables: GROQ_API_KEY, YARVIS_SECRET, ELEVENLABS_API_KEY (opcional)
Volumen Railway montado en /data para persistencia
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
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")
ELEVEN_KEY    = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE  = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")

groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

# Directorio de datos persistentes (necesita volumen en Railway montado en /data)
DATA_DIR = Path("/data") if Path("/data").exists() else Path("/tmp")
MEMORY_FILE = DATA_DIR / "yarvis_memory.json"

agents: dict[str, WebSocket] = {}

# Historial de sesión en memoria (se pierde al reiniciar, intencionalmente)
session_histories: dict[str, list] = {}

# ── Sistema de Memoria ────────────────────────────────────────────────────────

def load_memory() -> dict:
    try:
        if MEMORY_FILE.exists():
            return json.loads(MEMORY_FILE.read_text())
    except Exception as e:
        print(f"Error cargando memoria: {e}")
    return {
        "user_facts": [],
        "pending_followups": [],
        "routines": [],
        "tasks": [],
        "location": None,
        "conversation_count": 0
    }

def save_memory(mem: dict):
    try:
        MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Error guardando memoria: {e}")

memory = load_memory()

# ── Weather (Open-Meteo, gratis sin API key) ──────────────────────────────────

WEATHER_CODES = {
    0:"despejado",1:"casi despejado",2:"parcialmente nublado",3:"nublado",
    45:"niebla",48:"niebla con escarcha",51:"llovizna ligera",53:"llovizna moderada",
    55:"llovizna intensa",61:"lluvia ligera",63:"lluvia moderada",65:"lluvia fuerte",
    71:"nieve ligera",73:"nieve moderada",75:"nieve intensa",
    80:"chubascos ligeros",81:"chubascos moderados",82:"chubascos fuertes",
    95:"tormenta",96:"tormenta con granizo",99:"tormenta fuerte con granizo"
}

async def get_weather(lat: float, lon: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon,
                        "current": "temperature_2m,weather_code,wind_speed_10m",
                        "timezone": "auto"}
            )
            if r.status_code == 200:
                d = r.json()["current"]
                temp = round(d["temperature_2m"])
                desc = WEATHER_CODES.get(d["weather_code"], "variable")
                wind = round(d["wind_speed_10m"])
                return f"{temp}°C, {desc}, viento {wind} km/h"
    except Exception as e:
        print(f"Weather error: {e}")
    return "no disponible"

# ── Extracción de memoria con IA ──────────────────────────────────────────────

async def extract_memory_facts(conversation: list) -> dict:
    """Usa Groq para extraer hechos memorables y seguimientos pendientes."""
    if not groq_client or len(conversation) < 2:
        return {"facts": [], "followups": []}
    try:
        conv_text = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in conversation[-6:]])
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{
                "role": "system",
                "content": """Analiza esta conversación y extrae en JSON:
1. "facts": lista de hechos importantes sobre el usuario (nombre, preferencias, actividades, trabajo, familia, etc.)
   Solo hechos nuevos y relevantes. Máximo 3. Array de strings cortos.
2. "followups": lista de eventos futuros sobre los que preguntar después.
   Formato: {"text": "¿qué tal fue X?", "hours_later": N}
   Solo si el usuario mencionó algo que va a hacer próximamente. Máximo 2.
3. "location": si mencionó su ciudad/ubicación, {"city": "...", "lat": X, "lon": Y} o null.
4. "routine_request": si pidió configurar una rutina, {"trigger": "...", "description": "..."} o null.
5. "tasks": lista de tareas/recordatorios que pidió añadir. Array de strings. Puede estar vacío.

Responde SOLO con el JSON, sin explicaciones."""
            }, {
                "role": "user",
                "content": f"Conversación:\n{conv_text}"
            }],
            max_tokens=400,
            temperature=0.3
        )
        text = r.choices[0].message.content.strip()
        # Limpiar posibles markdown fences
        text = re.sub(r"```json|```", "", text).strip()
        return json.loads(text)
    except Exception as e:
        print(f"Memory extraction error: {e}")
        return {"facts": [], "followups": []}

# ── Rutinas ───────────────────────────────────────────────────────────────────

def check_routine_match(text: str) -> dict | None:
    """Comprueba si el mensaje activa alguna rutina configurada."""
    text_lower = text.lower().strip()
    for routine in memory.get("routines", []):
        trigger = routine.get("trigger", "").lower()
        if trigger and (trigger in text_lower or text_lower in trigger):
            return routine
    return None

async def build_routine_context(routine: dict) -> str:
    """Construye contexto adicional para ejecutar una rutina."""
    parts = [f"El usuario ha activado su rutina: '{routine.get('description', routine.get('trigger'))}'."]
    actions = routine.get("actions", [])

    if "weather" in actions and memory.get("location"):
        loc = memory["location"]
        weather = await get_weather(loc["lat"], loc["lon"])
        city = loc.get("city", "tu ubicación")
        parts.append(f"Tiempo actual en {city}: {weather}.")

    if "tasks" in actions:
        tasks = memory.get("tasks", [])
        if tasks:
            parts.append(f"Tareas pendientes: {', '.join(tasks[:5])}.")
        else:
            parts.append("No tienes tareas pendientes.")

    if "time" in actions:
        now = datetime.now()
        parts.append(f"Son las {now.strftime('%H:%M')} del {now.strftime('%A %d de %B')}.")

    return " ".join(parts)

# ── Prompt del sistema ────────────────────────────────────────────────────────

def build_system_prompt(mac_online: bool) -> str:
    now = datetime.now().strftime("%A %d de %B de %Y, %H:%M")
    parts = [f"""Eres Yarvis, asistente personal en español con voz tipo Jarvis de Iron Man.
Fecha y hora actual: {now}.
Hablas de forma directa, concisa y ligeramente formal. Máximo 2-3 frases por respuesta, sin markdown."""]

    # Hechos del usuario
    facts = memory.get("user_facts", [])
    if facts:
        parts.append(f"\nLo que sé del usuario:\n" + "\n".join(f"- {f}" for f in facts[-15:]))

    # Seguimientos pendientes
    followups = memory.get("pending_followups", [])
    due = []
    for f in followups:
        try:
            due_time = datetime.fromisoformat(f["due_after"])
            if datetime.now() >= due_time:
                due.append(f["text"])
        except:
            pass
    if due:
        parts.append(f"\nSi es natural en la conversación, pregunta sobre esto: {'; '.join(due[:2])}")

    # Rutinas configuradas
    routines = memory.get("routines", [])
    if routines:
        triggers = [r.get("trigger","") for r in routines]
        parts.append(f"\nRutinas configuradas (triggers): {', '.join(triggers)}")

    # Estado del Mac
    if mac_online:
        parts.append("""\nEl Mac ESTÁ conectado. Puedes usar:
- [URL:https://...] para abrir URLs
- [CMD:comando] para ejecutar en terminal
- [APP:nombre] para abrir aplicaciones""")
    else:
        parts.append("\nEl Mac NO está conectado. No uses etiquetas de acción. Si el usuario pide ejecutar algo en el Mac, dile que no está conectado.")

    return "\n".join(parts)

# ── API ───────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    secret: str = ""

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
    if not ELEVEN_KEY: return None
    clean = re.sub(r"[*_`#\[\]]", "", text)[:500]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={"xi-api-key": ELEVEN_KEY, "Content-Type":"application/json","Accept":"audio/mpeg"},
                json={"text":clean,"model_id":"eleven_multilingual_v2",
                      "voice_settings":{"stability":0.55,"similarity_boost":0.80,"style":0.1,"use_speaker_boost":True}}
            )
            if r.status_code == 200:
                return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        print(f"ElevenLabs error: {e}")
    return None

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET:
        raise HTTPException(401, "Clave incorrecta")
    if not groq_client:
        raise HTTPException(500, "GROQ_API_KEY no configurada")

    mac_online = bool(agents)
    hist = session_histories.setdefault(req.session_id, [])

    # Detectar rutina
    routine = check_routine_match(req.message)
    routine_context = ""
    if routine:
        routine_context = await build_routine_context(routine)

    # Construir mensaje enriquecido
    user_message = req.message
    if routine_context:
        user_message = f"{req.message}\n\n[Contexto de rutina: {routine_context}]"

    hist.append({"role": "user", "content": user_message})

    try:
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":build_system_prompt(mac_online)}] + hist[-20:],
            max_tokens=300,
            temperature=0.7
        )
        reply = r.choices[0].message.content
    except Exception as e:
        raise HTTPException(500, f"Error IA: {str(e)}")

    hist.append({"role":"assistant","content":reply})
    acts    = extract_actions(reply)
    display = clean_text(reply)

    # Enviar acciones al Mac
    if acts and agents:
        payload = json.dumps({"actions":acts})
        dead = []
        for aid, ws in agents.items():
            try: await ws.send_text(payload)
            except: dead.append(aid)
        for aid in dead: agents.pop(aid, None)

    # Extraer y guardar memoria en background
    asyncio.create_task(update_memory(hist[-6:], req.message, reply))

    audio_b64 = await generate_voice(display)

    return {"reply":display,"actions":acts if mac_online else [],
            "mac_online":mac_online,"audio_b64":audio_b64}

async def update_memory(recent_hist: list, user_msg: str, reply: str):
    """Extrae hechos y actualiza la memoria persistente."""
    global memory
    extracted = await extract_memory_facts(recent_hist)

    changed = False

    # Añadir nuevos hechos (evitar duplicados)
    for fact in extracted.get("facts", []):
        if fact and fact not in memory["user_facts"]:
            memory["user_facts"].append(fact)
            if len(memory["user_facts"]) > 50:
                memory["user_facts"] = memory["user_facts"][-50:]
            changed = True

    # Añadir seguimientos pendientes
    for fu in extracted.get("followups", []):
        try:
            hours = float(fu.get("hours_later", 6))
            due = (datetime.now() + timedelta(hours=hours)).isoformat()
            entry = {"text": fu["text"], "due_after": due}
            memory["pending_followups"].append(entry)
            # Limpiar seguimientos viejos (más de 7 días)
            cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            memory["pending_followups"] = [
                f for f in memory["pending_followups"]
                if f.get("due_after","") > cutoff
            ]
            changed = True
        except:
            pass

    # Actualizar ubicación
    loc = extracted.get("location")
    if loc and loc.get("lat") and loc.get("lon"):
        memory["location"] = loc
        changed = True

    # Añadir rutina si se configuró
    routine_req = extracted.get("routine_request")
    if routine_req and routine_req.get("trigger"):
        trigger = routine_req["trigger"].lower()
        # Detectar qué acciones incluye
        desc = routine_req.get("description","").lower()
        actions = []
        if any(w in desc for w in ["tiempo","clima","temperatura","weather"]): actions.append("weather")
        if any(w in desc for w in ["tarea","calendar","agenda","recordatorio"]): actions.append("tasks")
        if any(w in desc for w in ["hora","time"]): actions.append("time")

        # Evitar duplicados
        existing_triggers = [r.get("trigger","").lower() for r in memory["routines"]]
        if trigger not in existing_triggers:
            memory["routines"].append({
                "trigger": trigger,
                "description": routine_req.get("description", trigger),
                "actions": actions
            })
            changed = True

    # Añadir tareas
    for task in extracted.get("tasks", []):
        if task and task not in memory["tasks"]:
            memory["tasks"].append(task)
            changed = True

    # Limpiar seguimientos que ya se han cumplido y preguntado
    now_iso = datetime.now().isoformat()
    memory["pending_followups"] = [
        f for f in memory["pending_followups"]
        if f.get("due_after","") > now_iso
    ]

    memory["conversation_count"] = memory.get("conversation_count", 0) + 1

    if changed:
        save_memory(memory)

# ── Endpoints de gestión ──────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    return {"mac_online":bool(agents),"memories":len(memory.get("user_facts",[])),"routines":len(memory.get("routines",[])),
            "tasks":len(memory.get("tasks",[])),"voice":"elevenlabs" if ELEVEN_KEY else "browser"}

@app.get("/api/memory")
async def get_memory(secret: str = ""):
    if secret != YARVIS_SECRET: raise HTTPException(401)
    return memory

@app.delete("/api/memory")
async def clear_memory(secret: str = ""):
    global memory
    if secret != YARVIS_SECRET: raise HTTPException(401)
    memory = {"user_facts":[],"pending_followups":[],"routines":[],"tasks":[],"location":None,"conversation_count":0}
    save_memory(memory)
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

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({"name":"Yarvis","short_name":"Yarvis","start_url":"/",
                         "display":"standalone","background_color":"#000","theme_color":"#000","icons":[]})

BASE = Path(__file__).parent
def find(name):
    for p in [BASE/"static"/name, BASE/name]:
        if p.exists(): return p
    return None

@app.get("/")
async def root():
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    return JSONResponse({"status":"Yarvis API online","memories":len(memory.get("user_facts",[]))})

@app.get("/{path:path}")
async def spa(path: str):
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    raise HTTPException(404)
