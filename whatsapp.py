"""
Envoi de messages WhatsApp via UltraMsg + templates de messages (espagnol).
Chaque prospect reçoit UN message individuel. Jamais de message groupé.
"""
import re
import random
import requests

import config
from database import normalize_phone

# Fragments typiques d'un SUJET de mail (jamais un vrai nom de prospect)
_SUBJECT_MARKERS = (
    "sobre tu inmueble", "nuevo mensaje", "contacto para", "con ref",
    "referencia", "respuesta de", "en compra", "en venta", "de fotocasa",
    "de habitaclia", "de idealista", "llamada", "anuncio",
)

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
    """
    Nettoie le nom avant injection dans le message WhatsApp.
    Ne renvoie JAMAIS un sujet de mail : si le nom est vide, suspect, trop long
    ou ressemble à un sujet/URL/email, on retombe sur 'vecino/a'.
    """
    name = (nombre or "").strip()
    if not name:
        return config.FALLBACK_NAME
    # coupe sur séparateurs (garde la partie nom)
    name = re.split(r"[\n,;:|/]", name)[0]
    name = re.sub(r"\s+", " ", name).strip(" .,-—·\t")
    if not name:
        return config.FALLBACK_NAME
    low = name.lower()
    # rejette emails, URLs, fragments de sujet, chiffres
    if "@" in name or "http" in low or any(ch.isdigit() for ch in name):
        return config.FALLBACK_NAME
    if any(marker in low for marker in _SUBJECT_MARKERS):
        return config.FALLBACK_NAME
    # un vrai nom = au plus 4 mots et pas trop long
    if len(name) > 40 or len(name.split()) > 4:
        return config.FALLBACK_NAME
    return name


def build_first_contact(nombre, url):
    return random.choice(FIRST_CONTACT).format(nombre=_name_or_fallback(nombre), url=url or "")


def build_relance(nombre, url):
    return random.choice(RELANCE_J2).format(nombre=_name_or_fallback(nombre), url=url or "")


def build_long_search(nombre):
    return LONG_SEARCH_MESSAGE.format(nombre=_name_or_fallback(nombre))


# ---------------------------------------------------------------------------
# Normalisation du numéro pour l'envoi (sans modifier ce qui est stocké)
# ---------------------------------------------------------------------------
def to_dialable(phone):
    """
    Prépare un numéro pour UltraMsg : ajoute l'indicatif Espagne (34) aux
    numéros à 9 chiffres commençant par 6/7/9. Les numéros déjà avec indicatif
    sont laissés tels quels.
    """
    digits = normalize_phone(phone)
    if len(digits) == 9 and digits[0] in "679":
        return "34" + digits
    return digits


# ---------------------------------------------------------------------------
# Envoi UltraMsg
# ---------------------------------------------------------------------------
def send_message(phone, body):
    """
    Envoie un message WhatsApp individuel. Renvoie (ok: bool, info: str).
    """
    phone_n = to_dialable(phone)
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
