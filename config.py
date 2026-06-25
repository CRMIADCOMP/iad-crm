"""
Configuración central del CRM IAD.

Este archivo concentra toda la configuración del CRM inmobiliario "iad-crm"
desplegado en Railway. Su función es reunir en un solo lugar los parámetros
del sistema (IDs de hojas, direcciones de correo, tokens de API, reglas de
negocio, mapeo de columnas de las hojas de cálculo, etc.).

Casi todos los valores sensibles se leen desde las variables de entorno de
Railway mediante ``os.environ.get(...)``, con un valor por defecto de respaldo
para el entorno local. Esto permite cambiar la configuración en producción sin
modificar el código.

Las credenciales de Google se transmiten en formato base64 a través de
variables de entorno y se decodifican en tiempo de ejecución:
  - Gmail OAuth (credentials.json + token.json) para leer el buzón de correo.
  - Cuenta de servicio (Service Account) de Google para acceder a Sheets.
"""
import os
import re
import json
import base64

# ---------------------------------------------------------------------------
# Parámetros fijos del proyecto
# ---------------------------------------------------------------------------
# ID del Google Sheet principal donde viven todas las hojas de prospectos.
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1WYvelN50Hz_8gCo8o9BtsFpUUdLaxQbzlXAySSY4I9M")
# Nombre de la pestaña "Config" dentro del Google Sheet (mapeo de URLs/refs por portal).
CONFIG_SHEET_NAME = os.environ.get("CONFIG_SHEET_NAME", "🔗 Config")

# Dirección de Gmail que se va a leer (buzón del que se extraen los leads).
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "thibaut.montalat@iadespana.es")
# Dirección de correo destinataria del informe (reporte) generado por el CRM.
REPORT_EMAIL = os.environ.get("REPORT_EMAIL", "thibaut.montalat@iadespana.es")

# UltraMsg (WhatsApp): identificador de la instancia de UltraMsg usada para enviar WhatsApp.
ULTRAMSG_INSTANCE = os.environ.get("ULTRAMSG_INSTANCE", "instance181932")
# Token de autenticación de la API de UltraMsg.
ULTRAMSG_TOKEN = os.environ.get("ULTRAMSG_TOKEN", "00sfoebzfiih9jfa")
# URL base de la API de UltraMsg, construida a partir del identificador de instancia.
ULTRAMSG_BASE_URL = f"https://api.ultramsg.com/{ULTRAMSG_INSTANCE}"

# Anthropic (Claude Haiku para analizar las respuestas de los prospectos).
# Clave de la API de Anthropic (vacía por defecto: debe definirse en Railway).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Modelo de Anthropic empleado para el análisis de las respuestas.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# Zona horaria del planificador (scheduler) que dispara las ejecuciones.
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Madrid")
# Horas de disparo de las ejecuciones (en hora de Madrid). Lista de enteros.
# 8/12/18 = pipeline COMPLETO ; 10/15 = solo lectura de respuestas + actualización de columnas.
RUN_HOURS = [int(h) for h in os.environ.get("RUN_HOURS", "8,10,12,15,18").split(",")]
# Horas con pipeline completo (lectura Gmail + primeros contactos + respuestas).
FULL_RUN_HOURS = {int(h) for h in os.environ.get("FULL_RUN_HOURS", "8,12,18").split(",")}
# Horas en modo "solo respuestas" (sin leer Gmail ni enviar primeros contactos).
REPLIES_ONLY_HOURS = {int(h) for h in os.environ.get("REPLIES_ONLY_HOURS", "10,15").split(",")}

# Ruta de la base de datos SQLite (almacena las respuestas de WhatsApp y el estado de las ejecuciones).
DB_PATH = os.environ.get("DB_PATH", "crm.db")

# Retardo (en segundos) entre dos escrituras consecutivas en Sheets para evitar el límite de cuota 429.
SHEETS_WRITE_DELAY = float(os.environ.get("SHEETS_WRITE_DELAY", "1"))

# Fuentes (portales) de leads que el sistema debe detectar.
LEAD_SOURCES = ["Idealista", "Fotocasa", "Habitaclia"]

# Mapeo dirección del remitente -> fuente (detección por defecto).
# También se utiliza para construir el filtro "from:" de la consulta a Gmail.
# Nota: Fotocasa Y Habitaclia llegan desde "cliente@fotocasa.pro";
# la distinción se hace según el final del asunto ("- De Fotocasa" / "- De habitaclia").
PORTAL_SENDERS = {
    "reply@idealista.com": "Idealista",
    "cliente@fotocasa.pro": "Fotocasa",  # Fotocasa + Habitaclia (mismo remitente)
}

