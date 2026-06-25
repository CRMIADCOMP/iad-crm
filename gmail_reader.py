"""
Lectura de los emails de Gmail y extracción de los leads de Idealista / Fotocasa / Habitaclia.

Este es el módulo de parsing más complejo del CRM: se conecta a Gmail vía la
API, descarga los correos recientes de los portales inmobiliarios y, para cada
uno, intenta reconstruir un "lead" estructurado (nombre, teléfono, email,
mensaje, referencia y URL del anuncio). Cada portal usa un formato de correo
distinto (e incluso varios formatos por portal), por lo que el módulo combina
expresiones regulares, parsing línea por línea y heurísticas de respaldo.

Autenticación: OAuth (token.json generado por setup_auth.py).
En Railway, credentials.json y token.json se reconstruyen a partir de
las variables de entorno GMAIL_CREDENTIALS / GMAIL_TOKEN (base64) vía config.py.
"""
import re
import base64
import datetime
from email.utils import parsedate_to_datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config

# Scopes (permisos) OAuth solicitados a Gmail:
#   - gmail.modify: lectura de los correos Y movimiento a la papelera (trash()).
#     'modify' ya cubre la lectura, por eso no se pide 'gmail.readonly'.
#   - gmail.send: envío del email de reporte (función send_email).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Diccionario de firmas (dominios / palabras clave) que permiten atribuir una
# fuente (portal) a un email cuando no se reconoce por la dirección del remitente.
# Se usa como respaldo en _detect_source.
SOURCE_SIGNATURES = {
    "Idealista": ["idealista.com", "idealista"],
    "Fotocasa": ["fotocasa.es", "fotocasa"],
    "Habitaclia": ["habitaclia.com", "habitaclia"],
}


# Bandera de módulo: garantiza que los scopes solo se registren (log) una vez
# por proceso, aunque _get_service() se llame muchas veces.
_scopes_logged = False


def _get_service():
    """Construye y devuelve el cliente autenticado de la API de Gmail.

    Reconstruye (vía config) las rutas de credentials.json y token.json,
    carga las credenciales OAuth desde token.json con los SCOPES requeridos,
    registra una sola vez los scopes solicitados frente a los concedidos,
    refresca el token si ha caducado y lo reescribe en disco.

    Devuelve:
        Un recurso de servicio Gmail v1 (objeto build) listo para usar.
    """
    global _scopes_logged
    # Reconstruye credentials.json en disco si hace falta (no se usa el retorno,
    # solo el efecto secundario de materializar el fichero).
    config.gmail_credentials_path()
    token_path = config.gmail_token_path()
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not _scopes_logged:
        # --- Verificación y log de los scopes OAuth (solo la primera vez) ---
        _scopes_logged = True
        # Scopes realmente concedidos por el token (pueden diferir de los pedidos).
        granted = getattr(creds, "scopes", None)
        print(f"[gmail] scopes demandés (code): {SCOPES}")
        print(f"[gmail] scopes accordés (token): {granted}")
        # Comprueba si el token permite enviar correo: basta con que incluya
        # gmail.send, gmail.modify o el scope total mail.google.com.
        can_send = granted and any(
            s in granted for s in (
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://mail.google.com/",
            )
        )
        # Si el token no autoriza el envío, avisa de que hay que regenerarlo.
        print(f"[gmail] envoi autorisé par le token: {can_send}"
              + ("" if can_send else " -> RÉGÉNÉRER token.json avec setup_auth.py !"))
    # Refresco del token: si está caducado pero tiene refresh_token, se renueva
    # contra Google y el nuevo token se persiste de vuelta en token.json.
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    # cache_discovery=False evita warnings/escrituras de caché en entornos como Railway.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(to_addr, subject, body_text, html_body=None):
    """Envía un email desde la cuenta de Gmail configurada.

    Parámetros:
        to_addr (str): dirección del destinatario.
        subject (str): asunto del correo.
        body_text (str): cuerpo en texto plano (siempre se incluye).
        html_body (str | None): cuerpo HTML opcional. Si se proporciona, el
            correo se envía como multipart/alternative (texto + HTML); si no,
            se envía solo el texto plano.

    Devuelve:
        dict: la respuesta de la API de Gmail (incluye 'id' y 'threadId').

    Lanza:
        Exception: propaga cualquier error de la llamada a la API.
    """
    from email.mime.text import MIMEText
    print("[rapport] construction du message HTML..." if html_body
          else "[rapport] construction du message texte...")
    service = _get_service()
    print(f"[rapport] expéditeur: {config.GMAIL_ADDRESS}")
    print(f"[rapport] destinataire: {to_addr}")
    if html_body:
        # --- Construcción del email MIME multipart (alternativa texto + HTML) ---
        # 'alternative' indica al cliente de correo que muestre la versión más
        # rica que soporte: primero se adjunta el texto plano (respaldo) y
        # luego el HTML (preferido), respetando el orden de prioridad MIME.
        from email.mime.multipart import MIMEMultipart
        mime = MIMEMultipart("alternative")
        mime.attach(MIMEText(body_text, "plain", _charset="utf-8"))
        mime.attach(MIMEText(html_body, "html", _charset="utf-8"))
    else:
        # Sin HTML: mensaje simple de texto plano.
        mime = MIMEText(body_text, _charset="utf-8")
    # Cabeceras del mensaje.
    mime["to"] = to_addr
    mime["from"] = config.GMAIL_ADDRESS
    mime["subject"] = subject
    # Gmail API exige el mensaje completo codificado en base64 url-safe.
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    print("[rapport] appel Gmail API users().messages().send()...")
    try:
        resp = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        print(f"[rapport] réponse API: {{id: {resp.get('id')}, threadId: {resp.get('threadId')}}}")
        print("[rapport] email envoyé avec succès")
        return resp
    except Exception as e:  # noqa: BLE001
        print(f"[rapport] ERREUR: {e}")
        raise


