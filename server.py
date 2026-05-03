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
    return {"facts": [], "prefs": {}, "rules": [], "cost_eur": 0.0}

def save_mem(m: dict):
    try:
        MEM_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Mem save error: {e}")

mem = load_mem()
cost_total = mem.get("cost_eur", 0.0) / EUR  # restaurar en USD
print(f"[✓] Memoria: {len(mem['facts'])} hechos, {len(mem['rules'])} reglas, coste histórico: {mem['cost_eur']:.4f}€")

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

    # Reglas permanentes del usuario
    if mem["rules"]:
        lines.append("")
        lines.append("REGLAS PERMANENTES DEL USUARIO (cumple SIEMPRE):")
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
        lines.append("[CLICK:x,y]         → click en coordenadas")
        lines.append("[SCROLL:up/down]    → hacer scroll")
        lines.append("[SCREENSHOT]         → capturar pantalla")
        lines.append("[WAIT:segundos]      → esperar N segundos")
        lines.append("[SPOTLIGHT:texto]    → buscar con Spotlight")
        lines.append("Nombres de apps: siempre en inglés. Photos (no Fotos), Music (no iTunes), Mail, Safari, Finder...")
        lines.append("Puedes encadenar varias etiquetas en una respuesta.")
    else:
        lines.append("")
        lines.append("El Mac NO está conectado ahora mismo. No uses etiquetas de control.")

    return "\n".join(lines)

# ── Extracción de acciones ────────────────────────────────────────────────────
ACTION_RE = {
    "app":        re.compile(r'\[APP:([^\]]+)\]',        re.I),
    "url":        re.compile(r'\[URL:(https?://[^\]]+)\]', re.I),
    "cmd":        re.compile(r'\[CMD:([^\]]+)\]',         re.I),
    "key":        re.compile(r'\[KEY:([^\]]+)\]',         re.I),
    "type":       re.compile(r'\[TYPE:([^\]]+)\]',        re.I),
    "typewrite":  re.compile(r'\[TYPEWRITE:([^\]]+)\]',   re.I),
    "click":      re.compile(r'\[CLICK:([^\]]+)\]',       re.I),
    "scroll":     re.compile(r'\[SCROLL:([^\]]+)\]',      re.I),
    "screenshot": re.compile(r'\[SCREENSHOT\]',           re.I),
    "wait":       re.compile(r'\[WAIT:([^\]]+)\]',        re.I),
    "spotlight":  re.compile(r'\[SPOTLIGHT:([^\]]+)\]',   re.I),
    "vision":     re.compile(r'\[VISION_TASK:([^\]]+)\]', re.I),
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

# ── TTS ElevenLabs ────────────────────────────────────────────────────────────
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
    """Extrae hechos y reglas de la conversación y los guarda."""
    if not groq_client:
        return
    try:
        r = call_groq_sync([
            {"role":"system","content":"""Analiza este intercambio y extrae en JSON:
{
  "facts": ["hecho concreto sobre el usuario"],
  "prefs": {"clave": "valor"},
  "rules": ["regla permanente si el usuario pidió que no hiciera algo o siempre hiciera algo"]
}
Solo incluye datos reales y explícitos. Si no hay nada relevante, devuelve campos vacíos. SOLO JSON."""},
            {"role":"user","content":f"Usuario: {user_msg}\nYarvis: {assistant_reply}"}
        ])
        r = re.sub(r"```json|```","",r).strip()
        m = re.search(r'\{.*\}', r, re.DOTALL)
        if not m: return
        d = json.loads(m.group())
        changed = False
        for f in d.get("facts",[]):
            if f and f not in mem["facts"]:
                mem["facts"].append(f); changed = True
        mem["facts"] = mem["facts"][-40:]
        for k,v in d.get("prefs",{}).items():
            if k and v:
                mem["prefs"][k] = v; changed = True
        for rule in d.get("rules",[]):
            if rule and rule not in mem["rules"]:
                mem["rules"].append(rule); changed = True
        mem["rules"] = mem["rules"][-20:]
        if changed:
            save_mem(mem)
            print(f"[Mem] {len(mem['facts'])} hechos, {len(mem['rules'])} reglas")
    except Exception as e:
        print(f"[Learn error] {e}")

# ── Ejecutar acciones en Mac ──────────────────────────────────────────────────
async def send_actions(acts: list):
    if not acts or not agents:
        return
    aid = next(iter(agents))
    ws  = agents[aid]
    try:
        await ws.send_text(json.dumps({"type":"actions","actions":acts}))
    except Exception as e:
        print(f"[Agent send error] {e}")

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

    # ── Historial de conversación ─────────────────────────────────────────────
    # Guardamos las últimas 4 interacciones (8 mensajes)
    # Suficiente para conversación fluida, sin contaminación de sesiones viejas
    hist = conv_history.setdefault(req.session_id, [])
    hist.append({"role":"user","content":msg})
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
    # Enviar al Mac de forma asíncrona (no bloquea la respuesta)
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