# Remitentes cuyos correos se envían automáticamente a la papelera (limpieza del buzón).
# Requiere el scope de Gmail "modify" (regenerar token.json mediante setup_auth.py).
GMAIL_AUTO_DELETE = [
    "idealista@mailing.idealista.com",
    "conseiller@lr.caisse-epargne.fr",
    "no-reply@accounts.google.com",
    "reminders@facebookmail.com",
]
# Ventana de búsqueda para la eliminación automática (p. ej. "30d" = últimos 30 días).
GMAIL_DELETE_WINDOW = os.environ.get("GMAIL_DELETE_WINDOW", "30d")

# ---------------------------------------------------------------------------
# Columnas de las hojas de "prospectos" (índice 1 = columna A)
# ---------------------------------------------------------------------------
# Diccionario que asocia cada campo del prospecto con su número de columna en la hoja.
# El índice empieza en 1 (estilo gspread: 1 = columna A, 2 = columna B, ...).
COL = {
    "nombre": 1,        # A - Nombre del prospecto
    "telefono": 2,      # B - Teléfono
    "email": 3,         # C - Correo electrónico
    "fuente": 4,        # D - Fuente/portal de origen
    "notas": 5,         # E - Notas
    "presupuesto": 6,   # F - Presupuesto
    "tiempo_busqueda": 7,  # G - Tiempo de búsqueda
    "pago_validado": 8,    # H - Pago validado
    "fecha_contacto": 9,   # I - Fecha de contacto
    "ultimo_mensaje": 10,  # J - Último mensaje
    "relance_j2": 11,      # K - Reactivación J+2 (seguimiento al 2º día)
    "estado_final": 12,    # L - Estado final
}
# Encabezados (cabeceras) de las columnas de las hojas de prospectos, en orden.
PROSPECT_HEADERS = [
    "Nombre", "Teléfono", "Email", "Fuente", "Notas",
    "Presupuesto", "Tiempo búsqueda", "Pago validado",
    "Fecha contacto", "Último mensaje", "Relance J+2", "Estado final",
]

# ---------------------------------------------------------------------------
# Columnas de la pestaña "🔗 Config" (índice 0 = columna A)
# ---------------------------------------------------------------------------
# Diccionario que asocia cada campo de la pestaña Config con su índice de columna.
# Aquí el índice empieza en 0 (estilo lista: 0 = columna A, 1 = columna B, ...).
# Esta pestaña relaciona cada hoja/zona con sus URLs y referencias por portal.
CONFIG_COL = {
    "feuille": 0,        # A - Hoja/zona (nombre de la pestaña de prospectos)
    "description": 1,    # B - Descripción
    "url_idealista": 2,  # C - URL del anuncio en Idealista
    "ref_idealista": 3,  # D - Referencia del anuncio en Idealista
    "url_fotocasa": 4,   # E - URL del anuncio en Fotocasa
    "ref_fotocasa": 5,   # F - Referencia del anuncio en Fotocasa
    "url_habitaclia": 6, # G - URL del anuncio en Habitaclia
    "ref_habitaclia": 7, # H - Referencia del anuncio en Habitaclia
    "url_iad": 8,        # I - URL del anuncio en IAD
    "ref_iad": 9,        # J - Referencia del anuncio en IAD
}

# ---------------------------------------------------------------------------
# Tipos de inmueble y ciudades (parseo del nombre de la hoja)
# ---------------------------------------------------------------------------
# Tipos de inmueble: prefijo del nombre de la hoja -> nombre completo + artículo español.
BIEN_TYPES = {
    "T": {"name": "Terreno", "article": "el"},   # el terreno
    "C": {"name": "Casa", "article": "la"},       # la casa
    "P": {"name": "Piso", "article": "el"},       # el piso
    "Pa": {"name": "Parking", "article": "el"},   # el parking
    "L": {"name": "Local", "article": "el"},      # el local
}

# Abreviaturas de ciudad usadas en los nombres de hoja -> nombre completo de la ciudad.
# Editable desde el dashboard (se añaden ciudades nuevas en la base de datos).
CITY_NAMES = {
    "vaca": "Vacarisses",
    "sant vic": "Sant Vicenç dels Horts",
    "vall": "Vallirana",
    "esplu": "Esplugues de Llobregat",
    "ole": "Olesa de Montserrat",
    "figu": "Figueres",
    "barca": "Barcelona",
    "oli": "Olivella",
}

