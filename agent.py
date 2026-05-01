"""
YARVIS — Agente Local para Mac
================================
Conecta tu Mac al servidor en Railway para ejecutar comandos locales.
Instalar: pip install websockets
Ejecutar:  python agent.py
"""
import asyncio, json, os, subprocess, sys, webbrowser, time
from pathlib import Path

# ── Configuración ─────────────────────────────────────────────────────────────
CONFIG_FILE = Path.home() / ".yarvis_agent.json"

def load_cfg():
    try: return json.loads(CONFIG_FILE.read_text())
    except: return {}

def save_cfg(c):
    CONFIG_FILE.write_text(json.dumps(c, indent=2))

cfg = load_cfg()
SERVER_URL = cfg.get("server_url", os.environ.get("YARVIS_SERVER", ""))
SECRET     = cfg.get("secret",     os.environ.get("YARVIS_SECRET", ""))

if not SERVER_URL or not SECRET:
    print("="*55)
    print("  YARVIS — Agente Local")
    print("="*55)
    SERVER_URL = input("URL del servidor Railway (ej: https://xxx.up.railway.app): ").strip()
    SECRET     = input("Clave secreta: ").strip()
    cfg.update({"server_url": SERVER_URL, "secret": SECRET})
    save_cfg(cfg)

# Convertir https → wss
WS_URL = SERVER_URL.replace("https://", "wss://").replace("http://", "ws://")
WS_URL = WS_URL.rstrip("/") + f"/ws/agent?secret={SECRET}"

# ── Ejecutor de acciones ──────────────────────────────────────────────────────
def execute(action: dict) -> str:
    t = action.get("type")
    v = action.get("value", "")
    try:
        if t == "url":
            webbrowser.open(v)
            return f"✓ Abriendo URL: {v}"
        elif t == "cmd":
            result = subprocess.run(
                v, shell=True, capture_output=True, text=True, timeout=15
            )
            out = (result.stdout + result.stderr).strip()
            return f"✓ Comando: {v}\n{out[:200] if out else '(sin salida)'}"
        elif t == "app":
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", v])
            else:
                subprocess.Popen([v])
            return f"✓ Abriendo app: {v}"
        else:
            return f"Acción desconocida: {t}"
    except Exception as e:
        return f"✗ Error ejecutando {t}:{v} — {e}"

# ── Bucle WebSocket ───────────────────────────────────────────────────────────
async def run():
    try:
        import websockets
    except ImportError:
        print("Falta websockets. Instala: pip install websockets")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  YARVIS Agente Local")
    print(f"  Servidor: {SERVER_URL}")
    print(f"  Ctrl+C para detener")
    print(f"{'='*55}\n")

    retry_delay = 3
    while True:
        try:
            print(f"[→] Conectando a {WS_URL[:50]}…")
            async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                print("[✓] Conectado. Esperando comandos…\n")
                retry_delay = 3
                async for message in ws:
                    try:
                        data = json.loads(message)
                        actions = data.get("actions", [])
                        for action in actions:
                            print(f"[▸] Ejecutando: {action}")
                            result = execute(action)
                            print(f"    {result}")
                            await ws.send(json.dumps({"result": result}))
                    except json.JSONDecodeError:
                        print(f"[!] Mensaje no JSON: {message}")
        except KeyboardInterrupt:
            print("\n[✓] Agente detenido.")
            break
        except Exception as e:
            print(f"[!] Desconectado: {e}")
            print(f"    Reconectando en {retry_delay}s…")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

if __name__ == "__main__":
    asyncio.run(run())
