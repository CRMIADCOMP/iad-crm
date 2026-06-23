"""
Application Flask principale — tourne 24h/24 sur Railway.

- Sert le webhook UltraMsg (/webhook) pour recevoir les réponses WhatsApp.
- Lance APScheduler qui exécute le pipeline à 8h, 12h et 18h (heure Madrid).
- Expose /health (statut) et /run (déclenchement manuel sécurisé du pipeline).
"""
import os
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


def _run_pipeline_async():
    """Lance le pipeline dans un thread pour ne pas bloquer le scheduler/Flask."""
    threading.Thread(target=pipeline.run, daemon=True).start()


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
    _run_pipeline_async()
    return jsonify({"status": "pipeline_started"}), 202


if __name__ == "__main__":
    # Exécution locale (dev). En prod, gunicorn sert app:app.
    if not os.environ.get("_SCHEDULER_STARTED"):
        os.environ["_SCHEDULER_STARTED"] = "1"
        start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