def _decode_part(part):
    """Decodifica el contenido de una parte MIME de un mensaje de Gmail.

    Parámetros:
        part (dict): una parte del payload del mensaje (con clave 'body'/'data').

    Devuelve:
        str: el texto decodificado desde base64 url-safe (UTF-8, reemplazando
        los caracteres inválidos), o cadena vacía si la parte no tiene datos.
    """
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_body(payload):
    """Devuelve el texto plano + HTML concatenados de un mensaje.

    Recorre recursivamente el árbol de partes MIME del payload y acumula el
    contenido de todas las partes de tipo text/plain y text/html.

    Parámetros:
        payload (dict): el payload del mensaje devuelto por la API de Gmail.

    Devuelve:
        str: todas las partes textuales unidas por saltos de línea.
    """
    texts = []

    # Función recursiva que desciende por todas las sub-partes del árbol MIME.
    def walk(p):
        mime = p.get("mimeType", "")
        if mime in ("text/plain", "text/html"):
            texts.append(_decode_part(p))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(texts)


def _strip_html(html):
    """Convierte un cuerpo HTML en texto plano legible preservando la estructura.

    Es clave para el parsing posterior: las funciones de extracción trabajan
    línea por línea, así que conservar los saltos de línea reales es importante.

    Parámetros:
        html (str): el cuerpo HTML del correo.

    Devuelve:
        str: texto plano, una "frase"/dato por línea, sin líneas vacías.
    """
    # Elimina por completo los bloques <script>/<style> y su contenido (.*?)
    # para que el JS/CSS no contamine el texto.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    # Convierte cada <br> en un salto de línea real.
    html = re.sub(r"(?i)<\s*br\s*/?>", "\n", html)
    # Cierre de etiquetas de bloque (</p>, </div>, </tr>, </li>, </h1..6>,
    # </td>, </table>) -> salto de línea, para preservar la estructura visual.
    html = re.sub(r"(?i)</\s*(p|div|tr|li|h[1-6]|td|table)\s*>", "\n", html)
    # Elimina cualquier otra etiqueta restante (<...>), reemplazándola por espacio.
    text = re.sub(r"<[^>]+>", " ", html)
    # Decodifica las entidades HTML más comunes.
    text = re.sub(r"&nbsp;", " ", text)   # espacio no separable -> espacio
    text = re.sub(r"&amp;", "&", text)     # &amp; -> &
    # Cualquier otra entidad (&aacute;, &#39;, etc.) -> espacio.
    text = re.sub(r"&#?\w+;", " ", text)
    # Limpia cada línea (colapsa espacios/tabuladores) pero conserva los saltos
    # de línea, eliminando luego las líneas que quedan vacías.
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _detect_source(sender, subject, body):
    """Determina de qué portal procede un correo (la "fuente" del lead).

    Parámetros:
        sender (str): cabecera From del correo.
        subject (str): asunto del correo.
        body (str): cuerpo del correo ya convertido a texto plano.

    Devuelve:
        str | None: "Idealista", "Fotocasa", "Habitaclia", la fuente mapeada en
        config.PORTAL_SENDERS, o None si no se reconoce ninguna fuente.
    """
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()

    # Caso especial: Fotocasa y Habitaclia comparten el mismo remitente
    # (cliente@fotocasa.pro), así que no se pueden distinguir por la dirección.
    # Se desambigua buscando la palabra "habitaclia" en el asunto o el cuerpo;
    # si no aparece, se asume Fotocasa.
    if "cliente@fotocasa.pro" in sender_l:
        haystack = subject_l + " " + (body or "").lower()
        if "habitaclia" in haystack:
            return "Habitaclia"
        return "Fotocasa"

    # Coincidencia por dirección de remitente conocida (config.PORTAL_SENDERS).
    for addr, source in config.PORTAL_SENDERS.items():
        if addr.lower() in sender_l:
            return source

    # Respaldo: búsqueda por palabras clave / dominios en remitente + asunto + cuerpo.
    haystack = (sender_l + " " + subject_l + " " + body).lower()
    for source, sigs in SOURCE_SIGNATURES.items():
        if any(sig in haystack for sig in sigs):
            return source
    return None


