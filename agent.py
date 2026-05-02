"""
YARVIS — Agente Mac con Visión Bidireccional
pip3 install websockets pyautogui pillow pyobjc-framework-Quartz
Permisos: Accesibilidad + Grabación de pantalla → Terminal
"""
import asyncio, json, os, sys, subprocess, webbrowser, time, base64
from pathlib import Path
from io import BytesIO

for pkg in ['websockets','pyautogui','PIL']:
    try: __import__(pkg)
    except ImportError:
        print(f"Falta: pip3 install pyautogui pillow websockets"); sys.exit(1)

import pyautogui
import websockets
from PIL import Image

pyautogui.PAUSE = 0.1
pyautogui.FAILSAFE = False

CONFIG_FILE = Path.home() / ".yarvis_agent.json"
def load_cfg():
    try: return json.loads(CONFIG_FILE.read_text())
    except: return {}
def save_cfg(c): CONFIG_FILE.write_text(json.dumps(c, indent=2))

cfg = load_cfg()
SERVER_URL = cfg.get("server_url", os.environ.get("YARVIS_SERVER",""))
SECRET     = cfg.get("secret",     os.environ.get("YARVIS_SECRET",""))

if not SERVER_URL or not SECRET:
    print("="*55); print("  YARVIS — Agente Mac"); print("="*55)
    SERVER_URL = input("URL del servidor Railway (https://...): ").strip()
    SECRET     = input("Clave secreta: ").strip()
    cfg.update({"server_url":SERVER_URL,"secret":SECRET}); save_cfg(cfg)

WS_URL = SERVER_URL.replace("https://","wss://").replace("http://","ws://").rstrip("/")+f"/ws/agent?secret={SECRET}"

# ── Screenshot ────────────────────────────────────────────────────────────────
def take_screenshot(quality=60, max_w=1280) -> dict:
    """Captura pantalla y devuelve base64 + dimensiones reales."""
    try:
        screen_w, screen_h = pyautogui.size()
        img = pyautogui.screenshot()
        img_w, img_h = img.size
        img.thumbnail((max_w, max_w), Image.LANCZOS)
        thumb_w, thumb_h = img.size
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return {
            "data":     b64,
            "screen_w": screen_w,
            "screen_h": screen_h,
            "image_w":  thumb_w,
            "image_h":  thumb_h
        }
    except Exception as e:
        print(f"Screenshot error: {e}")
        return {"data": "", "screen_w": 1280, "screen_h": 800, "image_w": 1280, "image_h": 800}

