"""
Webhook Flask que recibe las respuestas de WhatsApp de UltraMsg.
A configurar en UltraMsg: Settings -> Webhook URL -> https://<tu-app>.railway.app/webhook
"""
from flask import Blueprint, request, jsonify

import database as db

webhook_bp = Blueprint("webhook", __name__)


@webhook_bp.route("/webhook", methods=["POST", "GET"])
def ultramsg_webhook():
    """
    Punto de entrada del webhook de UltraMsg para los mensajes de WhatsApp.

    Maneja dos métodos HTTP:
      - GET: responde a la verificación/ping de UltraMsg con {"status": "ok"}.
      - POST: procesa el evento entrante de WhatsApp. Filtra los mensajes
        salientes (enviados por nosotros), los eventos que no son de mensaje y
        las conversaciones de grupo; guarda en SQLite solo las respuestas
        individuales recibidas (@c.us).

    Devuelve siempre una respuesta JSON con un campo "status" y un código HTTP
    (200 en todos los casos), indicando lo que se ha hecho con el evento:
    "ok", "ignored_outgoing", "ignored_event", "no_phone", "ignored_group",
    "ignored_non_individual" o "saved" (con el id del mensaje guardado).
    """
    if request.method == "GET":
        # Verificación / ping: UltraMsg comprueba que la URL responde
        return jsonify({"status": "ok"}), 200

    # Lee el cuerpo de la petición: primero como JSON y, si no, como formulario
    payload = request.get_json(silent=True) or request.form.to_dict() or {}

    # UltraMsg envía un evento de tipo "message_received" con un objeto "data".
    # Detecta el tipo de evento y extrae el objeto de datos (o usa el payload completo).
    event_type = payload.get("event_type") or payload.get("type") or ""
    data = payload.get("data") or payload

    # Ignora los mensajes salientes (los que enviamos nosotros mismos)
    from_me = str(data.get("fromMe", data.get("self", "false"))).lower() in ("true", "1")
    if from_me:
        return jsonify({"status": "ignored_outgoing"}), 200

    # Solo procesamos los eventos de mensaje; descartamos cualquier otro evento
    if event_type and "message" not in event_type.lower():
        return jsonify({"status": "ignored_event", "event": event_type}), 200

    # Extrae el identificador del remitente (from_id) y el texto del mensaje (body)
    from_id = data.get("from") or data.get("author") or data.get("chatId") or ""
    body = data.get("body") or data.get("text") or ""
    msg_type = data.get("type", "")

    # Registra el from_id en bruto para verificar el formato exacto que envía UltraMsg
    print(f"[webhook] from_id reçu: '{from_id}' (type={msg_type})")

    if not from_id:
        return jsonify({"status": "no_phone"}), 200

    # Ignora los mensajes de grupo (@g.us); solo trata las conversaciones individuales (@c.us)
    if "@g.us" in from_id:
        print(f"[webhook] message de groupe ignoré: {from_id}")
        return jsonify({"status": "ignored_group"}), 200
    # Descarta cualquier from_id que no sea individual (sin marcador @c.us)
    if "@c.us" not in from_id:
        print(f"[webhook] from_id non individuel ignoré: {from_id}")
        return jsonify({"status": "ignored_non_individual"}), 200

    # Inicializa la base de datos y guarda el mensaje entrante en SQLite
    db.init_db()
    msg_id = db.save_incoming_message(phone=from_id, body=body, raw=payload)
    print(f"[webhook] réponse reçue de {from_id} (type={msg_type}) id={msg_id}: {body[:80]}")

    return jsonify({"status": "saved", "id": msg_id}), 200
