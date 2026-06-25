"""
Orquestación del pipeline del CRM (se ejecuta a las 8h, 12h y 18h, hora de Madrid).

Este es el cerebro del sistema: encadena todas las etapas de tratamiento de leads,
respuestas, relances (seguimientos) y cierres, y al final envía un informe por email.

Etapas:
 1-3  Lectura de Gmail + detección/extracción de los leads
 4    Matching anuncio -> hoja vía la pestaña Config del Google Sheets
 5    Deduplicación (teléfono Y email)
 6    Inserción / actualización del prospecto
 7    Envío del primer mensaje de WhatsApp personalizado
 8-10 Tratamiento de las respuestas recibidas + análisis IA -> columnas F/G/H
 11   Mensaje especial si la búsqueda lleva > 1 año
 12   Relances (seguimientos) a J+2 (2 días)
 13   Cierre "Sin respuesta - 7d" tras 7 días sin respuesta
 14   Informe por email
"""
import time
import json
import datetime
import traceback

import os
import socket
import html as _html

import config
import database as db
import gmail_reader
import sheets_handler as sheets
import whatsapp
import ai_analyzer
import report_assets

# Paleta de colores corporativa IAD (usada en el informe HTML por email)
C_DARK = "#00628C"     # azul oscuro IAD
C_LIGHT = "#00b1eb"    # azul claro IAD
C_ORANGE = "#E87722"   # naranja (botones / acentos)
C_GRAY_BG = "#F5F5F5"  # fondo gris claro
C_GREEN = "#28A745"    # verde (métricas positivas)
C_RED = "#DC3545"      # rojo (errores / leads sin clasificar)
C_YELLOW = "#FFC107"   # amarillo (insignia "DRY RUN")


def _today():
    """Devuelve la fecha de hoy (datetime.date). Atajo usado en todo el pipeline."""
    return datetime.date.today()


def _parse_date(value):
    """
    Convierte un texto de fecha (de una celda del Sheets) en datetime.date.
    Acepta varios formatos: ISO (YYYY-MM-DD), DD/MM/YYYY y DD-MM-YYYY.
    Devuelve None si el valor está vacío o no coincide con ningún formato.
    """
    if not value:
        return None
    # Prueba cada formato hasta que uno funcione (recorta a los 10 primeros caracteres)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(value.strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def _date_to_ts(d):
    """Convierte una fecha (date) en timestamp Unix (segundos). Sirve para comparar
    la fecha de contacto con los timestamps de las respuestas guardadas en SQLite."""
    return time.mktime(datetime.datetime(d.year, d.month, d.day).timetuple())


def _url_alert(nombre, phone, feuille):
    """Construye el texto de alerta cuando falta la URL del anuncio para una hoja
    (sin URL no se puede enviar el WhatsApp). Devuelve la cadena de alerta."""
    return (f"[ALERTA] No se pudo enviar WhatsApp a {nombre or '?'} ({phone}) — "
            f"URL manquante pour la feuille {feuille}. Vérifier l'onglet Config.")


# ---------------------------------------------------------------------------
# Índice de prospectos (teléfono -> ubicación) para emparejar las respuestas
# ---------------------------------------------------------------------------
def _build_prospect_index():
    """
    Construye un índice {telefono_normalizado: (hoja, fila, datos_prospecto)}
    recorriendo TODAS las hojas de prospectos. Permite localizar rápidamente
    a qué prospecto pertenece una respuesta de WhatsApp entrante.
    Devuelve el diccionario índice.
    """
    index = {}
    for feuille in sheets.list_all_prospect_sheets():
        for row_idx, p in sheets.iter_prospects(feuille):
            # Normaliza el teléfono para usarlo como clave fiable (sin +, espacios, etc.)
            phone = db.normalize_phone(p.get("telefono", ""))
            if phone:
                index[phone] = (feuille, row_idx, p)
    return index


