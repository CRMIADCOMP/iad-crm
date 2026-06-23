"""
Envoi de messages WhatsApp via UltraMsg + templates de messages (espagnol).
Chaque prospect reçoit UN message individuel. Jamais de message groupé.
"""
import random
import requests

import config
from database import normalize_phone

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
FIRST_CONTACT = [
    ("Hola {nombre} 👋 ¿Sigues interesado/a en este inmueble? {url}\n\n"
     "Si es así, me gustaría entender mejor tu proyecto. ¿Cuánto tiempo llevas buscando? "
     "¿Ya has hablado con un banco o un bróker para el tema de financiación?"),
    ("Hola {nombre}, te escribo por este inmueble: {url}\n\n"
     "¿Sigue siendo de tu interés? Me ayudaría saber un poco más sobre lo que buscas y desde cuándo. "
     "¿Tienes ya claro el tema de financiación o necesitas orientación? 🏠"),
    ("Hola {nombre} 😊 Solo quería saber si este inmueble sigue en tu radar: {url}\n\n"
     "Cuéntame un poco tu proyecto — ¿cuánto tiempo llevas buscando? "
     "¿Has consultado ya con algún banco o bróker?"),
]

RELANCE_J2 = [
    "Hola {nombre}, solo un pequeño seguimiento 👋 ¿Sigues interesado/a en este inmueble? {url}",
    ("Hola {nombre}, ¿has tenido ocasión de pensar en este inmueble? {url}\n\n"
     "Estoy aquí si tienes alguna pregunta 😊"),
    ("Hola {nombre}, te escribo de nuevo por este inmueble: {url}\n\n"
     "Sin compromiso — si ya no te interesa, solo dímelo y no te molesto más 🙏"),
]

LONG_SEARCH_MESSAGE = (
    "Hola {nombre} 😊 Llevas ya bastante tiempo buscando... A veces pasa que algo frena "
    "la decisión sin que nos demos cuenta. ¿Hay algo en particular que hasta ahora no ha "
    "terminado de convencerte? Quizás puedo ayudarte a verlo desde otro ángulo 🏠"
)


def _name_or_fallback(nombre):
    nombre = (nombre or "").strip()
    return nombre if nombre else config.FALLBACK_NAME


def build_first_contact(nombre, url):
    return random.choice(FIRST_CONTACT).format(nombre=_name_or_fallback(nombre), url=url or "")


def build_relance(nombre, url):
    return random.choice(RELANCE_J2).format(nombre=_name_or_fallback(nombre), url=url or "")


def build_long_search(nombre):
    return LONG_SEARCH_MESSAGE.format(nombre=_name_or_fallback(nombre))


# ---------------------------------------------------------------------------
# Envoi UltraMsg
# ---------------------------------------------------------------------------
def send_message(phone, body):
    """
    Envoie un message WhatsApp individuel. Renvoie (ok: bool, info: str).
    """
    phone_n = normalize_phone(phone)
    if not phone_n:
        return False, "numéro vide/invalide"

    url = f"{config.ULTRAMSG_BASE_URL}/messages/chat"
    payload = {
        "token": config.ULTRAMSG_TOKEN,
        "to": phone_n,
        "body": body,
    }
    try:
        resp = requests.post(url, data=payload, timeout=30)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.ok and (data.get("sent") in (True, "true") or "id" in data):
            return True, str(data)
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"exception: {e}"
