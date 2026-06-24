"""
Orchestration du pipeline CRM (exécuté à 8h, 12h, 18h heure Madrid).

Étapes :
 1-3  Lecture Gmail + détection/extraction des leads
 4    Matching annonce -> feuille via l'onglet Config
 5    Déduplication (téléphone ET email)
 6    Insertion / mise à jour du prospect
 7    Envoi du 1er message WhatsApp personnalisé
 8-10 Traitement des réponses reçues + analyse IA -> colonnes F/G/H
 11   Message spécial si recherche > 1 an
 12   Relances J+2
 13   Clôture "Sin respuesta - 7d" après 7 jours
 14   Rapport par email
"""
import time
import json
import datetime
import traceback

import html as _html

import config
import database as db
import gmail_reader
import sheets_handler as sheets
import whatsapp
import ai_analyzer
import report_assets

# Palette IAD
C_DARK = "#00628C"
C_LIGHT = "#00b1eb"
C_ORANGE = "#E87722"
C_GRAY_BG = "#F5F5F5"
C_GREEN = "#28A745"
C_RED = "#DC3545"
C_YELLOW = "#FFC107"


def _today():
    return datetime.date.today()


def _parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(value.strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def _date_to_ts(d):
    return time.mktime(datetime.datetime(d.year, d.month, d.day).timetuple())


# ---------------------------------------------------------------------------
# Index des prospects (phone -> emplacement) pour le matching des réponses
# ---------------------------------------------------------------------------
def _build_prospect_index():
    index = {}
    for feuille in sheets.list_all_prospect_sheets():
        for row_idx, p in sheets.iter_prospects(feuille):
            phone = db.normalize_phone(p.get("telefono", ""))
            if phone:
                index[phone] = (feuille, row_idx, p)
    return index


# ---------------------------------------------------------------------------
# Étapes 1 à 7 : nouveaux leads
# ---------------------------------------------------------------------------
def process_new_leads(stats):
    # Nettoyage : suppression auto des mails indésirables (corbeille)
    try:
        deleted = gmail_reader.delete_unwanted_mails()
        stats["mails_deleted"] = len(deleted)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"Gmail delete: {e}")

    # Full scan : 30 jours, traite tout (after_ts=0) ; sinon fenêtre normale.
    if stats.get("full_scan"):
        last_run = 0
        fetch_kwargs = {"window": "30d"}
    else:
        last_run = db.get_last_run_ts()
        fetch_kwargs = {}
    try:
        leads = gmail_reader.fetch_new_leads(last_run, **fetch_kwargs)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"Gmail: {e}")
        return
    stats["leads_detected"] = len(leads)

    for lead in leads:
        try:
            # Notification de réponse Idealista : pas un nouveau lead, juste au rapport.
            if lead.get("kind") == "respuesta_idealista":
                stats["idealista_responses"].append({
                    "nombre": lead.get("nombre") or "?",
                    "ref": lead.get("ref") or "?",
                })
                continue

            feuille, iad_url = sheets.match_lead_to_sheet(lead)
            matched = bool(feuille)

            if not matched:
                # Lead non matché : on l'écrit quand même dans la feuille de repli
                # pour ne rien perdre (et pouvoir diagnostiquer).
                stats["leads_unmatched"] += 1
                stats["unmatched"].append({
                    "telefono": lead.get("telefono") or lead.get("email") or "?",
                    "portail": lead.get("fuente", ""),
                    "raison": f"Ref/URL absente de l'onglet Config (ref={lead.get('ref') or '—'})",
                })
                feuille = config.FALLBACK_SHEET
                iad_url = ""

            # La feuille doit exister : le script ne crée JAMAIS de feuille.
            if not sheets.worksheet_exists(feuille):
                stats["alerts"].append(
                    f"[ALERTA] Feuille '{feuille}' introuvable dans le Sheets — "
                    f"vérifier l'onglet Config"
                )
                continue

            # URL utilisée dans le message (annonce IAD si dispo, sinon l'URL source)
            msg_url = iad_url or lead.get("url", "")
            lead["url"] = msg_url

            row_idx, is_new, _ = sheets.upsert_prospect(feuille, lead)
            if row_idx is None:  # sécurité (feuille disparue entre-temps)
                continue
            if is_new:
                stats["prospects_new"] += 1
            else:
                stats["prospects_updated"] += 1

            # On n'envoie pas de WhatsApp pour un lead non matché (sauf override),
            # car le message référence l'annonce et on n'a pas d'URL fiable.
            if not matched and not config.SEND_WHATSAPP_WHEN_UNMATCHED:
                # marque l'état pour l'exclure des relances/clôtures automatiques
                if is_new:
                    sheets.update_cells(feuille, row_idx, {"estado_final": "Sin clasificar"})
                continue

            # Étape 7 : envoi du 1er message si l'état l'autorise
            estado = sheets.get_cell(feuille, row_idx, "estado_final").strip()
            if estado not in config.SENDABLE_STATES:
                continue
            # déjà contacté ? (un message sortant a déjà fixé fecha_contacto + estado "WhatsApp enviado")
            already = sheets.get_cell(feuille, row_idx, "ultimo_mensaje").strip()
            if already:
                continue

            phone = lead.get("telefono", "")
            if not phone:
                stats["details"].append(f"⚠️ Pas de téléphone pour {lead.get('email')} ({feuille})")
                continue

            # URL obligatoire : sans URL d'annonce on n'envoie PAS, on alerte.
            if not msg_url:
                stats["alerts"].append(
                    f"[ALERTA] No se pudo enviar WhatsApp a {lead.get('nombre') or '?'} "
                    f"({phone}) — URL manquante pour la feuille {feuille}. "
                    f"Vérifier l'onglet Config."
                )
                continue

            if stats.get("dry_run"):
                print(f"[DRY RUN] whatsapp ignoré pour {phone} (primer contacto, {feuille})")
                stats["wa_dry_skipped"] += 1
                continue

            body = whatsapp.build_first_contact(lead.get("nombre", ""), msg_url)
            ok, info = whatsapp.send_message(phone, body)
            today = _today().isoformat()
            if ok:
                # ne touche PAS fecha_contacto (col I) : fixée à la 1ère détection
                sheets.update_cells(feuille, row_idx, {
                    "ultimo_mensaje": today,          # col J : maj à chaque envoi
                    "relance_j2": "Pendiente",        # col K
                    "estado_final": "WhatsApp enviado",  # col L
                })
                stats["wa_first_sent"] += 1
            else:
                stats["wa_failed"] += 1
                stats["details"].append(f"❌ Envoi WA échoué {phone}: {info}")
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"Lead {lead.get('telefono')}: {e}")


