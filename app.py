"""
Aplicación Flask principal del CRM IAD — se ejecuta 24/7 en Railway.

Responsabilidades de este archivo:
- App Flask principal: crea la instancia `app`, registra el blueprint del webhook
  e inicializa la base de datos SQLite al arrancar.
- Planificador APScheduler: ejecuta el pipeline automáticamente a las 8h, 12h y 18h
  (hora de Madrid), con una sola instancia garantizada por la guarda `_SCHEDULER_STARTED`.
- Webhook UltraMsg (`/webhook`, registrado vía blueprint en `webhook.py`): recibe las
  respuestas entrantes de WhatsApp.
- Dashboard protegido por contraseña: páginas `/login` y `/dashboard` que muestran el
  estado del sistema, los resultados del último run y los botones de acción.
- Endpoints de acción expuestos por la app:
    /run            -> ejecución manual del pipeline (newer_than:1d)
    /full_scan      -> escaneo único de los últimos 30 días (newer_than:30d)
    /reset_timestamp-> reinicia last_run_ts a 0
    /diag           -> diagnóstico completo (env, Gmail, Sheets)
    /status         -> resumen del último run
    /send_report    -> envía por email el informe del último run
    /test_email     -> envío mínimo de email para aislar problemas
    /setup_dropdowns-> aplica la validación/lista desplegable "Estado final" en las hojas
    /add_bien       -> añade un inmueble
    /close_bien     -> marca un inmueble como vendido/cerrado
    /list_biens     -> lista los inmuebles activos
    /login          -> inicio de sesión del dashboard
    /dashboard      -> panel de control (requiere sesión)
    /health         -> estado del servicio + próximo/último run
"""
import os
import json
import socket
import datetime
import threading

from flask import Flask, jsonify, request, session, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import config
import database as db
import pipeline
import report_assets
from webhook import webhook_bp

# Instancia principal de Flask.
app = Flask(__name__)
# Clave secreta para firmar las cookies de sesión. Se toma de SECRET_KEY si existe;
# en su defecto se deriva de DASHBOARD_PASSWORD para tener un valor estable por despliegue.
app.secret_key = os.environ.get(
    "SECRET_KEY", "iad-crm-" + os.environ.get("DASHBOARD_PASSWORD", "default")
)
# Registra el blueprint del webhook UltraMsg (define la ruta /webhook en webhook.py).
app.register_blueprint(webhook_bp)

# Crea las tablas de la base de datos SQLite si aún no existen.
db.init_db()

# Log de instancia: permite detectar en los logs de Railway si se están ejecutando
# VARIAS instancias a la vez (cada instancia imprime un pid/hostname distinto al arrancar).
print(f"[boot] instance démarrée — pid={os.getpid()} host={socket.gethostname()} "
      f"db={config.DB_PATH}")

# Contraseña del dashboard. Se lee de la variable de entorno DASHBOARD_PASSWORD de Railway;
# por defecto "iad-crm" si no está definida.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "iad-crm")


def _is_authed():
    """Indica si la petición actual tiene sesión de dashboard iniciada.

    Parámetros: ninguno (usa la sesión Flask del contexto actual).
    Devuelve: True si la clave "auth" de la sesión es exactamente True; False en caso contrario.
    """
    return session.get("auth") is True


def _authorized(req):
    """Comprueba si una petición está autorizada para ejecutar acciones protegidas.

    Autoriza en cualquiera de estos casos:
      - hay sesión de dashboard activa (_is_authed),
      - no hay token RUN_TOKEN configurado en el entorno (acceso abierto),
      - el parámetro `token` de la query coincide con RUN_TOKEN.

    Parámetros:
      req -- objeto request de Flask (se lee req.args.get("token")).
    Devuelve: True si está autorizada; False si no.
    """
    if _is_authed():
        return True
    token = os.environ.get("RUN_TOKEN")
    if not token:
        return True
    return req.args.get("token") == token


def _next_run():
    """Calcula la fecha/hora del próximo run automático del pipeline.

    Parámetros: ninguno (usa config.TIMEZONE y config.RUN_HOURS).
    Devuelve: un datetime con zona horaria (Madrid) correspondiente a la próxima
              hora programada (8h/12h/18h). Si ya pasaron todas las horas de hoy,
              devuelve la primera hora del día siguiente.
    """
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.datetime.now(tz)
    hours = sorted(config.RUN_HOURS)
    # Busca la primera hora programada de hoy que aún sea posterior al momento actual.
    for h in hours:
        c = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if c > now:
            return c
    # Si ninguna hora de hoy es futura, el próximo run es la primera hora de mañana.
    nxt = now + datetime.timedelta(days=1)
    return nxt.replace(hour=hours[0], minute=0, second=0, microsecond=0)


