"""
Envío de mensajes de WhatsApp a través de UltraMsg + plantillas de mensajes (en español).

Este módulo se encarga de:
- Construir el texto de los mensajes a partir de plantillas en español
  (primer contacto, seguimiento J+2 y mensaje para búsquedas largas).
- Limpiar el nombre del prospecto antes de inyectarlo en el mensaje.
- Normalizar el número de teléfono para que UltraMsg pueda marcarlo.
- Enviar el mensaje individual mediante la API de UltraMsg.

REGLA FUNDAMENTAL: cada prospecto recibe UN mensaje individual.
Nunca se envía un mensaje grupal ni de difusión.
"""
import re
import random
import requests

import config
from database import normalize_phone

# Fragmentos típicos del ASUNTO de un correo (nunca el nombre real de un prospecto).
# Sirven para detectar y descartar textos que parecen un asunto de email
# en lugar de un nombre de persona. CADENAS LITERALES: no modificar su contenido.
_SUBJECT_MARKERS = (
    "sobre tu inmueble", "nuevo mensaje", "contacto para", "con ref",
    "referencia", "respuesta de", "en compra", "en venta", "de fotocasa",
    "de habitaclia", "de idealista", "llamada", "anuncio",
)

# ---------------------------------------------------------------------------
# Plantillas de mensajes
# ---------------------------------------------------------------------------
# Plantillas de PRIMER CONTACTO (se elige una al azar). Mensajes para el cliente:
# NO modificar su contenido (ni una palabra, ni un emoji).
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

# Plantillas de SEGUIMIENTO a los 2 días (se elige una al azar). Mensajes para el
# cliente: NO modificar su contenido (ni una palabra, ni un emoji).
RELANCE_J2 = [
    "Hola {nombre}, solo un pequeño seguimiento 👋 ¿Sigues interesado/a en este inmueble? {url}",
    ("Hola {nombre}, ¿has tenido ocasión de pensar en este inmueble? {url}\n\n"
     "Estoy aquí si tienes alguna pregunta 😊"),
    ("Hola {nombre}, te escribo de nuevo por este inmueble: {url}\n\n"
     "Sin compromiso — si ya no te interesa, solo dímelo y no te molesto más 🙏"),
]

# Mensaje para prospectos con BÚSQUEDA LARGA (lleva mucho tiempo buscando).
# Mensaje para el cliente: NO modificar su contenido (ni una palabra, ni un emoji).
LONG_SEARCH_MESSAGE = (
    "Hola {nombre} 😊 Llevas ya bastante tiempo buscando... A veces pasa que algo frena "
    "la decisión sin que nos demos cuenta. ¿Hay algo en particular que hasta ahora no ha "
    "terminado de convencerte? Quizás puedo ayudarte a verlo desde otro ángulo 🏠"
)


def _name_or_fallback(nombre):
    """
    Limpia el nombre antes de inyectarlo en el mensaje de WhatsApp.

    NUNCA devuelve un asunto de correo: si el nombre está vacío, es sospechoso,
    es demasiado largo o se parece a un asunto/URL/email, se recurre al nombre
    de reserva (config.FALLBACK_NAME, p. ej. 'vecino/a').

    Parámetros:
        nombre (str | None): nombre en bruto del prospecto, posiblemente sucio
            (puede contener un asunto de correo, una URL, cifras, etc.).

    Devuelve:
        str: el nombre limpio si es válido, o config.FALLBACK_NAME en caso
            contrario.
    """
    name = (nombre or "").strip()
    if not name:
        return config.FALLBACK_NAME
    # Corta en el primer separador (salto de línea, coma, punto y coma, dos
    # puntos, barra vertical o barra) y conserva solo la primera parte (el nombre).
    name = re.split(r"[\n,;:|/]", name)[0]
    # Colapsa cualquier secuencia de espacios en blanco a un solo espacio y
    # elimina espacios y signos de puntuación sobrantes al principio y al final.
    name = re.sub(r"\s+", " ", name).strip(" .,-—·\t")
    if not name:
        return config.FALLBACK_NAME
    low = name.lower()
    # Rechaza emails (contiene '@'), URLs ('http') o cualquier cifra: nada de
    # eso forma parte de un nombre real, así que se usa el nombre de reserva.
    if "@" in name or "http" in low or any(ch.isdigit() for ch in name):
        return config.FALLBACK_NAME
    # Rechaza el texto si contiene algún marcador típico de asunto de correo:
    # significa que es un asunto y no un nombre de persona.
    if any(marker in low for marker in _SUBJECT_MARKERS):
        return config.FALLBACK_NAME
    # Un nombre real = como máximo 4 palabras y no demasiado largo (<= 40
    # caracteres); si lo supera, probablemente sea una frase y no un nombre.
    if len(name) > 40 or len(name.split()) > 4:
        return config.FALLBACK_NAME
    return name


