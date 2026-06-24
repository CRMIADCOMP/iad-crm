"""
Accès au Google Sheets via un compte de service (gspread).
- Lecture de l'onglet "🔗 Config" pour le matching annonce -> feuille.
- Déduplication / insertion / mise à jour des prospects.
- Mise à jour des colonnes F/G/H d'après l'analyse IA.
- Gestion des états (relances, clôtures).
"""
import time
import datetime

import gspread
from google.oauth2.service_account import Credentials

import config
from database import normalize_phone

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client = None
_spreadsheet = None

# --- Cache mémoire (réinitialisé à chaque run) pour limiter les appels API ----
_ws_objs = {}        # titre -> objet worksheet
_ws_list = None      # liste des worksheets (1 seul appel/run)
_values_cache = {}   # titre -> get_all_values()
_config_rows = None  # lignes de l'onglet Config


def reset_cache():
    """À appeler au début de chaque run du pipeline."""
    global _ws_list, _config_rows
    _ws_objs.clear()
    _values_cache.clear()
    _ws_list = None
    _config_rows = None


def _all_worksheets():
    global _ws_list
    if _ws_list is None:
        _ws_list = _get_spreadsheet().worksheets()
    return _ws_list


def _write_throttle():
    """Pause entre deux écritures pour éviter le quota 429."""
    time.sleep(config.SHEETS_WRITE_DELAY)


def _values_for(ws):
    """get_all_values() mis en cache par feuille."""
    name = ws.title
    if name not in _values_cache:
        _values_cache[name] = ws.get_all_values()
    return _values_cache[name]


def _get_spreadsheet():
    global _client, _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet
    info = config.google_service_account_info()
    if not info:
        raise RuntimeError(
            "Compte de service Google introuvable (GOOGLE_SERVICE_ACCOUNT manquant)."
        )
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _client = gspread.authorize(creds)
    _spreadsheet = _client.open_by_key(config.GOOGLE_SHEET_ID)
    return _spreadsheet


# ---------------------------------------------------------------------------
# Onglet Config : matching annonce -> feuille
# ---------------------------------------------------------------------------
def _get_config_worksheet():
    """
    Renvoie l'onglet Config. Tolérant au nom exact (emoji/espaces) :
    essaie le nom configuré, sinon le premier onglet contenant 'config'.
    """
    for ws in _all_worksheets():
        if ws.title == config.CONFIG_SHEET_NAME:
            return ws
    for ws in _all_worksheets():
        if "config" in ws.title.lower():
            print(f"[config] onglet Config trouvé par tolérance: '{ws.title}'")
            return ws
    raise gspread.WorksheetNotFound(config.CONFIG_SHEET_NAME)


def load_config_rows():
    """Renvoie les lignes de l'onglet Config (sans l'en-tête), mises en cache."""
    global _config_rows
    if _config_rows is None:
        ws = _get_config_worksheet()
        rows = _values_for(ws)
        _config_rows = rows[1:] if rows else []
    return _config_rows


def _ref_norm(value):
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


_config_logged = False


def _log_config_refs(rows):
    """Affiche une fois toutes les refs/URLs de l'onglet Config (debug matching)."""
    global _config_logged
    if _config_logged:
        return
    _config_logged = True
    cc = config.CONFIG_COL
    print("[config] === Refs disponibles dans l'onglet Config ===")
    for row in rows:
        def cell(idx):
            return row[idx] if idx < len(row) else ""
        feuille = cell(cc["feuille"]).strip()
        if not feuille:
            continue
        refs = {
            "idealista": cell(cc["ref_idealista"]).strip(),
            "fotocasa": cell(cc["ref_fotocasa"]).strip(),
            "habitaclia": cell(cc["ref_habitaclia"]).strip(),
            "iad": cell(cc["ref_iad"]).strip(),
        }
        refs_norm = {k: _ref_norm(v) for k, v in refs.items() if v}
        print(f"[config]   feuille='{feuille}' refs={refs} (norm={refs_norm})")
    print("[config] === fin ===")