# ---------------------------------------------------------------------------
# Étapes 8 à 11 : traitement des réponses
# ---------------------------------------------------------------------------
def process_replies(stats):
    msgs = db.get_unprocessed_messages()
    if not msgs:
        return
    # regroupe par numéro
    by_phone = {}
    for m in msgs:
        by_phone.setdefault(m["phone"], []).append(m)

    index = _build_prospect_index()

    for phone, messages in by_phone.items():
        ids = [m["id"] for m in messages]
        try:
            target = index.get(phone)
            if not target:
                # réponse d'un numéro inconnu : on marque traité pour ne pas boucler
                db.mark_processed(ids)
                stats["replies_unmatched"] += 1
                continue

            feuille, row_idx, p = target
            bodies = [m["body"] for m in messages if m.get("body")]
            analysis = ai_analyzer.analyze_replies(bodies)
            stats["replies_processed"] += 1

            # F/G/H : remplis UNIQUEMENT si la cellule est vide (ne pas écraser le manuel)
            updates = {}
            if analysis.get("presupuesto") and not sheets.get_cell(feuille, row_idx, "presupuesto").strip():
                updates["presupuesto"] = analysis["presupuesto"]
            if analysis.get("tiempo_busqueda_texto") and not sheets.get_cell(feuille, row_idx, "tiempo_busqueda").strip():
                updates["tiempo_busqueda"] = analysis["tiempo_busqueda_texto"]
            if analysis.get("pago_validado") and not sheets.get_cell(feuille, row_idx, "pago_validado").strip():
                updates["pago_validado"] = analysis["pago_validado"]
            # K : le prospect a répondu -> relance non nécessaire
            if (sheets.get_cell(feuille, row_idx, "relance_j2").strip() != "No necesaria"):
                updates["relance_j2"] = "No necesaria"
            # On ne modifie PAS l'état (col L) ni les états manuels.
            if updates:
                sheets.update_cells(feuille, row_idx, updates)

            # Étape 11 : recherche > 1 an -> message doux spécial
            meses = analysis.get("tiempo_busqueda_meses", 0)
            if meses and meses > config.LONG_SEARCH_THRESHOLD_MONTHS:
                if stats.get("dry_run"):
                    print(f"[DRY RUN] whatsapp ignoré pour {phone} (búsqueda larga)")
                    stats["wa_dry_skipped"] += 1
                    db.mark_processed(ids)
                    continue
                body = whatsapp.build_long_search(p.get("nombre", ""))
                ok, info = whatsapp.send_message(phone, body)
                if ok:
                    # message doux sans URL d'annonce : on met juste à jour col J
                    sheets.update_cells(feuille, row_idx, {
                        "ultimo_mensaje": _today().isoformat(),
                    })
                    stats["wa_long_search_sent"] += 1
                else:
                    stats["details"].append(f"❌ Msg búsqueda larga échoué {phone}: {info}")

            db.mark_processed(ids)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"Réponse {phone}: {e}")
            # on ne marque PAS traité pour réessayer au prochain run