# ---------------------------------------------------------------------------
# Etapas 1 a 7: nuevos leads
# ---------------------------------------------------------------------------
def process_new_leads(stats):
    """
    Etapas 1 a 7 del pipeline: limpia los correos no deseados, lee Gmail, detecta
    los leads, los empareja con la hoja correspondiente (pestaña Config), deduplica,
    inserta/actualiza el prospecto y envía el primer mensaje de WhatsApp.
    `stats` es el diccionario de contadores/listas que se va rellenando in situ.
    No devuelve valor (modifica `stats`).
    """
    # Limpieza: envía automáticamente a la papelera los correos no deseados
    try:
        deleted = gmail_reader.delete_unwanted_mails()
        stats["mails_deleted"] = len(deleted)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"Gmail delete: {e}")

    # Full scan: ventana de 30 días, procesa TODO (after_ts=0); si no, ventana normal.
    if stats.get("full_scan"):
        last_run = 0
        fetch_kwargs = {"window": "30d"}
    else:
        # Modo normal: solo correos posteriores al último run (evita reprocesar)
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
            # Notificación de respuesta de Idealista: NO es un lead nuevo, solo se
            # añade al informe (mensajería interna del portal, sin teléfono/email).
            if lead.get("kind") == "respuesta_idealista":
                stats["idealista_responses"].append({
                    "nombre": lead.get("nombre") or "?",
                    "ref": lead.get("ref") or "?",
                })
                continue

            # Etapa 4: empareja el lead con una hoja según la URL/referencia del anuncio
            feuille, iad_url = sheets.match_lead_to_sheet(lead)
            matched = bool(feuille)

            if not matched:
                # Lead sin emparejar: se escribe igualmente en la hoja de repuesto
                # (FALLBACK_SHEET) para no perder nada y poder diagnosticar.
                stats["leads_unmatched"] += 1
                stats["unmatched"].append({
                    "telefono": lead.get("telefono") or lead.get("email") or "?",
                    "portail": lead.get("fuente", ""),
                    "raison": f"Ref/URL absente de l'onglet Config (ref={lead.get('ref') or '—'})",
                })
                feuille = config.FALLBACK_SHEET
                iad_url = ""

            # La hoja DEBE existir: el script NUNCA crea hojas automáticamente.
            if not sheets.worksheet_exists(feuille):
                stats["alerts"].append(
                    f"[ALERTA] Feuille '{feuille}' introuvable dans le Sheets — "
                    f"vérifier l'onglet Config"
                )
                continue

            # URL usada en el mensaje (anuncio IAD si está disponible; si no, la URL de origen)
            msg_url = iad_url or lead.get("url", "")
            lead["url"] = msg_url

            # Etapas 5 y 6: deduplica e inserta/actualiza el prospecto en la hoja
            row_idx, is_new, _ = sheets.upsert_prospect(feuille, lead)
            if row_idx is None:  # seguridad (la hoja desapareció entretanto)
                continue
            if is_new:
                stats["prospects_new"] += 1
            else:
                stats["prospects_updated"] += 1

            # No se envía WhatsApp a un lead sin emparejar (salvo override por config),
            # porque el mensaje referencia el anuncio y no hay una URL fiable.
            if not matched and not config.SEND_WHATSAPP_WHEN_UNMATCHED:
                # marca el estado "Sin clasificar" para excluirlo de relances/cierres automáticos
                if is_new:
                    sheets.update_cells(feuille, row_idx, {"estado_final": "Sin clasificar"})
                continue

            # Etapa 7: envío del 1er mensaje solo si el estado lo permite (SENDABLE_STATES)
            estado = sheets.get_cell(feuille, row_idx, "estado_final").strip()
            if estado not in config.SENDABLE_STATES:
                continue
            # ¿ya contactado? (un mensaje saliente ya habría fijado la columna J "último mensaje")
            already = sheets.get_cell(feuille, row_idx, "ultimo_mensaje").strip()
            if already:
                continue

            phone = lead.get("telefono", "")
            if not phone:
                stats["details"].append(f"⚠️ Pas de téléphone pour {lead.get('email')} ({feuille})")
                continue

            # URL obligatoria: sin URL del anuncio NO se envía nada, se genera una alerta.
            if not msg_url:
                stats["alerts"].append(
                    f"[ALERTA] No se pudo enviar WhatsApp a {lead.get('nombre') or '?'} "
                    f"({phone}) — URL manquante pour la feuille {feuille}. "
                    f"Vérifier l'onglet Config."
                )
                continue

            # En modo simulación (dry_run) NO se envía nada, solo se cuenta
            if stats.get("dry_run"):
                print(f"[DRY RUN] whatsapp ignoré pour {phone} (primer contacto, {feuille})")
                stats["wa_dry_skipped"] += 1
                continue

            # Mensaje de PRIMER CONTACTO (Paso 1) adaptado al tipo de inmueble.
            info_bien = config.parse_bien_info(feuille, db.get_city_names())
            body = whatsapp.build_first_contact_typed(
                lead.get("nombre", ""), msg_url, info_bien["city"], info_bien["type"])
            ok, info = whatsapp.send_message(phone, body)
            today = _today().isoformat()
            if ok:
                # Envío correcto: actualiza columnas J (último mensaje), K (relance) y L (estado)
                upd = {
                    "ultimo_mensaje": today,          # col J: se actualiza en cada envío
                    "relance_j2": "Pendiente",        # col K: relance pendiente a J+2
                    "estado_final": config.STATE_PASO1,  # col L: "WhatsApp enviado - Paso 1"
                }
                # col I: se rellena solo si está vacía (fecha del primer contacto)
                if not sheets.get_cell(feuille, row_idx, "fecha_contacto").strip():
                    upd["fecha_contacto"] = today
                sheets.update_cells(feuille, row_idx, upd)
                stats["wa_first_sent"] += 1
            else:
                # Fallo de envío -> estado "Error envío WA" (se reintentará en el próximo run)
                sheets.update_cells(feuille, row_idx, {"estado_final": config.ERROR_WA_STATE})
                stats["wa_failed"] += 1
                stats["details"].append(f"❌ Envoi WA échoué {phone}: {info}")
                print(f"[pipeline] échec envoi WA {phone} -> '{config.ERROR_WA_STATE}': {info}")
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"Lead {lead.get('telefono')}: {e}")


