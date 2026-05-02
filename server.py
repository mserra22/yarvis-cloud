"""
YARVIS — Servidor Cloud Estable
Variables: GROQ_API_KEY, YARVIS_SECRET, ELEVENLABS_API_KEY, ANTHROPIC_API_KEY
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GROQ_KEY      = os.environ.get("GROQ_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")
ELEVEN_KEY    = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE  = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

claude_client = None
if ANTHROPIC_KEY:
    try:
        import anthropic
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    except Exception as e:
        print(f"Anthropic import error: {e}")

# ── Almacenamiento persistente ────────────────────────────────────────────────
DATA_DIR    = Path("/data") if Path("/data").exists() else Path("/tmp")
MEMORY_FILE = DATA_DIR / "yarvis_memory.json"

session_histories: dict = {}

# Agentes Mac conectados (WebSocket bidireccional)
agents: dict = {}
agent_queues: dict = {}  # asyncio.Queue por agente

def load_memory():
    default = {
        "user_facts": [],
        "preferences": {},
        "pending_followups": [],
        "routines": [],
        "tasks": [],
        "location": None,
        "conversation_count": 0,
        "learned_actions": {}
    }
    try:
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text())
            # Asegurar que todos los campos existen
            for k, v in default.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception as e:
        print(f"Memory load error: {e}")
    return default

def save_memory(m):
    try:
        MEMORY_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Memory save error: {e}")

memory = load_memory()
print(f"[✓] Memoria cargada: {len(memory['user_facts'])} hechos, {len(memory['routines'])} rutinas")

# ── Weather ───────────────────────────────────────────────────────────────────
WEATHER_CODES = {
    0:"despejado", 1:"casi despejado", 2:"parcialmente nublado", 3:"nublado",
    45:"niebla", 51:"llovizna", 61:"lluvia ligera", 63:"lluvia moderada",
    65:"lluvia fuerte", 71:"nieve ligera", 80:"chubascos", 95:"tormenta"
}

async def get_weather(lat: float, lon: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "timezone": "auto"
            })
            if r.status_code == 200:
                d = r.json()["current"]
                return f"{round(d['temperature_2m'])}°C, {WEATHER_CODES.get(d['weather_code'],'variable')}, viento {round(d['wind_speed_10m'])} km/h"
    except Exception as e:
        print(f"Weather error: {e}")
    return "no disponible"

# ── Visión: solo cuando se solicita explícitamente ───────────────────────────
async def request_screenshot_from_agent() -> str:
    """Pide captura al agente y espera respuesta."""
    if not agents:
        return ""
    aid = next(iter(agents))
    ws = agents[aid]
    q = agent_queues.get(aid)
    if not q:
        return ""
    try:
        await ws.send_text(json.dumps({"type": "request_screenshot"}))
        msg = await asyncio.wait_for(q.get(), timeout=12)
        if msg.get("type") == "screenshot":
            return msg.get("data", "")
    except asyncio.TimeoutError:
        print("Screenshot timeout")
    except Exception as e:
        print(f"Screenshot request error: {e}")
    return ""

async def vision_decide(screenshot_b64: str, task: str, step: int = 0) -> dict:
    """Usa modelo de visión para decidir próxima acción en pantalla."""
    if not groq_client or not screenshot_b64:
        return {"done": True}
    try:
        r = groq_client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
                {"type": "text", "text": f"""Mac screenshot analysis. Task: {task}. Step {step+1}.
