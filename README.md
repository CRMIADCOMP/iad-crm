# CRM IAD COMP — El Francés Inmobiliaria

Sistema automático de gestión de leads inmobiliarios (Idealista / Fotocasa / Habitaclia),
desplegado en **Railway**. Una app Flask funciona 24/7 y, a las **8h, 12h y 18h (hora de Madrid)**,
ejecuta un pipeline que:

1. Lee los correos nuevos de Gmail (bandeja de entrada, últimas 24h).
2. Detecta los leads de Idealista, Fotocasa y Habitaclia y extrae nombre, teléfono, email y referencia.
3. Hace el *matching* con la pestaña `🔗 Config` del Google Sheets (por URL o referencia del anuncio).
4. Deduplica (por teléfono Y email) e inserta/actualiza el prospecto en la hoja correcta.
5. Envía un mensaje de WhatsApp individual y personalizado vía UltraMsg.
6. Procesa las respuestas de WhatsApp recibidas (webhook → SQLite), las analiza con Claude Haiku
   y rellena las columnas F/G/H (presupuesto, tiempo de búsqueda, financiación).
7. Gestiona los seguimientos J+2, cierra los no-respondedores a los 7 días y envía un informe por email.

Incluye además un **dashboard web** protegido por contraseña para controlar todo manualmente.

---

## Descripción de cada archivo

| Archivo | Rol en el sistema |
|---|---|
| `app.py` | App Flask principal. Scheduler APScheduler (8h/12h/18h), webhook UltraMsg, dashboard `/dashboard` + login, y todos los endpoints de acción (`/run`, `/full_scan`, `/reset_timestamp`, `/diag`, `/status`, `/send_report`, `/test_email`, `/setup_dropdowns`, `/add_bien`, `/close_bien`, `/list_biens`, `/health`). |
| `pipeline.py` | Orquestación del pipeline (las 14 etapas) y construcción del informe por email (HTML + texto). |
| `gmail_reader.py` | Lectura de correos, extracción de leads (parsers Idealista y Fotocasa/Habitaclia), borrado automático de correos no deseados y envío de emails. |
| `sheets_handler.py` | Acceso a Google Sheets (cuenta de servicio): matching, deduplicación, inserción/actualización, gestión de inmuebles (añadir/cerrar) y configuración de las listas desplegables. |
| `whatsapp.py` | Plantillas de mensajes (español) y envío individual vía UltraMsg; limpieza del nombre y normalización del número. |
| `ai_analyzer.py` | Análisis de las respuestas de WhatsApp con Claude Haiku → presupuesto, tiempo de búsqueda, pago validado. |
| `webhook.py` | Webhook que recibe las respuestas de WhatsApp de UltraMsg y las guarda en SQLite. |
| `database.py` | Base SQLite: mensajes entrantes de WhatsApp y estado de los runs (timestamps). |
| `config.py` | Configuración central: variables de entorno, columnas, reglas de negocio y estados. |
| `report_assets.py` | Logo IAD (URL) y enlace del Google Sheets para el informe. |
| `setup_auth.py` | **Se ejecuta una sola vez en local** para generar `token.json` (OAuth Gmail). |
| `requirements.txt` | Dependencias Python. |
| `Procfile` | Comando de arranque en Railway: `gunicorn app:app --workers 1 --threads 4 --timeout 120`. |
| `runtime.txt` | Versión de Python (3.11). |
| `.gitignore` | Excluye los secretos (`credentials.json`, `token.json`, `service_account.json`, `*.db`, `.env`). |

---

## Instalación y despliegue (guía para agentes IAD España)

### 1. Autenticación de Gmail (en local, una sola vez)

1. Coloca tu `credentials.json` (cliente OAuth de tipo *Escritorio*) en la carpeta del proyecto.
2. Instala las dependencias y ejecuta el script:
   ```bash
   pip install -r requirements.txt
   python setup_auth.py
   ```
3. Conéctate con **thibaut.montalat@iadespana.es** en el navegador y acepta los permisos.
   El scope incluye `gmail.modify` (leer + mover a la papelera) y `gmail.send` (enviar el informe).
4. Se crea `token.json` y el script muestra los valores **base64** de `GMAIL_CREDENTIALS` y `GMAIL_TOKEN`
   para pegarlos en Railway.

> ⚠️ Si cambias los scopes, hay que **regenerar `token.json`** (borrar el antiguo y volver a ejecutar
> `setup_auth.py`), porque un token viejo no tendrá permiso de envío/papelera.

### 2. Cuenta de servicio de Google (acceso a Sheets)