# --- Expresiones regulares de parsing (constantes load-bearing: NO modificar) ---
# RE_EMAIL: captura una dirección de email estándar (usuario@dominio.tld),
# admitiendo puntos, guiones y los caracteres habituales en la parte local.
RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# RE_PHONE: captura un teléfono español (móvil/fijo que empieza por 6/7/8/9),
# con prefijo internacional +34 opcional y separadores (espacio, punto, guion).
RE_PHONE = re.compile(r"(?:\+?34[\s.\-]?)?(?:[6789]\d{2}[\s.\-]?\d{3}[\s.\-]?\d{3})")
# RE_URL: captura una URL http(s) de idealista/fotocasa/habitaclia (con www opcional)
# hasta el primer espacio, comilla o carácter de cierre.
RE_URL = re.compile(r"https?://(?:www\.)?(?:idealista|fotocasa|habitaclia)\.[a-z.]+/[^\s\"'>)]+", re.I)
# RE_REF: captura la referencia del anuncio precedida de "ref"/"referencia"
# (con punto, ':' o '#' opcionales). El grupo 1 es el código (>=4 alfanuméricos/guion).
RE_REF = re.compile(r"(?:ref(?:erencia)?\.?\s*[:#]?\s*)([A-Za-z0-9\-]{4,})", re.I)
# RE_NUM_IN_URL: captura un bloque de >=6 dígitos tras una barra '/' dentro de
# una URL (el identificador numérico del anuncio).
RE_NUM_IN_URL = re.compile(r"/(\d{6,})")
# RE_NAME: captura el nombre del contacto cuando va precedido de una etiqueta
# ("nombre", "name", "nom", "contacto", "cliente", "interesado/a") seguida de ':'/'-'.
# El grupo 1 admite letras (incl. acentuadas), apóstrofos, puntos y espacios (2-40 car.).
RE_NAME = re.compile(
    r"(?:nombre|name|nom|contacto|cliente|interesad[oa])\s*[:\-]\s*([A-Za-zÀ-ÿ'’\.\s]{2,40})",
    re.I,
)


