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
        "constraints": [],  # cosas que Yarvis NUNCA debe hacer
        "pending_followups": [],
        "routines": [],
        "tasks": [],
        "location": None,
        "conversation_count": 0,
        "learned_actions": {},
        "fixes": {}
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

# ── Visión ────────────────────────────────────────────────────────────────────
async def request_screenshot_from_agent():
    """Pide captura al agente con dimensiones reales de pantalla."""
    if not agents:
        return "", 1280, 800
    aid = next(iter(agents))
    ws  = agents[aid]
    q   = agent_queues.get(aid)
    if not q:
        return "", 1280, 800
    try:
        # Pedir screenshot con info de dimensiones
        await ws.send_text(json.dumps({"type": "request_screenshot"}))
        msg = await asyncio.wait_for(q.get(), timeout=12)
        if msg.get("type") == "screenshot":
            b64 = msg.get("data", "")
            sw  = msg.get("screen_w", 1280)
            sh  = msg.get("screen_h", 800)
            iw  = msg.get("image_w", sw)
            ih  = msg.get("image_h", sh)
            return b64, sw, sh, iw, ih
    except asyncio.TimeoutError:
        print("Screenshot timeout")
    except Exception as e:
        print(f"Screenshot error: {e}")
    return "", 1280, 800, 1280, 800

async def vision_decide(screenshot_b64: str, task: str, step: int,
                        img_w: int, img_h: int) -> dict:
    """Analiza screenshot y decide próxima acción. Usa porcentajes para coords."""
    if not groq_client or not screenshot_b64:
        return {"done": True}
    try:
        r = groq_client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
                {"type": "text", "text": f"""Mac screenshot ({img_w}x{img_h}px). Task: {task}. Step {step+1}.
Return ONLY JSON:
{{"action":"click|type|key|scroll|wait|done","px":0.0,"py":0.0,"text":"","key":"","scroll":"up|down","wait":1,"done":false,"reason":""}}
- px, py: position as PERCENTAGE of image (0.0 to 1.0). Example: center = px:0.5, py:0.5
- click: find the exact element and give its percentage position
- key: shortcuts like "cmd+l", "enter", "escape", "cmd+f"
- done: true when task complete or impossible
Analyze carefully. Describe what you see in reason."""}
            ]}],
            max_tokens=180,
            temperature=0.1
        )
        text = re.sub(r"```json|```", "", r.choices[0].message.content).strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"Vision error: {e}")
    return {"done": True}

async def run_vision_loop(task: str, max_steps: int = 8) -> str:
    """Bucle ver→decidir→actuar con coordenadas corregidas."""
    if not agents:
        return "El Mac no está conectado."

    aid = next(iter(agents))
    ws = agents[aid]
    q = agent_queues.get(aid)
    if not q:
        return "Error de conexión con el Mac."

    log = []
    screen_w, screen_h = 1280, 800  # defaults

    for step in range(max_steps):
        # Capturar pantalla con dimensiones reales
        result = await request_screenshot_from_agent()
        if len(result) == 5:
            sc, screen_w, screen_h, img_w, img_h = result
        else:
            sc = result[0] if result else ""
            img_w, img_h = screen_w, screen_h

        if not sc:
            return "No pude capturar la pantalla del Mac."

        # Analizar con modelo de visión
        decision = await vision_decide(sc, task, step, img_w, img_h)
        reason = decision.get("reason", "")
        print(f"[Vision step {step+1}] {decision}")

        if decision.get("done") or decision.get("action") == "done":
            log.append(f"Completado: {reason}")
            break

        action_type = decision.get("action", "done")

        # Convertir porcentajes a píxeles reales de pantalla
        action = None
        if action_type == "click":
            px = float(decision.get("px", 0.5))
            py = float(decision.get("py", 0.5))
            # Convertir de % imagen → píxeles pantalla real
            x = int(px * screen_w)
            y = int(py * screen_h)
            action = {"type": "click", "value": f"{x},{y}"}
            log.append(f"Click ({x},{y}) [{reason[:40]}]")
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
            log.append(f"Scroll: {decision.get('scroll')}")
        elif action_type == "wait":
            secs = decision.get("wait", 1)
            action = {"type": "wait", "value": str(secs)}
            log.append(f"Esperar {secs}s")

        if action and agents:
            aid = next(iter(agents))
            ws_a = agents[aid]
            q_a  = agent_queues.get(aid)
            await ws_a.send_text(json.dumps({"type": "actions", "actions": [action]}))
            try:
                await asyncio.wait_for(q_a.get(), timeout=6)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.8)

    if log:
        learned = memory.setdefault("learned_actions", {})
        learned[task[:60]] = {"steps": log, "ts": datetime.now().isoformat()}
        memory["learned_actions"] = dict(list(learned.items())[-30:])
        save_memory(memory)

    return f"Hecho en {len(log)} pasos." if log else "Tarea completada."