# ---------------------------------------------------------------------------
# Etapas 8 a 11: tratamiento de las respuestas
# ---------------------------------------------------------------------------
def process_replies(stats):
    """
    Etapas 8 a 11: procesa las respuestas de WhatsApp recibidas (guardadas en SQLite
    por el webhook). Las agrupa por número, las analiza con la IA (Claude Haiku),
    rellena las columnas F/G/H del prospecto y, si lleva más de 1 año buscando, le
    envía un mensaje especial. Modifica `stats` in situ; no devuelve valor.
    """
    # Recupera los mensajes aún no procesados; si no hay ninguno, termina
    msgs = db.get_unprocessed_messages()
    if not msgs:
        return
    # Agrupa los mensajes por número de teléfono
    by_phone = {}
    for m in msgs:
        by_phone.setdefault(m["phone"], []).append(m)

    # Índice teléfono -> ubicación del prospecto, para emparejar cada respuesta
    index = _build_prospect_index()

    for phone, messages in by_phone.items():
        ids = [m["id"] for m in messages]
        try:
            target = index.get(phone)
            if not target:
                # Respuesta de un número desconocido: se marca como procesada para no repetir
                db.mark_processed(ids)
                stats["replies_unmatched"] += 1
                continue

            feuille, row_idx, p = target
            bodies = [m["body"] for m in messages if m.get("body")]
            text = " ".join(bodies)
            estado = (sheets.get_cell(feuille, row_idx, "estado_final") or "").strip()
            stats["replies_processed"] += 1
            info_bien = config.parse_bien_info(feuille, db.get_city_names())
            today = _today().isoformat()

            # Estados manuales (incl. "Fuera") o perfil ya completado: no continuar el flujo.
            if estado in config.MANUAL_STATES or estado == config.STATE_COMPLETED:
                db.mark_processed(ids)
                continue

            # === PASO 2 respondido: analizar con IA -> F/G/H + bróker si hace falta ===
            if estado == config.STATE_PASO2:
                analysis = ai_analyzer.analyze_replies(bodies)
                updates = {}
                if analysis.get("presupuesto") and not sheets.get_cell(feuille, row_idx, "presupuesto").strip():
                    updates["presupuesto"] = analysis["presupuesto"]
                if analysis.get("tiempo_busqueda_texto") and not sheets.get_cell(feuille, row_idx, "tiempo_busqueda").strip():
                    updates["tiempo_busqueda"] = analysis["tiempo_busqueda_texto"]
                pago = analysis.get("pago_validado", "")
                if pago and not sheets.get_cell(feuille, row_idx, "pago_validado").strip():
                    updates["pago_validado"] = pago
                # Bróker: si no tiene financiación validada (rechazado / pendiente / en curso)
                needs_broker = pago in ("No - Rechazado", "Pendiente", "En curso")
                if needs_broker:
                    if stats.get("dry_run"):
                        print(f"[DRY RUN] whatsapp ignoré pour {phone} (bróker)")
                        stats["wa_dry_skipped"] += 1
                    else:
                        bname, bphone = db.get_broker()
                        ok, info = whatsapp.send_message(phone, whatsapp.build_broker_message(bname, bphone))
                        if ok:
                            updates["ultimo_mensaje"] = today
                            stats["wa_broker_sent"] += 1
                        else:
                            stats["details"].append(f"❌ Broker msg échoué {phone}: {info}")
                updates["estado_final"] = config.STATE_COMPLETED   # col L
                updates["relance_j2"] = "No necesaria"             # col K
                sheets.update_cells(feuille, row_idx, updates)
                stats["profiles_completed"] += 1
                db.mark_processed(ids)
                continue

            # === PASO 1 respondido (o estados abiertos): clasificar interés ===
            sentiment = ai_analyzer.classify_paso1(text)
            if sentiment == "negative":
                # Respuesta negativa -> "Fuera", no se envían más mensajes
                sheets.update_cells(feuille, row_idx, {
                    "estado_final": config.STATE_OUT, "relance_j2": "No necesaria"})
                stats["replies_negative"] += 1
                db.mark_processed(ids)
                continue
            # Positivo o indeterminado -> se trata como interesado: enviar Paso 2
            if stats.get("dry_run"):
                print(f"[DRY RUN] whatsapp ignoré pour {phone} (Paso 2)")
                stats["wa_dry_skipped"] += 1
                db.mark_processed(ids)
                continue
            body = whatsapp.build_paso2(
                info_bien["city"], info_bien["type_code"], info_bien["type"], info_bien["article"])
            ok, info = whatsapp.send_message(phone, body)
            if ok:
                upd = {"ultimo_mensaje": today, "relance_j2": "No necesaria",
                       "estado_final": config.STATE_PASO2}
                # col I: se rellena si está vacía
                if not sheets.get_cell(feuille, row_idx, "fecha_contacto").strip():
                    upd["fecha_contacto"] = today
                sheets.update_cells(feuille, row_idx, upd)
                stats["wa_paso2_sent"] += 1
            else:
                sheets.update_cells(feuille, row_idx, {"estado_final": config.ERROR_WA_STATE})
                stats["details"].append(f"❌ Paso2 échoué {phone}: {info}")
            db.mark_processed(ids)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"Réponse {phone}: {e}")
            # NO se marca como procesada para reintentar en el próximo run