# --- Parsing de los cuerpos de los correos -----------------------------------
# Estructura del cuerpo Idealista: "...espera tu respuesta [NOMBRE] [TEL] [EMAIL] [MENSAJE]"
# RE_IDEALISTA_MARKER: localiza el marcador "espera tu respuesta", a partir del
# cual empieza el bloque de datos del contacto en los correos de Idealista.
RE_IDEALISTA_MARKER = re.compile(r"espera tu respuesta", re.I)
# RE_SUBJECT_REF: captura la referencia en el asunto de Fotocasa/Habitaclia,
# p. ej. "... con referencia 926287 - De ...". El grupo 1 es el código.
RE_SUBJECT_REF = re.compile(r"con referencia\s+([A-Za-z0-9\-]+)", re.I)
# RE_IDEALISTA_SUBJECT_NAME: captura el nombre en el asunto de Idealista,
# p. ej. "Nuevo mensaje de [NOMBRE] sobre tu inmueble". El grupo 1 es el nombre.
RE_IDEALISTA_SUBJECT_NAME = re.compile(r"nuevo mensaje de\s+(.+?)\s+sobre tu inmueble", re.I)
# RE_IDEALISTA_SUBJECT_REF: captura la referencia en el asunto de Idealista,
# p. ej. "con ref: 802931". El grupo 1 empieza por dígito (la ref del anuncio).
RE_IDEALISTA_SUBJECT_REF = re.compile(r"con\s+ref\.?:?\s*(\d[A-Za-z0-9\-]*)", re.I)
# RE_RESPUESTA_DE: detecta el asunto "respuesta de [NOMBRE] sobre tu inmueble",
# que es una notificación de respuesta en la mensajería interna de Idealista,
# NO un lead nuevo. El grupo 1 es el nombre.
RE_RESPUESTA_DE = re.compile(r"^\s*respuesta de\s+(.+?)\s+sobre tu inmueble", re.I)
# RE_PHONE_RUN: captura una secuencia de dígitos separados por espacios (al
# menos 8 cifras en total), con un posible '+' inicial de prefijo internacional.
# Se usa como respaldo cuando el teléfono no está en su propia línea.
RE_PHONE_RUN = re.compile(r"\+?\d[\d\s]{6,}\d")


def _clean_phone(raw):
    """Normaliza un teléfono dejando únicamente sus dígitos.

    Elimina espacios, '+', puntos, guiones y cualquier carácter no numérico.

    Parámetros:
        raw: el teléfono en bruto (cualquier valor convertible a str).

    Devuelve:
        str: solo los dígitos del número.
    """
    return "".join(ch for ch in str(raw) if ch.isdigit())


def _is_phone_line(line):
    """Indica si una línea contiene únicamente un número de teléfono.

    Una línea se considera "de teléfono" si no incluye ninguna letra y, una
    vez limpiada, tiene al menos 7 dígitos.

    Parámetros:
        line (str): la línea de texto a evaluar.

    Devuelve:
        bool: True si la línea es solo un teléfono, False en caso contrario.
    """
    if any(ch.isalpha() for ch in line):
        return False
    return len(_clean_phone(line)) >= 7


def _extract_idealista_lead(text):
    """Extrae los datos de contacto de un correo de Idealista.

    Realiza un parsing línea por línea del bloque que sigue al marcador
    'espera tu respuesta', cuya estructura típica es:
        [NOMBRE] / [TELÉFONO] / [EMAIL] / [MENSAJE...]

    Parámetros:
        text (str): el texto del correo (asunto + cuerpo en texto plano).

    Devuelve:
        tuple[str, str, str, str]: (nombre, telefono, email, message), cada uno
        ya recortado; los que no se encuentren quedan como cadena vacía.
    """
    # Localiza el marcador y se queda con el segmento que va a continuación;
    # si no hay marcador, se analiza todo el texto.
    m = RE_IDEALISTA_MARKER.search(text)
    segment = text[m.end():] if m else text

    # Divide el segmento en líneas no vacías y limpias (si el marcador termina
    # la línea, este troceo nos lleva directamente a la línea siguiente).
    lines = [ln.strip() for ln in segment.splitlines()]
    lines = [ln for ln in lines if ln]

    name = phone = email = message = ""
    email_idx = None

    # Nombre = primera línea que no es ni un email ni un teléfono.
    name_idx = None
    for i, ln in enumerate(lines):
        if RE_EMAIL.search(ln) or _is_phone_line(ln):
            continue
        name = ln
        name_idx = i
        break

    # Teléfono = primera línea "de teléfono" buscando a partir de la del nombre.
    start = (name_idx + 1) if name_idx is not None else 0
    for i in range(start, len(lines)):
        if _is_phone_line(lines[i]):
            phone = _clean_phone(lines[i])
            break

    # Email = primera línea que contenga una dirección de correo.
    for i, ln in enumerate(lines):
        em = RE_EMAIL.search(ln)
        if em:
            email = em.group(0).strip()
            email_idx = i
            break

    # Mensaje = todo lo que aparezca en las líneas posteriores al email.
    if email_idx is not None and email_idx + 1 < len(lines):
        message = " ".join(lines[email_idx + 1:]).strip()

    # Respaldo si la estructura no es la esperada (p. ej. todo en una sola línea):
    # se busca la última secuencia de dígitos como teléfono y/o el primer email.
    if not phone:
        runs = list(RE_PHONE_RUN.finditer(segment))
        if runs:
            phone = _clean_phone(runs[-1].group(0))
    if not email:
        em = RE_EMAIL.search(segment)
        if em:
            email = em.group(0).strip()

    return name.strip(), phone.strip(), email.strip(), message.strip()