# ── Sistema de autocorrección ─────────────────────────────────────────────────

CORRECTION_PATTERNS = [
    "no,","eso está mal","te equivocas","incorrecto","no es así",
    "la app se llama","el nombre es","corrígete","no era eso",
    "hazlo así","debería ser","en realidad","no funciona","está fallando",
    "recuerda que","te dije que","ya te dije","aprende que"
]

def is_user_correction(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in CORRECTION_PATTERNS)

def store_fix(error_desc: str, solution: str):
    """Guarda una corrección aprendida."""
    fixes = memory.setdefault("fixes", {})
    key = error_desc[:80].lower().strip()
    fixes[key] = {"solution": solution, "ts": datetime.now().isoformat(), "uses": fixes.get(key, {}).get("uses", 0) + 1}
    memory["fixes"] = dict(list(fixes.items())[-50:])
    save_memory(memory)
    print(f"[Fix guardado] {key} → {solution[:60]}")

def find_known_fix(action: dict) :
    """Busca si hay una corrección conocida para esta acción."""
    fixes = memory.get("fixes", {})
    action_str = f"{action.get('type','')}: {action.get('value','')}".lower()
    for key, fix in fixes.items():
        if any(word in action_str for word in key.split()[:3] if len(word) > 3):
            return fix.get("solution")
    return None