# ---------------------------------------------------------------------------
# Etapas 12 a 13: relances (seguimientos) a J+2 y cierres a los 7 días
# ---------------------------------------------------------------------------
def process_relances_and_closures(stats):
    """
    Etapas 12 y 13: recorre TODOS los prospectos de TODAS las hojas y, según su
    estado y la fecha de contacto, reintenta envíos fallidos, manda el seguimiento
    a J+2 (RELANCE_DELAY_DAYS) o cierra el prospecto en "Sin respuesta - 7d"
    (NO_REPLY_CLOSE_DAYS) si no ha respondido. Modifica `stats`; no devuelve valor.
    """
    today = _today()
    for feuille in sheets.list_all_prospect_sheets():
        for row_idx, p in sheets.iter_prospects(feuille):
            try:
                phone = db.normalize_phone(p.get("telefono", ""))
                estado = (p.get("estado_final") or "").strip()
                nombre = p.get("nombre", "")
                today_s = today.isoformat()
                if not phone:
                    continue

                # Estados manuales (Visita apuntada/hecha, Fuera): NUNCA se tocan
                if estado in config.MANUAL_STATES:
                    continue

                # --- Reintento de los envíos fallidos (estado "Error envío WA") ---
                if estado == config.ERROR_WA_STATE:
                    url = sheets.get_iad_url_for_sheet(feuille)
                    if not url:
                        stats["alerts"].append(_url_alert(nombre, phone, feuille))
                        continue
                    if stats.get("dry_run"):
                        print(f"[DRY RUN] whatsapp ignoré pour {phone} (retry Error WA)")
                        stats["wa_dry_skipped"] += 1
                        continue
                    info_bien = config.parse_bien_info(feuille, db.get_city_names())
                    ok, info = whatsapp.send_message(
                        phone,
                        whatsapp.build_first_contact_typed(nombre, url, info_bien["city"], info_bien["type"]))
                    if ok:
                        upd = {"ultimo_mensaje": today_s, "relance_j2": "Pendiente",
                               "estado_final": config.STATE_PASO1}
                        if not (p.get("fecha_contacto") or "").strip():
                            upd["fecha_contacto"] = today_s
                        sheets.update_cells(feuille, row_idx, upd)
                        stats["wa_retry_ok"] += 1
                        print(f"[pipeline] retry envoi OK {phone} -> Paso 1")
                    else:
                        stats["wa_retry_fail"] += 1
                        print(f"[pipeline] retry envoi ÉCHEC (#{stats['wa_retry_fail']}) {phone}: {info}")
                    continue

                relance = (p.get("relance_j2") or "").strip()
                fecha = _parse_date(p.get("fecha_contacto"))

                # --- Caso especial: Paso 1 enviado pero fecha de contacto VACÍA -> relance ---
                if estado in (config.STATE_PASO1, "WhatsApp enviado") and not fecha:
                    url = sheets.get_iad_url_for_sheet(feuille)
                    if not url:
                        stats["alerts"].append(_url_alert(nombre, phone, feuille))
                        continue
                    if stats.get("dry_run"):
                        print(f"[DRY RUN] whatsapp ignoré pour {phone} (relance sans fecha)")
                        stats["wa_dry_skipped"] += 1
                        continue
                    ok, info = whatsapp.send_message(phone, whatsapp.build_relance(nombre, url))
                    if ok:
                        sheets.update_cells(feuille, row_idx, {
                            "fecha_contacto": today_s, "ultimo_mensaje": today_s,
                            "relance_j2": "Enviada", "estado_final": "No responde"})
                        stats["relances_sent"] += 1
                        print(f"[pipeline] prospect {nombre or phone} sans fecha contacto -> relance envoyée")
                    else:
                        stats["details"].append(f"❌ Relance échouée {phone}: {info}")
                    continue

                if not fecha:
                    continue

                # ¿ha respondido? (al menos un mensaje recibido después del 1er contacto)
                replied = bool(db.get_messages_for_phone(phone, since=_date_to_ts(fecha)))
                if replied:
                    # col K: el seguimiento ya no es necesario
                    if relance != "No necesaria":
                        sheets.update_cells(feuille, row_idx, {"relance_j2": "No necesaria"})
                    continue

                # Días transcurridos desde la fecha de primer contacto
                days = (today - fecha).days

                # Cierre tras 7 días sin respuesta (solo desde un estado "abierto")
                if days >= config.NO_REPLY_CLOSE_DAYS and estado in config.AUTO_CLOSE_FROM \
                        and estado != "Sin respuesta - 7d":
                    sheets.update_cells(feuille, row_idx, {"estado_final": "Sin respuesta - 7d"})
                    stats["closed_7d"] += 1
                    continue

                # Relance a J+2: solo si la col K == "Pendiente" (1er mensaje ya enviado)
                if days >= config.RELANCE_DELAY_DAYS and relance == "Pendiente":
                    url = sheets.get_iad_url_for_sheet(feuille)
                    if not url:
                        stats["alerts"].append(_url_alert(nombre, phone, feuille))
                        continue
                    if stats.get("dry_run"):
                        print(f"[DRY RUN] whatsapp ignoré pour {phone} (relance J+2)")
                        stats["wa_dry_skipped"] += 1
                        continue
                    ok, info = whatsapp.send_message(phone, whatsapp.build_relance(nombre, url))
                    if ok:
                        sheets.update_cells(feuille, row_idx, {
                            "relance_j2": "Enviada",            # col K
                            "ultimo_mensaje": today_s,            # col J
                            "estado_final": "No responde",        # col L
                        })
                        stats["relances_sent"] += 1
                    else:
                        stats["details"].append(f"❌ Relance échouée {phone}: {info}")
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"Relance {feuille} L{row_idx}: {e}")