# --- Parsing Fotocasa / Habitaclia (3 tipos de correos) ----------------------
def _label_value(text, label_pattern):
    """Devuelve el valor que sigue a una etiqueta "label:" en el texto.

    Parámetros:
        text (str): el texto donde buscar.
        label_pattern (str): patrón regex de la etiqueta (p. ej. r"Nombre").

    Devuelve:
        str: el contenido tras "label:" hasta el final de la línea, recortado,
        o cadena vacía si la etiqueta no aparece.
    """
    m = re.search(label_pattern + r"\s*:\s*(.+)", text, re.I)
    return m.group(1).strip() if m else ""


def _extract_fotocasa_habitaclia(subject, full):
    """Extrae el lead de un correo de Fotocasa/Habitaclia (3 formatos posibles).

    Estos portales envían tres tipos de correo distintos:
      - TIPO 1: mensaje escrito por el interesado (misma estructura que Idealista).
      - TIPO 2: aviso de llamada telefónica recibida (con teléfono y duración).
      - TIPO 3: contacto estructurado con etiquetas Nombre/Teléfono/Email/Mensaje.

    Parámetros:
        subject (str): asunto del correo.
        full (str): asunto + cuerpo en texto plano.

    Devuelve:
        dict: {nombre, telefono, email, message, ref}.
    """
    low = full.lower()

    # Referencia del anuncio: se busca primero en el asunto (TIPO 1) y, si no,
    # en el cuerpo (TIPO 3).
    ref = ""
    mref = RE_SUBJECT_REF.search(subject) or RE_SUBJECT_REF.search(full)
    if mref:
        ref = mref.group(1).strip()

    # TIPO 2 — llamada telefónica: se identifica por las frases típicas y se
    # extraen teléfono y duración para anotarlos como mensaje.
    if "datos de la llamada" in low or "has recibido una llamada" in low:
        phone = _clean_phone(_label_value(full, r"Tel[eé]fono"))
        duracion = _label_value(full, r"Duraci[oó]n")
        notas = ("Llamada recibida " + duracion).strip()
        return {"nombre": "", "telefono": phone, "email": "", "message": notas, "ref": ref}

    # TIPO 3 — contacto estructurado: se reconoce por "nuevo contacto de" o por
    # la presencia conjunta de las etiquetas "nombre:" y "email:".
    if "nuevo contacto de" in low or ("nombre:" in low and "email:" in low):
        nombre = _label_value(full, r"Nombre")
        phone = _clean_phone(_label_value(full, r"Tel[eé]fono"))
        email_raw = _label_value(full, r"Email")
        # El valor de Email puede traer texto extra; se aísla la dirección real.
        em = RE_EMAIL.search(email_raw)
        email = em.group(0).strip() if em else email_raw.strip()
        mensaje = _label_value(full, r"Mensaje")
        # Descarta el mensaje si es "no especificado" o está vacío.
        if mensaje.strip().lower().startswith("no especificado") or not mensaje.strip():
            mensaje = ""
        return {"nombre": nombre.strip(), "telefono": phone, "email": email,
                "message": mensaje.strip(), "ref": ref}

    # TIPO 1 — mensaje escrito: se reutiliza el parser de Idealista por compartir
    # la misma estructura línea por línea.
    name, phone, email, message = _extract_idealista_lead(full)
    return {"nombre": name, "telefono": phone, "email": email, "message": message, "ref": ref}