# ---------------------------------------------------------------------------
# Étapes 12 à 13 : relances J+2 et clôtures 7 jours
# ---------------------------------------------------------------------------
def process_relances_and_closures(stats):
    today = _today()
    for feuille in sheets.list_all_prospect_sheets():
        for row_idx, p in sheets.iter_prospects(feuille):
            try:
                phone = db.normalize_phone(p.get("telefono", ""))
                estado = (p.get("estado_final") or "").strip()
                fecha = _parse_date(p.get("fecha_contacto"))
                if not phone or not fecha:
                    continue

                # États manuels : ne JAMAIS les toucher
                if estado in config.MANUAL_STATES:
                    continue

                relance = (p.get("relance_j2") or "").strip()

                # a-t-il répondu ? (au moins un message reçu après le 1er contact)
                replied = bool(db.get_messages_for_phone(phone, since=_date_to_ts(fecha)))
                if replied:
                    # col K : relance non nécessaire
                    if relance != "No necesaria":
                        sheets.update_cells(feuille, row_idx, {"relance_j2": "No necesaria"})
                    continue

                days = (today - fecha).days

                # Clôture après 7 jours sans réponse (depuis un état "ouvert")
                if days >= config.NO_REPLY_CLOSE_DAYS and estado in config.AUTO_CLOSE_FROM \
                        and estado != "Sin respuesta - 7d":
                    sheets.update_cells(feuille, row_idx, {"estado_final": "Sin respuesta - 7d"})
                    stats["closed_7d"] += 1
                    continue

                # Relance J+2 : seulement si col K == "Pendiente" (1er message déjà envoyé)
                if days >= config.RELANCE_DELAY_DAYS and relance == "Pendiente":
                    # URL obligatoire : sans URL on n'envoie pas, on alerte.
                    url = sheets.get_iad_url_for_sheet(feuille)
                    if not url:
                        stats["alerts"].append(
                            f"[ALERTA] No se pudo enviar WhatsApp a {p.get('nombre') or '?'} "
                            f"({phone}) — URL manquante pour la feuille {feuille}. "
                            f"Vérifier l'onglet Config."
                        )
                        continue
                    if stats.get("dry_run"):
                        print(f"[DRY RUN] whatsapp ignoré pour {phone} (relance J+2)")
                        stats["wa_dry_skipped"] += 1
                        continue
                    body = whatsapp.build_relance(p.get("nombre", ""), url)
                    ok, info = whatsapp.send_message(phone, body)
                    if ok:
                        sheets.update_cells(feuille, row_idx, {
                            "relance_j2": "Enviada",            # col K
                            "ultimo_mensaje": today.isoformat(),  # col J
                            "estado_final": "No responde",        # col L
                        })
                        stats["relances_sent"] += 1
                    else:
                        stats["details"].append(f"❌ Relance échouée {phone}: {info}")
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"Relance {feuille} L{row_idx}: {e}")