def build_first_contact(nombre, url):
    """
    Construye un mensaje de PRIMER CONTACTO.

    Elige una plantilla al azar de FIRST_CONTACT y la rellena con el nombre
    limpio del prospecto y la URL del inmueble.

    Parámetros:
        nombre (str | None): nombre en bruto del prospecto (se limpia con
            _name_or_fallback).
        url (str | None): enlace al inmueble; si es None se sustituye por "".

    Devuelve:
        str: el texto del mensaje listo para enviar.
    """
    return random.choice(FIRST_CONTACT).format(nombre=_name_or_fallback(nombre), url=url or "")


def build_relance(nombre, url):
    """
    Construye un mensaje de SEGUIMIENTO (J+2).

    Elige una plantilla al azar de RELANCE_J2 y la rellena con el nombre limpio
    del prospecto y la URL del inmueble.

    Parámetros:
        nombre (str | None): nombre en bruto del prospecto (se limpia con
            _name_or_fallback).
        url (str | None): enlace al inmueble; si es None se sustituye por "".

    Devuelve:
        str: el texto del mensaje listo para enviar.
    """
    return random.choice(RELANCE_J2).format(nombre=_name_or_fallback(nombre), url=url or "")


def build_long_search(nombre):
    """
    Construye el mensaje para prospectos con BÚSQUEDA LARGA.

    Usa la plantilla LONG_SEARCH_MESSAGE y la rellena con el nombre limpio del
    prospecto. Esta plantilla no incluye URL.

    Parámetros:
        nombre (str | None): nombre en bruto del prospecto (se limpia con
            _name_or_fallback).

    Devuelve:
        str: el texto del mensaje listo para enviar.
    """
    return LONG_SEARCH_MESSAGE.format(nombre=_name_or_fallback(nombre))


# ---------------------------------------------------------------------------
# Normalización del número para el envío (sin modificar lo que está almacenado)
# ---------------------------------------------------------------------------
def to_dialable(phone):
    """
    Prepara un número de teléfono para UltraMsg.

    Añade el prefijo de España (34) a los números de 9 cifras que empiezan por
    6, 7 o 9 (móviles y fijos españoles sin prefijo internacional). Los números
    que ya llevan prefijo se dejan tal cual.

    Parámetros:
        phone (str | None): número de teléfono en bruto.

    Devuelve:
        str: el número listo para marcar (solo dígitos), con prefijo 34 si
            procede. Puede ser una cadena vacía si el número no es válido.
    """
    digits = normalize_phone(phone)
    # Número español de 9 cifras que empieza por 6/7/9 (móvil o fijo nacional):
    # se le antepone el prefijo internacional de España (34).
    if len(digits) == 9 and digits[0] in "679":
        return "34" + digits
    return digits


# ---------------------------------------------------------------------------
# Envío mediante UltraMsg
# ---------------------------------------------------------------------------
def send_message(phone, body):
    """
    Envía un mensaje de WhatsApp INDIVIDUAL a un único destinatario.

    Normaliza el número con to_dialable y lo envía mediante una petición POST a
    la API de UltraMsg. Nunca envía mensajes grupales.

    Parámetros:
        phone (str | None): número de teléfono del destinatario.
        body (str): texto del mensaje a enviar.

    Devuelve:
        tuple[bool, str]: (ok, info) donde 'ok' indica si el envío fue correcto
            e 'info' contiene la respuesta de la API o un mensaje de error.
    """
    phone_n = to_dialable(phone)
    if not phone_n:
        return False, "numéro vide/invalide"

    url = f"{config.ULTRAMSG_BASE_URL}/messages/chat"
    # Cuerpo (payload) de la petición a UltraMsg: token de autenticación,
    # destinatario y texto del mensaje.
    payload = {
        "token": config.ULTRAMSG_TOKEN,
        "to": phone_n,
        "body": body,
    }
    try:
        # Llamada POST a UltraMsg (form-data) con un tiempo máximo de 30 s.
        resp = requests.post(url, data=payload, timeout=30)
        # Solo se intenta parsear JSON si la respuesta lo es; si no, dict vacío.
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        # Éxito si la respuesta HTTP es correcta (resp.ok) Y la API confirma el
        # envío: data["sent"] == True/"true", o bien viene un "id" de mensaje.
        if resp.ok and (data.get("sent") in (True, "true") or "id" in data):
            return True, str(data)
        # En caso contrario se devuelve el código HTTP y los primeros 200
        # caracteres del cuerpo de la respuesta para diagnóstico.
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:  # noqa: BLE001
        # Cualquier error de red/excepción se captura y se devuelve como fallo.
        return False, f"exception: {e}"