def _clean_name(raw):
    """Limpia y valida un nombre extraído del cuerpo del correo.

    Recorta signos de puntuación, corta el nombre en cuanto aparece una
    palabra "parásita" frecuente (p. ej. "te ha", "ha contactado", "email")
    y colapsa los espacios. Si el resultado parece una frase entera (más de
    5 palabras), lo descarta.

    Parámetros:
        raw (str): el nombre en bruto.

    Devuelve:
        str: el nombre limpio, o cadena vacía si no es válido.
    """
    if not raw:
        return ""
    name = raw.strip().strip(".,;:")
    # Corta en cuanto aparece una palabra parásita habitual que indica que el
    # nombre ya terminó y empieza otra cosa (verbo, etiqueta de email/teléfono...).
    name = re.split(r"\b(te ha|ha contactado|está interesad|email|tel|phone|móvil)\b", name, flags=re.I)[0]
    name = re.sub(r"\s+", " ", name).strip()
    # Rechaza el resultado si parece una frase completa en lugar de un nombre.
    if len(name.split()) > 5:
        return ""
    return name


def _extract_lead(source, body, sender_email):
    """Extrae los campos de un lead desde el cuerpo del correo (parser genérico).

    Es la ruta de respaldo usada cuando la fuente no es Idealista/Fotocasa/
    Habitaclia, basada únicamente en las expresiones regulares genéricas.

    Parámetros:
        source (str): la fuente/portal detectado.
        body (str): asunto + cuerpo en texto plano.
        sender_email (str): dirección del remitente (no usada actualmente).

    Devuelve:
        dict: {nombre, telefono, email, fuente, url, ref, raw_excerpt}.
    """
    # Recoge los emails del cuerpo descartando los de los propios portales y
    # las direcciones noreply/no-reply (no son del interesado).
    emails = [e for e in RE_EMAIL.findall(body)
              if not any(d in e.lower() for d in ("idealista", "fotocasa", "habitaclia", "noreply", "no-reply"))]
    phones = RE_PHONE.findall(body)
    urls = RE_URL.findall(body)

    # Referencia: se intenta por etiqueta "ref" y, si no, por el número de la URL.
    ref = ""
    m = RE_REF.search(body)
    if m:
        ref = m.group(1)
    elif urls:
        mnum = RE_NUM_IN_URL.search(urls[0])
        if mnum:
            ref = mnum.group(1)

    # Nombre: mediante la etiqueta genérica RE_NAME, ya limpiado.
    name = ""
    mname = RE_NAME.search(body)
    if mname:
        name = _clean_name(mname.group(1))

    # Teléfono: el primero encontrado, dejando solo sus dígitos.
    phone = ""
    if phones:
        phone = "".join(ch for ch in phones[0] if ch.isdigit())

    return {
        "nombre": name,
        "telefono": phone,
        "email": emails[0] if emails else "",
        "fuente": source,
        "url": urls[0] if urls else "",
        "ref": ref,
        "raw_excerpt": body[:500],
    }


def delete_unwanted_mails():
    """Mueve a la papelera los correos de remitentes no deseados.

    Recorre todos los correos cuyos remitentes están en config.GMAIL_AUTO_DELETE
    (dentro de la ventana config.GMAIL_DELETE_WINDOW) y los manda a la papelera.
    Requiere el scope gmail.modify.

    Devuelve:
        list[tuple[str, str]]: lista de (remitente, asunto) de los eliminados;
        lista vacía si no hay remitentes configurados.
    """
    if not config.GMAIL_AUTO_DELETE:
        return []
    service = _get_service()
    # Construye la query Gmail: from:(remitente1 OR remitente2 ...) limitada a
    # los correos más recientes que la ventana configurada (newer_than).
    senders = " OR ".join(config.GMAIL_AUTO_DELETE)
    query = f"from:({senders}) newer_than:{config.GMAIL_DELETE_WINDOW}"
    print(f"[gmail] requête suppression: {query}")

    deleted = []
    page_token = None
    # Pagina la lista de resultados (100 por página) hasta agotar nextPageToken.
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=100
        ).execute()
        for ref in resp.get("messages", []):
            mid = ref["id"]
            try:
                # Solo se piden las cabeceras From/Subject (para el log) antes de
                # mandar el mensaje a la papelera con trash().
                msg = service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject"]).execute()
                hdr = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
                sender = hdr.get("from", "")
                subject = hdr.get("subject", "")
                service.users().messages().trash(userId="me", id=mid).execute()
                deleted.append((sender, subject))
                print(f"[gmail] mail supprimé : {sender} - {subject}")
            except Exception as e:  # noqa: BLE001
                print(f"[gmail] échec suppression {mid}: {e}")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return deleted