# ---------------------------------------------------------------------------
# Étape 14 : rapport email
# ---------------------------------------------------------------------------
def _esc(v):
    return _html.escape(str(v if v is not None else ""))


def _resolve_bien(ref):
    """Tente de retrouver (feuille, url IAD) pour une réponse Idealista via la ref."""
    try:
        feuille, url = sheets.match_lead_to_sheet({"ref": ref, "url": ""})
        return feuille, url
    except Exception:  # noqa: BLE001
        return None, None


def _build_text_report(stats, now):
    lines = [
        f"Rapport CRM IAD — {now.strftime('%d/%m/%Y %H:%M')}"
        + ("  [DRY RUN]" if stats.get("dry_run") else ""),
        "=" * 50,
        f"Mails supprimés (corbeille) : {stats['mails_deleted']}",
        f"Leads détectés (Gmail)      : {stats['leads_detected']}",
        f"  - non matchés Config      : {stats['leads_unmatched']}",
        f"Nouveaux prospects          : {stats['prospects_new']}",
        f"Prospects mis à jour        : {stats['prospects_updated']}",
        f"1ers messages WhatsApp      : {stats['wa_first_sent']}",
        f"Relances J+2 envoyées       : {stats['relances_sent']}",
        f"Réponses traitées (IA)      : {stats['replies_processed']}",
        f"Clôtures 'Sin respuesta-7d' : {stats['closed_7d']}",
        "",
    ]
    for r in stats.get("idealista_responses", []):
        lines.append(f"Respuesta Idealista: {r['nombre']} (ref {r['ref']})")
    for a in stats.get("alerts", []):
        lines.append(a)
    for e in stats.get("errors", []):
        lines.append("ERROR: " + e)
    for u in stats.get("unmatched", []):
        lines.append(f"No clasificado: {u['telefono']} — {u['portail']} — {u['raison']}")
    return "\n".join(lines)


def _section(title, color, inner):
    return (
        f'<div style="background:#fff;border-left:4px solid {color};border-radius:4px;'
        f'padding:16px 20px;margin:0 0 18px 0;box-shadow:0 1px 2px rgba(0,0,0,0.06);">'
        f'<h2 style="margin:0 0 12px 0;font-size:16px;color:{C_DARK};">{title}</h2>'
        f'{inner}</div>'
    )


