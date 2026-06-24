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

import config
import database as db
import gmail_reader
import sheets_handler as sheets
import whatsapp
import ai_analyzer


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
    last_run = db.get_last_run_ts()
    try:
        leads = gmail_reader.fetch_new_leads(last_run)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"Gmail: {e}")
        return
    stats["leads_detected"] = len(leads)

    for lead in leads:
        try:
            # Notification de réponse Idealista : pas un nouveau lead, juste au rapport.
            if lead.get("kind") == "respuesta_idealista":
                nom = lead.get("nombre") or "?"
                ref = lead.get("ref") or "?"
                stats["idealista_responses"].append(
                    f"El prospecto {nom} ha respondido en Idealista (ref: {ref})"
                )
                continue

            feuille, iad_url = sheets.match_lead_to_sheet(lead)
            matched = bool(feuille)

            if not matched:
                # Lead non matché : on l'écrit quand même dans la feuille de repli
                # pour ne rien perdre (et pouvoir diagnostiquer).
                stats["leads_unmatched"] += 1
                stats["details"].append(
                    f"⚠️ Lead non matché ({lead.get('fuente')}) -> '{config.FALLBACK_SHEET}': "
                    f"{lead.get('telefono') or lead.get('email')} ref={lead.get('ref')}"
                )
                feuille = config.FALLBACK_SHEET
                iad_url = ""

            # URL utilisée dans le message (annonce IAD si dispo, sinon l'URL source)
            msg_url = iad_url or lead.get("url", "")
            lead["url"] = msg_url

            row_idx, is_new, _ = sheets.upsert_prospect(feuille, lead)
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

            body = whatsapp.build_first_contact(lead.get("nombre", ""), msg_url)
            ok, info = whatsapp.send_message(phone, body)
            today = _today().isoformat()
            if ok:
                sheets.update_cells(feuille, row_idx, {
                    "fecha_contacto": today,
                    "ultimo_mensaje": today,
                    "estado_final": "WhatsApp enviado",
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

            updates = {}
            if analysis.get("presupuesto"):
                updates["presupuesto"] = analysis["presupuesto"]
            if analysis.get("tiempo_busqueda_texto"):
                updates["tiempo_busqueda"] = analysis["tiempo_busqueda_texto"]
            if analysis.get("pago_validado"):
                updates["pago_validado"] = analysis["pago_validado"]
            updates["estado_final"] = "Respondió"
            sheets.update_cells(feuille, row_idx, updates)

            # Étape 11 : recherche > 1 an -> message doux spécial
            meses = analysis.get("tiempo_busqueda_meses", 0)
            if meses and meses > config.LONG_SEARCH_THRESHOLD_MONTHS:
                body = whatsapp.build_long_search(p.get("nombre", ""))
                ok, info = whatsapp.send_message(phone, body)
                if ok:
                    sheets.update_cells(feuille, row_idx, {
                        "ultimo_mensaje": _today().isoformat(),
                        "estado_final": "Respondió - búsqueda larga",
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

                # a-t-il répondu ? (au moins un message reçu après le 1er contact)
                replied = bool(db.get_messages_for_phone(phone, since=_date_to_ts(fecha)))
                if replied:
                    continue
                if estado not in config.SENDABLE_STATES:
                    continue

                days = (today - fecha).days

                # Clôture après 7 jours sans réponse
                if days >= config.NO_REPLY_CLOSE_DAYS:
                    sheets.update_cells(feuille, row_idx, {"estado_final": "Sin respuesta - 7d"})
                    stats["closed_7d"] += 1
                    continue

                # Relance J+2 (une seule fois)
                if days >= config.RELANCE_DELAY_DAYS and not (p.get("relance_j2") or "").strip():
                    # URL de l'annonce relue depuis l'onglet Config (Notas contient le message)
                    url = sheets.get_iad_url_for_sheet(feuille)
                    body = whatsapp.build_relance(p.get("nombre", ""), url)
                    ok, info = whatsapp.send_message(phone, body)
                    if ok:
                        sheets.update_cells(feuille, row_idx, {
                            "relance_j2": today.isoformat(),
                            "ultimo_mensaje": today.isoformat(),
                        })
                        stats["relances_sent"] += 1
                    else:
                        stats["details"].append(f"❌ Relance échouée {phone}: {info}")
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"Relance {feuille} L{row_idx}: {e}")


# ---------------------------------------------------------------------------
# Étape 14 : rapport email
# ---------------------------------------------------------------------------
def send_report(stats):
    now = datetime.datetime.now()
    lines = [
        f"Rapport CRM IAD — {now.strftime('%d/%m/%Y %H:%M')} (heure serveur)",
        "=" * 50,
        f"Leads détectés (Gmail)      : {stats['leads_detected']}",
        f"  - non matchés Config      : {stats['leads_unmatched']}",
        f"Nouveaux prospects          : {stats['prospects_new']}",
        f"Prospects mis à jour        : {stats['prospects_updated']}",
        f"1ers messages WhatsApp      : {stats['wa_first_sent']}",
        f"Relances J+2 envoyées       : {stats['relances_sent']}",
        f"Messages 'búsqueda larga'   : {stats['wa_long_search_sent']}",
        f"Réponses traitées (IA)      : {stats['replies_processed']}",
        f"  - réponses inconnues      : {stats['replies_unmatched']}",
        f"Clôtures 'Sin respuesta-7d' : {stats['closed_7d']}",
        f"Échecs d'envoi WhatsApp     : {stats['wa_failed']}",
        "",
    ]
    if stats.get("idealista_responses"):
        lines.append("Respuestas en Idealista (mensajería interna) :")
        lines.extend("  " + r for r in stats["idealista_responses"][:60])
        lines.append("")
    if stats["details"]:
        lines.append("Détails :")
        lines.extend("  " + d for d in stats["details"][:60])
        lines.append("")
    if stats["errors"]:
        lines.append("Erreurs :")
        lines.extend("  " + e for e in stats["errors"][:40])

    body = "\n".join(lines)
    try:
        gmail_reader.send_email(config.REPORT_EMAIL, "Rapport CRM IAD", body)
    except Exception as e:  # noqa: BLE001
        print(f"[report] envoi email échoué: {e}\n{body}")


# ---------------------------------------------------------------------------
# Point d'entrée du pipeline
# ---------------------------------------------------------------------------
def run():
    db.init_db()
    started = time.time()
    stats = {
        "leads_detected": 0, "leads_unmatched": 0,
        "prospects_new": 0, "prospects_updated": 0,
        "wa_first_sent": 0, "wa_failed": 0, "wa_long_search_sent": 0,
        "relances_sent": 0, "closed_7d": 0,
        "replies_processed": 0, "replies_unmatched": 0,
        "details": [], "errors": [], "idealista_responses": [],
    }
    sheets.reset_cache()
    print(f"[pipeline] démarrage {datetime.datetime.now().isoformat()}")
    try:
        process_new_leads(stats)
        process_replies(stats)
        process_relances_and_closures(stats)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"FATAL: {e}\n{traceback.format_exc()}")

    send_report(stats)
    db.set_last_run_ts(started)
    db.set_last_reply_check_ts(started)
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