# ---------------------------------------------------------------------------
# Etapa 14: informe por email
# ---------------------------------------------------------------------------
def _esc(v):
    """Escapa un valor para insertarlo de forma segura en HTML. Devuelve la cadena escapada."""
    return _html.escape(str(v if v is not None else ""))


def _resolve_bien(ref):
    """Intenta recuperar (hoja, url IAD) para una respuesta de Idealista a partir
    de la referencia del anuncio. Devuelve (hoja, url) o (None, None) si falla."""
    try:
        feuille, url = sheets.match_lead_to_sheet({"ref": ref, "url": ""})
        return feuille, url
    except Exception:  # noqa: BLE001
        return None, None


def _build_text_report(stats, now):
    """Construye la versión en TEXTO PLANO del informe (resumen de contadores,
    respuestas Idealista, alertas, errores y leads sin clasificar). Devuelve la cadena."""
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
    # Envuelve un bloque de contenido HTML en una "tarjeta" con título y color de borde.
    # Devuelve el HTML de la sección. NO modificar el contenido de la cadena.
    return (
        f'<div style="background:#fff;border-left:4px solid {color};border-radius:4px;'
        f'padding:16px 20px;margin:0 0 18px 0;box-shadow:0 1px 2px rgba(0,0,0,0.06);">'
        f'<h2 style="margin:0 0 12px 0;font-size:16px;color:{C_DARK};">{title}</h2>'
        f'{inner}</div>'
    )