def _build_html_report(stats, now):
    A = report_assets
    # --- Résumé (tableau 2 colonnes) ---
    rows = [
        ("✅ Leads détectés", stats["leads_detected"]),
        ("✅ Nouveaux prospects", stats["prospects_new"]),
        ("✅ Prospects mis à jour", stats["prospects_updated"]),
        ("✅ WhatsApp envoyés", stats["wa_first_sent"]),
        ("✅ Relances J+2 envoyées", stats["relances_sent"]),
        ("🗑️ Mails supprimés", stats["mails_deleted"]),
        ("⚠️ Leads non matchés", stats["leads_unmatched"]),
    ]
    summary_rows = "".join(
        f'<tr>'
        f'<td style="padding:6px 8px;border-bottom:1px solid {C_GRAY_BG};font-size:14px;color:#333;">{lbl}</td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid {C_GRAY_BG};font-size:14px;color:{C_DARK};'
        f'font-weight:bold;text-align:right;">{val}</td></tr>'
        for lbl, val in rows
    )
    summary = _section(
        "📊 RÉSUMÉ DU RUN", C_DARK,
        f'<table style="width:100%;border-collapse:collapse;">{summary_rows}</table>',
    )

    parts = [summary]

    # --- Réponses Idealista, groupées par bien ---
    responses = stats.get("idealista_responses", [])
    if responses:
        groups = {}
        for r in responses:
            feuille, url = _resolve_bien(r["ref"])
            key = feuille or f"Ref {r['ref']}"
            groups.setdefault(key, {"url": url, "names": []})
            groups[key]["names"].append(r["nombre"])
        blocks = []
        for bien, data in groups.items():
            url = data["url"]
            link = (f'<a href="{_esc(url)}" style="color:{C_LIGHT};">{_esc(url)}</a>'
                    if url else '<span style="color:#999;">(annonce non liée)</span>')
            names = "".join(
                f'<div style="margin:2px 0 2px 16px;color:#444;font-size:13px;">'
                f'└ {_esc(n)} a répondu</div>' for n in data["names"]
            )
            blocks.append(
                f'<div style="margin:0 0 12px 0;">'
                f'<div style="font-size:14px;color:{C_DARK};font-weight:bold;">🏠 {_esc(bien)} — {link}</div>'
                f'{names}</div>'
            )
        parts.append(_section("📬 RÉPONSES IDEALISTA", C_LIGHT, "".join(blocks)))

    # --- Bugs et alertes ---
    alerts = stats.get("alerts", [])
    errors = stats.get("errors", [])
    if alerts or errors:
        items = "".join(
            f'<div style="font-size:13px;color:#444;margin:4px 0;">⚠️ {_esc(a)}</div>'
            for a in alerts
        ) + "".join(
            f'<div style="font-size:13px;color:{C_RED};margin:4px 0;">❌ {_esc(e)}</div>'
            for e in errors
        )
        parts.append(_section("🐛 BUGS ET ALERTES", C_ORANGE, items))

    # --- Leads non classifiés ---
    unmatched = stats.get("unmatched", [])
    if unmatched:
        items = "".join(
            f'<div style="font-size:13px;color:#444;margin:4px 0;">'
            f'<strong>{_esc(u["telefono"])}</strong> — {_esc(u["portail"])} — '
            f'Raison : {_esc(u["raison"])}</div>'
            for u in unmatched
        )
        parts.append(_section("📋 LEADS NON CLASSIFIÉS", C_RED, items))

    sections_html = "".join(parts)
    dry_badge = (f'<span style="background:{C_YELLOW};color:#333;padding:2px 8px;'
                 f'border-radius:4px;font-size:12px;font-weight:bold;">DRY RUN</span>'
                 if stats.get("dry_run") else "")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{C_GRAY_BG};font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" style="width:100%;background:{C_GRAY_BG};border-collapse:collapse;"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" style="width:600px;max-width:100%;border-collapse:collapse;">

  <!-- En-tête -->
  <tr><td style="background:#fff;border-radius:8px 8px 0 0;padding:24px;text-align:center;">
    <img src="{A.LOGO_DATA_URI}" width="120" alt="IAD" style="display:block;margin:0 auto 12px auto;">
    <div style="font-size:24px;font-weight:bold;color:{C_DARK};">CRM IAD COMP</div>
    <div style="font-size:13px;color:#888;margin-top:4px;">
      Rapport automatique — {now.strftime('%d/%m/%Y')} à {now.strftime('%H:%M')} {dry_badge}
    </div>
  </td></tr>

  <!-- Bouton principal -->
  <tr><td style="background:#fff;padding:0 24px 20px 24px;text-align:center;">
    <a href="{A.GOOGLE_SHEET_URL}" style="display:inline-block;background:{C_ORANGE};color:#fff;
      text-decoration:none;padding:12px 22px;border-radius:6px;font-size:15px;font-weight:bold;">
      📊 Ouvrir le tableau des acquéreurs</a>
  </td></tr>

  <!-- Sections -->
  <tr><td style="padding:20px 24px;">{sections_html}</td></tr>

  <!-- Pied de page -->
  <tr><td style="background:{C_GRAY_BG};border-radius:0 0 8px 8px;padding:20px 24px;text-align:center;">
    <hr style="border:none;border-top:1px solid #ddd;margin:0 0 14px 0;">
    <div style="font-size:15px;font-weight:bold;color:{C_DARK};">El Francés Inmobiliaria</div>
    <div style="font-size:13px;color:#555;margin-top:4px;">Thibaut MONTALAT — thibaut.montalat@iadespana.es</div>
    <div style="font-size:11px;color:#999;margin-top:8px;">Ce rapport est généré automatiquement par votre CRM IAD</div>
  </td></tr>

