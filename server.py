"""
YARVIS — Servidor Cloud v4 (reescritura limpia)
Variables Railway: GROQ_API_KEY, ANTHROPIC_API_KEY, YARVIS_SECRET, ELEVENLABS_API_KEY
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

# ── Configuración ─────────────────────────────────────────────────────────────
GROQ_KEY      = os.environ.get("GROQ_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET", "yarvis-secret")
ELEVEN_KEY    = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE  = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")

groq_client   = Groq(api_key=GROQ_KEY) if GROQ_KEY else None
claude_client = None
if ANTHROPIC_KEY:
    try:
        import anthropic as _ant
        claude_client = _ant.Anthropic(api_key=ANTHROPIC_KEY)
        print("[✓] Claude disponible")
    except Exception as e:
        print(f"[!] Claude no disponible: {e}")

# ── Persistencia ──────────────────────────────────────────────────────────────
DATA_DIR    = Path("/data") if Path("/data").exists() else Path("/tmp")
MEM_FILE    = DATA_DIR / "yarvis_v4.json"

agents:      dict[str, WebSocket] = {}
agent_q:     dict[str, asyncio.Queue] = {}
conv_history: dict[str, list] = {}  # historial por sesión

# ── Costes Claude ─────────────────────────────────────────────────────────────
PRICE_IN  = 3.0    # $ por millón tokens entrada
PRICE_OUT = 15.0   # $ por millón tokens salida
EUR       = 0.93

cost_session = 0.0
cost_total   = 0.0
claude_ok    = bool(claude_client)
force_engine = "auto"  # "auto" | "claude" | "groq"

# ── Nombres correctos de apps macOS ──────────────────────────────────────────
APP_MAP = {
    "fotos":"Photos","foto":"Photos","iphoto":"Photos",
    "música":"Music","musica":"Music","itunes":"Music",
    "mensajes":"Messages","correo":"Mail","calendario":"Calendar",
    "recordatorios":"Reminders","notas":"Notes","contactos":"Contacts",
    "mapas":"Maps","noticias":"News","clima":"Weather",
    "calculadora":"Calculator","ajustes":"System Settings",
    "configuracion":"System Settings","configuración":"System Settings",
    "system preferences":"System Settings",
    "photos":"Photos","music":"Music","mail":"Mail","messages":"Messages",
    "notes":"Notes","calendar":"Calendar","reminders":"Reminders",
    "finder":"Finder","safari":"Safari","terminal":"Terminal",
    "facetime":"FaceTime","spotify":"Spotify",
    "chrome":"Google Chrome","google chrome":"Google Chrome",
    "vscode":"Visual Studio Code","vs code":"Visual Studio Code",
    "word":"Microsoft Word","excel":"Microsoft Excel",
    "powerpoint":"Microsoft PowerPoint","teams":"Microsoft Teams",
    "zoom":"Zoom","slack":"Slack","discord":"Discord","whatsapp":"WhatsApp",
}

# ── Memoria ───────────────────────────────────────────────────────────────────
def load_mem() -> dict:
    try:
        if MEM_FILE.exists():
            d = json.loads(MEM_FILE.read_text())
            d.setdefault("facts", [])
            d.setdefault("prefs", {})
            d.setdefault("rules", [])   # cosas que NUNCA debe hacer
            d.setdefault("cost_eur", 0.0)
            return d
    except Exception as e:
        print(f"Mem load error: {e}")
    return {"facts": [], "prefs": {}, "rules": [], "routines": [], "cost_eur": 0.0}

def save_mem(m: dict):
    try:
        MEM_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Mem save error: {e}")

mem = load_mem()
mem.setdefault("routines", [])
cost_total = mem.get("cost_eur", 0.0) / EUR
print(f"[✓] Memoria: {len(mem['facts'])} hechos, {len(mem['rules'])} reglas, {len(mem['routines'])} rutinas, coste: {mem['cost_eur']:.4f}€")

# ── Prompt del sistema ────────────────────────────────────────────────────────
def system_prompt(mac_online: bool) -> str:
    now = datetime.now().strftime("%A %d de %B de %Y, %H:%M")

    lines = [
        f"Eres Yarvis, asistente personal de voz tipo Jarvis (Iron Man). Fecha: {now}.",
        "",
        "CÓMO DEBES COMPORTARTE:",
        "- Responde SOLO al último mensaje del usuario. Sigue el hilo natural de la conversación.",
        "- Sé conciso: máximo 2 frases para respuestas de voz. Sin markdown, sin asteriscos.",
        "- Si vas a ejecutar algo en el Mac, di qué vas a hacer en 1 frase y hazlo.",
        "- Si el usuario te corrige, di 'Entendido' y aplica la corrección de inmediato.",
        "- Si algo falla, admítelo y propón una alternativa diferente.",
        "- NUNCA repitas algo que ya falló. Cambia el enfoque.",
        "- Responde siempre en español.",
    ]

    # Rutinas configuradas
    routines = mem.get("routines", [])
    if routines:
        lines.append("")
        lines.append("RUTINAS CONFIGURADAS (ejecútalas cuando el usuario diga el trigger):")
        for rt in routines[-10:]:
            lines.append(f"- Cuando diga '{rt['trigger']}': {rt['description']}")

    # Reglas permanentes del usuario
    if mem["rules"]:
        lines.append("")
        lines.append("REGLAS PERMANENTES (cumple SIEMPRE):")
        for r in mem["rules"][-10:]:
            lines.append(f"- {r}")

    # Preferencias
    if mem["prefs"]:
        lines.append("")
        lines.append("Preferencias: " + ", ".join(f"{k}={v}" for k,v in list(mem["prefs"].items())[:8]))

    # Hechos del usuario
    if mem["facts"]:
        lines.append("")
        lines.append("Sabes del usuario: " + " · ".join(mem["facts"][-6:]))

    # Control del Mac
    if mac_online:
        lines.append("")
        lines.append("El Mac está conectado. Puedes controlarlo con estas etiquetas en tu respuesta:")
        lines.append("[APP:nombre]         → abrir aplicación (usa nombre en inglés)")
        lines.append("[URL:https://...]    → abrir URL en navegador")
        lines.append("[CMD:comando]        → ejecutar en terminal")
        lines.append("[KEY:atajo]          → pulsar tecla/atajo (ej: cmd+space, enter)")
        lines.append("[TYPE:texto]         → escribir texto")
        lines.append("[TYPEWRITE:texto]    → escribir texto con acentos/caracteres especiales")
        lines.append("[CLICK:x,y]         → click en coordenadas absolutas")
        lines.append("[SCROLL:up/down]    → hacer scroll")
        lines.append("[SCREENSHOT]         → capturar pantalla")
        lines.append("[WAIT:segundos]      → esperar N segundos")
        lines.append("[SPOTLIGHT:texto]    → buscar con Spotlight")
        lines.append("[VISION_TASK:desc]   → VER la pantalla y actuar según lo que haya")
        lines.append("")
        lines.append("COMANDOS DE ARCHIVOS (estilo Cowork):")
        lines.append("[FILE_ORG:carpeta]   → organizar archivos de una carpeta por tipo/fecha")
        lines.append("[FILE_FIND:patron]   → buscar archivos que coincidan con un patrón")
        lines.append("[FILE_MOVE:origen|destino] → mover archivo o carpeta")
        lines.append("[FILE_RENAME:ruta|nuevo_nombre] → renombrar archivo")
        lines.append("[FILE_NEW:ruta]      → crear carpeta nueva")
        lines.append("[FILE_LIST:carpeta]  → listar contenido de carpeta")
        lines.append("")
        lines.append("Visión: usa [VISION_TASK:descripción en inglés] cuando el usuario pida interactuar con algo en pantalla")
        lines.append("Apps: siempre en inglés. Photos, Music, Mail, Safari, Finder...")
    else:
        lines.append("")
        lines.append("El Mac NO está conectado ahora mismo. No uses etiquetas de control.")

    return "\n".join(lines)

# ── Extracción de acciones ────────────────────────────────────────────────────
ACTION_RE = {
    "app":         re.compile(r'\[APP:([^\]]+)\]',              re.I),
    "url":         re.compile(r'\[URL:(https?://[^\]]+)\]',      re.I),
    "cmd":         re.compile(r'\[CMD:([^\]]+)\]',               re.I),
    "key":         re.compile(r'\[KEY:([^\]]+)\]',               re.I),
    "type":        re.compile(r'\[TYPE:([^\]]+)\]',              re.I),
    "typewrite":   re.compile(r'\[TYPEWRITE:([^\]]+)\]',         re.I),
    "click":       re.compile(r'\[CLICK:([^\]]+)\]',             re.I),
    "scroll":      re.compile(r'\[SCROLL:([^\]]+)\]',            re.I),
    "screenshot":  re.compile(r'\[SCREENSHOT\]',                 re.I),
    "wait":        re.compile(r'\[WAIT:([^\]]+)\]',              re.I),
    "spotlight":   re.compile(r'\[SPOTLIGHT:([^\]]+)\]',         re.I),
    "vision":      re.compile(r'\[VISION_TASK:([^\]]+)\]',       re.I),
    "file_org":    re.compile(r'\[FILE_ORG:([^\]]+)\]',          re.I),
    "file_find":   re.compile(r'\[FILE_FIND:([^\]]+)\]',         re.I),
    "file_move":   re.compile(r'\[FILE_MOVE:([^\]]+)\]',         re.I),
    "file_rename": re.compile(r'\[FILE_RENAME:([^\]]+)\]',       re.I),
    "file_new":    re.compile(r'\[FILE_NEW:([^\]]+)\]',          re.I),
    "file_list":   re.compile(r'\[FILE_LIST:([^\]]+)\]',         re.I),
}

def extract_actions(text: str) -> list:
    acts = []
    for atype, pattern in ACTION_RE.items():
        if atype == "screenshot":
            if pattern.search(text):
                acts.append({"type": "screenshot", "value": ""})
        else:
            for val in pattern.findall(text):
                v = val.strip()
                if atype == "app":
                    v = APP_MAP.get(v.lower(), v)  # corregir nombre
                acts.append({"type": atype, "value": v})
    return acts

def clean_reply(text: str) -> str:
    for pattern in ACTION_RE.values():
        text = pattern.sub("", text)
    return text.strip()

# ── IA: Claude + Groq ────────────────────────────────────────────────────────
def use_claude_for(msg: str) -> bool:
    """Decide si usar Claude basándose en preferencia y complejidad."""
    global force_engine
    if not claude_ok or not claude_client:
        return False
    if force_engine == "groq":
        return False
    if force_engine == "claude":
        return True
    # Auto: Groq para mensajes cortos y simples
    simple = len(msg.split()) <= 5 or any(msg.lower().startswith(w) for w in
             ["hola","abre","pon","para","sí","no","vale","ok","gracias","qué hora","qué día"])
    return not simple

def call_groq_sync(messages: list) -> str:
    models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"]
    for model in models:
        try:
            r = groq_client.chat.completions.create(
                model=model, messages=messages,
                max_tokens=200, temperature=0.6
            )
            return r.choices[0].message.content.strip()
        except Exception as e:
            if "rate_limit" in str(e) or "429" in str(e):
                continue
            raise
    raise Exception("Todos los modelos Groq fallaron")

async def ask_ai(messages: list, mac_online: bool) -> tuple:
    """Llama a la IA y devuelve (respuesta, motor_usado)."""
    global cost_session, cost_total, claude_ok, force_engine

    sys = system_prompt(mac_online)

    # Intentar Claude
    if use_claude_for(messages[-1]["content"]) and claude_client:
        try:
            r = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                system=sys,
                messages=messages
            )
            reply = r.content[0].text.strip()
            # Registrar coste
            c = (r.usage.input_tokens/1e6*PRICE_IN + r.usage.output_tokens/1e6*PRICE_OUT) * EUR
            cost_session += c
            cost_total   += c
            mem["cost_eur"] = cost_total
            save_mem(mem)
            print(f"[Claude] {r.usage.input_tokens}in/{r.usage.output_tokens}out = {c:.5f}€")
            return reply, "claude"
        except Exception as e:
            err = str(e).lower()
            if "credit" in err or "balance" in err or "402" in err or "529" in err:
                claude_ok = False
                print("[!] Claude sin fondos → Groq")
            else:
                print(f"[Claude error] {e}")

    # Groq como fallback o motor principal
    if groq_client:
        try:
            reply = call_groq_sync([{"role":"system","content":sys}] + messages)
            return reply, "groq"
        except Exception as e:
            print(f"[Groq error] {e}")

    return "Error temporal, inténtalo de nuevo.", "error"

# ── Tiempo meteorológico ──────────────────────────────────────────────────────
WEATHER_CODES = {
    0:"despejado",1:"casi despejado",2:"parcialmente nublado",3:"nublado",
    45:"niebla",61:"lluvia ligera",63:"lluvia moderada",65:"lluvia fuerte",
    71:"nieve ligera",80:"chubascos",95:"tormenta"
}

async def get_weather() -> str:
    loc = mem.get("prefs", {}).get("ubicacion") or mem.get("prefs", {}).get("ciudad") or mem.get("prefs", {}).get("location")
    lat = mem.get("prefs", {}).get("lat")
    lon = mem.get("prefs", {}).get("lon")
    if not lat or not lon:
        return "ubicación no configurada"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast",
                params={"latitude":lat,"longitude":lon,"current":"temperature_2m,weather_code,wind_speed_10m","timezone":"auto"})
            if r.status_code == 200:
                d = r.json()["current"]
                desc = WEATHER_CODES.get(d["weather_code"], "variable")
                return f"{round(d['temperature_2m'])}°C, {desc}, viento {round(d['wind_speed_10m'])} km/h"
    except Exception as e:
        print(f"[Weather] {e}")
    return "no disponible"

# ── Rutinas ───────────────────────────────────────────────────────────────────
def find_routine(msg: str):
    for rt in mem.get("routines", []):
        if rt.get("trigger", "").lower() in msg.lower():
            return rt
    return None

async def build_routine_context(rt: dict) -> str:
    parts = [f"Rutina '{rt['trigger']}': {rt['description']}"]
    if rt.get("weather"):
        w = await get_weather()
        parts.append(f"Tiempo: {w}")
    if rt.get("tasks") and mem.get("tasks"):
        parts.append(f"Tareas: {', '.join(mem['tasks'][:5])}")
    if rt.get("time"):
        parts.append(f"Hora: {datetime.now().strftime('%H:%M')}")
    return ". ".join(parts)


async def tts(text: str):
    if not ELEVEN_KEY or not text:
        return None
    clean = re.sub(r"[*_`#\[\]]", "", text)[:450]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={"xi-api-key":ELEVEN_KEY,"Content-Type":"application/json","Accept":"audio/mpeg"},
                json={"text":clean,"model_id":"eleven_multilingual_v2",
                      "voice_settings":{"stability":0.55,"similarity_boost":0.80,"style":0.1,"use_speaker_boost":True}}
            )
            if r.status_code == 200:
                return base64.b64encode(r.content).decode()
    except Exception as e:
        print(f"[ElevenLabs] {e}")
    return None

# ── Aprendizaje en background ─────────────────────────────────────────────────
async def learn(user_msg: str, assistant_reply: str):
    """Extrae hechos, preferencias, reglas explícitas y rutinas."""
    if not groq_client:
        return
    try:
        r = call_groq_sync([
            {"role":"system","content":"""Analiza este intercambio y extrae en JSON:
{
  "facts": ["hecho concreto sobre el usuario (nombre, trabajo, ciudad, gustos)"],
  "prefs": {"clave": "valor"},
  "rules": ["regla SOLO si el usuario dijo: nunca, siempre, recuerda que, no vuelvas a, cada vez que"],
  "routine": {"trigger": "frase exacta que activa la rutina", "description": "qué debe hacer Yarvis", "weather": false, "tasks": false, "time": false}
}
IMPORTANTE:
- "rules" solo si el usuario ordenó explícitamente algo permanente
- "routine" solo si el usuario pidió configurar una rutina (ej: "cuando diga buenos días, dime el tiempo")
- "routine" debe ser null si no hay rutina
- weather/tasks/time son true si la rutina incluye esas cosas
- SOLO JSON"""},
            {"role":"user","content":f"Usuario: {user_msg}\nYarvis: {assistant_reply}"}
        ])
        r = re.sub(r"```json|```","",r).strip()
        m = re.search(r'\{.*\}', r, re.DOTALL)
        if not m: return
        d = json.loads(m.group())
        changed = False

        for f in d.get("facts",[]):
            if f and len(f) > 5 and f not in mem["facts"]:
                mem["facts"].append(f); changed = True
        mem["facts"] = mem["facts"][-40:]

        for k,v in d.get("prefs",{}).items():
            if k and v:
                mem["prefs"][k] = v; changed = True

        for rule in d.get("rules",[]):
            if rule and rule not in mem["rules"]:
                mem["rules"].append(rule); changed = True
        mem["rules"] = mem["rules"][-20:]

        rt = d.get("routine")
        if rt and rt.get("trigger") and rt.get("description"):
            trigger = rt["trigger"].lower().strip()
            existing = [r.get("trigger","").lower() for r in mem.get("routines",[])]
            if trigger not in existing:
                mem.setdefault("routines", []).append(rt)
                mem["routines"] = mem["routines"][-20:]
                changed = True
                print(f"[Rutina guardada] '{trigger}' → {rt['description']}")

        if changed:
            save_mem(mem)
            print(f"[Mem] facts={len(mem['facts'])} rules={len(mem['rules'])} routines={len(mem.get('routines',[]))}")
    except Exception as e:
        print(f"[Learn error] {e}")

# ── Sistema de visión ────────────────────────────────────────────────────────
async def get_screenshot() -> dict:
    """Pide captura al agente Mac."""
    if not agents:
        return {}
    aid = next(iter(agents))
    ws  = agents[aid]
    q   = agent_q.get(aid)
    if not q:
        return {}
    try:
        await ws.send_text(json.dumps({"type": "request_screenshot"}))
        msg = await asyncio.wait_for(q.get(), timeout=12)
        if msg.get("type") == "screenshot":
            print(f"[Vision] Screenshot recibido: {msg.get('screen_w')}x{msg.get('screen_h')}")
            return msg
    except asyncio.TimeoutError:
        print("[Vision] Timeout esperando screenshot")
    except Exception as e:
        print(f"[Vision] Error: {e}")
    return {}

async def vision_step(b64: str, img_w: int, img_h: int,
                      screen_w: int, screen_h: int, task: str, step: int) -> dict:
    """Analiza screenshot con Claude Vision (mejor que Groq para esta tarea)."""
    if not b64:
        return {"done": True, "reason": "sin imagen"}

    prompt = f"""Analyze this Mac screenshot ({img_w}x{img_h}px, real screen {screen_w}x{screen_h}px).
