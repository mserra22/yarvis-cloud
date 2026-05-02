"""
YARVIS — Servidor con Visión, Aprendizaje y Control Total
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

GROQ_KEY      = os.environ.get("GROQ_API_KEY","")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY","")
YARVIS_SECRET = os.environ.get("YARVIS_SECRET","yarvis-secret")
ELEVEN_KEY    = os.environ.get("ELEVENLABS_API_KEY","")
ELEVEN_VOICE  = os.environ.get("ELEVENLABS_VOICE_ID","onwK4e9ZLuTAKqWW03F9")
GROQ_MODEL    = os.environ.get("GROQ_MODEL","llama-3.3-70b-versatile")
VISION_MODEL  = "llama-3.2-11b-vision-preview"  # Groq gratis con visión

groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

claude_client = None
if ANTHROPIC_KEY:
    try:
        import anthropic
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    except: pass

DATA_DIR    = Path("/data") if Path("/data").exists() else Path("/tmp")
MEMORY_FILE = DATA_DIR / "yarvis_memory.json"
session_histories: dict[str, list] = {}

# ── Agentes con colas bidireccionales ─────────────────────────────────────────
class AgentConn:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue()

agents: dict[str, AgentConn] = {}

# ── Memoria ───────────────────────────────────────────────────────────────────
def load_memory():
    try:
        if MEMORY_FILE.exists(): return json.loads(MEMORY_FILE.read_text())
    except: pass
    return {"user_facts":[],"pending_followups":[],"routines":[],"tasks":[],"location":None,"conversation_count":0,"learned_actions":{}}

def save_memory(m):
    try: MEMORY_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2))
    except Exception as e: print(f"Memory save error: {e}")

memory = load_memory()

# ── Weather ───────────────────────────────────────────────────────────────────
WEATHER_CODES = {0:"despejado",1:"casi despejado",2:"parcialmente nublado",3:"nublado",45:"niebla",
                 51:"llovizna",61:"lluvia ligera",63:"lluvia moderada",65:"lluvia fuerte",
                 71:"nieve ligera",80:"chubascos",95:"tormenta"}

async def get_weather(lat, lon):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast",
                params={"latitude":lat,"longitude":lon,"current":"temperature_2m,weather_code,wind_speed_10m","timezone":"auto"})
            if r.status_code==200:
                d=r.json()["current"]
                return f"{round(d['temperature_2m'])}°C, {WEATHER_CODES.get(d['weather_code'],'variable')}, viento {round(d['wind_speed_10m'])} km/h"
    except: pass
    return "no disponible"

# ── Visión ────────────────────────────────────────────────────────────────────
async def request_screenshot() -> str | None:
    """Pide screenshot al primer agente conectado y espera la respuesta."""
    if not agents: return None
    conn = next(iter(agents.values()))
    await conn.ws.send_text(json.dumps({"type":"request_screenshot"}))
    try:
        msg = await asyncio.wait_for(conn.queue.get(), timeout=12)
        if msg.get("type") == "screenshot":
            return msg.get("data")
    except asyncio.TimeoutError:
        print("Screenshot timeout")
    return None

async def vision_analyze(screenshot_b64: str, task: str, step: int = 0) -> dict:
    """Analiza screenshot con Groq Vision para determinar próxima acción."""
    if not groq_client or not screenshot_b64:
        return {"done": True, "reason": "no vision available"}
    try:
        r = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role":"user","content":[
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{screenshot_b64}"}},
                {"type":"text","text":f"""Analyzes this Mac screenshot. Task: {task}. Step: {step+1}.

Determine the SINGLE next action needed. Return ONLY valid JSON:
{{"action":"click|type|key|scroll|wait|done","x":0,"y":0,"text":"","key":"","scroll":"up|down","wait":1,"reason":"","done":false}}