# ---------------------------------------------------------------------------
# Flujo conversacional en 3 pasos — plantillas por tipo de inmueble
# ---------------------------------------------------------------------------
# PASO 1: primer contacto adaptado al tipo (3 variantes al azar). {nombre}{url}{ville}.
# Mensajes para el cliente: NO modificar su contenido.
FIRST_CONTACT_BY_TYPE = {
    "Terreno": [
        "Hola {nombre} 👋 Te escribo porque contactaste con nosotros por {url} — un terreno en {ville}. ¿Sigue siendo de tu interés?",
        "Hola {nombre} 😊 Vi que te interesó el terreno en {ville}: {url} ¿Sigues buscando terreno en la zona?",
        "Hola {nombre}, te contacto por el terreno en {ville}: {url} ¿Te sigue interesando?",
    ],
    "Casa": [
        "Hola {nombre} 👋 Te escribo porque contactaste con nosotros por {url} — una casa en {ville}. ¿Sigue siendo de tu interés?",
        "Hola {nombre} 😊 Vi que te interesó la casa en {ville}: {url} ¿Sigues buscando en la zona?",
        "Hola {nombre}, te contacto por la casa en {ville}: {url} ¿Te sigue interesando?",
    ],
    "Piso": [
        "Hola {nombre} 👋 Te escribo porque contactaste con nosotros por {url} — un piso en {ville}. ¿Sigue siendo de tu interés?",
        "Hola {nombre} 😊 Vi que te interesó el piso en {ville}: {url} ¿Sigues buscando piso en la zona?",
        "Hola {nombre}, te contacto por el piso en {ville}: {url} ¿Te sigue interesando?",
    ],
    "Parking": [
        "Hola {nombre} 👋 Te escribo porque contactaste con nosotros por {url} — un parking en {ville}. ¿Sigue siendo de tu interés?",
        "Hola {nombre} 😊 Vi que te interesó el parking en {ville}: {url} ¿Sigue disponible para ti?",
        "Hola {nombre}, te contacto por el parking en {ville}: {url} ¿Te sigue interesando?",
    ],
    "Local": [
        "Hola {nombre} 👋 Te escribo porque contactaste con nosotros por {url} — un local en {ville}. ¿Sigue siendo de tu interés?",
        "Hola {nombre} 😊 Vi que te interesó el local comercial en {ville}: {url} ¿Sigues buscando local en la zona?",
        "Hola {nombre}, te contacto por el local en {ville}: {url} ¿Te sigue interesando?",
    ],
}

# PASO 2: preguntas de cualificación, según el tipo. Mensajes para el cliente: NO modificar.
PASO2_TERRENO = (
    "¡Perfecto! Para ayudarte mejor con el terreno en {ville}, cuéntame un poco más 😊\n"
    "¿Para qué lo necesitas — construcción, inversión, agricultura? ¿Cuánto tiempo llevas "
    "buscando? ¿Tienes ya claro el presupuesto? ¿Has hablado con algún banco o bróker para la financiación?"
)
PASO2_CASA_PISO = (
    "¡Perfecto! Para ayudarte mejor con {type_bien} en {ville}, cuéntame un poco más 😊\n"
    "¿Cuántas habitaciones necesitas? ¿Cuánto tiempo llevas buscando? ¿Tienes ya claro el "
    "presupuesto? ¿Has hablado con algún banco o bróker para la financiación?"
)
PASO2_PARKING_LOCAL = (
    "¡Perfecto! Para ayudarte mejor, cuéntame un poco más 😊\n"
    "¿Es para uso personal o profesional? ¿Cuánto tiempo llevas buscando? ¿Tienes ya claro el presupuesto?"
)

# Mensaje del bróker (financiación). {BROKER_NAME}{BROKER_PHONE}. NO modificar el texto base.
BROKER_MESSAGE = (
    "¡Perfecto! Puedo ayudarte con la financiación 😊 Te recomiendo contactar directamente con "
    "mi bróker de confianza, se encargará de encontrarte la mejor financiación y te hará un "
    "estudio gratuito sin compromiso. Se llama {BROKER_NAME} y su número es: {BROKER_PHONE} 📞"
)


def build_first_contact_typed(nombre, url, ville, type_name):
    """Construye el mensaje de PRIMER CONTACTO (Paso 1) adaptado al tipo de inmueble.

    Elige una variante al azar de la lista del tipo (o las genéricas si el tipo
    no se reconoce) y la rellena con nombre, url y ciudad.
    """
    plantillas = FIRST_CONTACT_BY_TYPE.get(type_name) or FIRST_CONTACT
    return random.choice(plantillas).format(
        nombre=_name_or_fallback(nombre), url=url or "", ville=ville or "la zona")


def build_paso2(ville, type_code, type_name, article):
    """Construye el mensaje de cualificación (Paso 2) según el tipo de inmueble."""
    if type_code == "T":
        return PASO2_TERRENO.format(ville=ville or "la zona")
    if type_code in ("C", "P"):
        # type_bien = artículo + nombre en minúscula, p. ej. "la casa" / "el piso".
        type_bien = f"{article or 'el'} {(type_name or 'inmueble').lower()}"
        return PASO2_CASA_PISO.format(ville=ville or "la zona", type_bien=type_bien)
    if type_code in ("Pa", "L"):
        return PASO2_PARKING_LOCAL
    # Tipo desconocido: usa la plantilla casa/piso con un término genérico.
    return PASO2_CASA_PISO.format(ville=ville or "la zona", type_bien="el inmueble")


def build_broker_message(broker_name, broker_phone):
    """Construye el mensaje que recomienda al bróker de financiación."""
    return BROKER_MESSAGE.format(BROKER_NAME=broker_name, BROKER_PHONE=broker_phone)