def diag():
    """Diagnóstico de Gmail: verifica la autenticación y cuenta los correos.

    Comprueba que el servicio Gmail se autentica correctamente y cuenta cuántos
    correos de los portales (config.PORTAL_SENDERS) hay en la bandeja de entrada
    en el último día, devolviendo además una muestra de los primeros.

    Devuelve:
        dict: con claves como 'query', 'matched_messages' y 'sample', o
        'error_auth'/'error_query' si algo falla.
    """
    out = {}
    try:
        service = _get_service()
    except Exception as e:  # noqa: BLE001
        out["error_auth"] = f"{type(e).__name__}: {e}"
        return out
    # Query Gmail: bandeja de entrada (label:INBOX), del último día (newer_than:1d)
    # y solo de los remitentes de los portales conocidos.
    senders = " OR ".join(config.PORTAL_SENDERS.keys())
    query = f"label:INBOX newer_than:1d from:({senders})"
    out["query"] = query
    try:
        resp = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
        ids = resp.get("messages", [])
        out["matched_messages"] = len(ids)
        subjects = []
        for ref in ids[:5]:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From"]).execute()
            hdr = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            subjects.append({"from": hdr.get("from", ""), "subject": hdr.get("subject", "")})
        out["sample"] = subjects
    except Exception as e:  # noqa: BLE001
        out["error_query"] = f"{type(e).__name__}: {e}"
    return out


