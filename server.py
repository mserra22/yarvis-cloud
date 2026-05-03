"""
YARVIS — Servidor Cloud
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

# ── Sistema de routing y presupuesto ──────────────────────────────────────────
# Precios Claude Sonnet 4 (USD por millón de tokens)
CLAUDE_INPUT_PRICE  = 3.0   # $3 / 1M input tokens
CLAUDE_OUTPUT_PRICE = 15.0  # $15 / 1M output tokens
USD_TO_EUR = 0.93           # tasa aproximada

# Estado del motor en memoria (se resetea al reiniciar)
ai_state = {
    "preferred_engine": "auto",   # "auto" | "claude" | "groq"
    "claude_available": bool(ANTHROPIC_KEY),
    "claude_failed":    False,     # True si falló por fondos agotados
    "session_cost_usd": 0.0,       # coste acumulado en esta sesión
    "total_cost_usd":   0.0,       # coste total histórico (se carga de memoria)
    "total_input_tokens":  0,
    "total_output_tokens": 0,
}

# Palabras clave que indican una tarea compleja → Claude
COMPLEX_KW = [
    "crea","desarrolla","programa","escribe código","script","analiza","análisis",
    "diseña","arquitectura","función","clase","algoritmo","html","python",
    "javascript","css","api","web","sistema","automatiza","explica en detalle",
    "resume","traduce","redacta","carta","email","informe","propuesta"
]

# Frases que son preguntas/acciones simples → Groq (para ahorrar créditos)
SIMPLE_PATTERNS = [
    r"^(hola|buenos días|buenas|qué tal|cómo estás)",
    r"^(abre|cierra|pon|para|sube|baja|activa|desactiva)\s",
    r"^(qué hora|qué día|qué tiempo|cuánto son|cuánto es)\b",
    r"^(pon música|pausa|siguiente|anterior|volumen)\b",
    r"^(sí|no|vale|ok|gracias|perfecto|bien)\b",
]

def is_simple_request(text: str) -> bool:
    t = text.lower().strip()
    # Menos de 8 palabras y sin keywords complejas → simple
    if len(t.split()) <= 6 and not any(k in t for k in COMPLEX_KW):
        return True
    return any(re.match(p, t) for p in SIMPLE_PATTERNS)

def decide_engine(text: str) -> str:
    """Decide qué motor usar para este mensaje."""
    pref = ai_state["preferred_engine"]

    # Órdenes explícitas de cambio de motor
    t = text.lower()
    if any(p in t for p in ["usa claude","cambia a claude","modo claude","activa claude"]):
        ai_state["preferred_engine"] = "claude"
        return "claude"
    if any(p in t for p in ["usa groq","cambia a groq","modo groq","activa groq","ahorra créditos"]):
        ai_state["preferred_engine"] = "groq"
        return "groq"

    # Si Claude no está disponible → Groq siempre
    if not claude_client or ai_state["claude_failed"]:
        return "groq"

    # Preferencia manual explícita
    if pref == "claude":
        return "claude"
    if pref == "groq":
        return "groq"

    # Modo auto: simple → Groq, complejo → Claude
    if is_simple_request(text):
        return "groq"
    return "claude"

def track_cost(input_tokens: int, output_tokens: int):
    """Registra el coste de una llamada a Claude."""
    cost = (input_tokens  / 1_000_000 * CLAUDE_INPUT_PRICE +
            output_tokens / 1_000_000 * CLAUDE_OUTPUT_PRICE)
    ai_state["session_cost_usd"]   += cost
    ai_state["total_cost_usd"]     += cost
    ai_state["total_input_tokens"] += input_tokens
    ai_state["total_output_tokens"]+= output_tokens
    return cost

def format_cost_report() -> str:
    """Genera informe de costes en euros."""
    sess  = ai_state["session_cost_usd"] * USD_TO_EUR
    total = ai_state["total_cost_usd"]   * USD_TO_EUR
    engine = ai_state["preferred_engine"]
    claude_ok = claude_client and not ai_state["claude_failed"]
    return (
        f"Esta sesión: {sess:.4f}€. "
        f"Total histórico: {total:.4f}€. "
        f"Motor actual: {'Claude (auto)' if engine=='auto' else engine.capitalize()}. "
        f"Claude: {'disponible' if claude_ok else 'sin fondos, usando Groq'}."
    )

def is_cost_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in [
        "cuánto llevo","cuánto he gastado","cuánto gasto","coste","costo",
        "cuánto me queda","fondos","créditos","euros","dinero","presupuesto",
        "cuánto cuesta","gasto de hoy","informe de gasto"
    ])

def is_engine_switch(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in [
        "usa claude","cambia a claude","modo claude","activa claude",
        "usa groq","cambia a groq","modo groq","activa groq","ahorra créditos"
    ])

# ── Almacenamiento persistente ────────────────────────────────────────────────
DATA_DIR    = Path("/data") if Path("/data").exists() else Path("/tmp")
MEMORY_FILE = DATA_DIR / "yarvis_memory.json"

session_histories: dict = {}
agents:       dict = {}
agent_queues: dict = {}

def load_memory():
    default = {
        "user_facts": [],
        "preferences": {},
        "constraints": [],
        "pending_followups": [],
        "routines": [],
        "tasks": [],
        "location": None,
        "conversation_count": 0,
        "learned_actions": {},
        "fixes": {},
        "total_cost_usd": 0.0,       # coste histórico persistente
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }
    try:
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text())
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
# Restaurar coste histórico desde memoria
ai_state["total_cost_usd"]      = memory.get("total_cost_usd", 0.0)
ai_state["total_input_tokens"]  = memory.get("total_input_tokens", 0)
ai_state["total_output_tokens"] = memory.get("total_output_tokens", 0)
print(f"[✓] Memoria cargada: {len(memory['user_facts'])} hechos, {len(memory['routines'])} rutinas, coste total: {memory.get('total_cost_usd',0)*USD_TO_EUR:.4f}€")

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

    base = f"""Eres Yarvis, asistente personal tipo Jarvis de Iron Man. Fecha y hora: {now}.