def _build_html_report(stats, now):
    # Construye la versión HTML del informe por email (con logo, métricas, secciones
    # de respuestas Idealista, alertas/errores y leads sin clasificar). Devuelve el HTML.
    # ATENCIÓN: las plantillas/cadenas HTML de abajo NO deben modificarse.
    A = report_assets
    # --- Resumen (tabla de 2 columnas) ---
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

    # --- Respuestas de Idealista, agrupadas por inmueble (bien) ---
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

    # --- Bugs y alertas ---
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

    # --- Leads sin clasificar ---
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
    """Construye (versión texto + HTML) y envía el informe del run por email vía
    Gmail. Parámetro `stats`: contadores del run. Devuelve (ok: bool, message: str)."""
    now = datetime.datetime.now()
    text_body = _build_text_report(stats, now)
    subject = "Rapport CRM IAD" + (" [DRY RUN]" if stats.get("dry_run") else "")
    try:
        html_body = _build_html_report(stats, now)
    except Exception as e:  # noqa: BLE001
        print(f"[report] construction HTML échouée: {e}")
        html_body = None
    print(f"[rapport] tentative envoi à {config.REPORT_EMAIL}...")
    try:
        gmail_reader.send_email(config.REPORT_EMAIL, subject, text_body, html_body=html_body)
        print("[rapport] envoi OK")
        return True, "Rapport envoyé"
    except Exception as e:  # noqa: BLE001
        print(f"[rapport] ERREUR: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Punto de entrada del pipeline
# ---------------------------------------------------------------------------
def run(dry_run=False, full_scan=False, replies_only=False):
    """
    Punto de entrada del pipeline. Inicializa la BD, crea el diccionario `stats`
    (todos los contadores a 0), ejecuta las etapas en orden (nuevos leads ->
    respuestas -> relances/cierres), envía el informe y guarda un resumen del run.
    Parámetros:
      - dry_run: si True, simula sin enviar WhatsApp reales.
      - full_scan: si True, escanea los últimos 30 días en lugar de la ventana normal.
    Devuelve el diccionario `stats` del run.
    """
    db.init_db()
    started = time.time()
    stats = {
        "dry_run": bool(dry_run), "full_scan": bool(full_scan),
        "replies_only": bool(replies_only),
        "mails_deleted": 0,
        "leads_detected": 0, "leads_unmatched": 0,
        "prospects_new": 0, "prospects_updated": 0,
        "wa_first_sent": 0, "wa_failed": 0, "wa_long_search_sent": 0,
        "wa_dry_skipped": 0, "wa_retry_ok": 0, "wa_retry_fail": 0,
        "wa_paso2_sent": 0, "wa_broker_sent": 0, "profiles_completed": 0, "replies_negative": 0,
        "relances_sent": 0, "closed_7d": 0,
        "replies_processed": 0, "replies_unmatched": 0,
        "details": [], "errors": [], "idealista_responses": [], "alerts": [],
        "unmatched": [],
    }
    sheets.reset_cache()
    print(f"[pipeline] démarrage {datetime.datetime.now().isoformat()} "
          f"(dry_run={dry_run}, full_scan={full_scan}, replies_only={replies_only}) "
          f"pid={os.getpid()} host={socket.gethostname()}")
    try:
        if replies_only:
            # Runs de 10h/15h: SOLO respuestas + actualización de columnas.
            # No se lee Gmail ni se envían primeros contactos ni relances/cierres.
            process_replies(stats)
        else:
            # Runs completos (8h/12h/18h)
            process_new_leads(stats)
            process_replies(stats)
            process_relances_and_closures(stats)
            # Sincroniza descripción Config + cabecera de navegación (fila 1) + ref N1.
            try:
                sheets.sync_sheets(stats)
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"sync_sheets: {e}")
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