def fetch_new_leads(after_ts, window=None):
    """Devuelve los leads detectados en los correos recibidos tras `after_ts`.

    Es la función principal del módulo: consulta Gmail, filtra los correos por
    fuente (Idealista/Fotocasa/Habitaclia) y construye la lista de leads.

    Parámetros:
        after_ts (float): timestamp Unix; solo se procesan los correos cuya
            fecha interna (internalDate) sea posterior a este valor.
        window (str | None): fuerza la ventana de la query Gmail (p. ej. "30d"
            para un escaneo completo). Si es None, se elige automáticamente.

    Devuelve:
        list[dict]: la lista de leads; cada lead incluye al menos un teléfono o
        un email (los demás se descartan), más los de tipo "respuesta_idealista".
    """
    service = _get_service()
    # Solo bandeja de entrada + remitentes conocidos.
    # Si se pasa window explícita (escaneo completo) tiene prioridad; si no,
    # se usan 3 días tras un reset (after_ts<=0) y 24h en funcionamiento normal.
    # Luego se vuelve a filtrar por timestamp (internalDate) para procesar solo
    # lo realmente nuevo, ya que newer_than tiene granularidad de días.
    if not window:
        window = "3d" if after_ts <= 0 else "1d"
    # Query Gmail: label:INBOX (bandeja de entrada) + newer_than:<ventana> +
    # from:(remitente1 OR remitente2 ...) con los remitentes de los portales.
    senders = " OR ".join(config.PORTAL_SENDERS.keys())
    query = f"label:INBOX newer_than:{window} from:({senders})"
    print(f"[gmail] requête: {query}")

    leads = []
    seen_ids = set()  # evita procesar dos veces el mismo mensaje (paginación).
    page_token = None
    # Pagina los resultados (100 por página) hasta agotar nextPageToken.
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=100
        ).execute()
        for ref in resp.get("messages", []):
            mid = ref["id"]
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            # Descarga el mensaje completo para poder leer cabeceras y cuerpo.
            msg = service.users().messages().get(userId="me", id=mid, format="full").execute()

            # Filtrado fino por internalDate (en ms -> s): se descartan los
            # correos que no sean estrictamente posteriores a after_ts.
            internal_ts = int(msg.get("internalDate", "0")) / 1000.0
            if internal_ts <= after_ts:
                continue

            # Cabeceras indexadas en minúsculas para leer From/Subject.
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            sender = headers.get("from", "")
            subject = headers.get("subject", "")

            # Cuerpo: se extrae el texto del árbol MIME y, si parece HTML
            # (contiene '<'), se limpia con _strip_html. 'full' = asunto + cuerpo.
            raw_body = _extract_body(msg["payload"])
            body = _strip_html(raw_body) if "<" in raw_body else raw_body
            full = subject + "\n" + body

            # Detecta el portal; si no se reconoce ninguna fuente, se ignora.
            source = _detect_source(sender, subject, full)
            if not source:
                continue

            # Notificación de respuesta en la mensajería interna de Idealista:
            # NO es un lead nuevo. Se registra como tipo "respuesta_idealista"
            # (con nombre y ref) para informar, pero sin teléfono/email.
            mresp = RE_RESPUESTA_DE.search(subject)
            if mresp:
                resp_name = mresp.group(1).strip()
                mref = RE_IDEALISTA_SUBJECT_REF.search(subject) or RE_REF.search(full)
                resp_ref = mref.group(1).strip() if mref else ""
                lead = {
                    "kind": "respuesta_idealista",
                    "nombre": resp_name, "ref": resp_ref, "fuente": "Idealista",
                    "telefono": "", "email": "", "url": "", "message": "",
                    "gmail_id": mid, "received_at": internal_ts, "subject": subject,
                }
                print(f"[gmail] RESPUESTA Idealista | nombre='{resp_name}' ref='{resp_ref}' (non écrit)")
                leads.append(lead)
                continue

            if source == "Idealista":
                # Parsing línea por línea del cuerpo de Idealista.
                name, phone, email, message = _extract_idealista_lead(full)
                # Nombre: el del asunto tiene prioridad sobre el del cuerpo.
                msname = RE_IDEALISTA_SUBJECT_NAME.search(subject)
                if msname:
                    name = msname.group(1).strip()
                urls = RE_URL.findall(full)
                # Referencia: prioridad al asunto ("con ref:"); si no, al cuerpo
                # ("ref...") y, como último recurso, al número de la primera URL.
                ref = ""
                msref = RE_IDEALISTA_SUBJECT_REF.search(subject)
                if msref:
                    ref = msref.group(1).strip()
                else:
                    mref = RE_REF.search(full)
                    if mref:
                        ref = mref.group(1).strip()
                    elif urls:
                        mnum = RE_NUM_IN_URL.search(urls[0])
                        if mnum:
                            ref = mnum.group(1)
                lead = {
                    "nombre": name, "telefono": phone, "email": email,
                    "fuente": source, "url": urls[0].strip() if urls else "",
                    "ref": ref, "message": message, "raw_excerpt": full[:500],
                }
            elif source in ("Fotocasa", "Habitaclia"):
                # Parser específico que maneja los 3 formatos de estos portales.
                info = _extract_fotocasa_habitaclia(subject, full)
                urls = RE_URL.findall(full)
                lead = {
                    "nombre": info["nombre"], "telefono": info["telefono"],
                    "email": info["email"], "fuente": source,
                    "url": urls[0].strip() if urls else "",
                    "ref": info["ref"], "message": info["message"],
                    "raw_excerpt": full[:500],
                }
            else:
                # Cualquier otra fuente: parser genérico de respaldo.
                lead = _extract_lead(source, full, sender)
                lead["message"] = ""

            # Metadatos comunes a todos los leads.
            lead["gmail_id"] = mid
            lead["received_at"] = internal_ts
            lead["subject"] = subject

            # Logs detallados para verificar el resultado del parsing.
            print(
                f"[gmail] lead {source} | nombre='{lead['nombre']}' "
                f"telefono='{lead['telefono']}' email='{lead['email']}' "
                f"ref='{lead.get('ref','')}' url='{lead.get('url','')}'"
            )
            print(f"[gmail]   message='{(lead.get('message') or '')[:160]}'")

            # Condición final: solo se conservan los leads que tengan al menos
            # un teléfono O un email (los demás carecen de valor de contacto).
            if lead["telefono"] or lead["email"]:
                leads.append(lead)
            else:
                print(f"[gmail]   ignoré (ni téléphone ni email) — subject='{subject}'")

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return leads