# Datos del bróker de financiación (recomendado a los prospectos sin financiación).
# Editables desde el dashboard (se guardan en la base de datos como override).
BROKER_NAME = os.environ.get("BROKER_NAME", "Thom")
BROKER_PHONE = os.environ.get("BROKER_PHONE", "+34651386644")


def parse_bien_info(sheet_name, city_names=None):
    """Extrae el tipo, la ciudad, el precio y el artículo de un nombre de hoja.

    El nombre de hoja sigue el formato: ``[TIPO] [ABREV_CIUDAD] [PRECIO]``
    (ej. "T Vaca 55k", "C sant vic 340k", "Pa Esplu 12 500").

    Parámetros:
        sheet_name (str): nombre de la hoja del inmueble.
        city_names (dict | None): mapeo abreviatura->ciudad; si es None usa CITY_NAMES.

    Devuelve:
        dict con las claves: "type" (nombre completo), "type_code" (T/C/P/Pa/L),
        "city" (nombre completo o la abreviatura si no se conoce), "price" (texto)
        y "article" (el/la). Los campos desconocidos quedan como "".
    """
    city_names = city_names or CITY_NAMES
    res = {"type": "", "type_code": "", "city": "", "price": "", "article": ""}
    tokens = (sheet_name or "").split()
    if not tokens:
        return res
    # El primer token es el código de tipo si coincide con BIEN_TYPES.
    code = tokens[0]
    info = BIEN_TYPES.get(code)
    if info:
        res["type_code"] = code
        res["type"] = info["name"]
        res["article"] = info["article"]
        rest = tokens[1:]
    else:
        rest = tokens
    # El precio son los tokens finales que parecen un precio (cifras, opcional "k").
    price_tokens = []
    while rest and re.match(r"^\d+k?$", rest[-1], re.I):
        price_tokens.insert(0, rest.pop())
    res["price"] = " ".join(price_tokens)
    # Lo que queda en medio es la abreviatura de ciudad.
    abbrev = " ".join(rest).strip()
    res["city"] = city_names.get(abbrev.lower(), abbrev)
    return res


# ---------------------------------------------------------------------------
# Reglas de negocio
# ---------------------------------------------------------------------------
# Los datos de prospectos empiezan SIEMPRE en la fila 4.
# Las filas 1, 2 y 3 (títulos/encabezados existentes) NUNCA deben modificarse.
DATA_START_ROW = 4

# --- Estados del flujo conversacional en 3 pasos ---
STATE_NEW = "Nuevo contacto"
STATE_PASO1 = "WhatsApp enviado - Paso 1"   # primer contacto enviado
STATE_PASO2 = "WhatsApp enviado - Paso 2"   # el prospecto confirmó interés -> preguntas
STATE_COMPLETED = "Perfil completado"        # perfil cualificado (financiación tratada)
STATE_OUT = "Fuera"                          # respuesta negativa / no interesado
ERROR_WA_STATE = "Error envío WA"            # fallo de envío (se reintenta), color #FF6B6B

# Estados que permiten el envío del PRIMER mensaje (Paso 1).
# "WhatsApp enviado" (legacy) se mantiene por compatibilidad con filas antiguas.
SENDABLE_STATES = {"", STATE_NEW}
# Estados introducidos manualmente: NUNCA deben sobrescribirse de forma automática.
MANUAL_STATES = {"Visita apuntada", "Visita hecha", STATE_OUT}
# Estados desde los cuales se permite el cierre automático a los 7 días (Paso 1 sin respuesta).
AUTO_CLOSE_FROM = {STATE_PASO1, "WhatsApp enviado", "No responde"}

