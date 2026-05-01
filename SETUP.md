# YARVIS Cloud — Guía de instalación

## Qué hay en esta carpeta

```
server.py          ← Servidor cloud (se despliega en Railway)
requirements.txt   ← Dependencias del servidor
Procfile           ← Instrucciones de arranque para Railway
static/
  index.html       ← App web (iPhone + Mac)
  manifest.json    ← Configuración PWA
agent.py           ← Agente local para el Mac
```

---

## PASO 1 — Subir el servidor a Railway (gratis)

1. Ve a https://github.com y crea una cuenta si no tienes.
2. Crea un repositorio nuevo llamado `yarvis-cloud`.
3. Sube todos los archivos de esta carpeta a ese repositorio.
4. Ve a https://railway.app y regístrate con tu cuenta de GitHub.
5. Haz clic en **"New Project" → "Deploy from GitHub repo"** → selecciona `yarvis-cloud`.
6. Railway detectará el `Procfile` automáticamente.

### Variables de entorno en Railway

En tu proyecto Railway, ve a **Variables** y añade:

| Variable | Valor |
|----------|-------|
| `ANTHROPIC_API_KEY` | Tu clave de Anthropic (https://console.anthropic.com) |
| `YARVIS_SECRET` | Una contraseña que tú elijas (ej: `m1-yarvis-2024`) |

7. Haz clic en **Deploy**. En 2 minutos tendrás tu URL tipo:
   `https://yarvis-cloud-xxxx.up.railway.app`

---

## PASO 2 — Instalar la app en el iPhone

1. En Safari del iPhone, abre la URL de Railway.
2. Toca el botón **Compartir** (cuadrado con flecha) → **"Añadir a pantalla de inicio"**.
3. Ponle nombre "Yarvis" y toca **Añadir**.
4. Abre la app → toca el icono de ajustes (⚙) → introduce:
   - **URL del servidor**: `https://tu-app.up.railway.app`
   - **Clave secreta**: la misma que pusiste en Railway
5. Guarda. ¡Ya puedes hablar con Yarvis desde el iPhone!

---

## PASO 3 — Conectar el Mac (opcional, para control local)

Solo necesario si quieres que Yarvis abra apps, ejecute comandos, etc. en tu Mac.

### Instalar dependencia
```bash
pip install websockets
```

### Ejecutar el agente
```bash
python agent.py
```

La primera vez te pedirá la URL del servidor y la clave secreta. Las guarda automáticamente para las siguientes veces.

### Arranque automático (para que siempre esté activo)

En Mac, puedes configurarlo para que arranque con el sistema:
1. Abre **Automator** → crea una **App**.
2. Añade "Ejecutar script de shell": `python3 /ruta/a/agent.py`
3. Guarda como app y añádela en **Preferencias del Sistema → Elementos de inicio de sesión**.

---

## Uso

| Dispositivo | Cómo usarlo |
|-------------|-------------|
| iPhone | Abre la app Yarvis → toca el micrófono y habla |
| Mac | Abre la URL en Safari/Chrome → igual que el iPhone |
| Mac (control local) | El agente corre en segundo plano, Yarvis controla el Mac automáticamente |

---

## Solución de problemas

**"Clave incorrecta"**: Verifica que `YARVIS_SECRET` en Railway sea igual a la que escribiste en la app.

**El micrófono no funciona en iPhone**: Safari requiere que la URL sea HTTPS. Railway la proporciona automáticamente.

**El Mac no aparece como conectado**: Asegúrate de que `agent.py` esté corriendo en el Mac.

**Railway se queda dormido**: El plan gratuito de Railway tiene $5 de crédito/mes, suficiente para un servidor pequeño siempre activo. Si se consume, considera Render.com (también gratis, aunque se duerme tras 15min de inactividad).