# ── Ejecutor ──────────────────────────────────────────────────────────────────
def execute(action: dict):
    t = action.get("type","").lower()
    v = action.get("value","")
    try:
        if t == "url":
            webbrowser.open(v); return f"✓ URL: {v}", None
        elif t == "cmd":
            r = subprocess.run(v,shell=True,capture_output=True,text=True,timeout=20)
            out = (r.stdout+r.stderr).strip()
            return f"✓ CMD: {out[:200] if out else 'ok'}", None
        elif t == "app":
            subprocess.Popen(["open","-a",v]); time.sleep(0.8)
            return f"✓ App: {v}", None
        elif t == "applescript":
            r = subprocess.run(["osascript","-e",v],capture_output=True,text=True,timeout=10)
            return f"✓ AS: {(r.stdout+r.stderr).strip()[:100]}", None
        elif t == "spotlight":
            pyautogui.hotkey("command","space"); time.sleep(0.5)
            pyautogui.write(str(v),interval=0.05); time.sleep(0.3)
            pyautogui.press("return"); time.sleep(0.8)
            return f"✓ Spotlight: {v}", None
        elif t == "mouse_move":
            p = [int(x.strip()) for x in str(v).split(",")]
            pyautogui.moveTo(p[0],p[1],duration=0.25); return f"✓ Mouse: {p}", None
        elif t == "click":
            if v and "," in str(v):
                p = [int(x.strip()) for x in str(v).split(",")]
                pyautogui.click(p[0],p[1]); return f"✓ Click: {p}", None
            pyautogui.click(); return "✓ Click", None
        elif t == "rclick":
            if v and "," in str(v):
                p = [int(x.strip()) for x in str(v).split(",")]
                pyautogui.rightClick(p[0],p[1])
            else: pyautogui.rightClick()
            return "✓ RClick", None
        elif t == "dclick":
            if v and "," in str(v):
                p = [int(x.strip()) for x in str(v).split(",")]
                pyautogui.doubleClick(p[0],p[1])
            else: pyautogui.doubleClick()
            return "✓ DClick", None
        elif t == "type":
            pyautogui.write(str(v),interval=0.04); return f"✓ Type: {v[:40]}", None
        elif t == "typewrite":
            proc = subprocess.Popen(['pbcopy'],stdin=subprocess.PIPE)
            proc.communicate(str(v).encode('utf-8'))
            time.sleep(0.1); pyautogui.hotkey('command','v')
            return f"✓ Paste: {v[:40]}", None
        elif t == "key":
            km = {"cmd":"command","ctrl":"ctrl","alt":"option","shift":"shift",
                  "enter":"return","esc":"escape","space":"space","tab":"tab",
                  "up":"up","down":"down","left":"left","right":"right","delete":"delete","backspace":"backspace"}
            keys = [km.get(k.strip(),k.strip()) for k in str(v).replace("+"," ").split()]
            pyautogui.hotkey(*keys) if len(keys)>1 else pyautogui.press(keys[0])
            return f"✓ Key: {v}", None
        elif t == "scroll":
            p = str(v).split(",")
            if len(p)==3: pyautogui.scroll(int(p[2]),x=int(p[0]),y=int(p[1]))
            elif v=="up": pyautogui.scroll(5)
            elif v=="down": pyautogui.scroll(-5)
            return f"✓ Scroll: {v}", None
        elif t == "screenshot":
            sc_data = take_screenshot()
            return "✓ Screenshot capturado", sc_data.get("data","")
        elif t == "clipboard":
            proc = subprocess.Popen(['pbcopy'],stdin=subprocess.PIPE)
            proc.communicate(str(v).encode('utf-8'))
            return f"✓ Clipboard: {v[:40]}", None
        elif t == "wait":
            secs = float(v) if v else 1.0
            time.sleep(min(secs,5)); return f"✓ Wait: {secs}s", None
        elif t == "find_and_click":
            script = f'''tell application "System Events"
set frontApp to name of first process whose frontmost is true
tell process frontApp
click (first UI element whose description contains "{v}" or name contains "{v}")
end tell
end tell'''
            r = subprocess.run(["osascript","-e",script],capture_output=True,text=True,timeout=8)
            return f"✓ Find+click '{v}': {(r.stdout+r.stderr).strip()[:80]}", None
        else:
            return f"? Desconocido: {t}", None
    except Exception as e:
        return f"✗ {t}: {e}", None

# ── Bucle WebSocket bidireccional ─────────────────────────────────────────────
async def run():
    print(f"\n{'='*55}\n  YARVIS Agente Mac — Visión + Control Total")
    print(f"  {SERVER_URL}\n  Ctrl+C para detener\n{'='*55}\n")
    retry = 3
    while True:
        try:
            print(f"[→] Conectando…")
            async with websockets.connect(WS_URL,ping_interval=30,ping_timeout=10) as ws:
                print("[✓] Conectado. Esperando comandos…\n")
                retry = 3
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        msg_type = data.get("type","actions")

                        # Petición de screenshot desde el servidor
                        if msg_type == "request_screenshot":
                            print("[📷] Screenshot solicitado")
                            sc_data = take_screenshot()
                            # Enviar screenshot con dimensiones reales
                            await ws.send(json.dumps({
                                "type":     "screenshot",
                                "data":     sc_data["data"],
                                "screen_w": sc_data["screen_w"],
                                "screen_h": sc_data["screen_h"],
                                "image_w":  sc_data["image_w"],
                                "image_h":  sc_data["image_h"]
                            }))
                            print(f"[✓] Screenshot {sc_data['screen_w']}x{sc_data['screen_h']} enviado")

                        # Acciones normales
                        elif msg_type == "actions" or "actions" in data:
                            actions = data.get("actions",[])
                            results = []
                            for action in actions:
                                print(f"[▸] {action}")
                                result, screenshot = execute(action)
                                print(f"    {result}")
                                results.append(result)
                                if screenshot:
                                    await ws.send(json.dumps({"type":"screenshot","data":screenshot}))
                                time.sleep(0.15)
                            await ws.send(json.dumps({"type":"results","data":results}))

                    except json.JSONDecodeError: pass
        except KeyboardInterrupt:
            print("\n[✓] Agente detenido."); break
        except Exception as e:
            print(f"[!] {e}\n    Reconectando en {retry}s…")
            await asyncio.sleep(retry); retry = min(retry*2,60)

if __name__ == "__main__":
    asyncio.run(run())