Rules:
- For click: find the exact pixel position of the element. x,y are pixel coordinates.
- If task is complete or impossible: set done=true
- Be precise with coordinates based on what you see
- If element not visible, return next step to navigate to it"""}
            ]}],
            max_tokens=200, temperature=0.1
        )
        text = re.sub(r"```json|```","",r.choices[0].message.content).strip()
        # Encontrar el JSON en la respuesta
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"done":True,"reason":"no parseable response"}
    except Exception as e:
        print(f"Vision error: {e}")
        return {"done":True,"reason":str(e)}

async def computer_use_loop(task: str, max_steps: int = 8) -> str:
    """Bucle ver→pensar→actuar para tareas que requieren navegar por la UI."""
    if not agents:
        return "El Mac no está conectado. Ejecuta el agente primero."

    conn = next(iter(agents.values()))
    log = []

    for step in range(max_steps):
        # 1. Capturar pantalla
        sc = await request_screenshot()
        if not sc:
            return "No pude capturar la pantalla del Mac."

        # 2. Analizar qué hacer
        decision = await vision_analyze(sc, task, step)
        action_type = decision.get("action","done")
        reason = decision.get("reason","")

        if decision.get("done") or action_type == "done":
            log.append(f"Completado: {reason}")
            break

        # 3. Construir acción
        action = None
        if action_type == "click":
            x, y = int(decision.get("x",0)), int(decision.get("y",0))
            action = {"type":"click","value":f"{x},{y}"}
            log.append(f"Click en ({x},{y}): {reason}")
        elif action_type == "type":
            text = decision.get("text","")
            action = {"type":"typewrite","value":text}
            log.append(f"Escribir: {text[:40]}")
        elif action_type == "key":
            key = decision.get("key","")
            action = {"type":"key","value":key}
            log.append(f"Tecla: {key}")
        elif action_type == "scroll":
            action = {"type":"scroll","value":decision.get("scroll","down")}
            log.append(f"Scroll: {decision.get('scroll')}")
        elif action_type == "wait":
            action = {"type":"wait","value":str(decision.get("wait",1))}
            log.append(f"Esperar {decision.get('wait',1)}s")

        # 4. Ejecutar acción
        if action:
            await conn.ws.send_text(json.dumps({"type":"actions","actions":[action]}))
            try:
                result_msg = await asyncio.wait_for(conn.queue.get(), timeout=8)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.8)  # esperar que la UI reaccione
        else:
            break

    # Guardar secuencia aprendida en memoria
    if log:
        learned = memory.setdefault("learned_actions",{})
        task_key = task[:50].lower().strip()
        learned[task_key] = {"steps": log, "timestamp": datetime.now().isoformat()}
        memory["learned_actions"] = dict(list(learned.items())[-50:])  # max 50 aprendizajes
        save_memory(memory)

    return f"Completado en {len(log)} pasos: {'. '.join(log[-2:])}"

# ── Rutinas ───────────────────────────────────────────────────────────────────
def check_routine(text):
    for r in memory.get("routines",[]):
        if r.get("trigger","").lower() in text.lower(): return r
    return None

async def routine_ctx(routine):
    parts = [f"Rutina: {routine.get('description',routine.get('trigger'))}."]
    acts = routine.get("actions",[])
    if "weather" in acts and memory.get("location"):
        loc=memory["location"]; w=await get_weather(loc["lat"],loc["lon"])
        parts.append(f"Tiempo en {loc.get('city','tu ciudad')}: {w}.")
    if "tasks" in acts:
        tasks=memory.get("tasks",[])
        parts.append(f"Tareas: {', '.join(tasks[:5])}" if tasks else "Sin tareas pendientes.")
    if "time" in acts:
        parts.append(f"Son las {datetime.now().strftime('%H:%M')}.")
    return " ".join(parts)

# ── Detección de tarea compleja ───────────────────────────────────────────────
VISION_KEYWORDS = ["busca","encuentra","haz click","escribe en","navega a","abre y","ve a","pon en",
                   "selecciona","rellena","introduce","pulsa el botón","cierra","minimiza","maximiza"]
COMPLEX_KEYWORDS = ["crea","desarrolla","programa","escribe código","script","aplicación","app",
                    "analiza","análisis","diseña","arquitectura","función","clase","algoritmo","html",
                    "python","javascript","css","api","web","sistema","automatiza"]

def needs_vision(text): return any(k in text.lower() for k in VISION_KEYWORDS)
def is_complex(text):   return any(k in text.lower() for k in COMPLEX_KEYWORDS)

# ── Prompt del sistema ────────────────────────────────────────────────────────
def build_system(mac_online, use_claude=False):
    now = datetime.now().strftime("%A %d de %B %Y, %H:%M")
    if use_claude:
        base = f"Eres Yarvis, asistente con capacidades avanzadas. Fecha: {now}. Crea código completo y funcional. Responde en español."
    else:
        base = f"Eres Yarvis, asistente personal tipo Jarvis de Iron Man. Fecha: {now}. Directo, conciso, max 2 frases para voz. Sin markdown."

    parts = [base]
    facts = memory.get("user_facts",[])
    if facts: parts.append("Conoces al usuario:\n"+"\n".join(f"- {f}" for f in facts[-12:]))

    followups = [f["text"] for f in memory.get("pending_followups",[])
                 if datetime.now()>=datetime.fromisoformat(f.get("due_after","2099-01-01"))]
    if followups: parts.append(f"Pregunta si es natural: {'; '.join(followups[:2])}")

    if mac_online:
        parts.append("""Mac conectado. Para tareas visuales/UI usa [VISION_TASK:descripción de la tarea] y el sistema verá la pantalla automáticamente.