# Valores de la lista desplegable de la columna L (Estado final).
# Se aplican automáticamente a todas las hojas mediante /setup_dropdowns.
ESTADO_FINAL_OPTIONS = [
    STATE_NEW,
    STATE_PASO1,
    STATE_PASO2,
    STATE_COMPLETED,
    ERROR_WA_STATE,
    "No responde",
    "Sin respuesta - 7d",
    "Visita apuntada",
    "Visita hecha",
    STATE_OUT,
]
# Colores (RGB 0-1) de la mise en forme conditionnelle por estado para /setup_dropdowns.
# (texto blanco indicado cuando el fondo es oscuro).
ESTADO_COLORS = {
    STATE_PASO1: {"bg": (0.741, 0.843, 0.933)},                     # #BDD7EE azul claro
    STATE_PASO2: {"bg": (0.0, 0.694, 0.922), "white": True},        # #00b1eb azul medio
    STATE_COMPLETED: {"bg": (0.216, 0.337, 0.137), "white": True},  # #375623 verde oscuro
    ERROR_WA_STATE: {"bg": (1.0, 0.42, 0.42)},                      # #FF6B6B rojo claro
}
# Nombre de respaldo (fallback) que se usa cuando no se conoce el nombre del prospecto.
FALLBACK_NAME = "vecino/a"
# Hoja de respaldo: los leads que no se asocian (match) a la pestaña Config se escriben
# aquí (en lugar de perderse). No se envía ningún WhatsApp para estos leads.
FALLBACK_SHEET = os.environ.get("FALLBACK_SHEET", "Leads sin clasificar")
# ¿Enviar un WhatsApp aunque no haya anuncio asociado? (desaconsejado: mensaje sin URL).
SEND_WHATSAPP_WHEN_UNMATCHED = os.environ.get("SEND_WHATSAPP_WHEN_UNMATCHED", "0") == "1"
# Número de días tras los cuales se realiza la reactivación/seguimiento (relance J+2).
RELANCE_DELAY_DAYS = 2          # reactivación al 2º día (J+2)
# Número de días sin respuesta tras los cuales el prospecto pasa a "Sin respuesta - 7d".
NO_REPLY_CLOSE_DAYS = 7         # paso a "Sin respuesta - 7d"
# Umbral (en meses) a partir del cual se considera una búsqueda larga (> 1 año => mensaje especial).
LONG_SEARCH_THRESHOLD_MONTHS = 12  # > 1 año => mensaje especial


# ---------------------------------------------------------------------------
# Decodificación de credenciales en base64 (Railway -> archivos locales efímeros)
# ---------------------------------------------------------------------------
def _b64_to_file(env_var, path):
    """Decodifica una variable de entorno en base64 y la escribe en un archivo.

    Lee el valor base64 de la variable de entorno indicada, lo decodifica a
    bytes y lo guarda en la ruta dada. Si la variable no existe o está vacía,
    no hace nada.

    Parámetros:
        env_var (str): Nombre de la variable de entorno con el contenido base64.
        path (str): Ruta del archivo de destino donde escribir los bytes decodificados.

    Devuelve:
        bool: True si la variable existía y se escribió el archivo; False en caso contrario.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return False
    with open(path, "wb") as f:
        f.write(base64.b64decode(raw))
    return True


def _b64_to_dict(env_var):
    """Decodifica una variable de entorno en base64 hacia un diccionario JSON.

    Lee el valor base64 de la variable de entorno, lo decodifica a texto UTF-8
    y lo interpreta como JSON.

    Parámetros:
        env_var (str): Nombre de la variable de entorno con el JSON en base64.

    Devuelve:
        dict | None: El diccionario resultante del JSON, o None si la variable
        no existe o está vacía.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return None
    return json.loads(base64.b64decode(raw).decode("utf-8"))


def gmail_credentials_path():
    """Garantiza la existencia de credentials.json y devuelve su ruta.

    Si el archivo credentials.json no existe localmente, lo genera decodificando
    la variable de entorno GMAIL_CREDENTIALS (base64). Estas son las credenciales
    OAuth de cliente de Gmail.

    Devuelve:
        str: La ruta del archivo credentials.json.
    """
    path = "credentials.json"
    if not os.path.exists(path):
        _b64_to_file("GMAIL_CREDENTIALS", path)
    return path


def gmail_token_path():
    """Garantiza la existencia de token.json y devuelve su ruta.

    Si el archivo token.json no existe localmente, lo genera decodificando la
    variable de entorno GMAIL_TOKEN (base64). Este token OAuth permite acceder
    al buzón de Gmail sin reautenticarse.

    Devuelve:
        str: La ruta del archivo token.json.
    """
    path = "token.json"
    if not os.path.exists(path):
        _b64_to_file("GMAIL_TOKEN", path)
    return path


def google_service_account_info():
    """Devuelve la información de la cuenta de servicio de Google (Sheets).

    Intenta primero decodificar la cuenta de servicio desde la variable de
    entorno GOOGLE_SERVICE_ACCOUNT (JSON en base64). Si no está disponible,
    recurre al archivo local service_account.json si existe.

    Devuelve:
        dict | None: El diccionario con las credenciales de la cuenta de
        servicio, o None si no se encuentra en ninguna fuente.
    """
    info = _b64_to_dict("GOOGLE_SERVICE_ACCOUNT")
    if info is None and os.path.exists("service_account.json"):
        with open("service_account.json", "r", encoding="utf-8") as f:
            info = json.load(f)
    return info
