"""
Webhook Flask recevant les réponses WhatsApp d'UltraMsg.
À configurer dans UltraMsg : Settings -> Webhook URL -> https://<ton-app>.railway.app/webhook
"""
from flask import Blueprint, request, jsonify

import database as db

webhook_bp = Blueprint("webhook", __name__)


@webhook_bp.route("/webhook", methods=["POST", "GET"])
def ultramsg_webhook():
    if request.method == "GET":
        # Vérification / ping
        return jsonify({"status": "ok"}), 200

    payload = request.get_json(silent=True) or request.form.to_dict() or {}

    # UltraMsg envoie un événement de type "message_received" avec un objet "data".
    event_type = payload.get("event_type") or payload.get("type") or ""
    data = payload.get("data") or payload

    # Ignore les messages sortants (envoyés par nous)
    from_me = str(data.get("fromMe", data.get("self", "false"))).lower() in ("true", "1")
    if from_me:
        return jsonify({"status": "ignored_outgoing"}), 200

    # On ne traite que les messages entrants
    if event_type and "message" not in event_type.lower():
        return jsonify({"status": "ignored_event", "event": event_type}), 200

    phone = data.get("from") or data.get("author") or data.get("chatId") or ""
    body = data.get("body") or data.get("text") or ""
    msg_type = data.get("type", "")

    # On ne stocke que les messages texte/chat avec un numéro
    if not phone:
        return jsonify({"status": "no_phone"}), 200

    db.init_db()
    msg_id = db.save_incoming_message(phone=phone, body=body, raw=payload)
    print(f"[webhook] réponse reçue de {phone} (type={msg_type}) id={msg_id}: {body[:80]}")

    return jsonify({"status": "saved", "id": msg_id}), 200
