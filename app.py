"""
Application Flask principale — tourne 24h/24 sur Railway.

- Sert le webhook UltraMsg (/webhook) pour recevoir les réponses WhatsApp.
- Lance APScheduler qui exécute le pipeline à 8h, 12h et 18h (heure Madrid).
- Expose /health (statut) et /run (déclenchement manuel sécurisé du pipeline).
"""
import os
import json
import threading

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import config
import database as db
import pipeline
from webhook import webhook_bp

app = Flask(__name__)
app.register_blueprint(webhook_bp)

db.init_db()


def _run_pipeline_async(dry_run=False, full_scan=False):
    """Lance le pipeline dans un thread pour ne pas bloquer le scheduler/Flask."""
    threading.Thread(
        target=pipeline.run,
        kwargs={"dry_run": dry_run, "full_scan": full_scan},
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Scheduler (8h, 12h, 18h heure Madrid)
# ---------------------------------------------------------------------------
def start_scheduler():
    tz = pytz.timezone(config.TIMEZONE)
    scheduler = BackgroundScheduler(timezone=tz)
    hours = ",".join(str(h) for h in config.RUN_HOURS)
    scheduler.add_job(
        _run_pipeline_async,
        CronTrigger(hour=hours, minute=0, timezone=tz),
        id="crm_pipeline",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    print(f"[scheduler] démarré — runs à {hours}h ({config.TIMEZONE})")
    return scheduler


# Démarre le scheduler une seule fois (gunicorn peut importer le module plusieurs fois).
if os.environ.get("RUN_SCHEDULER", "1") == "1" and not os.environ.get("_SCHEDULER_STARTED"):
    os.environ["_SCHEDULER_STARTED"] = "1"
    start_scheduler()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({"service": "iad-crm", "status": "running", "runs": config.RUN_HOURS})


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "last_run_ts": db.get_last_run_ts(),
        "timezone": config.TIMEZONE,
        "run_hours": config.RUN_HOURS,
    })


@app.route("/run", methods=["POST", "GET"])
def manual_run():
    """Déclenchement manuel. Protégé par un token optionnel (RUN_TOKEN)."""
    token = os.environ.get("RUN_TOKEN")
    if token and request.args.get("token") != token:
        return jsonify({"error": "unauthorized"}), 401
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    _run_pipeline_async(dry_run=dry_run)
    return jsonify({"status": "pipeline_started", "dry_run": dry_run,
                    "note": "Voir /status dans ~30s pour le résumé"}), 202


@app.route("/full_scan", methods=["GET", "POST"])
def full_scan():
    """
    Scan unique des 30 derniers jours (newer_than:30d) : traite tous les leads.
    Après ce run, le fonctionnement normal reprend (newer_than:1d aux runs 8h/12h/18h).
    Option : ?dry_run=true pour ne pas envoyer de WhatsApp.
    """
    token = os.environ.get("RUN_TOKEN")
    if token and request.args.get("token") != token:
        return jsonify({"error": "unauthorized"}), 401
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    _run_pipeline_async(dry_run=dry_run, full_scan=True)
    return jsonify({"status": "full_scan_started", "dry_run": dry_run,
                    "window": "30d",
                    "note": "Run unique. Ensuite retour automatique à newer_than:1d. Voir /status."}), 202


@app.route("/reset_timestamp", methods=["POST"])
def reset_timestamp():
    """Remet last_run_ts à 0 : le prochain /run retraitera tous les mails des dernières 24h."""
    token = os.environ.get("RUN_TOKEN")
    if token and request.args.get("token") != token:
        return jsonify({"error": "unauthorized"}), 401
    db.set_last_run_ts(0)
    return jsonify({"status": "timestamp_reset", "last_run_ts": db.get_last_run_ts(),
                    "note": "Lance /run pour retraiter les mails des dernières 24h"}), 200


@app.route("/status")
def status():
    """Résumé du dernier run (sans dépendre de l'email)."""
    raw = db.get_state("last_run_stats")
    last = json.loads(raw) if raw else None
    return jsonify({"last_run_ts": db.get_last_run_ts(), "last_run": last})


@app.route("/diag")
def diag():
    """
    Diagnostic complet : variables d'env, accès Gmail, accès Sheets.
    À ouvrir dans le navigateur pour voir pourquoi rien ne s'ajoute.
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


if __name__ == "__main__":
    # Exécution locale (dev). En prod, gunicorn sert app:app.
    if not os.environ.get("_SCHEDULER_STARTED"):
        os.environ["_SCHEDULER_STARTED"] = "1"
        start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