Task: {task}
Step: {step+1}

Return ONLY valid JSON (no explanation, no markdown):
{{"action":"click|type|key|scroll|wait|done","px":0.5,"py":0.5,"text":"","key":"","scroll":"up","wait":1,"done":false,"reason":"brief description of what you see"}}

- px, py: position as fraction of image (0.0=top-left, 1.0=bottom-right)
- click: find the EXACT element center
- If task is already done or element not found after looking: done=true
- Elements not visible: use scroll or key first"""

    # Intentar con Claude primero (mejor visión)
    if claude_client:
        try:
            import anthropic as _ant
            r = claude_client.messages.create(
                model="claude-opus-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64
                    }},
                    {"type": "text", "text": prompt}
                ]}]
            )
            text = re.sub(r"```json|```", "", r.content[0].text).strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                d = json.loads(m.group())
                print(f"[Vision/Claude step {step+1}] {d.get('action')} px={d.get('px'):.2f} py={d.get('py'):.2f} — {d.get('reason','')[:60]}")
                return d
        except Exception as e:
            print(f"[Vision/Claude error] {e}")

    # Fallback: Groq vision
    if groq_client:
        try:
            r = groq_client.chat.completions.create(
                model="llama-3.2-11b-vision-preview",
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt}
                ]}],
                max_tokens=200, temperature=0.05
            )
            text = re.sub(r"```json|```", "", r.choices[0].message.content).strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                d = json.loads(m.group())
                print(f"[Vision/Groq step {step+1}] {d.get('action')} — {d.get('reason','')[:60]}")
                return d
        except Exception as e:
            print(f"[Vision/Groq error] {e}")

    return {"done": True, "reason": "vision unavailable"}

async def vision_task(task: str, max_steps: int = 8) -> str:
    """Bucle: captura → analiza → actúa → repite."""
    if not agents:
        return "El Mac no está conectado."
    if not claude_client and not groq_client:
        return "Sin modelo de visión disponible."

    aid = next(iter(agents))
    ws  = agents[aid]
    q   = agent_q.get(aid)
    if not q:
        return "Error de conexión con el Mac."

    log = []
    print(f"[Vision] Iniciando tarea: {task}")

    for step in range(max_steps):
        # Capturar pantalla
        sc = await get_screenshot()
        if not sc or not sc.get("data"):
            return "No pude capturar la pantalla. Verifica permisos de Grabación de pantalla en Terminal."

        b64      = sc["data"]
        screen_w = sc.get("screen_w", 1440)
        screen_h = sc.get("screen_h", 900)
        img_w    = sc.get("image_w", screen_w)
        img_h    = sc.get("image_h", screen_h)

        # Analizar
        d = await vision_step(b64, img_w, img_h, screen_w, screen_h, task, step)

        if d.get("done") or d.get("action") == "done":
            log.append(f"OK: {d.get('reason','completado')}")
            break

        # Convertir fracción → píxeles reales
        action = None
        act = d.get("action", "")
        if act == "click":
            x = int(float(d.get("px", 0.5)) * screen_w)
            y = int(float(d.get("py", 0.5)) * screen_h)
            action = {"type": "click", "value": f"{x},{y}"}
            log.append(f"Click ({x},{y}): {d.get('reason','')[:40]}")
        elif act == "type":
            action = {"type": "typewrite", "value": d.get("text", "")}
            log.append(f"Escribir: {d.get('text','')[:30]}")
        elif act == "key":
            action = {"type": "key", "value": d.get("key", "")}
            log.append(f"Tecla: {d.get('key','')}")
        elif act == "scroll":
            action = {"type": "scroll", "value": d.get("scroll", "down")}
            log.append(f"Scroll {d.get('scroll','')}")
        elif act == "wait":
            action = {"type": "wait", "value": str(d.get("wait", 1))}
            log.append(f"Esperar {d.get('wait',1)}s")

        if action:
            try:
                await ws.send_text(json.dumps({"type":"actions","actions":[action]}))
                await asyncio.wait_for(q.get(), timeout=5)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.8)
        else:
            break

    result = f"Hecho en {len(log)} pasos." if log else "Sin acciones."
    print(f"[Vision] Completado: {log}")
    return result

# ── Ejecutar acciones en Mac ──────────────────────────────────────────────────
async def send_actions(acts: list):
    """Envía acciones normales al agente. Las VISION_TASK se ejecutan aparte."""
    normal = [a for a in acts if a["type"] != "vision"]
    vision = [a for a in acts if a["type"] == "vision"]

    if normal and agents:
        aid = next(iter(agents))
        ws  = agents[aid]
        try:
            await ws.send_text(json.dumps({"type": "actions", "actions": normal}))
        except Exception as e:
            print(f"[Agent send] {e}")

    for vt in vision:
        asyncio.create_task(vision_task(vt["value"]))

# ── API principal ─────────────────────────────────────────────────────────────
class ChatReq(BaseModel):
    message:    str
    session_id: str = "default"
    secret:     str = ""

@app.post("/api/chat")
async def chat(req: ChatReq):
    global force_engine, cost_session

    if req.secret != YARVIS_SECRET:
        raise HTTPException(401, "Clave incorrecta")

    mac_online = bool(agents)
    msg        = req.message.strip()
    msg_lower  = msg.lower()

    # ── Comandos especiales sin IA ────────────────────────────────────────────

    # Cerrar conversación
    if any(w in msg_lower for w in ["chao yarvis","adiós yarvis","adios yarvis","hasta luego yarvis"]):
        conv_history.pop(req.session_id, None)
        reply = "Hasta pronto."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # Consulta de costes
    if any(w in msg_lower for w in ["cuánto llevo","cuánto he gastado","cuánto gasto","coste","euros gastados","presupuesto","fondos"]):
        reply = (f"Esta sesión: {cost_session:.4f}€. "
                 f"Total histórico: {cost_total:.4f}€. "
                 f"Motor: {'Claude' if claude_ok and force_engine!='groq' else 'Groq'}. "
                 f"Claude: {'disponible' if claude_ok else 'sin fondos, usando Groq'}.")
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # Cambio de motor
    if any(w in msg_lower for w in ["usa claude","cambia a claude","modo claude"]):
        force_engine = "claude"
        reply = "Cambiado a Claude para todo."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}
    if any(w in msg_lower for w in ["usa groq","cambia a groq","ahorra créditos","modo groq"]):
        force_engine = "groq"
        reply = "Cambiado a Groq, ahorrando créditos."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}
    if any(w in msg_lower for w in ["modo auto","motor automático","motor auto"]):
        force_engine = "auto"
        reply = "Motor en modo automático: Claude para tareas complejas, Groq para lo simple."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # Borrar restricciones/reglas
    if any(w in msg_lower for w in ["borra las restricciones","elimina las restricciones","borra las reglas",
                                     "elimina las reglas","borra tus reglas","sin restricciones",
                                     "quita las restricciones","limpia las reglas"]):
        mem["rules"] = []
        save_mem(mem)
        reply = "Restricciones eliminadas. No tengo ninguna regla permanente activa."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # Borrar toda la memoria
    if any(w in msg_lower for w in ["borra tu memoria","olvida todo","resetea tu memoria","limpia tu memoria"]):
        mem["facts"] = []; mem["prefs"] = {}; mem["rules"] = []; mem["routines"] = []
        save_mem(mem); conv_history.clear()
        reply = "Memoria borrada. Empezamos desde cero."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # Mostrar restricciones actuales
    if any(w in msg_lower for w in ["qué restricciones tienes","qué reglas tienes","cuáles son tus reglas"]):
        if mem["rules"]:
            reply = "Mis reglas activas: " + ". ".join(mem["rules"][:5])
        else:
            reply = "No tengo ninguna restricción o regla activa actualmente."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # Ver rutinas configuradas
    if any(w in msg_lower for w in ["qué rutinas tienes","cuáles son tus rutinas","mis rutinas"]):
        rts = mem.get("routines", [])
        if rts:
            reply = "Rutinas: " + ". ".join(f"'{r['trigger']}': {r['description']}" for r in rts[:4])
        else:
            reply = "No tienes rutinas configuradas todavía."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # Borrar rutinas
    if any(w in msg_lower for w in ["borra las rutinas","elimina las rutinas","borra tus rutinas"]):
        mem["routines"] = []; save_mem(mem)
        reply = "Rutinas eliminadas."
        return {"reply":reply,"actions":[],"mac_online":mac_online,"audio_b64":await tts(reply),"engine":"local"}

    # ── Detectar si el mensaje activa una rutina ──────────────────────────────
    routine = find_routine(msg)
    extra_context = ""
    if routine:
        extra_context = await build_routine_context(routine)

    # ── Historial de conversación ─────────────────────────────────────────────
    hist = conv_history.setdefault(req.session_id, [])
    user_content = msg + (f"\n[Contexto rutina: {extra_context}]" if extra_context else "")
    hist.append({"role":"user","content":user_content})
    if len(hist) > 8:
        hist = hist[-8:]
    conv_history[req.session_id] = hist

    # ── Llamar a la IA ────────────────────────────────────────────────────────
    reply, engine_used = await ask_ai(hist, mac_online)

    # Guardar respuesta en historial (limpia, sin etiquetas)
    clean = clean_reply(reply)
    hist.append({"role":"assistant","content":clean or reply})
    if len(hist) > 8:
        hist = hist[-8:]
    conv_history[req.session_id] = hist

    # ── Extraer y ejecutar acciones ───────────────────────────────────────────
    acts = extract_actions(reply)
    if acts and mac_online:

        asyncio.create_task(send_actions(acts))

    # ── Aprender en background ────────────────────────────────────────────────
    asyncio.create_task(learn(msg, clean or reply))

    # ── TTS ───────────────────────────────────────────────────────────────────
    audio = await tts(clean or reply)

    return {
        "reply":      clean or reply,
        "actions":    acts if mac_online else [],
        "mac_online": mac_online,
        "audio_b64":  audio,
        "engine":     engine_used
    }

# ── WebSocket agente Mac ──────────────────────────────────────────────────────
@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    if ws.query_params.get("secret","") != YARVIS_SECRET:
        await ws.close(1008); return
    await ws.accept()
    aid = f"mac-{id(ws)}"
    agents[aid]  = ws
    agent_q[aid] = asyncio.Queue()
    print(f"[+] Mac conectado")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                d = json.loads(raw)
                if d.get("type") in ("screenshot","results"):
                    await agent_q[aid].put(d)
            except Exception:
                pass
    except WebSocketDisconnect:
        agents.pop(aid, None)
        agent_q.pop(aid, None)
        print(f"[-] Mac desconectado")

# ── Status y memoria ──────────────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return {
        "mac_online":    bool(agents),
        "facts":         len(mem["facts"]),
        "rules":         len(mem["rules"]),
        "prefs":         len(mem["prefs"]),
        "routines":      len(mem.get("routines",[])),
        "engine":        force_engine,
        "claude_ok":     claude_ok,
        "cost_session":  round(cost_session, 5),
        "cost_total":    round(cost_total * EUR, 5),
    }

@app.get("/api/memory")
async def get_memory(secret: str = ""):
    if secret != YARVIS_SECRET: raise HTTPException(401)
    return mem

@app.delete("/api/memory")
async def clear_memory(secret: str = ""):
    global mem
    if secret != YARVIS_SECRET: raise HTTPException(401)
    mem = {"facts":[],"prefs":{},"rules":[],"cost_eur":0.0}
    save_mem(mem)
    conv_history.clear()
    return {"ok": True}

# ── Frontend ──────────────────────────────────────────────────────────────────
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
    return JSONResponse({"status":"Yarvis v4 online","claude":claude_ok,"facts":len(mem["facts"])})

@app.get("/{path:path}")
async def spa(path: str):
    p = find("index.html")
    if p: return FileResponse(str(p), media_type="text/html")
    raise HTTPException(404)