REGLAS ABSOLUTAS (nunca las incumplas):
1. Responde SOLO a lo que el usuario acaba de decir. No mezcles temas anteriores.
2. Máximo 2 frases. Sin markdown, asteriscos ni guiones.
3. NO hagas preguntas salvo que sea imposible actuar sin más información.
4. Si el usuario te dice que dejes de hacer algo, para INMEDIATAMENTE y confirma.
5. Si el usuario te corrige, di "Entendido" y aplica la corrección ahora mismo.
6. NUNCA repitas algo que ya falló. Si algo no funciona, prueba diferente.
7. La memoria (hechos, preferencias, restricciones) es para aprender, NO para meter respuestas antiguas en la conversación actual.
8. Responde siempre en español."""

    parts = [base]

    # ── RESTRICCIONES — se muestran primero y en mayúsculas para máxima prioridad
    constraints = memory.get("constraints", [])
    if constraints:
        c_text = "\n".join(f"- {c}" for c in constraints[-20:])
        parts.insert(1, f"⛔ RESTRICCIONES PERMANENTES (cumplir SIEMPRE sin excepción):\n{c_text}")

    # Preferencias del usuario
    # Restricciones — siempre primero y con máxima prioridad
    constraints = memory.get("constraints", [])
    if constraints:
        parts.append("⛔ NUNCA hagas esto (órdenes permanentes del usuario):\n" +
                     "\n".join(f"- {c}" for c in constraints[-10:]))

    # Solo las preferencias más relevantes
    prefs = memory.get("preferences", {})
    if prefs:
        parts.append("Preferencias: " + ", ".join(f"{k}={v}" for k,v in list(prefs.items())[:6]))

    # Hechos del usuario — solo los más recientes y relevantes
    facts = memory.get("user_facts", [])
    if facts:
        parts.append("Conoces al usuario: " + " · ".join(facts[-6:]))

    # Correcciones aprendidas — muy importantes
    fixes = memory.get("fixes", {})
    if fixes:
        parts.append("Correcciones (aplica siempre): " +
                     " · ".join(f"'{k[:30]}'→'{v['solution'][:30]}'" for k,v in list(fixes.items())[-5:]))

    # Seguimientos — solo si toca
    now_dt = datetime.now()
    followups = [f["text"] for f in memory.get("pending_followups",[])
                 if now_dt >= datetime.fromisoformat(f.get("due_after","2099-01-01"))]
    if followups:
        parts.append(f"Pregunta esto si es natural: {followups[0]}")

    # Estado del Mac
    if mac_online:
        parts.append("""Mac conectado. Etiquetas:
[APP:nombre] [URL:https://...] [CMD:comando] [SPOTLIGHT:texto]
[KEY:atajo] [TYPE:texto] [TYPEWRITE:texto con acentos]
[CLICK:x,y] [SCROLL:up/down] [SCREENSHOT] [WAIT:segundos]
[VISION_TASK:descripción] para tareas visuales complejas.
Apps: usa siempre nombre en inglés (Photos, Music, Mail, Safari, Finder...).""")
    else:
        parts.append("Mac NO conectado. No uses etiquetas de acción.")

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
    if not groq_client and not claude_client:
        raise HTTPException(500, "Sin API keys configuradas en Railway")

    mac_online = bool(agents)
    msg = req.message.strip()
    msg_lower = msg.lower()

    # ── Cerrar conversación ───────────────────────────────────────────────────
    if any(w in msg_lower for w in ["chao yarvis","adiós yarvis","adios yarvis","hasta luego yarvis"]):
        session_histories.pop(req.session_id, None)
        reply = "Hasta pronto."
        audio = await generate_voice(reply)
        return {"reply": reply, "actions": [], "mac_online": mac_online, "audio_b64": audio, "ai_used": "local"}

    # ── Respuestas locales sin IA ─────────────────────────────────────────────
    if is_cost_question(msg):
        report = format_cost_report()
        audio  = await generate_voice(report)
        return {"reply": report, "actions": [], "mac_online": mac_online, "audio_b64": audio, "ai_used": "local"}

    # ── Restricciones inmediatas ──────────────────────────────────────────────
    constraint = detect_constraint(msg)
    if constraint:
        apply_constraint(constraint)

    # ── Historial MUY limitado: solo el último intercambio ───────────────────
    # Solo guardamos el mensaje ANTERIOR para que Yarvis entienda referencias
    # como "eso", "lo anterior", etc. Nada más.
    prev = session_histories.get(req.session_id, [])
    if len(prev) > 2:  # max 1 intercambio previo (user + assistant)
        prev = prev[-2:]
        session_histories[req.session_id] = prev

    # Contexto de rutina
    routine = find_routine(msg)
    user_msg = msg
    if routine:
        ctx = await build_routine_context(routine)
        user_msg = f"{msg}\n[Rutina: {ctx}]"

    # Mensajes para la IA: solo sistema + (opcional) último intercambio + mensaje actual
    messages_for_ai = prev + [{"role": "user", "content": user_msg}]

    # ── Decidir motor ─────────────────────────────────────────────────────────
    engine = decide_engine(msg)
    engine_change_msg = None
    if is_engine_switch(msg):
        engine_change_msg = (
            "Cambiado a Claude." if engine == "claude"
            else "Cambiado a Groq, ahorrando créditos."
        )

    # ── Llamar a la IA ────────────────────────────────────────────────────────
    reply   = ""
    ai_used = engine

    try:
        if engine == "claude" and claude_client:
            r = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                system=build_system(mac_online),
                messages=messages_for_ai
            )
            reply = r.content[0].text
            cost  = track_cost(r.usage.input_tokens, r.usage.output_tokens)
            memory["total_cost_usd"]      = ai_state["total_cost_usd"]
            memory["total_input_tokens"]  = ai_state["total_input_tokens"]
            memory["total_output_tokens"] = ai_state["total_output_tokens"]
            print(f"[Claude] {r.usage.input_tokens}in/{r.usage.output_tokens}out = {cost*USD_TO_EUR:.5f}€")
        else:
            reply   = call_groq([{"role":"system","content":build_system(mac_online)}] + messages_for_ai, max_tokens=200)
            ai_used = "groq"

    except Exception as e:
        err = str(e)
        print(f"[Error {engine}]: {err}")
        # Fallback: si Claude falla → Groq
        if engine == "claude" and ("credit" in err.lower() or "balance" in err.lower() or "402" in err or "529" in err):
            ai_state["claude_failed"] = True
            try:
                reply   = call_groq([{"role":"system","content":build_system(mac_online)}] + messages_for_ai, max_tokens=200)
                ai_used = "groq"
                reply   = "Sin créditos en Claude, usando Groq. " + reply
            except Exception as e2:
                reply   = "Error temporal. Inténtalo de nuevo."
                ai_used = "error"
        elif groq_client:
            # Cualquier otro error → intentar con Groq como último recurso
            try:
                reply   = call_groq([{"role":"system","content":build_system(mac_online)}] + messages_for_ai, max_tokens=200)
                ai_used = "groq_fallback"
            except Exception:
                reply   = "Error temporal. Inténtalo de nuevo."
                ai_used = "error"
        else:
            reply   = "Error temporal. Inténtalo de nuevo."
            ai_used = "error"

    if engine_change_msg:
        reply = engine_change_msg + " " + reply

    # ── Procesar respuesta ────────────────────────────────────────────────────
    acts    = extract_actions(reply)
    display = clean_text(reply)

    # Guardar solo el último intercambio (limpio, sin etiquetas)
    session_histories[req.session_id] = [
        {"role": "user",      "content": msg},
        {"role": "assistant", "content": display or reply}
    ]

    # Acciones
    vision_tasks = [a for a in acts if a["type"] == "vision_task"]
    normal_acts  = [a for a in acts if a["type"] != "vision_task"]

    if normal_acts and agents:
        _, corrections = await execute_with_correction(normal_acts, msg)
        if corrections:
            print(f"[Fix aplicado] {corrections}")

    for vt in vision_tasks:
        vr = await run_vision_loop(vt["value"])
        if vr:
            display = (display + " " + vr).strip()

    # Memoria en background
    asyncio.create_task(update_memory_background(
        [{"role":"user","content":msg}, {"role":"assistant","content":display}],
        msg
    ))

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
        # Motor y costes
        "engine":          ai_state["preferred_engine"],
        "claude_available": claude_client is not None and not ai_state["claude_failed"],
        "session_cost_eur": round(ai_state["session_cost_usd"] * USD_TO_EUR, 5),
        "total_cost_eur":   round(ai_state["total_cost_usd"]   * USD_TO_EUR, 5),
        "total_tokens":     ai_state["total_input_tokens"] + ai_state["total_output_tokens"],
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