def _run_pipeline_async(dry_run=False, full_scan=False, replies_only=False):
    """Ejecuta el pipeline en un hilo aparte para no bloquear al scheduler ni a Flask.

    Parámetros:
      dry_run      -- si True, simula sin enviar WhatsApp (modo prueba).
      full_scan    -- si True, escanea los últimos 30 días en lugar del día actual.
      replies_only -- si True, solo procesa respuestas (runs de 10h/15h).
    Devuelve: None (el trabajo continúa en un hilo daemon en segundo plano).
    """
    threading.Thread(
        target=pipeline.run,
        kwargs={"dry_run": dry_run, "full_scan": full_scan, "replies_only": replies_only},
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Planificador (8h, 12h, 18h hora de Madrid)
# ---------------------------------------------------------------------------
def start_scheduler():
    """Crea y arranca el planificador APScheduler que ejecuta el pipeline.

    Programa un trigger cron en las horas definidas en config.RUN_HOURS (por defecto
    8, 12 y 18) en la zona horaria config.TIMEZONE (Madrid). misfire_grace_time=3600
    permite recuperar un run que no se haya disparado a tiempo (hasta 1 hora de margen).

    Parámetros: ninguno.
    Devuelve: la instancia BackgroundScheduler ya iniciada.
    """
    tz = pytz.timezone(config.TIMEZONE)
    scheduler = BackgroundScheduler(timezone=tz)
    # Dos trabajos cron distintos:
    #  - runs COMPLETOS a las horas de FULL_RUN_HOURS (8/12/18)
    #  - runs SOLO RESPUESTAS a las horas de REPLIES_ONLY_HOURS (10/15)
    full_hours = ",".join(str(h) for h in sorted(config.FULL_RUN_HOURS))
    replies_hours = ",".join(str(h) for h in sorted(config.REPLIES_ONLY_HOURS))
    scheduler.add_job(
        lambda: _run_pipeline_async(replies_only=False),
        CronTrigger(hour=full_hours, minute=0, timezone=tz),
        id="crm_pipeline_full", replace_existing=True, misfire_grace_time=3600,
    )
    if replies_hours:
        scheduler.add_job(
            lambda: _run_pipeline_async(replies_only=True),
            CronTrigger(hour=replies_hours, minute=0, timezone=tz),
            id="crm_pipeline_replies", replace_existing=True, misfire_grace_time=3600,
        )
    scheduler.start()
    print(f"[scheduler] démarré — full à {full_hours}h, réponses à {replies_hours}h ({config.TIMEZONE})")
    return scheduler


# Arranca el planificador una sola vez. Se usa la guarda _SCHEDULER_STARTED porque
# gunicorn puede importar este módulo varias veces; sin ella se crearían planificadores
# duplicados y el pipeline se ejecutaría más de una vez por hora. RUN_SCHEDULER=0 lo desactiva.
if os.environ.get("RUN_SCHEDULER", "1") == "1" and not os.environ.get("_SCHEDULER_STARTED"):
    os.environ["_SCHEDULER_STARTED"] = "1"
    start_scheduler()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Endpoint raíz: ping simple del servicio.

    Parámetros: ninguno.
    Devuelve: JSON con el nombre del servicio, su estado y las horas de ejecución.
    """
    return jsonify({"service": "iad-crm", "status": "running", "runs": config.RUN_HOURS})


@app.route("/health")
def health():
    """Estado de salud del servicio.

    Parámetros: ninguno.
    Devuelve: JSON con el estado, el timestamp y la fecha del último run, la fecha del
              próximo run automático, la zona horaria y las horas de ejecución.
    """
    nxt = _next_run()
    last_ts = db.get_last_run_ts()
    last_str = (datetime.datetime.fromtimestamp(last_ts).strftime("%d/%m/%Y %H:%M")
                if last_ts else "—")
    return jsonify({
        "status": "ok",
        "last_run_ts": last_ts,
        "last_run_str": last_str,
        "next_run": nxt.strftime("%d/%m/%Y %H:%M"),
        "timezone": config.TIMEZONE,
        "run_hours": config.RUN_HOURS,
    })


@app.route("/run", methods=["POST", "GET"])
def manual_run():
    """Lanza el pipeline manualmente (ventana normal newer_than:1d).

    Protegido por _authorized (sesión de dashboard o token RUN_TOKEN).
    Parámetros (query): dry_run=1/true/yes para simular sin enviar WhatsApp.
    Devuelve: 202 con el estado de arranque, o 401 si no está autorizado.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    # Interpreta el parámetro dry_run de la query como booleano.
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    _run_pipeline_async(dry_run=dry_run)
    return jsonify({"status": "pipeline_started", "dry_run": dry_run,
                    "note": "Voir /status dans ~30s pour le résumé"}), 202


@app.route("/full_scan", methods=["GET", "POST"])
def full_scan():
    """Escaneo único de los últimos 30 días (newer_than:30d): procesa todos los leads.

    Tras este run, el funcionamiento normal se reanuda (newer_than:1d en los runs de
    8h/12h/18h). Protegido por _authorized.
    Parámetros (query): dry_run=1/true/yes para no enviar WhatsApp.
    Devuelve: 202 con el estado de arranque, o 401 si no está autorizado.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    # Interpreta el parámetro dry_run de la query como booleano.
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    _run_pipeline_async(dry_run=dry_run, full_scan=True)
    return jsonify({"status": "full_scan_started", "dry_run": dry_run,
                    "window": "30d",
                    "note": "Run unique. Ensuite retour automatique à newer_than:1d. Voir /status."}), 202


@app.route("/reset_timestamp", methods=["POST"])
def reset_timestamp():
    """Reinicia last_run_ts a 0: el próximo /run reprocesará los correos de las últimas 24h.

    Protegido por _authorized.
    Parámetros: ninguno.
    Devuelve: 200 con el nuevo valor de last_run_ts, o 401 si no está autorizado.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    db.set_last_run_ts(0)
    return jsonify({"status": "timestamp_reset", "last_run_ts": db.get_last_run_ts(),
                    "note": "Lance /run pour retraiter les mails des dernières 24h"}), 200


@app.route("/send_report", methods=["GET", "POST"])
def send_report_route():
    """Envía de inmediato por email el informe del último run.

    Protegido por _authorized. Lee las estadísticas guardadas (last_run_stats) en la
    base de datos y delega el envío a pipeline.send_report.
    Parámetros: ninguno.
    Devuelve: 200/500 con el resultado del envío, 400 si no hay ningún run disponible,
              o 401 si no está autorizado.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    raw = db.get_state("last_run_stats")
    if not raw:
        return jsonify({"status": "error",
                        "message": "Aucun run disponible — lance d'abord le pipeline"}), 400
    try:
        stats = json.loads(raw)
        ok, message = pipeline.send_report(stats)
        return jsonify({"status": "ok" if ok else "error", "message": message}), (200 if ok else 500)
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/list_biens")
def list_biens():
    """Lista los inmuebles activos leídos de Google Sheets.

    Protegido por _authorized. Refresca la caché de sheets_handler antes de leer.
    Parámetros: ninguno.
    Devuelve: JSON {"biens": [...]} con los nombres de inmuebles activos, o 401/500 en error.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    import sheets_handler
    try:
        sheets_handler.reset_cache()
        return jsonify({"biens": sheets_handler.list_active_biens()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"biens": [], "error": str(e)}), 500


@app.route("/check_bien")
def check_bien():
    """Indica si un nombre de bien ya existe (Config u hoja). Param: ?name=..."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    import sheets_handler
    name = request.args.get("name", "")
    try:
        sheets_handler.reset_cache()
        return jsonify({"exists": sheets_handler.bien_exists(name), "name": name})
    except Exception as e:  # noqa: BLE001
        return jsonify({"exists": False, "error": str(e)}), 500


@app.route("/list_cities")
def list_cities():
    """Devuelve el mapeo de ciudades (config + personalizadas)."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"cities": db.get_city_names()})


@app.route("/update_config", methods=["GET", "POST"])
def update_config():
    """GET: devuelve la config editable (bróker). POST: guarda bróker y/o ciudad nueva."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict() or {}
        msgs = []
        if data.get("broker_name") or data.get("broker_phone"):
            db.set_broker(data.get("broker_name"), data.get("broker_phone"))
            msgs.append("bróker actualizado")
        if data.get("new_city_abbrev") and data.get("new_city_full"):
            db.add_custom_city(data["new_city_abbrev"], data["new_city_full"])
            msgs.append(f"ciudad añadida: {data['new_city_full']}")
        bn, bp = db.get_broker()
        return jsonify({"status": "ok", "message": "; ".join(msgs) or "sin cambios",
                        "broker_name": bn, "broker_phone": bp})
    bn, bp = db.get_broker()
    return jsonify({"broker_name": bn, "broker_phone": bp})


@app.route("/add_bien", methods=["POST"])
def add_bien():
    """Añade un nuevo inmueble (crea su hoja/entrada en Google Sheets).

    Protegido por _authorized. Acepta los datos como JSON o como formulario.
    Parámetros (body): nom, description y URLs opcionales (idealista/fotocasa/habitaclia/iad).
    Devuelve: 200 con un mensaje de éxito, 400 en error de datos, o 401 si no autorizado.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    import sheets_handler
    # Acepta tanto cuerpo JSON como datos de formulario.
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        sheets_handler.reset_cache()
        msg = sheets_handler.add_bien(data or {})
        return jsonify({"status": "ok", "message": msg}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/close_bien", methods=["POST"])
def close_bien():
    """Marca un inmueble como vendido/cerrado en Google Sheets.

    Protegido por _authorized. Acepta los datos como JSON o como formulario.
    Parámetros (body): nom -- nombre del inmueble a cerrar.
    Devuelve: 200 con un mensaje de éxito, 400 en error, o 401 si no autorizado.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    import sheets_handler
    # Acepta tanto cuerpo JSON como datos de formulario.
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        sheets_handler.reset_cache()
        msg = sheets_handler.close_bien((data or {}).get("nom"))
        return jsonify({"status": "ok", "message": msg}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/setup_dropdowns", methods=["GET", "POST"])
def setup_dropdowns():
    """Aplica la lista desplegable "Estado final" + color en todas las hojas (1 sola vez).

    Protegido por _authorized.
    Parámetros: ninguno.
    Devuelve: 200 con las hojas configuradas, su número y los valores aplicados; 401/500 en error.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    import sheets_handler
    try:
        sheets_handler.reset_cache()
        done = sheets_handler.setup_estado_validation()
        return jsonify({"status": "ok", "feuilles": done,
                        "count": len(done), "valeurs": config.ESTADO_FINAL_OPTIONS}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test_email", methods=["GET", "POST"])
def test_email():
    """Envío mínimo de email para aislar un problema de envío.

    Protegido por _authorized. Envía un correo de prueba a config.REPORT_EMAIL.
    Parámetros: ninguno.
    Devuelve: 200 con el id del mensaje si se envió, 500 en error, o 401 si no autorizado.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    import gmail_reader
    try:
        resp = gmail_reader.send_email(config.REPORT_EMAIL, "Test CRM IAD", "Test envoi rapport")
        msg_id = resp.get("id") if isinstance(resp, dict) else None
        return jsonify({"status": "ok", "message": "Email de test envoyé", "id": msg_id}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/status")
def status():
    """Resumen del último run (sin depender del email).

    Parámetros: ninguno.
    Devuelve: JSON con el timestamp del último run y las estadísticas guardadas
              (leads detectados, escritos, no clasificados, errores, etc.).
    """
    raw = db.get_state("last_run_stats")
    last = json.loads(raw) if raw else None
    return jsonify({"last_run_ts": db.get_last_run_ts(), "last_run": last})


@app.route("/diag")
def diag():
    """Diagnóstico completo: variables de entorno, acceso a Gmail y acceso a Sheets.

    Pensado para abrirse en el navegador y ver por qué no se añade nada.
    Parámetros: ninguno.
    Devuelve: JSON con tres bloques: "env" (presencia de variables), "gmail"
              (resultado de gmail_reader.diag) y "sheets" (resultado de sheets_handler.diag).
    """
    import gmail_reader
    import sheets_handler
    out = {
        "env": {
            "GMAIL_CREDENTIALS": bool(os.environ.get("GMAIL_CREDENTIALS")),
            "GMAIL_TOKEN": bool(os.environ.get("GMAIL_TOKEN")),
            "GOOGLE_SERVICE_ACCOUNT": bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT")),
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "ULTRAMSG_INSTANCE": config.ULTRAMSG_INSTANCE,
            "portal_senders": list(config.PORTAL_SENDERS.keys()),
        },
    }
    try:
        out["gmail"] = gmail_reader.diag()
    except Exception as e:  # noqa: BLE001
        out["gmail"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        sheets_handler.reset_cache()
        out["sheets"] = sheets_handler.diag()
    except Exception as e:  # noqa: BLE001
        out["sheets"] = {"error": f"{type(e).__name__}: {e}"}
    return jsonify(out)


# ---------------------------------------------------------------------------
# Dashboard (protégé par mot de passe)
# ---------------------------------------------------------------------------
# HTML del logo "iAD REAL ESTATE" que se inserta en las páginas (sustituye __LOGO__). NO modificar su contenido.
_LOGO_HTML = (
    '<div style="text-align:center;line-height:1;">'
    '<div style="font-family:Arial,sans-serif;font-weight:bold;font-size:42px;'
    'color:#00b1eb;letter-spacing:1px;">iAD</div>'
    '<div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:4px;'
    'color:#00b1eb;margin-top:2px;">REAL ESTATE</div></div>'
)

# HTML/CSS de la página de inicio de sesión del dashboard. NO modificar su contenido (HTML, CSS ni JS).
LOGIN_HTML = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CRM IAD — Connexion</title>
<style>
body{margin:0;font-family:Arial,sans-serif;background:#F5F5F5;display:flex;min-height:100vh;
align-items:center;justify-content:center;}
.card{background:#fff;padding:36px 32px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);
width:320px;text-align:center;}
h1{color:#00628C;font-size:18px;margin:18px 0 4px;}
p{color:#888;font-size:13px;margin:0 0 22px;}
input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #ddd;border-radius:6px;
font-size:15px;margin-bottom:14px;}
button{width:100%;padding:12px;background:#E87722;color:#fff;border:none;border-radius:6px;
font-size:15px;font-weight:bold;cursor:pointer;}
.err{color:#DC3545;font-size:13px;margin-bottom:12px;}
</style></head><body>
<form class="card" method="post">
  __LOGO__
  <h1>CRM IAD COMP</h1>
  <p>El Francés Inmobiliaria</p>
  __ERROR__
  <input type="password" name="password" placeholder="Mot de passe" autofocus>
  <button type="submit">Se connecter</button>
</form></body></html>"""

# HTML/CSS/JS de la página del dashboard (panel de control). NO modificar su contenido (HTML, CSS ni JS).
DASHBOARD_HTML = r"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CRM IAD — Dashboard</title>
<style>
:root{--dark:#00628C;--light:#00b1eb;--orange:#E87722;--gray:#F5F5F5;--green:#28A745;--red:#DC3545;}
*{box-sizing:border-box;}
body{margin:0;font-family:Arial,sans-serif;background:var(--gray);color:#333;}
.wrap{max-width:900px;margin:0 auto;padding:18px;}
.header{background:#fff;border-radius:12px;padding:22px;text-align:center;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.06);}
.header h1{color:var(--dark);font-size:20px;margin:12px 0 2px;}
.header h2{color:#888;font-size:14px;font-weight:normal;margin:0;}
.card{background:#fff;border-radius:12px;padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.06);}
.card h3{margin:0 0 14px;color:var(--dark);font-size:15px;border-left:4px solid var(--light);padding-left:10px;}
.status-line{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--gray);font-size:14px;}
.status-line b{color:var(--dark);}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
@media(max-width:560px){.actions{grid-template-columns:1fr;}}
.btn{padding:14px;border:none;border-radius:8px;font-size:14px;font-weight:bold;cursor:pointer;color:#fff;background:var(--dark);}
.btn.light{background:var(--light);}.btn.orange{background:var(--orange);}.btn.gray{background:#6c757d;}
.btn:active{opacity:.8;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;}
.metric{border-radius:10px;padding:14px;color:#fff;text-align:center;}
.metric .n{font-size:26px;font-weight:bold;}.metric .l{font-size:12px;opacity:.95;margin-top:4px;}
.spinner{display:none;margin:14px auto;width:28px;height:28px;border:3px solid #ddd;border-top-color:var(--orange);
border-radius:50%;animation:spin 1s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
pre{background:#1e1e1e;color:#9cdcfe;padding:14px;border-radius:8px;overflow:auto;font-size:12px;max-height:340px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
td,th{padding:7px 8px;border-bottom:1px solid var(--gray);text-align:left;}
.alert{font-size:13px;margin:6px 0;}.alert.warn{color:#9a6b00;}.alert.err{color:var(--red);}
.bien{font-weight:bold;color:var(--dark);font-size:14px;margin-top:8px;}
.bien a{color:var(--light);} .resp{margin-left:14px;color:#555;font-size:13px;}
.sheetbtn{display:inline-block;background:var(--orange);color:#fff;text-decoration:none;padding:13px 22px;
border-radius:8px;font-weight:bold;font-size:15px;margin:6px;}
.extbtn{display:inline-block;background:#fff;border:2px solid var(--dark);color:var(--dark);
text-decoration:none;padding:11px 18px;border-radius:8px;font-weight:bold;font-size:14px;margin:6px;}
.muted{color:#999;font-size:13px;}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:50;}
.modal.show{display:flex;align-items:center;justify-content:center;}
.modalbox{background:#fff;border-radius:12px;padding:22px;width:360px;max-width:92%;}
.modalbox h3{margin:0 0 14px;color:var(--dark);border:none;padding:0;}
.modalbox label{font-size:12px;color:#666;display:block;margin:6px 0 2px;}
.modalbox input,.modalbox select{width:100%;box-sizing:border-box;padding:10px;margin-bottom:6px;border:1px solid #ddd;border-radius:6px;font-size:14px;}
.modalbtns{display:flex;gap:10px;margin-top:12px;}.modalbtns .btn{flex:1;padding:11px;}
</style></head><body><div class="wrap">

<div class="header">
  __LOGO__
  <h1>CRM IAD COMP — El Francés Inmobiliaria</h1>
  <h2>Thibaut MONTALAT</h2>
</div>

<div class="card">
  <h3>📡 Statut en temps réel</h3>
  <div class="status-line"><span>Service</span><b id="svc">…</b></div>
  <div class="status-line"><span>Prochain run automatique</span><b id="next">…</b></div>
  <div class="status-line"><span>Dernier run</span><b id="last">…</b></div>
</div>

<div class="card">
  <h3>⚡ Actions rapides</h3>
  <div class="actions">
    <button class="btn" onclick="act('POST','/run','Lancer le pipeline')">▶️ Lancer le pipeline</button>
    <button class="btn" onclick="act('GET','/full_scan','Scan complet 30j')">🔄 Scan complet 30 jours</button>
    <button class="btn" onclick="act('POST','/send_report','Envoi du rapport')">📧 Générer et envoyer le rapport</button>
    <button class="btn gray" onclick="act('POST','/setup_dropdowns','Listes déroulantes')">🔧 Configurer listes déroulantes (1 fois)</button>
  </div>
  <div class="spinner" id="spin"></div>
  <pre id="result" style="display:none;"></pre>
</div>

<div class="card">
  <h3>📊 Résultats du dernier run</h3>
  <div class="cards" id="metrics"><span class="muted">Chargement…</span></div>
</div>

<div class="card" id="alertsCard" style="display:none;">
  <h3 style="border-color:var(--orange);">🐛 Alertes et bugs</h3>
  <div id="alerts"></div>
</div>

<div class="card" id="unmatchedCard" style="display:none;">
  <h3 style="border-color:var(--red);">📋 Leads non classifiés</h3>
  <table><thead><tr><th>Téléphone</th><th>Portail</th><th>Raison</th></tr></thead><tbody id="unmatched"></tbody></table>
</div>

<div class="card" id="respCard" style="display:none;">
  <h3>📬 Réponses Idealista</h3>
  <div id="resp"></div>
</div>

<div class="card">
  <h3 style="border-color:var(--orange);">🏠 Gestion des biens</h3>
  <div class="actions">
    <button class="btn" onclick="openModal('addModal')">➕ Ajouter un nouveau bien</button>
    <button class="btn orange" onclick="openModal('closeModal')">🏁 Marquer un bien comme vendu</button>
  </div>
</div>

<div class="card" style="text-align:center;">
  <h3 style="border:none;padding:0;text-align:center;">🔗 Liens rapides</h3>
  <a class="sheetbtn" href="__SHEET_URL__" target="_blank">📊 Ouvrir Google Sheets</a>
  <a class="extbtn" href="https://www.idealista.com/tools/listadooffice?Agent=39869786&ItemsPerPage=20&CurrentPage=1&OrderedBy=activationdateCol&OrderedType=DESC" target="_blank">🏠 Ma page Idealista ↗</a>
  <a class="extbtn" href="https://www.iadespana.es/agente-inmobiliario/thibaut.montalat" target="_blank">👤 Ma page IAD ↗</a>
</div>

<!-- Configuración (bróker) -->
<div class="card">
  <h3>⚙️ Configuración</h3>
  <label style="font-size:12px;color:#666;">Nombre del bróker</label>
  <input id="cfg_broker_name" style="width:100%;box-sizing:border-box;padding:10px;border:1px solid #ddd;border-radius:6px;margin-bottom:8px;">
  <label style="font-size:12px;color:#666;">Teléfono del bróker</label>
  <input id="cfg_broker_phone" style="width:100%;box-sizing:border-box;padding:10px;border:1px solid #ddd;border-radius:6px;margin-bottom:10px;">
  <button class="btn" onclick="saveConfig()">💾 Guardar configuración</button>
</div>

<!-- Tests & Simulation (bas de page) -->
<div class="card" style="background:var(--gray);border:1px solid var(--orange);">
  <h3 style="border-color:var(--orange);">🧪 Tests &amp; Simulation</h3>
  <div class="actions">
    <button class="btn light" onclick="act('GET','/run?dry_run=true','Simulation (dry run)')">🔍 Simulation (dry run)</button>
    <button class="btn light" onclick="act('GET','/full_scan?dry_run=true','Scan complet simulation')">🔄 Scan complet en simulation</button>
    <button class="btn light" onclick="act('POST','/send_report','Test email')">📧 Test email minimal</button>
    <button class="btn orange" onclick="act('POST','/reset_timestamp','Reset timestamp')">🔁 Reset timestamp</button>
    <button class="btn gray" onclick="act('GET','/diag','Diagnostic complet')">🔍 Diagnostic complet</button>
  </div>
  <p style="font-size:12px;color:#9a6b00;margin:12px 0 0;">⚠️ Ces actions sont réservées aux tests — ne pas utiliser en production courante</p>
</div>

<!-- Modal Ajouter un bien (formulario completo con generación en tiempo real) -->
<div id="addModal" class="modal"><div class="modalbox">
  <h3>➕ Añadir un nuevo inmueble</h3>
  <label>Tipo *</label>
  <select id="ab_type" onchange="updatePreview()">
    <option value="T">T — Terreno</option>
    <option value="C">C — Casa</option>
    <option value="P">P — Piso</option>
    <option value="Pa">Pa — Parking</option>
    <option value="L">L — Local</option>
  </select>
  <label>Ciudad *</label>
  <select id="ab_city" onchange="onCityChange()"></select>
  <div id="ab_newcity" style="display:none;">
    <input id="ab_city_full" oninput="updatePreview()" placeholder="Nombre completo (ej: Roses)">
    <input id="ab_city_abbr" maxlength="6" oninput="updatePreview()" placeholder="Abreviación (máx 6, ej: roses)">
  </div>
  <label>Precio (€) *</label><input id="ab_price" type="number" oninput="updatePreview()" placeholder="ej: 55000">
  <label>Nombre del propietario *</label><input id="ab_owner" oninput="updatePreview()" placeholder="ej: Juan García">
  <label>URL Idealista *</label><input id="ab_idea" oninput="updatePreview()" placeholder="OBLIGATORIA">
  <label>URL Fotocasa</label><input id="ab_foto" placeholder="opcional">
  <label>URL Habitaclia</label><input id="ab_habi" placeholder="opcional">
  <label>URL IAD</label><input id="ab_iad" placeholder="opcional">
  <div style="background:#f5f5f5;padding:8px;border-radius:6px;font-size:13px;margin:10px 0;">
    <div>Hoja: <b id="pv_name">—</b></div>
    <div>Título: <b id="pv_title">—</b></div>
    <div id="pv_err" style="color:#DC3545;margin-top:4px;"></div>
  </div>
  <div class="modalbtns">
    <button class="btn gray" onclick="closeModal('addModal')">Cancelar</button>
    <button class="btn" id="ab_submit" onclick="submitAddBien()">Añadir</button>
  </div>
</div></div>

<!-- Modal Clôturer un bien -->
<div id="closeModal" class="modal"><div class="modalbox">
  <h3>🏁 Marquer un bien comme vendu</h3>
  <label>Bien actif</label><select id="cb_nom"><option>Chargement…</option></select>
  <div class="modalbtns">
    <button class="btn gray" onclick="closeModal('closeModal')">Annuler</button>
    <button class="btn orange" onclick="submitCloseBien()">Marquer vendu</button>
  </div>
</div></div>

</div>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function refreshStatus(){
  try{
    const h=await (await fetch('/health')).json();
    document.getElementById('svc').innerHTML='🟢 En ligne';
    document.getElementById('next').textContent=h.next_run||'—';
    const s=await (await fetch('/status')).json();
    const lr=s.last_run;
    document.getElementById('last').textContent = lr&&lr.finished_at
      ? (new Date(lr.finished_at).toLocaleString('fr-FR'))+' ('+(lr.duration_s||0)+'s)'
      : (h.last_run_str||'—');
    renderResults(lr);
  }catch(e){ document.getElementById('svc').innerHTML='🔴 Hors ligne'; }
}
function metric(n,l,bg){return '<div class="metric" style="background:'+bg+'"><div class="n">'+(n||0)+'</div><div class="l">'+l+'</div></div>';}
function renderResults(lr){
  const m=document.getElementById('metrics');
  if(!lr){m.innerHTML='<span class="muted">Aucun run pour le moment.</span>';return;}
  const alerts=(lr.alerts||[]).length;
  m.innerHTML =
    metric(lr.leads_detected,'Leads détectés','#00628C')+
    metric(lr.prospects_new,'Nouveaux prospects','#28A745')+
    metric(lr.wa_first_sent,'WhatsApp envoyés','#00b1eb')+
    metric(lr.leads_unmatched,'Leads non matchés','#E87722')+
    metric(alerts,'Alertes', alerts>0?'#DC3545':'#6c757d')+
    metric(lr.mails_deleted,'Mails supprimés','#6c757d');
  // alertes + erreurs
  const ac=document.getElementById('alertsCard'),ad=document.getElementById('alerts');
  const al=(lr.alerts||[]),er=(lr.errors||[]);
  if(al.length||er.length){ac.style.display='block';
    ad.innerHTML=al.map(a=>'<div class="alert warn">⚠️ '+esc(a)+'</div>').join('')+
                 er.map(e=>'<div class="alert err">❌ '+esc(e)+'</div>').join('');
  }else ac.style.display='none';
  // non classifiés
  const uc=document.getElementById('unmatchedCard'),ub=document.getElementById('unmatched');
  const un=(lr.unmatched||[]);
  if(un.length){uc.style.display='block';
    ub.innerHTML=un.map(u=>'<tr><td>'+esc(u.telefono)+'</td><td>'+esc(u.portail)+'</td><td>'+esc(u.raison)+'</td></tr>').join('');
  }else uc.style.display='none';
  // réponses idealista
  const rc=document.getElementById('respCard'),rd=document.getElementById('resp');
  const rs=(lr.idealista_responses||[]);
  if(rs.length){rc.style.display='block';
    const g={};rs.forEach(r=>{const k=r.bien||('Ref '+r.ref);(g[k]=g[k]||{url:r.url,names:[]}).names.push(r.nombre);});
    rd.innerHTML=Object.keys(g).map(k=>{const d=g[k];
      const link=d.url?' — <a href="'+esc(d.url)+'" target="_blank">'+esc(d.url)+'</a>':'';
      return '<div class="bien">🏠 '+esc(k)+link+'</div>'+d.names.map(n=>'<div class="resp">└ '+esc(n)+' a répondu</div>').join('');
    }).join('');
  }else rc.style.display='none';
}
async function act(method,url,label){
  const sp=document.getElementById('spin'),res=document.getElementById('result');
  sp.style.display='block';res.style.display='none';
  try{
    const r=await fetch(url,{method:method});
    const j=await r.json();
    res.textContent=label+' →\n'+JSON.stringify(j,null,2);
  }catch(e){res.textContent=label+' → erreur: '+e;}
  sp.style.display='none';res.style.display='block';
  setTimeout(refreshStatus,1500);
}
function val(id){return document.getElementById(id).value.trim();}
function showResult(label,j){const res=document.getElementById('result');res.style.display='block';res.textContent=label+' →\n'+JSON.stringify(j,null,2);}
function openModal(id){document.getElementById(id).classList.add('show');if(id==='closeModal')loadBiens();if(id==='addModal'){loadCities();updatePreview();}}
function closeModal(id){document.getElementById(id).classList.remove('show');}
async function loadBiens(){
  try{const r=await (await fetch('/list_biens')).json();
    const sel=document.getElementById('cb_nom');
    const biens=r.biens||[];
    sel.innerHTML = biens.length ? biens.map(b=>'<option>'+esc(b)+'</option>').join('') : '<option value="">(aucun bien actif)</option>';
  }catch(e){}
}
// --- Gestión de inmuebles: ciudades, abreviaturas, vista previa en tiempo real ---
const TYPE_NAMES={T:'Terreno',C:'Casa',P:'Piso',Pa:'Parking',L:'Local'};
async function loadCities(){
  try{const r=await (await fetch('/list_cities')).json();
    const c=r.cities||{}; const sel=document.getElementById('ab_city');
    let opts=Object.keys(c).sort().map(k=>'<option value="'+esc(k)+'" data-full="'+esc(c[k])+'">'+esc(c[k])+'</option>').join('');
    opts+='<option value="__new__">➕ Agregar nueva ciudad</option>';
    sel.innerHTML=opts; onCityChange();
  }catch(e){}
}
function onCityChange(){
  const sel=document.getElementById('ab_city');
  document.getElementById('ab_newcity').style.display = sel.value==='__new__' ? 'block':'none';
  updatePreview();
}
function curCityAbbr(){const s=document.getElementById('ab_city');return s.value==='__new__'?val('ab_city_abbr').toLowerCase():s.value;}
function curCityFull(){const s=document.getElementById('ab_city');if(s.value==='__new__')return val('ab_city_full');const o=s.options[s.selectedIndex];return o?(o.dataset.full||''):'';}
function fmtThousands(p){return (parseInt(p,10)||0).toLocaleString('es-ES').replace(/\./g,' ');}
function abbrevPrice(p){p=parseInt(p,10)||0;if(!p)return '';if(p<20000)return fmtThousands(p);return Math.floor(p/1000)+'k';}
function fullPrice(p){p=parseInt(p,10)||0;if(!p)return '';return fmtThousands(p)+' €';}
function capWords(s){return (s||'').split(' ').map(w=>w?w[0].toUpperCase()+w.slice(1):w).join(' ');}
function genName(){const t=document.getElementById('ab_type').value;const a=capWords(curCityAbbr());const p=abbrevPrice(val('ab_price'));return (t+' '+a+' '+p).replace(/\s+/g,' ').trim();}
function genTitle(){const t=TYPE_NAMES[document.getElementById('ab_type').value];return t+' / '+(curCityFull()||'—')+' / '+(val('ab_owner')||'—')+' / '+(fullPrice(val('ab_price'))||'—');}
let _checkTimer=null;
async function updatePreview(){
  const name=genName();document.getElementById('pv_name').textContent=name||'—';
  document.getElementById('pv_title').textContent=genTitle();
  const err=document.getElementById('pv_err');const btn=document.getElementById('ab_submit');
  let msg='';
  if(!val('ab_idea')) msg='La URL de Idealista es obligatoria para poder enviar mensajes WhatsApp.';
  if(err) err.textContent=msg;
  if(btn) btn.disabled = !!msg;
  // verifica existencia del nombre (con debounce)
  clearTimeout(_checkTimer);
  if(name && !msg){_checkTimer=setTimeout(async()=>{
    try{const r=await (await fetch('/check_bien?name='+encodeURIComponent(name))).json();
      if(r.exists){err.textContent='Ya existe un bien con el nombre "'+name+'".';if(btn)btn.disabled=true;}
    }catch(e){}
  },400);}
}
async function submitAddBien(){
  const idea=val('ab_idea');
  if(!idea){alert('La URL de Idealista es obligatoria para poder enviar mensajes WhatsApp.');return;}
  const abbr=curCityAbbr(),cityFull=curCityFull(),price=val('ab_price'),owner=val('ab_owner');
  if(!abbr||!price||!owner){alert('Completa tipo, ciudad, precio y propietario');return;}
  const sp=document.getElementById('spin');sp.style.display='block';
  try{
    if(document.getElementById('ab_city').value==='__new__'){
      await fetch('/update_config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({new_city_abbrev:abbr,new_city_full:cityFull})});
    }
    const body={nom:genName(),description:genTitle(),url_idealista:idea,
      url_fotocasa:val('ab_foto'),url_habitaclia:val('ab_habi'),url_iad:val('ab_iad')};
    const r=await fetch('/add_bien',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    showResult('Añadir bien',await r.json());closeModal('addModal');loadBiens();
  }catch(e){showResult('Añadir bien',{status:'error',message:String(e)});}
  sp.style.display='none';
}
async function loadConfig(){
  try{const r=await (await fetch('/update_config')).json();
    document.getElementById('cfg_broker_name').value=r.broker_name||'';
    document.getElementById('cfg_broker_phone').value=r.broker_phone||'';
  }catch(e){}
}
async function saveConfig(){
  const sp=document.getElementById('spin');sp.style.display='block';
  try{const r=await fetch('/update_config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({broker_name:val('cfg_broker_name'),broker_phone:val('cfg_broker_phone')})});
    showResult('Configuración',await r.json());
  }catch(e){showResult('Configuración',{status:'error',message:String(e)});}
  sp.style.display='none';
}
async function submitCloseBien(){
  const nom=val('cb_nom');
  if(!nom){alert('Choisis un bien');return;}
  const sp=document.getElementById('spin');sp.style.display='block';
  try{const r=await fetch('/close_bien',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nom:nom})});
    showResult('Clôture bien',await r.json());closeModal('closeModal');loadBiens();}
  catch(e){showResult('Clôture bien',{status:'error',message:String(e)});}
  sp.style.display='none';
}
refreshStatus();
loadConfig();
setInterval(refreshStatus,10000);
</script></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    """Página de inicio de sesión del dashboard.

    En GET muestra el formulario. En POST verifica la contraseña enviada contra
    DASHBOARD_PASSWORD; si coincide, marca la sesión como autenticada y redirige a /dashboard.
    Parámetros (body en POST): password -- contraseña introducida.
    Devuelve: el HTML de login (con mensaje de error si la contraseña es incorrecta) o
              una redirección a /dashboard si el login es correcto.
    """
    error = ""
    if request.method == "POST":
        print(f"[login] tentative mot de passe, DASHBOARD_PASSWORD défini: "
              f"{bool(os.environ.get('DASHBOARD_PASSWORD'))}")
        # .strip(): evita fallos por un espacio o salto de línea en la variable de Railway.
        # Se compara la contraseña del formulario con DASHBOARD_PASSWORD ya saneada.
        if (request.form.get("password") or "").strip() == (DASHBOARD_PASSWORD or "").strip():
            session["auth"] = True
            return redirect("/dashboard")
        error = '<div class="err">Mot de passe incorrect</div>'
    # Construye la página inyectando el logo y el posible error en la plantilla mediante .replace.
    html = LOGIN_HTML.replace("__LOGO__", _LOGO_HTML).replace("__ERROR__", error)
    return html


@app.route("/logout")
def logout():
    """Cierra la sesión del dashboard.

    Parámetros: ninguno.
    Devuelve: una redirección a /login tras vaciar la sesión.
    """
    session.clear()
    return redirect("/login")


@app.route("/dashboard")
def dashboard():
    """Panel de control (requiere sesión iniciada).

    Si no hay sesión, redirige a /login. Si la hay, devuelve el HTML del dashboard.
    Parámetros: ninguno.
    Devuelve: el HTML del dashboard o una redirección a /login.
    """
    if not _is_authed():
        return redirect("/login")
    # Inyecta el logo y la URL del Google Sheet en la plantilla mediante .replace.
    html = (DASHBOARD_HTML
            .replace("__LOGO__", _LOGO_HTML)
            .replace("__SHEET_URL__", report_assets.GOOGLE_SHEET_URL))
    return html


if __name__ == "__main__":
    # Ejecución local (desarrollo). En producción, gunicorn sirve app:app.
    # Arranca el planificador aquí si aún no se inició (misma guarda _SCHEDULER_STARTED).
    if not os.environ.get("_SCHEDULER_STARTED"):
        os.environ["_SCHEDULER_STARTED"] = "1"
        start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