def match_lead_to_sheet(lead):
    """
    Cherche dans l'onglet Config la feuille correspondant au lead,
    via l'URL ou la référence de l'annonce.
    Renvoie (feuille, iad_url) ou (None, None).
    """
    cc = config.CONFIG_COL
    lead_url = (lead.get("url") or "").lower()
    lead_ref = _ref_norm(lead.get("ref", ""))

    rows = load_config_rows()
    _log_config_refs(rows)
    print(f"[match] lead {lead.get('fuente')} -> ref='{lead.get('ref','')}' "
          f"(norm='{lead_ref}') url='{lead_url}'")

    for row in rows:
        def cell(idx):
            return row[idx] if idx < len(row) else ""

        feuille = cell(cc["feuille"]).strip()
        if not feuille:
            continue

        urls = [cell(cc["url_idealista"]), cell(cc["url_fotocasa"]),
                cell(cc["url_habitaclia"]), cell(cc["url_iad"])]
        refs = [cell(cc["ref_idealista"]), cell(cc["ref_fotocasa"]),
                cell(cc["ref_habitaclia"]), cell(cc["ref_iad"])]

        # match par URL (substring dans un sens ou l'autre)
        if lead_url:
            for u in urls:
                u = u.strip().lower()
                if u and (u in lead_url or lead_url in u):
                    print(f"[match]   ✅ match URL -> feuille='{feuille}'")
                    return feuille, cell(cc["url_iad"]).strip()

        # match par référence
        if lead_ref:
            for r in refs:
                if r and _ref_norm(r) == lead_ref:
                    print(f"[match]   ✅ match REF -> feuille='{feuille}'")
                    return feuille, cell(cc["url_iad"]).strip()

    print(f"[match]   ❌ aucun match pour ref='{lead_ref}' url='{lead_url}'")
    return None, None


# ---------------------------------------------------------------------------
# Feuilles prospects
# ---------------------------------------------------------------------------
def get_iad_url_for_sheet(feuille):
    """Renvoie l'URL IAD (col I de Config) associée à une feuille, pour les relances."""
    cc = config.CONFIG_COL
    for row in load_config_rows():
        def cell(idx):
            return row[idx] if idx < len(row) else ""
        if cell(cc["feuille"]).strip() == feuille:
            return cell(cc["url_iad"]).strip()
    return ""


def _get_or_create_worksheet(name):
    # 1) cache d'objets
    if name in _ws_objs:
        return _ws_objs[name]

    # 2) recherche par nom EXACT parmi les feuilles existantes (évite les doublons)
    for ws in _all_worksheets():
        if ws.title == name:
            _ws_objs[name] = ws
            # garantit l'en-tête (via valeurs en cache)
            vals = _values_for(ws)
            if not vals or vals[0][:1] != [config.PROSPECT_HEADERS[0]]:
                _write_throttle()
                ws.insert_row(config.PROSPECT_HEADERS, 1)
                _values_cache[name] = [list(config.PROSPECT_HEADERS)] + vals
            return ws

    # 3) création seulement si elle n'existe vraiment pas
    ss = _get_spreadsheet()
    _write_throttle()
    ws = ss.add_worksheet(title=name, rows=200, cols=len(config.PROSPECT_HEADERS))
    _write_throttle()
    ws.append_row(config.PROSPECT_HEADERS)
    _ws_objs[name] = ws
    _values_cache[name] = [list(config.PROSPECT_HEADERS)]
    if _ws_list is not None:
        _ws_list.append(ws)
    return ws


def find_prospect(ws, phone="", email=""):
    """
    Déduplication par téléphone ET email.
    Renvoie (row_index, row_values) si trouvé, sinon (None, None).
    row_index est 1-based (en comptant l'en-tête).
    """
    phone_n = normalize_phone(phone)
    email_n = (email or "").strip().lower()
    values = _values_for(ws)
    for i, row in enumerate(values[1:], start=2):  # ligne 1 = en-tête
        r_phone = normalize_phone(row[config.COL["telefono"] - 1] if len(row) >= config.COL["telefono"] else "")
        r_email = (row[config.COL["email"] - 1] if len(row) >= config.COL["email"] else "").strip().lower()
        if phone_n and r_phone and phone_n == r_phone:
            return i, row
        if email_n and r_email and email_n == r_email:
            return i, row
    return None, None


def upsert_prospect(feuille, lead):
    """
    Insère ou met à jour un prospect dans la feuille `feuille`.
    Renvoie (row_index, is_new, row_values).
    """
    ws = _get_or_create_worksheet(feuille)
    row_idx, existing = find_prospect(ws, lead.get("telefono", ""), lead.get("email", ""))
    today = datetime.date.today().isoformat()

    # Nettoyage : aucune donnée copiée avec des espaces superflus
    nombre = (lead.get("nombre") or "").strip()
    telefono = (lead.get("telefono", "") or "").strip()
    email = (lead.get("email", "") or "").strip()
    fuente = (lead.get("fuente", "") or "").strip()
    # Colonne E (Notas) laissée VIDE : le prospect la remplira manuellement.

    if row_idx is None:
        new_row = [""] * len(config.PROSPECT_HEADERS)
        new_row[config.COL["nombre"] - 1] = nombre
        new_row[config.COL["telefono"] - 1] = telefono
        new_row[config.COL["email"] - 1] = email
        new_row[config.COL["fuente"] - 1] = fuente
        new_row[config.COL["fecha_contacto"] - 1] = today
        new_row[config.COL["estado_final"] - 1] = "Nuevo contacto"
        vals = _values_for(ws)
        new_index = len(vals) + 1
        _write_throttle()
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        vals.append(new_row)  # garde le cache synchronisé
        return new_index, True, new_row

    # mise à jour : complète les champs manquants seulement (jamais Notas)
    def _existing(col_name):
        idx = config.COL[col_name] - 1
        return (existing[idx] if len(existing) > idx else "").strip()

    updates = {}
    if nombre and not _existing("nombre"):
        updates["nombre"] = nombre
    if email and not _existing("email"):
        updates["email"] = email
    if telefono and not _existing("telefono"):
        updates["telefono"] = telefono
    if updates:
        update_cells(feuille, row_idx, updates)
    return row_idx, False, existing


