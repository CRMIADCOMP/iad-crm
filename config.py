"""
Configuration centrale du CRM IAD.
Toutes les valeurs sensibles sont lues depuis les variables d'environnement Railway.
Les credentials Google (Gmail OAuth + Service Account Sheets) sont passés en base64.
"""
import os
import json
import base64

# ---------------------------------------------------------------------------
# Paramètres fixes du projet
# ---------------------------------------------------------------------------
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1WYvelN50Hz_8gCo8o9BtsFpUUdLaxQbzlXAySSY4I9M")
CONFIG_SHEET_NAME = os.environ.get("CONFIG_SHEET_NAME", "🔗 Config")

# Gmail à lire
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "thibaut.montalat@iadespana.es")
# Email destinataire du rapport
REPORT_EMAIL = os.environ.get("REPORT_EMAIL", "thibaut.montalat@iadespana.es")

# UltraMsg (WhatsApp)
ULTRAMSG_INSTANCE = os.environ.get("ULTRAMSG_INSTANCE", "instance181932")
ULTRAMSG_TOKEN = os.environ.get("ULTRAMSG_TOKEN", "00sfoebzfiih9jfa")
ULTRAMSG_BASE_URL = f"https://api.ultramsg.com/{ULTRAMSG_INSTANCE}"

# Anthropic (Claude Haiku pour l'analyse des réponses)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# Fuseau horaire du scheduler
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Madrid")
# Heures de déclenchement (heure Madrid)
RUN_HOURS = [int(h) for h in os.environ.get("RUN_HOURS", "8,12,18").split(",")]

# Base de données SQLite (réponses WhatsApp + état des runs)
DB_PATH = os.environ.get("DB_PATH", "crm.db")

# Sources de leads à détecter
LEAD_SOURCES = ["Idealista", "Fotocasa", "Habitaclia"]

# ---------------------------------------------------------------------------
# Colonnes des feuilles "prospects" (index 1 = colonne A)
# ---------------------------------------------------------------------------
COL = {
    "nombre": 1,        # A
    "telefono": 2,      # B
    "email": 3,         # C
    "fuente": 4,        # D
    "notas": 5,         # E
    "presupuesto": 6,   # F
    "tiempo_busqueda": 7,  # G
    "pago_validado": 8,    # H
    "fecha_contacto": 9,   # I
    "ultimo_mensaje": 10,  # J
    "relance_j2": 11,      # K
    "estado_final": 12,    # L
}
PROSPECT_HEADERS = [
    "Nombre", "Teléfono", "Email", "Fuente", "Notas",
    "Presupuesto", "Tiempo búsqueda", "Pago validado",
    "Fecha contacto", "Último mensaje", "Relance J+2", "Estado final",
]

# ---------------------------------------------------------------------------
# Colonnes de l'onglet "🔗 Config" (index 0 = colonne A)
# ---------------------------------------------------------------------------
CONFIG_COL = {
    "feuille": 0,        # A
    "description": 1,    # B
    "url_idealista": 2,  # C
    "ref_idealista": 3,  # D
    "url_fotocasa": 4,   # E
    "ref_fotocasa": 5,   # F
    "url_habitaclia": 6, # G
    "ref_habitaclia": 7, # H
    "url_iad": 8,        # I
    "ref_iad": 9,        # J
}

# ---------------------------------------------------------------------------
# Règles métier
# ---------------------------------------------------------------------------
# États autorisant l'envoi d'un nouveau message
SENDABLE_STATES = {"", "Nuevo contacto", "WhatsApp enviado"}
FALLBACK_NAME = "vecino/a"
RELANCE_DELAY_DAYS = 2          # relance J+2
NO_REPLY_CLOSE_DAYS = 7         # passage en "Sin respuesta - 7d"
LONG_SEARCH_THRESHOLD_MONTHS = 12  # > 1 an => message spécial


# ---------------------------------------------------------------------------
# Décodage des credentials base64 (Railway -> fichiers locaux éphémères)
# ---------------------------------------------------------------------------
def _b64_to_file(env_var, path):
    """Décode une variable d'env base64 vers un fichier, si présente."""
    raw = os.environ.get(env_var)
    if not raw:
        return False
    with open(path, "wb") as f:
        f.write(base64.b64decode(raw))
    return True


def _b64_to_dict(env_var):
    """Décode une variable d'env base64 vers un dict JSON, ou None."""
    raw = os.environ.get(env_var)
    if not raw:
        return None
    return json.loads(base64.b64decode(raw).decode("utf-8"))


def gmail_credentials_path():
    """Écrit credentials.json depuis GMAIL_CREDENTIALS et renvoie le chemin."""
    path = "credentials.json"
    if not os.path.exists(path):
        _b64_to_file("GMAIL_CREDENTIALS", path)
    return path


def gmail_token_path():
    """Écrit token.json depuis GMAIL_TOKEN et renvoie le chemin."""
    path = "token.json"
    if not os.path.exists(path):
        _b64_to_file("GMAIL_TOKEN", path)
    return path


def google_service_account_info():
    """Renvoie le dict du compte de service Google (Sheets) depuis GOOGLE_SERVICE_ACCOUNT."""
    info = _b64_to_dict("GOOGLE_SERVICE_ACCOUNT")
    if info is None and os.path.exists("service_account.json"):
        with open("service_account.json", "r", encoding="utf-8") as f:
            info = json.load(f)
    return info