1. En [console.cloud.google.com](https://console.cloud.google.com), activa las APIs **Google Sheets** y **Google Drive**.
2. Crea una **cuenta de servicio** y genera una clave **JSON**.
3. Comparte el Google Sheets (como **Editor**) con el email de la cuenta de servicio (`...@...iam.gserviceaccount.com`).
4. Codifica el JSON en base64 (`base64 -i service_account.json`) y pégalo en la variable `GOOGLE_SERVICE_ACCOUNT`.

### 3. Subir a GitHub y desplegar en Railway

```bash
git init
git add .
git commit -m "CRM IAD"
git branch -M main
git remote add origin https://github.com/CRMIADCOMP/iad-crm.git
git push -u origin main
```

En Railway: **New Project → Deploy from GitHub repo**, luego añade las variables de entorno y genera el dominio
(Settings → Networking → Generate Domain).

> **IMPORTANTE — una sola instancia + base persistente:** asegúrate de que el servicio se ejecuta con
> **1 réplica** (Settings → Replicas = 1) para evitar planificadores duplicados (mensajes de WhatsApp dobles)
> y estados incoherentes. Añade un **Volumen** montado en `/data` y la variable `DB_PATH=/data/crm.db` para que
> la base SQLite sobreviva a los redespliegues.

### Variables de entorno de Railway

| Variable | Valor |
|---|---|
| `GMAIL_CREDENTIALS` | base64 de `credentials.json` |
| `GMAIL_TOKEN` | base64 de `token.json` (con scope modify+send) |
| `GOOGLE_SERVICE_ACCOUNT` | base64 del JSON de la cuenta de servicio |
| `ANTHROPIC_API_KEY` | clave API de Anthropic (`sk-ant-...`) |
| `DASHBOARD_PASSWORD` | contraseña del dashboard |
| `SECRET_KEY` | cadena aleatoria (sesiones estables entre despliegues) |
| `DB_PATH` | `/data/crm.db` (con volumen) |
| `GOOGLE_SHEET_ID` | `1WYvelN50Hz_8gCo8o9BtsFpUUdLaxQbzlXAySSY4I9M` *(por defecto)* |
| `ULTRAMSG_INSTANCE` | `instance181932` *(por defecto)* |
| `ULTRAMSG_TOKEN` | token UltraMsg *(por defecto)* |
| `RUN_TOKEN` | *(opcional)* protege los endpoints por token |

### 4. Webhook de UltraMsg

En el panel de UltraMsg → **Settings → Webhook** → Webhook URL = `https://<tu-dominio>/webhook`,
y activa «On Message Received».

---

## Endpoints

- `GET /` — ping del servicio.
- `GET /health` — estado, próximo run y último run.
- `GET /dashboard` — panel de control (requiere login).
- `GET /login` — inicio de sesión.
- `POST /run` (`?dry_run=true`) — ejecuta el pipeline ahora.
- `GET /full_scan` (`?dry_run=true`) — escaneo único de 30 días, luego vuelve a 24h.
- `POST /reset_timestamp` — pone `last_run_ts` a 0 (reprocesa 3 días en el siguiente run).
- `GET /status` — resumen del último run.
- `GET /diag` — diagnóstico (variables, Gmail, Sheets).
- `POST /send_report` — envía por email el informe del último run.
- `POST /setup_dropdowns` — aplica la lista desplegable «Estado final» y el color en todas las hojas.
- `POST /add_bien`, `POST /close_bien`, `GET /list_biens` — gestión de inmuebles.

---

## Estructura del Google Sheets

**Hojas de prospectos** (los datos empiezan SIEMPRE en la fila 4; las filas 1-3 son títulos y no se tocan):

| Col | Campo | Relleno |
|---|---|---|
| A | Nombre | automático (del correo) |
| B | Teléfono | automático, normalizado |
| C | Email | automático |
| D | Fuente | portal de origen |
| E | Notas | **siempre vacío** (manual) |
| F | Presupuesto | IA, solo si la celda está vacía |
| G | Tiempo búsqueda | IA, solo si la celda está vacía |
| H | Pago validado | IA: `Sí - Validado` / `En curso` / `No - Rechazado` / `Pendiente` |
| I | Fecha contacto | fecha del primer contacto (no se modifica luego) |
| J | Último mensaje | fecha de cada envío de WhatsApp |
| K | Relance J+2 | `Pendiente` → `Enviada` → `No necesaria` |
| L | Estado final | ver lógica de estados |

**Pestaña `🔗 Config`** (mapea cada anuncio a una hoja): A=Hoja, B=Descripción, C/D=URL/Ref Idealista,
E/F=URL/Ref Fotocasa, G/H=URL/Ref Habitaclia, I/J=URL/Ref IAD.

### Lógica de estados (columna L)

Transiciones automáticas: `""` → `Nuevo contacto` → `WhatsApp enviado` → `No responde` → `Sin respuesta - 7d`.
- `Error envío WA`: cuando falla el envío; se reintenta en el siguiente run (color rojo claro #FF6B6B).
- Estados manuales (NUNCA se sobrescriben): `Visita apuntada`, `Visita hecha`, `Fuera`.

La selección de URL del anuncio sigue la prioridad: **C (Idealista) → E (Fotocasa) → G (Habitaclia) → I (IAD)**;
si no hay ninguna URL válida, no se envía el WhatsApp y se añade una alerta `[ALERTA]` al informe.

---

## Gestión de inmuebles (desde el dashboard)

- **➕ Añadir un inmueble**: crea la fila en `Config` y duplica la hoja plantilla `T Ole 155k`,
  la renombra, vacía los datos (filas 4+) y pone la descripción en la fila 2. Las referencias se
  extraen automáticamente de las URLs.
- **🏁 Marcar como vendido**: renombra la hoja y la fila de Config con el prefijo `VEND ` y vacía las URLs.

---

## Notas

- Un mensaje por prospecto, a su número personal. Nunca mensajes grupales.
- El script **nunca crea ni borra hojas** automáticamente (salvo duplicar la plantilla al añadir un inmueble).
  Si una hoja del Config no existe, se genera una alerta en el informe.
- La base SQLite (`crm.db`) es efímera salvo que se use un Volumen de Railway con `DB_PATH`.