Return ONLY JSON with the next action:
{{"action":"click|type|key|scroll|wait|done","x":0,"y":0,"text":"","key":"","scroll":"up|down","wait":1,"done":false,"reason":""}}
- click: exact pixel x,y coordinates of element to click
- type: text to type (ASCII only)
- key: keyboard shortcut (e.g. "cmd+l", "enter", "escape")
- done: true if task is complete or impossible
Be precise. If element not visible, use key/scroll to navigate first."""}
            ]}],
            max_tokens=150,
            temperature=0.1
        )
        text = re.sub(r"```json|```", "", r.choices[0].message.content).strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"Vision decide error: {e}")
    return {"done": True}

async def run_vision_loop(task: str, max_steps: int = 8) -> str:
    """Bucle ver→decidir→actuar para tareas visuales en el Mac."""
    if not agents:
        return "El Mac no está conectado."

    aid = next(iter(agents))
    ws = agents[aid]
    q = agent_queues.get(aid)
    if not q:
        return "Error de conexión con el Mac."

    log = []
    for step in range(max_steps):
        # Capturar pantalla
        sc = await request_screenshot_from_agent()
        if not sc:
            return "No pude capturar la pantalla del Mac."

        # Analizar
        decision = await vision_decide(sc, task, step)

        if decision.get("done"):
            reason = decision.get("reason", "completado")
            log.append(f"Listo: {reason}")
            break

        action_type = decision.get("action", "done")
        if action_type == "done":
            break

        # Construir acción
        action = None
        if action_type == "click":
            x, y = int(decision.get("x", 0)), int(decision.get("y", 0))
            action = {"type": "click", "value": f"{x},{y}"}
            log.append(f"Click ({x},{y})")
        elif action_type == "type":
            text = decision.get("text", "")
            action = {"type": "typewrite", "value": text}
            log.append(f"Escribir: {text[:30]}")
        elif action_type == "key":
            key = decision.get("key", "")
            action = {"type": "key", "value": key}
            log.append(f"Tecla: {key}")
        elif action_type == "scroll":
            action = {"type": "scroll", "value": decision.get("scroll", "down")}
            log.append(f"Scroll {decision.get('scroll')}")
        elif action_type == "wait":
            secs = decision.get("wait", 1)
            action = {"type": "wait", "value": str(secs)}
            log.append(f"Esperar {secs}s")

        if action:
            await ws.send_text(json.dumps({"type": "actions", "actions": [action]}))
            # Esperar resultado o timeout
            try:
                await asyncio.wait_for(q.get(), timeout=6)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.6)

    # Guardar en memoria lo aprendido
    if log:
        learned = memory.setdefault("learned_actions", {})
        learned[task[:60]] = {"steps": log, "ts": datetime.now().isoformat()}
        memory["learned_actions"] = dict(list(learned.items())[-30:])
        save_memory(memory)

    return f"Hecho en {len(log)} pasos." if log else "Tarea completada."

# ── Rutinas ───────────────────────────────────────────────────────────────────
def find_routine(text: str):
    for r in memory.get("routines", []):
        trigger = r.get("trigger", "").lower()
        if trigger and trigger in text.lower():
            return r
    return None

async def build_routine_context(routine: dict) -> str:
    parts = [f"Rutina activa: {routine.get('description', routine.get('trigger', ''))}"]
    actions = routine.get("actions", [])
    if "weather" in actions and memory.get("location"):
        loc = memory["location"]
        w = await get_weather(loc["lat"], loc["lon"])
        parts.append(f"Tiempo en {loc.get('city', 'tu ciudad')}: {w}")
    if "tasks" in actions:
        tasks = memory.get("tasks", [])
        parts.append(f"Tareas pendientes: {', '.join(tasks[:5])}" if tasks else "Sin tareas pendientes")
    if "time" in actions:
        parts.append(f"Son las {datetime.now().strftime('%H:%M')}")
    return ". ".join(parts)

# ── Sistema de prompt ─────────────────────────────────────────────────────────
def build_system(mac_online: bool, complex_task: bool = False) -> str:
    now = datetime.now().strftime("%A %d de %B de %Y, %H:%M")

    if complex_task:
        base = f"""Eres Yarvis, asistente personal avanzado. Fecha: {now}.
Crea código completo y funcional cuando te lo pidan. Responde en español."""
    else:
        base = f"""Eres Yarvis, asistente personal tipo Jarvis de Iron Man. Fecha: {now}.
REGLAS ESTRICTAS:
- Máximo 2 frases por respuesta (se lee en voz alta)
- Sin markdown, asteriscos ni guiones
- Directo y conciso
- Responde siempre en español"""

    parts = [base]

    # Preferencias del usuario
    prefs = memory.get("preferences", {})
    if prefs:
        pref_text = "\n".join(f"- {k}: {v}" for k, v in list(prefs.items())[:10])
        parts.append(f"Preferencias del usuario:\n{pref_text}")

    # Hechos conocidos
    facts = memory.get("user_facts", [])
    if facts:
        parts.append("Lo que sé del usuario:\n" + "\n".join(f"- {f}" for f in facts[-15:]))

    # Seguimientos pendientes
    now_dt = datetime.now()
    followups = []
    for f in memory.get("pending_followups", []):
        try:
            if now_dt >= datetime.fromisoformat(f.get("due_after", "2099-01-01")):
                followups.append(f["text"])
        except Exception:
            pass
    if followups:
        parts.append(f"Si es natural en la conversación, pregunta: {'; '.join(followups[:2])}")

    # Estado del Mac
    if mac_online:
        parts.append("""Mac conectado. Etiquetas disponibles:
[APP:nombre] abre app · [URL:https://...] abre URL · [CMD:comando] terminal
[SPOTLIGHT:texto] busca con Spotlight · [KEY:cmd+space] atajos
[TYPE:texto] escribe ASCII · [TYPEWRITE:texto con acentos] escribe con acentos
[CLICK:x,y] click · [RCLICK:x,y] click derecho · [DCLICK:x,y] doble click
[MOUSE_MOVE:x,y] mover ratón · [SCROLL:up] o [SCROLL:down] scroll
[SCREENSHOT] captura pantalla · [CLIPBOARD:texto] copiar · [WAIT:segundos] esperar
[APPLESCRIPT:script] ejecutar AppleScript · [VISION_TASK:descripción] ver pantalla y actuar

IMPORTANTE: Para abrir apps usa SIEMPRE el nombre en inglés.
Ejemplos: [APP:Safari] [APP:Spotify] [APP:Finder] [APP:Terminal]
Para tareas visuales complejas (buscar en una web, rellenar formularios, navegar por la UI): usa [VISION_TASK:descripción detallada]""")
    else:
        parts.append("Mac NO conectado. No uses etiquetas de acción. Si el usuario pide algo del Mac, dile que el agente no está activo.")

    return "\n\n".join(parts)

# ── Acciones ──────────────────────────────────────────────────────────────────
ALL_ACTION_TYPES = [
    "URL", "CMD", "APP", "SPOTLIGHT", "KEY", "TYPE", "TYPEWRITE",
    "CLICK", "RCLICK", "DCLICK", "MOUSE_MOVE", "SCROLL",
    "SCREENSHOT", "CLIPBOARD", "APPLESCRIPT", "FIND_AND_CLICK",
    "WAIT", "VISION_TASK"
]

def extract_actions(text: str) -> list:
    acts = []
    if re.search(r'\[SCREENSHOT\]', text, re.I):
        acts.append({"type": "screenshot", "value": ""})
    for atype in ALL_ACTION_TYPES:
        if atype == "SCREENSHOT":
            continue
        for val in re.findall(rf'\[{atype}:([^\]]+)\]', text, re.I):
            acts.append({"type": atype.lower(), "value": val.strip()})
    return acts

def clean_text(t: str) -> str:
    pattern = "|".join(ALL_ACTION_TYPES)
    t = re.sub(rf'\[(?:{pattern}):[^\]]+\]', '', t, flags=re.I)
    t = re.sub(r'\[SCREENSHOT\]', '', t, flags=re.I)
    return t.strip()

# ── Groq con fallback ─────────────────────────────────────────────────────────
def call_groq(messages: list, max_tokens: int = 200) -> str:
    models = [GROQ_MODEL, "llama-3.1-8b-instant", "gemma2-9b-it"]
    for model in models:
        try:
            r = groq_client.chat.completions.create(
                model=model, messages=messages,
                max_tokens=max_tokens, temperature=0.7
            )
            return r.choices[0].message.content
        except Exception as e:
            err_str = str(e)
            if "rate_limit" in err_str or "429" in err_str:
                print(f"Rate limit {model}, trying next...")
                continue
            raise
    raise Exception("Todos los modelos de Groq han fallado")

# ── TTS ───────────────────────────────────────────────────────────────────────
async def generate_voice(text: str):
    if not ELEVEN_KEY:
        return None
    clean = re.sub(r"[*_`#\[\]]", "", text)[:500]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json={"text": clean, "model_id": "eleven_multilingual_v2",
                      "voice_settings": {"stability": 0.55, "similarity_boost": 0.80, "style": 0.1, "use_speaker_boost": True}}
            )
            if r.status_code == 200:
                return base64.b64encode(r.content).decode("utf-8")
            print(f"ElevenLabs {r.status_code}")
    except Exception as e:
        print(f"ElevenLabs error: {e}")
    return None

# ── API Chat ──────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    secret: str = ""

COMPLEX_KW = ["crea","desarrolla","programa","escribe código","script","aplicación","app",
              "analiza","análisis","diseña","arquitectura","función","clase","algoritmo",
              "html","python","javascript","css","api","web","sistema","automatiza"]

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET:
        raise HTTPException(401, "Clave incorrecta")
    if not groq_client:
        raise HTTPException(500, "GROQ_API_KEY no configurada en Railway → Variables")

    mac_online = bool(agents)
    hist = session_histories.setdefault(req.session_id, [])

    # Contexto de rutina
    routine = find_routine(req.message)
    user_msg = req.message
    if routine:
        ctx = await build_routine_context(routine)
        user_msg = f"{req.message}\n[Contexto de rutina: {ctx}]"

    hist.append({"role": "user", "content": user_msg})

    # Decidir qué motor usar
    use_claude = bool(claude_client) and any(k in req.message.lower() for k in COMPLEX_KW)

    try:
        if use_claude:
            r = claude_client.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=2000,
                system=build_system(mac_online, complex_task=True),
                messages=hist[-20:]
            )
            reply = r.content[0].text
            ai_used = "claude"
        else:
            reply = call_groq(
                [{"role": "system", "content": build_system(mac_online)}] + hist[-20:],
                max_tokens=200
            )
            ai_used = "groq"
    except Exception as e:
        print(f"AI error: {e}")
        raise HTTPException(500, f"Error IA: {str(e)}")

    hist.append({"role": "assistant", "content": reply})
    acts    = extract_actions(reply)
    display = clean_text(reply)

    # Separar vision tasks de acciones normales
    vision_tasks  = [a for a in acts if a["type"] == "vision_task"]
    normal_acts   = [a for a in acts if a["type"] != "vision_task"]

    # Ejecutar acciones normales en Mac
    if normal_acts and agents:
        aid = next(iter(agents))
        ws  = agents[aid]
        await ws.send_text(json.dumps({"type": "actions", "actions": normal_acts}))

    # Ejecutar vision tasks (solo si las hay, no automáticamente)
    vision_results = []
    for vt in vision_tasks:
        vr = await run_vision_loop(vt["value"])
        vision_results.append(vr)

    if vision_results:
        display = (display + " " + " ".join(vision_results)).strip()

    # Actualizar memoria en background
    asyncio.create_task(update_memory_background(hist[-6:], req.message))

    audio = await generate_voice(display)

    return {
        "reply":      display,
        "actions":    normal_acts if mac_online else [],
        "mac_online": mac_online,
        "audio_b64":  audio,
        "ai_used":    ai_used
    }

# ── Actualización de memoria ──────────────────────────────────────────────────
async def update_memory_background(hist: list, last_user_msg: str):
    global memory
    if not groq_client or len(hist) < 2:
        return
    try:
        conv = "\n".join([f"{m['role'].upper()}: {m['content'][:250]}" for m in hist])
        result = call_groq([
            {"role": "system", "content": """Analiza la conversación y extrae en JSON (solo lo que está explícito):
{
  "facts": ["hecho sobre el usuario"],
  "preferences": {"clave": "valor"},
  "followups": [{"text": "pregunta de seguimiento", "hours_later": 6}],
  "location": {"city": "nombre", "lat": 0.0, "lon": 0.0},
  "routine": {"trigger": "frase", "description": "qué hacer", "actions": ["weather","tasks","time"]},
  "tasks": ["tarea a añadir"]
}
Campos vacíos si no hay datos. SOLO JSON, sin explicaciones.
Ejemplos de preferencias: {"idioma_apps": "inglés", "música": "reggaeton", "despertador": "7:00"}"""},
            {"role": "user", "content": conv}
        ], max_tokens=300)

        text = re.sub(r"```json|```", "", result).strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return
        extracted = json.loads(m.group())
        changed = False

        # Hechos
        for f in extracted.get("facts", []):
            if f and f not in memory["user_facts"]:
                memory["user_facts"].append(f)
                changed = True
        memory["user_facts"] = memory["user_facts"][-50:]

        # Preferencias (clave→valor, muy importantes)
        new_prefs = extracted.get("preferences", {})
        if new_prefs:
            memory["preferences"].update(new_prefs)
            changed = True

        # Seguimientos
        for fu in extracted.get("followups", []):
            try:
                due = (datetime.now() + timedelta(hours=float(fu.get("hours_later", 6)))).isoformat()
                memory["pending_followups"].append({"text": fu["text"], "due_after": due})
                changed = True
            except Exception:
                pass
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        memory["pending_followups"] = [f for f in memory["pending_followups"] if f.get("due_after", "") > cutoff]

        # Ubicación
        loc = extracted.get("location")
        if loc and loc.get("lat") and loc.get("lon") and loc.get("city"):
            memory["location"] = loc
            changed = True

        # Rutina
        rt = extracted.get("routine")
        if rt and rt.get("trigger"):
            trigger = rt["trigger"].lower().strip()
            existing = [r.get("trigger", "").lower() for r in memory["routines"]]
            if trigger and trigger not in existing:
                memory["routines"].append({
                    "trigger":     trigger,
                    "description": rt.get("description", trigger),
                    "actions":     rt.get("actions", [])
                })
                changed = True

        # Tareas
        for task in extracted.get("tasks", []):
            if task and task not in memory["tasks"]:
                memory["tasks"].append(task)
                changed = True

        memory["conversation_count"] = memory.get("conversation_count", 0) + 1

        if changed:
            save_memory(memory)
            print(f"[Memory] Guardado: {len(memory['user_facts'])} hechos, {len(memory['preferences'])} prefs, {len(memory['routines'])} rutinas")

    except Exception as e:
        print(f"Memory update error: {e}")

# ── WebSocket bidireccional ───────────────────────────────────────────────────
@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    if ws.query_params.get("secret", "") != YARVIS_SECRET:
        await ws.close(1008)
        return
    await ws.accept()
    aid = f"mac-{id(ws)}"
    agents[aid] = ws
    agent_queues[aid] = asyncio.Queue()
    print(f"[+] Mac conectado: {aid}")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
                msg_type = data.get("type", "")
                if msg_type in ("screenshot", "results"):
                    await agent_queues[aid].put(data)
            except Exception:
                pass
    except WebSocketDisconnect:
        agents.pop(aid, None)
        agent_queues.pop(aid, None)
        print(f"[-] Mac desconectado: {aid}")

# ── Estado y gestión ──────────────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return {
        "mac_online":   bool(agents),
        "memories":     len(memory.get("user_facts", [])),
        "preferences":  len(memory.get("preferences", {})),
        "routines":     len(memory.get("routines", [])),
        "tasks":        len(memory.get("tasks", [])),
        "learned":      len(memory.get("learned_actions", {})),
        "conversations": memory.get("conversation_count", 0),
        "claude":       bool(claude_client),
        "data_path":    str(MEMORY_FILE)
    }

@app.get("/api/memory")
async def get_mem(secret: str = ""):
    if secret != YARVIS_SECRET:
        raise HTTPException(401, "No autorizado")
    return memory

@app.delete("/api/memory")
async def clear_mem(secret: str = ""):
    global memory
    if secret != YARVIS_SECRET:
        raise HTTPException(401, "No autorizado")
    memory = {"user_facts":[],"preferences":{},"pending_followups":[],"routines":[],"tasks":[],"location":None,"conversation_count":0,"learned_actions":{}}
    save_memory(memory)
    return {"ok": True, "message": "Memoria borrada"}

# ── Frontend ──────────────────────────────────────────────────────────────────
@app.get("/manifest.json")
async def manifest():
    return JSONResponse({"name":"Yarvis","short_name":"Yarvis","start_url":"/","display":"standalone","background_color":"#000","theme_color":"#000","icons":[]})

BASE = Path(__file__).parent

def find_file(name: str):
    for p in [BASE / "static" / name, BASE / name]:
        if p.exists():
            return p
    return None

@app.get("/")
async def root():
    p = find_file("index.html")
    if p:
        return FileResponse(str(p), media_type="text/html")
    return JSONResponse({"status": "Yarvis API online", "memories": len(memory.get("user_facts", []))})

@app.get("/{path:path}")
async def spa(path: str):
    p = find_file("index.html")
    if p:
        return FileResponse(str(p), media_type="text/html")
    raise HTTPException(404)