def _cache_set(name, row_idx, col_idx, val):
    """Met à jour la valeur en cache (row_idx/col_idx 1-based)."""
    vals = _values_cache.get(name)
    if vals is None:
        return
    while len(vals) < row_idx:
        vals.append([])
    row = vals[row_idx - 1]
    while len(row) < col_idx:
        row.append("")
    row[col_idx - 1] = val


def update_cells(feuille, row_idx, col_values):
    """col_values : dict {nom_colonne_config.COL: valeur}."""
    ws = _get_or_create_worksheet(feuille)
    for col_name, val in col_values.items():
        col_idx = config.COL[col_name]
        _write_throttle()
        ws.update_cell(row_idx, col_idx, val)
        _cache_set(ws.title, row_idx, col_idx, val)


def get_cell(feuille, row_idx, col_name):
    ws = _get_or_create_worksheet(feuille)
    vals = _values_for(ws)
    idx = config.COL[col_name] - 1
    if row_idx - 1 < len(vals) and idx < len(vals[row_idx - 1]):
        return vals[row_idx - 1][idx] or ""
    return ""


def list_all_prospect_sheets():
    """Noms de toutes les feuilles de prospects (exclut tout onglet 'Config')."""
    names = []
    for ws in _all_worksheets():
        if ws.title == config.CONFIG_SHEET_NAME or "config" in ws.title.lower():
            continue
        names.append(ws.title)
    return names


def diag():
    """Diagnostic : vérifie l'accès au Sheets et résume le contenu."""
    out = {}
    try:
        ss = _get_spreadsheet()
        out["spreadsheet_title"] = ss.title
        out["worksheets"] = [ws.title for ws in ss.worksheets()]
    except Exception as e:  # noqa: BLE001
        out["error_spreadsheet"] = f"{type(e).__name__}: {e}"
        return out
    try:
        rows = load_config_rows()
        out["config_rows"] = len(rows)
        cc = config.CONFIG_COL
        sample = []
        for row in rows[:10]:
            def cell(idx):
                return row[idx] if idx < len(row) else ""
            sample.append({
                "feuille": cell(cc["feuille"]).strip(),
                "ref_idealista": cell(cc["ref_idealista"]).strip(),
                "ref_fotocasa": cell(cc["ref_fotocasa"]).strip(),
                "ref_habitaclia": cell(cc["ref_habitaclia"]).strip(),
            })
        out["config_sample"] = sample
    except Exception as e:  # noqa: BLE001
        out["error_config"] = f"{type(e).__name__}: {e}"
    # test d'écriture dans la feuille de repli
    try:
        ws = _get_or_create_worksheet(config.FALLBACK_SHEET)
        out["fallback_sheet_ok"] = ws.title
    except Exception as e:  # noqa: BLE001
        out["error_write"] = f"{type(e).__name__}: {e}"
    return out


def iter_prospects(feuille):
    """Génère (row_idx, dict_colonnes) pour chaque prospect d'une feuille."""
    ws = _get_or_create_worksheet(feuille)
    values = _values_for(ws)
    for i, row in enumerate(values[1:], start=2):
        def g(name):
            idx = config.COL[name] - 1
            return row[idx] if len(row) > idx else ""
        yield i, {
            "nombre": g("nombre"),
            "telefono": g("telefono"),
            "email": g("email"),
            "fuente": g("fuente"),
            "notas": g("notas"),
            "presupuesto": g("presupuesto"),
            "tiempo_busqueda": g("tiempo_busqueda"),
            "pago_validado": g("pago_validado"),
            "fecha_contacto": g("fecha_contacto"),
            "ultimo_mensaje": g("ultimo_mensaje"),
            "relance_j2": g("relance_j2"),
            "estado_final": g("estado_final"),
        }
