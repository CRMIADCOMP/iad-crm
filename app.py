"""
Application Flask principale — tourne 24h/24 sur Railway.

- Sert le webhook UltraMsg (/webhook) pour recevoir les réponses WhatsApp.
- Lance APScheduler qui exécute le pipeline à 8h, 12h et 18h (heure Madrid).
- Expose /health (statut) et /run (déclenchement manuel sécurisé du pipeline).
"""
import os
import json
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

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY", "iad-crm-" + os.environ.get("DASHBOARD_PASSWORD", "default")
)
app.register_blueprint(webhook_bp)

db.init_db()

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "iad-crm")


def _is_authed():
    return session.get("auth") is True


def _authorized(req):
    """Autorise si session dashboard OU token correct OU aucun token configuré."""
    if _is_authed():
        return True
    token = os.environ.get("RUN_TOKEN")
    if not token:
        return True
    return req.args.get("token") == token


def _next_run():
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.datetime.now(tz)
    hours = sorted(config.RUN_HOURS)
    for h in hours:
        c = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if c > now:
            return c
    nxt = now + datetime.timedelta(days=1)
    return nxt.replace(hour=hours[0], minute=0, second=0, microsecond=0)


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
    """Déclenchement manuel. Protégé par un token optionnel (RUN_TOKEN)."""
    if not _authorized(request):
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
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    _run_pipeline_async(dry_run=dry_run, full_scan=True)
    return jsonify({"status": "full_scan_started", "dry_run": dry_run,
                    "window": "30d",
                    "note": "Run unique. Ensuite retour automatique à newer_than:1d. Voir /status."}), 202


@app.route("/reset_timestamp", methods=["POST"])
def reset_timestamp():
    """Remet last_run_ts à 0 : le prochain /run retraitera tous les mails des dernières 24h."""
    if not _authorized(request):
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


# ---------------------------------------------------------------------------
# Dashboard (protégé par mot de passe)
# ---------------------------------------------------------------------------
_LOGO_HTML = (
    '<div style="text-align:center;line-height:1;">'
    '<div style="font-family:Arial,sans-serif;font-weight:bold;font-size:42px;'
    'color:#00b1eb;letter-spacing:1px;">iAD</div>'
    '<div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:4px;'
    'color:#00b1eb;margin-top:2px;">REAL ESTATE</div></div>'
)

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
border-radius:8px;font-weight:bold;font-size:15px;}
.muted{color:#999;font-size:13px;}
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
    <button class="btn light" onclick="act('GET','/run?dry_run=true','Simulation')">🔍 Simulation (dry run)</button>
    <button class="btn" onclick="act('GET','/full_scan','Scan complet 30j')">🔄 Scan complet 30 jours</button>
    <button class="btn light" onclick="act('GET','/full_scan?dry_run=true','Scan complet simulation')">🔄 Scan complet (simulation)</button>
    <button class="btn orange" onclick="act('POST','/reset_timestamp','Reset timestamp')">🔁 Reset timestamp</button>
    <button class="btn gray" onclick="act('GET','/diag','Diagnostic')">🔍 Diagnostic complet</button>
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

<div class="card" style="text-align:center;">
  <a class="sheetbtn" href="__SHEET_URL__" target="_blank">📊 Ouvrir Google Sheets</a>
</div>

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
refreshStatus();
setInterval(refreshStatus,10000);
</script></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["auth"] = True
            return redirect("/dashboard")
        error = '<div class="err">Mot de passe incorrect</div>'
    html = LOGIN_HTML.replace("__LOGO__", _LOGO_HTML).replace("__ERROR__", error)
    return html


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/dashboard")
def dashboard():
    if not _is_authed():
        return redirect("/login")
    html = (DASHBOARD_HTML
            .replace("__LOGO__", _LOGO_HTML)
            .replace("__SHEET_URL__", report_assets.GOOGLE_SHEET_URL))
    return html


if __name__ == "__main__":
    # Exécution locale (dev). En prod, gunicorn sert app:app.
    if not os.environ.get("_SCHEDULER_STARTED"):
        os.environ["_SCHEDULER_STARTED"] = "1"
        start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