</table></td></tr></table></body></html>"""


def send_report(stats):
    now = datetime.datetime.now()
    text_body = _build_text_report(stats, now)
    subject = "Rapport CRM IAD" + (" [DRY RUN]" if stats.get("dry_run") else "")
    try:
        html_body = _build_html_report(stats, now)
    except Exception as e:  # noqa: BLE001
        print(f"[report] construction HTML échouée: {e}")
        html_body = None
    try:
        gmail_reader.send_email(config.REPORT_EMAIL, subject, text_body, html_body=html_body)
    except Exception as e:  # noqa: BLE001
        print(f"[report] envoi email échoué: {e}\n{text_body}")


# ---------------------------------------------------------------------------
# Point d'entrée du pipeline
# ---------------------------------------------------------------------------
def run(dry_run=False, full_scan=False):
    db.init_db()
    started = time.time()
    stats = {
        "dry_run": bool(dry_run), "full_scan": bool(full_scan),
        "mails_deleted": 0,
        "leads_detected": 0, "leads_unmatched": 0,
        "prospects_new": 0, "prospects_updated": 0,
        "wa_first_sent": 0, "wa_failed": 0, "wa_long_search_sent": 0,
        "wa_dry_skipped": 0,
        "relances_sent": 0, "closed_7d": 0,
        "replies_processed": 0, "replies_unmatched": 0,
        "details": [], "errors": [], "idealista_responses": [], "alerts": [],
        "unmatched": [],
    }
    sheets.reset_cache()
    print(f"[pipeline] démarrage {datetime.datetime.now().isoformat()} "
          f"(dry_run={dry_run}, full_scan={full_scan})")
    try:
        process_new_leads(stats)
        process_replies(stats)
        process_relances_and_closures(stats)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"FATAL: {e}\n{traceback.format_exc()}")

    send_report(stats)
    db.set_last_run_ts(started)
    db.set_last_reply_check_ts(started)
    # Enrichit les réponses Idealista avec le bien + URL (pour le dashboard)
    for r in stats.get("idealista_responses", []):
        bien, url = _resolve_bien(r.get("ref", ""))
        r["bien"] = bien or f"Ref {r.get('ref', '')}"
        r["url"] = url or ""
    # Stocke un résumé du dernier run (consultable via /status, sans email)
    summary = {k: v for k, v in stats.items() if k not in ("details", "errors")}
    summary["details"] = stats["details"][:30]
    summary["errors"] = stats["errors"][:20]
    summary["finished_at"] = datetime.datetime.now().isoformat()
    summary["duration_s"] = round(time.time() - started, 1)
    try:
        db.set_state("last_run_stats", json.dumps(summary, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    print(f"[pipeline] terminé en {time.time() - started:.1f}s — {summary}")
    return stats


if __name__ == "__main__":
    run()