async def execute_with_correction(actions: list, context: str = "") -> tuple:
    """Ejecuta acciones con detección de errores y autocorrección."""
    if not agents:
        return [], []

    aid = next(iter(agents))
    ws  = agents[aid]
    q   = agent_queues.get(aid)
    if not q:
        return [], []

    results   = []
    corrected = []

    for action in actions:
        # Comprobar si hay un fix conocido para esta acción
        known_fix = find_known_fix(action)
        if known_fix:
            print(f"[Fix aplicado] {action} → {known_fix}")
            action = {**action, "value": known_fix}

        # Intentar la acción
        await ws.send_text(json.dumps({"type": "actions", "actions": [action]}))
        try:
            result_msg = await asyncio.wait_for(q.get(), timeout=8)
            action_results = result_msg.get("data", [])
            result_text = action_results[0] if action_results else "ok"
        except asyncio.TimeoutError:
            result_text = "timeout"

        # Detectar error
        is_error = (result_text.startswith("✗") or
                    "not found" in result_text.lower() or
                    "no such" in result_text.lower() or
                    "error" in result_text.lower() and not result_text.startswith("✓"))

        if is_error and groq_client:
            # Pedir corrección a la IA
            try:
                fix_prompt = f"""La acción "{action['type']}: {action['value']}" falló con: "{result_text}".
Contexto: {context}.
Sugiere la corrección exacta en JSON: {{"type": "...", "value": "..."}}
Solo JSON, sin explicación."""
                fix_reply = call_groq([{"role":"user","content":fix_prompt}], max_tokens=80)
                fix_m = re.search(r'\{.*\}', fix_reply, re.DOTALL)
                if fix_m:
                    fixed_action = json.loads(fix_m.group())
                    # Aplicar corrección
                    await ws.send_text(json.dumps({"type":"actions","actions":[fixed_action]}))
                    try:
                        r2 = await asyncio.wait_for(q.get(), timeout=8)
                        retry_result = r2.get("data",[""])[0]
                        if not retry_result.startswith("✗"):
                            # Guardar el fix aprendido
                            store_fix(f"{action['type']}: {action['value']}", fixed_action["value"])
                            corrected.append(f"{action['value']} → {fixed_action['value']}")
                            result_text = retry_result
                    except asyncio.TimeoutError:
                        pass
            except Exception as e:
                print(f"Autocorrección error: {e}")

        results.append(result_text)

    return results, corrected

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
        base = f"""Eres Yarvis, asistente personal inteligente tipo Jarvis de Iron Man. Fecha: {now}.

PERSONALIDAD:
- Hablas en español, directo y conciso. Máximo 2 frases por respuesta (se lee en voz alta).
- Antes de ejecutar una acción, di brevemente lo que vas a hacer. Ej: "Abriendo Photos ahora."
- Si algo no está claro, pregunta UNA cosa concreta antes de actuar.
- Cuando cometes un error y el usuario te corrige, confirma que lo has aprendido. Ej: "Entendido, usaré Photos en vez de iPhoto. Lo recuerdo para siempre."
- Si el usuario dice que algo falló, admítelo y propón la corrección inmediatamente.
- NUNCA repitas un comando que ya falló sin antes cambiarlo.
- Sin markdown, asteriscos ni guiones en las respuestas de voz."""

    parts = [base]

    # ── RESTRICCIONES — se muestran primero y en mayúsculas para máxima prioridad
    constraints = memory.get("constraints", [])
    if constraints:
        c_text = "\n".join(f"- {c}" for c in constraints[-20:])
        parts.insert(1, f"⛔ RESTRICCIONES PERMANENTES (cumplir SIEMPRE sin excepción):\n{c_text}")

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

    # Correcciones aprendidas
    fixes = memory.get("fixes", {})
    if fixes:
        fix_lines = [f"- '{k}' → usar '{v['solution']}'" for k, v in list(fixes.items())[-8:]]
        parts.append("Correcciones aprendidas (aplícalas siempre):\n" + "\n".join(fix_lines))

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
Para tareas visuales complejas (buscar en una web, rellenar formularios): usa [VISION_TASK:...]""")
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

# ── Mapa de nombres de apps macOS (corrige nombres antiguos/incorrectos) ───────
MAC_APP_MAP = {
    # Español → inglés correcto
    "fotos": "Photos", "foto": "Photos",
    "música": "Music", "musica": "Music",
    "mensajes": "Messages",
    "correo": "Mail",
    "calendario": "Calendar",
    "recordatorios": "Reminders",
    "notas": "Notes",
    "contactos": "Contacts",
    "mapas": "Maps",
    "noticias": "News",
    "clima": "Weather",
    "calculadora": "Calculator",
    "ajustes": "System Preferences",
    "configuracion": "System Preferences",
    "configuración del sistema": "System Settings",
    "finder": "Finder",
    "safari": "Safari",
    "terminal": "Terminal",
    "páginas": "Pages",
    "números": "Numbers",
    "keynote": "Keynote",
    # Nombres legacy incorrectos
    "iphoto": "Photos",
    "itunes": "Music",
    "system preferences": "System Settings",
    "imovie": "iMovie",
    "garageband": "GarageBand",
    # Comunes en inglés (asegurar capitalización correcta)
    "photos": "Photos", "music": "Music", "mail": "Mail",
    "messages": "Messages", "notes": "Notes", "calendar": "Calendar",
    "reminders": "Reminders", "maps": "Maps", "weather": "Weather",
    "calculator": "Calculator", "facetime": "FaceTime",
    "spotify": "Spotify", "chrome": "Google Chrome",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code",
    "word": "Microsoft Word", "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "teams": "Microsoft Teams",
    "zoom": "Zoom", "slack": "Slack", "discord": "Discord",
    "whatsapp": "WhatsApp",
}

def normalize_app_name(name: str) -> str:
    """Corrige el nombre de una app al nombre correcto en macOS."""
    key = name.lower().strip()
    return MAC_APP_MAP.get(key, name)  # si no está en el mapa, devuelve el original

def extract_actions(text: str) -> list:
    acts = []
    if re.search(r'\[SCREENSHOT\]', text, re.I):
        acts.append({"type": "screenshot", "value": ""})
    for atype in ALL_ACTION_TYPES:
        if atype == "SCREENSHOT":
            continue
        for val in re.findall(rf'\[{atype}:([^\]]+)\]', text, re.I):
            v = val.strip()
            # Normalizar nombres de apps
            if atype == "APP":
                v = normalize_app_name(v)
            acts.append({"type": atype.lower(), "value": v})
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

# Patrones que indican una instrucción permanente
CONSTRAINT_PATTERNS = [
    r"no (hagas|vuelvas a hacer|me|quiero que)",
    r"deja de ",r"para de ",r"nunca (más|vuelvas)",
    r"no (más|sigas|repitas|uses|pongas|digas|hagas)",
    r"recuerda que no",r"te (digo|pido) que no",
    r"stop ",r"elimina ",r"quita ",
]

def detect_constraint(text: str) :
    """Detecta si el mensaje es una instrucción permanente de 'no hagas X'."""
    t = text.lower().strip()
    for pattern in CONSTRAINT_PATTERNS:
        if re.search(pattern, t):
            return text.strip()
    return None

def apply_constraint(instruction: str):
    """Guarda una restricción permanente de forma inmediata."""
    constraints = memory.setdefault("constraints", [])
    # Evitar duplicados similares
    if not any(instruction.lower()[:40] in c.lower() for c in constraints):
        constraints.append(instruction)
        memory["constraints"] = constraints[-30:]  # max 30 restricciones
        save_memory(memory)
        print(f"[Restricción guardada] {instruction[:80]}")

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET:
        raise HTTPException(401, "Clave incorrecta")
    if not groq_client:
        raise HTTPException(500, "GROQ_API_KEY no configurada en Railway → Variables")

    mac_online = bool(agents)
    hist = session_histories.setdefault(req.session_id, [])

    # Limpiar historial si el usuario cierra la conversación
    msg_lower = req.message.lower()
    if any(w in msg_lower for w in ["chao yarvis","adiós yarvis","adios yarvis","hasta luego yarvis"]):
        session_histories[req.session_id] = []
        hist = []

    # Limitar historial a 10 mensajes máximo para evitar contaminación
    if len(hist) > 10:
        hist = hist[-10:]
        session_histories[req.session_id] = hist

    # ── Detección inmediata de restricciones (síncrona, no en background) ──
    constraint = detect_constraint(req.message)
    if constraint:
        apply_constraint(constraint)

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
                messages=hist[-10:]
            )
            reply = r.content[0].text
            ai_used = "claude"
        else:
            reply = call_groq(
                [{"role": "system", "content": build_system(mac_online)}] + hist[-10:],
                max_tokens=200
            )
            ai_used = "groq"
    except Exception as e:
        print(f"AI error: {e}")
        raise HTTPException(500, f"Error IA: {str(e)}")

    acts    = extract_actions(reply)
    display = clean_text(reply)

    # Guardar en historial la versión LIMPIA (sin etiquetas de acción)
    # Así no se contaminan las respuestas siguientes con acciones antiguas
    hist.append({"role": "assistant", "content": display or reply})

    # Separar vision tasks de acciones normales
    vision_tasks = [a for a in acts if a["type"] == "vision_task"]
    normal_acts  = [a for a in acts if a["type"] != "vision_task"]

    # Ejecutar acciones con autocorrección
    action_corrections = []
    if normal_acts and agents:
        _, corrections = await execute_with_correction(normal_acts, req.message)
        action_corrections = corrections

    # Ejecutar vision tasks
    vision_results = []
    for vt in vision_tasks:
        vr = await run_vision_loop(vt["value"])
        vision_results.append(vr)

    if vision_results:
        display = (display + " " + " ".join(vision_results)).strip()

    # Si se autocorrigió algo, mencionarlo brevemente
    if action_corrections:
        print(f"[Autocorrección] {action_corrections}")

    # Actualizar memoria (incluye detección de correcciones del usuario)
    asyncio.create_task(update_memory_background(hist[-6:], req.message))

    audio = await generate_voice(display)

    return {
        "reply":       display,
        "actions":     normal_acts if mac_online else [],
        "mac_online":  mac_online,
        "audio_b64":   audio,
        "ai_used":     ai_used,
        "corrections": action_corrections
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
  "tasks": ["tarea a añadir"],
  "fixes": [{"error": "descripcion del error o lo que falló", "solution": "como hacerlo bien"}]
}
Campos vacíos si no hay datos. SOLO JSON.
Preferencias: {"idioma_apps": "inglés", "app_música": "Spotify"}
Fixes: cuando el usuario dice que algo estaba mal o cómo corregirlo."""},
            {"role": "user", "content": conv}
        ], max_tokens=350)

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

        # Correcciones aprendidas del usuario
        for fix in extracted.get("fixes", []):
            err  = fix.get("error", "").strip()
            sol  = fix.get("solution", "").strip()
            if err and sol:
                store_fix(err, sol)
                changed = True

        memory["conversation_count"] = memory.get("conversation_count", 0) + 1

        if changed:
            save_memory(memory)
            print(f"[Memory] {len(memory['user_facts'])} hechos, {len(memory['preferences'])} prefs, {len(memory['fixes'])} fixes")

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
        "mac_online":    bool(agents),
        "memories":      len(memory.get("user_facts", [])),
        "preferences":   len(memory.get("preferences", {})),
        "constraints":   len(memory.get("constraints", [])),
        "routines":      len(memory.get("routines", [])),
        "tasks":         len(memory.get("tasks", [])),
        "fixes":         len(memory.get("fixes", {})),
        "conversations": memory.get("conversation_count", 0),
        "claude":        bool(claude_client),
        "data_path":     str(MEMORY_FILE)
    }

@app.get("/api/constraints")
async def get_constraints(secret: str = ""):
    if secret != YARVIS_SECRET:
        raise HTTPException(401, "No autorizado")
    return {"constraints": memory.get("constraints", [])}

@app.delete("/api/constraints")
async def clear_constraints(secret: str = ""):
    if secret != YARVIS_SECRET:
        raise HTTPException(401, "No autorizado")
    memory["constraints"] = []
    save_memory(memory)
    return {"ok": True}

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