Para acciones directas: [URL:https://...] [CMD:comando] [APP:nombre] [SPOTLIGHT:app] [KEY:cmd+space] [TYPE:texto] [TYPEWRITE:texto con acentos] [CLICK:x,y] [SCROLL:up/down] [SCREENSHOT] [CLIPBOARD:texto] [APPLESCRIPT:script] [WAIT:segundos]
Encadena acciones: [APP:Safari][WAIT:1][KEY:cmd+l][TYPEWRITE:youtube.com][KEY:enter]
Para buscar en una web o interactuar con la UI: usa [VISION_TASK:...] para que vea la pantalla.""")
    else:
        parts.append("Mac NO conectado. No uses etiquetas de acción.")
    return "\n\n".join(parts)

# ── Extractor de acciones ─────────────────────────────────────────────────────
ALL_TYPES = ["URL","CMD","APP","SPOTLIGHT","KEY","TYPE","TYPEWRITE","CLICK","RCLICK",
             "DCLICK","MOUSE_MOVE","SCROLL","SCREENSHOT","CLIPBOARD","APPLESCRIPT",
             "FIND_AND_CLICK","WAIT","VISION_TASK"]

def extract_actions(text):
    acts = []
    if re.search(r'\[SCREENSHOT\]', text, re.I):
        acts.append({"type":"screenshot","value":""})
    for atype in ALL_TYPES:
        if atype == "SCREENSHOT": continue
        for val in re.findall(rf'\[{atype}:([^\]]+)\]', text, re.I):
            acts.append({"type":atype.lower(),"value":val.strip()})
    return acts

def clean_text(t):
    pattern = "|".join(ALL_TYPES)
    t = re.sub(rf'\[(?:{pattern}):[^\]]+\]','',t,flags=re.I)
    t = re.sub(r'\[SCREENSHOT\]','',t,flags=re.I)
    return t.strip()

# ── TTS ElevenLabs ────────────────────────────────────────────────────────────
async def generate_voice(text):
    if not ELEVEN_KEY: return None
    clean = re.sub(r"[*_`#\[\]]","",text)[:500]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers={"xi-api-key":ELEVEN_KEY,"Content-Type":"application/json","Accept":"audio/mpeg"},
                json={"text":clean,"model_id":"eleven_multilingual_v2",
                      "voice_settings":{"stability":0.55,"similarity_boost":0.80,"style":0.1,"use_speaker_boost":True}})
            if r.status_code==200: return base64.b64encode(r.content).decode("utf-8")
    except Exception as e: print(f"EL: {e}")
    return None

# ── API Chat ──────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str; session_id: str = "default"; secret: str = ""

@app.post("/api/chat")
async def chat(req: ChatRequest):
    if req.secret != YARVIS_SECRET: raise HTTPException(401,"Clave incorrecta")
    if not groq_client: raise HTTPException(500,"GROQ_API_KEY no configurada")

    mac_online = bool(agents)
    hist = session_histories.setdefault(req.session_id,[])

    # Rutina
    routine = check_routine(req.message)
    user_msg = req.message
    if routine:
        ctx = await routine_ctx(routine)
        user_msg = f"{req.message}\n[Contexto: {ctx}]"

    # ── Tarea visual en el Mac ──
    if mac_online and needs_vision(req.message):
        try:
            result = await computer_use_loop(req.message)
            audio = await generate_voice(result)
            return {"reply":result,"actions":[],"mac_online":True,"audio_b64":audio,"ai_used":"vision"}
        except Exception as e:
            print(f"Vision loop error: {e}")

    hist.append({"role":"user","content":user_msg})
    use_claude = claude_client and is_complex(req.message)
    ai_used = "claude" if use_claude else "groq"

    try:
        if use_claude:
            r = claude_client.messages.create(model="claude-sonnet-4-20250514",max_tokens=2000,
                system=build_system(mac_online,use_claude=True),messages=hist[-20:])
            reply = r.content[0].text
        else:
            models = [GROQ_MODEL,"llama-3.1-8b-instant","gemma2-9b-it"]
            last_err = None
            for model in models:
                try:
                    r = groq_client.chat.completions.create(model=model,
                        messages=[{"role":"system","content":build_system(mac_online)}]+hist[-20:],
                        max_tokens=250,temperature=0.7)
                    reply = r.choices[0].message.content; last_err=None; break
                except Exception as me:
                    print(f"Groq {model}: {me}"); last_err=me
            if last_err: raise last_err
    except Exception as e:
        print(f"AI error: {e}"); raise HTTPException(500,f"Error IA: {str(e)}")

    hist.append({"role":"assistant","content":reply})
    acts = extract_actions(reply)
    display = clean_text(reply)

    # Ejecutar acciones en Mac (las que no son VISION_TASK)
    non_vision_acts = [a for a in acts if a["type"]!="vision_task"]
    if non_vision_acts and agents:
        conn = next(iter(agents.values()))
        await conn.ws.send_text(json.dumps({"type":"actions","actions":non_vision_acts}))

    # Ejecutar vision tasks
    vision_results = []
    for a in acts:
        if a["type"]=="vision_task":
            vr = await computer_use_loop(a["value"])
            vision_results.append(vr)

    if vision_results:
        display = display + " " + " ".join(vision_results) if display else " ".join(vision_results)

    asyncio.create_task(update_memory(hist[-6:]))
    audio = await generate_voice(display)

    return {"reply":display,"actions":non_vision_acts if mac_online else [],"mac_online":mac_online,"audio_b64":audio,"ai_used":ai_used}

# ── Memoria en background ─────────────────────────────────────────────────────
async def update_memory(hist):
    global memory
    if not groq_client or len(hist)<2: return
    try:
        conv="\n".join([f"{m['role'].upper()}: {m['content'][:200]}" for m in hist])
        r=groq_client.chat.completions.create(model="llama-3.1-8b-instant",
            messages=[{"role":"system","content":'Extrae en JSON: {"facts":[],"followups":[{"text":"","hours_later":6}],"location":{"city":"","lat":0,"lon":0},"routine_request":{"trigger":"","description":"","actions":[]},"tasks":[]}. Solo datos reales. Solo JSON.'},
            {"role":"user","content":conv}],max_tokens=250,temperature=0.2)
        text=re.sub(r"```json|```","",r.choices[0].message.content).strip()
        m=re.search(r'\{.*\}',text,re.DOTALL)
        if not m: return
        ex=json.loads(m.group()); changed=False
        for f in ex.get("facts",[]):
            if f and f not in memory["user_facts"]: memory["user_facts"].append(f); changed=True
        memory["user_facts"]=memory["user_facts"][-50:]
        for fu in ex.get("followups",[]):
            try:
                due=(datetime.now()+timedelta(hours=float(fu.get("hours_later",6)))).isoformat()
                memory["pending_followups"].append({"text":fu["text"],"due_after":due}); changed=True
            except: pass
        cutoff=(datetime.now()-timedelta(days=7)).isoformat()
        memory["pending_followups"]=[f for f in memory["pending_followups"] if f.get("due_after","")>cutoff]
        loc=ex.get("location")
        if loc and loc.get("lat") and loc.get("lon"): memory["location"]=loc; changed=True
        rr=ex.get("routine_request")
        if rr and rr.get("trigger"):
            t=rr["trigger"].lower()
            if t not in [r.get("trigger","").lower() for r in memory["routines"]]:
                memory["routines"].append({"trigger":t,"description":rr.get("description",t),"actions":rr.get("actions",[])}); changed=True
        for task in ex.get("tasks",[]):
            if task and task not in memory["tasks"]: memory["tasks"].append(task); changed=True
        memory["conversation_count"]=memory.get("conversation_count",0)+1
        if changed: save_memory(memory)
    except Exception as e: print(f"Memory update: {e}")

# ── WebSocket agente (bidireccional) ──────────────────────────────────────────
@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    if ws.query_params.get("secret","")!=YARVIS_SECRET:
        await ws.close(1008); return
    await ws.accept()
    aid=f"mac-{id(ws)}"
    conn=AgentConn(ws); agents[aid]=conn
    print(f"[+] Mac conectado: {aid}")
    try:
        while True:
            raw=await ws.receive_text()
            try:
                data=json.loads(raw)
                # Mensajes del agente → meterlos en la cola
                if data.get("type") in ("screenshot","results"):
                    await conn.queue.put(data)
            except: pass
    except WebSocketDisconnect:
        agents.pop(aid,None); print(f"[-] Mac desconectado: {aid}")

@app.get("/api/status")
async def status():
    return {"mac_online":bool(agents),"memories":len(memory.get("user_facts",[])),"routines":len(memory.get("routines",[])),"tasks":len(memory.get("tasks",[])),"learned_actions":len(memory.get("learned_actions",{})),"claude":bool(claude_client)}

@app.get("/api/memory")
async def get_mem(secret:str=""):
    if secret!=YARVIS_SECRET: raise HTTPException(401)
    return memory

@app.delete("/api/memory")
async def clear_mem(secret:str=""):
    global memory
    if secret!=YARVIS_SECRET: raise HTTPException(401)
    memory={"user_facts":[],"pending_followups":[],"routines":[],"tasks":[],"location":None,"conversation_count":0,"learned_actions":{}}
    save_memory(memory); return {"ok":True}

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({"name":"Yarvis","short_name":"Yarvis","start_url":"/","display":"standalone","background_color":"#000","theme_color":"#000","icons":[]})

BASE=Path(__file__).parent
def find(n):
    for p in [BASE/"static"/n,BASE/n]:
        if p.exists(): return p
    return None

@app.get("/")
async def root():
    p=find("index.html")
    if p: return FileResponse(str(p),media_type="text/html")
    return JSONResponse({"status":"Yarvis online","vision":"groq-llama-vision","memories":len(memory.get("user_facts",[]))})

@app.get("/{path:path}")
async def spa(path:str):
    p=find("index.html")
    if p: return FileResponse(str(p),media_type="text/html")
    raise HTTPException(404)
