"""
Acceso a Google Sheets mediante una cuenta de servicio (gspread).

Este modulo concentra toda la interaccion con la hoja de calculo del CRM:

- Lectura de la pestana "🔗 Config" para el matching anuncio -> hoja (feuille):
  a partir de la URL o de la referencia de un anuncio se localiza la hoja de
  prospectos del bien correspondiente.
- Deduplicacion / insercion / actualizacion de prospectos: se evita duplicar
  un mismo contacto (por telefono o email) y se completan solo los campos
  vacios al actualizar.
- Actualizacion de las columnas F/G/H segun el analisis de la IA (presupuesto,
  tiempo de busqueda, validacion de pago, etc.).
- Gestion de los estados de cada prospecto (relances/seguimientos, cierres),
  incluida la lista desplegable de "Estado final" y su formato condicional.

Toda la lectura se apoya en una cache en memoria (se reinicia en cada
ejecucion del pipeline) para minimizar las llamadas a la API de Google y
respetar la cuota de peticiones.
"""
import os
import re
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

# Cliente gspread autenticado y objeto de la hoja de calculo (se crean una sola
# vez de forma perezosa y se reutilizan en toda la ejecucion).
_client = None
_spreadsheet = None

# --- Cache en memoria (reiniciada en cada ejecucion) para limitar las llamadas
# --- a la API y no agotar la cuota de Google Sheets. ---
# Cada estructura cachea un nivel distinto de acceso a la hoja:
_ws_objs = {}        # titulo de hoja -> objeto worksheet (evita re-buscarla)
_ws_list = None      # lista de todas las worksheets (una sola llamada por ejecucion)
_values_cache = {}   # titulo de hoja -> get_all_values() (valores leidos una vez)
_config_rows = None  # filas ya normalizadas de la pestana Config


def reset_cache():
    """
    Vacia por completo la cache en memoria.

    Debe llamarse al principio de cada ejecucion del pipeline (y tras cambios
    estructurales como add_bien/close_bien) para que la siguiente lectura
    vuelva a consultar Google Sheets en lugar de servir datos obsoletos.

    No recibe parametros ni devuelve valor; actua sobre las variables globales
    de cache (_ws_objs, _ws_list, _values_cache, _config_rows).
    """
    global _ws_list, _config_rows
    _ws_objs.clear()
    _values_cache.clear()
    _ws_list = None
    _config_rows = None


def _all_worksheets():
    """
    Devuelve la lista de todas las worksheets de la hoja de calculo.

    Usa la cache _ws_list: la primera vez consulta la API (worksheets()) y en
    las siguientes llamadas de la misma ejecucion reutiliza el resultado.

    Devuelve: lista de objetos worksheet de gspread.
    """
    global _ws_list
    if _ws_list is None:
        _ws_list = _get_spreadsheet().worksheets()
    return _ws_list


def _write_throttle():
    """
    Introduce una pausa entre dos escrituras consecutivas.

    Sirve para no superar la cuota de la API de Google Sheets y evitar el
    error 429 (demasiadas peticiones). La duracion la define
    config.SHEETS_WRITE_DELAY.

    No recibe parametros ni devuelve valor.
    """
    time.sleep(config.SHEETS_WRITE_DELAY)


def _values_for(ws):
    """
    Devuelve todos los valores de una worksheet, cacheados por hoja.

    La primera vez llama a get_all_values() y guarda el resultado en
    _values_cache indexado por el titulo de la hoja; las siguientes lecturas
    de esa misma hoja se sirven desde la cache.

    Parametros:
        ws: objeto worksheet de gspread.

    Devuelve: lista de filas (cada fila es una lista de cadenas).
    """
    name = ws.title
    if name not in _values_cache:
        _values_cache[name] = ws.get_all_values()
    return _values_cache[name]


def _get_spreadsheet():
    """
    Devuelve el objeto Spreadsheet, autenticando si hace falta.

    De forma perezosa crea el cliente gspread a partir de la cuenta de servicio
    de Google (config.google_service_account_info()) y abre la hoja de calculo
    indicada por config.GOOGLE_SHEET_ID. El cliente y la hoja se cachean en
    _client/_spreadsheet para reutilizarlos.

    Devuelve: el objeto Spreadsheet de gspread.
    Lanza RuntimeError si no hay credenciales de cuenta de servicio.
    """
    global _client, _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet
    info = config.google_service_account_info()
    if not info:
        raise RuntimeError(
            "Compte de service Google introuvable (GOOGLE_SERVICE_ACCOUNT manquant)."
        )
    # Construye las credenciales OAuth2 a partir del JSON de la cuenta de
    # servicio, autoriza el cliente gspread y abre la hoja por su ID.
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _client = gspread.authorize(creds)
    _spreadsheet = _client.open_by_key(config.GOOGLE_SHEET_ID)
    return _spreadsheet


# ---------------------------------------------------------------------------
# Pestana Config: matching anuncio -> hoja (feuille)
# ---------------------------------------------------------------------------
def _get_config_worksheet():
    """
    Devuelve la pestana Config.

    Es tolerante con el nombre exacto de la pestana (que suele llevar emoji y
    espacios, p. ej. "🔗 Config"): primero busca una coincidencia exacta con
    config.CONFIG_SHEET_NAME y, si no la encuentra, acepta la primera pestana
    cuyo titulo contenga "config" (sin distinguir mayusculas).

    Devuelve: el objeto worksheet de la pestana Config.
    Lanza gspread.WorksheetNotFound si no existe ninguna pestana Config.
    """
    # 1) Coincidencia exacta con el nombre configurado.
    for ws in _all_worksheets():
        if ws.title == config.CONFIG_SHEET_NAME:
            return ws
    # 2) Tolerancia: cualquier pestana que contenga "config" (ignora emoji,
    #    espacios o diferencias de mayusculas en el titulo).
    for ws in _all_worksheets():
        if "config" in ws.title.lower():
            print(f"[config] onglet Config trouvé par tolérance: '{ws.title}'")
            return ws
    raise gspread.WorksheetNotFound(config.CONFIG_SHEET_NAME)


def load_config_rows():
    """
    Devuelve las filas de la pestana Config (sin la fila de cabecera), cacheadas.

    Todas las celdas se fuerzan a cadena (str) porque algunas referencias se
    almacenan como numeros enteros (p. ej. 926287) y deben poder compararse
    como texto ("926287"). El resultado se guarda en _config_rows para no
    releer la pestana en la misma ejecucion.

    Devuelve: lista de filas (cada fila es una lista de cadenas).
    """
    global _config_rows
    if _config_rows is None:
        ws = _get_config_worksheet()
        rows = _values_for(ws)
        # rows[0] es la cabecera; los datos reales empiezan en rows[1:].
        data = rows[1:] if rows else []
        _config_rows = [[str(c) for c in row] for row in data]
    return _config_rows


def _ref_norm(value):
    """
    Normaliza una referencia de anuncio para poder compararla de forma robusta.

    Convierte el valor a cadena, lo pasa a minusculas y conserva solo los
    caracteres alfanumericos (descarta espacios, guiones, puntos, etc.). Asi
    "Ref-92.62/87 " y "REF926287" se reducen a la misma forma normalizada.

    Parametros:
        value: referencia en cualquier formato (str o numero).

    Devuelve: la referencia normalizada (cadena alfanumerica en minusculas).
    """
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _pick_url(row):
    """
    Selecciona UNA URL para el bien, por orden de prioridad.

    Prioridad de las columnas del Config:
    C (Idealista) -> E (Fotocasa) -> G (Habitaclia) -> I (IAD).
    Una celda se considera "vacia" si es None, "" o no empieza por "http".
    Se devuelve la primera URL valida segun ese orden.

    Parametros:
        row: fila de la pestana Config (lista de cadenas).

    Devuelve: la URL elegida, o "" si ninguna celda contiene una URL valida.
    """
    cc = config.CONFIG_COL
    # Acceso seguro a la celda por indice (devuelve "" si la fila es mas corta).
    def cell(idx):
        return row[idx] if idx < len(row) else ""
    # Recorre las columnas de URL en el orden de prioridad C->E->G->I.
    for key in ("url_idealista", "url_fotocasa", "url_habitaclia", "url_iad"):
        v = (cell(cc[key]) or "").strip()
        if v.lower().startswith("http"):
            return v
    return ""


# Bandera para registrar las refs del Config una sola vez por ejecucion.
_config_logged = False


def _log_config_refs(rows):
    """
    Imprime una sola vez todas las refs/URLs de la pestana Config.

    Es una ayuda de depuracion del matching: muestra, por cada hoja con datos,
    las referencias de cada portal y su forma normalizada (_ref_norm). Solo se
    ejecuta la primera vez gracias a la bandera _config_logged.

    Parametros:
        rows: filas del Config (lista de filas).

    No devuelve valor (solo imprime).
    """
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
    Busca en la pestana Config la hoja (feuille) correspondiente a un lead.

    El emparejamiento se intenta de dos formas, por cada fila del Config:
      1) Por URL: si la URL del lead y alguna URL del Config coinciden como
         subcadena en cualquiera de los dos sentidos (una contenida en la otra).
      2) Por referencia: si la ref del lead, una vez normalizada (_ref_norm),
         es igual a alguna ref normalizada de la fila.

    Parametros:
        lead: dict del lead, con claves como "url", "ref", "fuente".

    Devuelve: la tupla (feuille, iad_url) de la hoja que coincide, o
    (None, None) si no se encuentra ninguna.
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

        # Matching por URL: coincidencia como subcadena en ambos sentidos
        # (la URL del Config dentro de la del lead o viceversa), util cuando
        # una de las dos lleva parametros extra de seguimiento.
        if lead_url:
            for u in urls:
                u = u.strip().lower()
                if u and (u in lead_url or lead_url in u):
                    print(f"[match]   ✅ match URL -> feuille='{feuille}'")
                    return feuille, _pick_url(row)

        # Matching por referencia: comparacion de refs ya normalizadas
        # (alfanumerico en minusculas) para tolerar formatos distintos.
        if lead_ref:
            for r in refs:
                if r and _ref_norm(r) == lead_ref:
                    print(f"[match]   ✅ match REF -> feuille='{feuille}'")
                    return feuille, _pick_url(row)

    print(f"[match]   ❌ aucun match pour ref='{lead_ref}' url='{lead_url}'")
    return None, None


# ---------------------------------------------------------------------------
# Hojas de prospectos
# ---------------------------------------------------------------------------
def get_iad_url_for_sheet(feuille):
    """
    Devuelve la URL del bien asociada a una hoja, para usarla en los relances.

    Busca en el Config la fila cuya columna "feuille" coincide con el nombre de
    hoja indicado (comparando en minusculas y sin espacios) y aplica _pick_url
    para escoger la URL por prioridad C->E->G->I.

    Parametros:
        feuille: nombre de la hoja de prospectos.

    Devuelve: la URL del bien, o "" si la hoja no aparece en el Config.
    """
    cc = config.CONFIG_COL
    target = (feuille or "").strip().lower()
    for row in load_config_rows():
        feuille_cfg = (row[cc["feuille"]] if cc["feuille"] < len(row) else "").strip().lower()
        if feuille_cfg == target:
            return _pick_url(row)
    return ""


def _get_worksheet(name):
    """
    Devuelve la worksheet cuyo titulo coincide con el nombre dado.

    La comparacion es tolerante: se aplica strip().lower() a ambos lados. El
    resultado se cachea en _ws_objs por nombre. IMPORTANTE: nunca crea una
    hoja nueva; si no la encuentra, registra las hojas existentes y devuelve
    None.

    Parametros:
        name: nombre (titulo) de la hoja buscada.

    Devuelve: el objeto worksheet, o None si no existe.
    """
    if name in _ws_objs:
        return _ws_objs[name]
    target = (name or "").strip().lower()
    all_ws = _all_worksheets()
    for ws in all_ws:
        if ws.title.strip().lower() == target:
            _ws_objs[name] = ws
            return ws
    print(f"[sheets] feuille introuvable: '{name}' (repr: {repr(name)}) ; "
          f"existantes: {[w.title for w in all_ws]}")
    return None


def worksheet_exists(name):
    """
    Indica si existe una hoja con el nombre dado.

    Parametros:
        name: nombre (titulo) de la hoja.

    Devuelve: True si _get_worksheet la encuentra, False en caso contrario.
    """
    return _get_worksheet(name) is not None


def find_prospect(ws, phone="", email=""):
    """
    Busca un prospecto existente en una hoja por telefono O por email.

    Deduplicacion: recorre las filas de datos y devuelve la primera fila cuyo
    telefono normalizado coincida con el del lead, o cuyo email (en minusculas)
    coincida. Asi se evita crear duplicados de un mismo contacto.

    Parametros:
        ws: objeto worksheet de la hoja de prospectos.
        phone: telefono del lead (se normaliza con normalize_phone).
        email: email del lead.

    Devuelve: (row_index, row_values) si lo encuentra, o (None, None). El
    row_index es 1-based (contando la cabecera), apto para la API de Sheets.
    """
    phone_n = normalize_phone(phone)
    email_n = (email or "").strip().lower()
    values = _values_for(ws)
    # Los datos empiezan en la fila 4 (config.DATA_START_ROW); las filas 1-3
    # estan reservadas para cabeceras y no se tocan.
    start = config.DATA_START_ROW
    for i, row in enumerate(values[start - 1:], start=start):
        r_phone = normalize_phone(row[config.COL["telefono"] - 1] if len(row) >= config.COL["telefono"] else "")
        r_email = (row[config.COL["email"] - 1] if len(row) >= config.COL["email"] else "").strip().lower()
        if phone_n and r_phone and phone_n == r_phone:
            return i, row
        if email_n and r_email and email_n == r_email:
            return i, row
    return None, None


def upsert_prospect(feuille, lead):
    """
    Inserta o actualiza un prospecto en la hoja `feuille` (logica de upsert).

    Comportamiento:
      - Si el prospecto no existe (find_prospect no lo encuentra), inserta una
        fila nueva en la primera fila libre (nunca antes de la fila 4) con los
        datos basicos y el estado inicial "Nuevo contacto". La columna Notas
        se deja vacia.
      - Si ya existe, solo completa los campos que esten vacios (nombre, email,
        telefono); nunca sobrescribe datos existentes y nunca toca Notas.

    Parametros:
        feuille: nombre de la hoja de prospectos destino.
        lead: dict del lead con nombre/telefono/email/fuente.

    Devuelve: (row_index, is_new, row_values). Si la hoja no existe,
    (None, False, None).
    """
    ws = _get_worksheet(feuille)
    if ws is None:
        return None, False, None
    row_idx, existing = find_prospect(ws, lead.get("telefono", ""), lead.get("email", ""))
    today = datetime.date.today().isoformat()

    # Limpieza: ningun dato se copia con espacios sobrantes.
    nombre = (lead.get("nombre") or "").strip()
    telefono = (lead.get("telefono", "") or "").strip()
    email = (lead.get("email", "") or "").strip()
    fuente = (lead.get("fuente", "") or "").strip()
    # La columna E (Notas) se deja VACIA: el prospecto la rellenara a mano.

    if row_idx is None:
        # --- Insercion de un prospecto nuevo ---
        new_row = [""] * len(config.PROSPECT_HEADERS)
        new_row[config.COL["nombre"] - 1] = nombre
        new_row[config.COL["telefono"] - 1] = telefono
        new_row[config.COL["email"] - 1] = email
        new_row[config.COL["fuente"] - 1] = fuente
        new_row[config.COL["fecha_contacto"] - 1] = today  # col I: fecha de 1a deteccion
        new_row[config.COL["estado_final"] - 1] = "Nuevo contacto"
        vals = _values_for(ws)
        # Escribe en la primera fila libre, nunca antes de la fila 4
        # (las filas 1-3 estan reservadas para cabeceras).
        new_index = max(len(vals) + 1, config.DATA_START_ROW)
        _write_throttle()
        ws.update(range_name=f"A{new_index}", values=[new_row],
                  value_input_option="USER_ENTERED")
        # Mantiene la cache sincronizada con lo que acabamos de escribir.
        while len(vals) < new_index:
            vals.append([])
        vals[new_index - 1] = new_row
        return new_index, True, new_row

    # --- Actualizacion: solo completa los campos vacios (nunca Notas) ---
    def _existing(col_name):
        idx = config.COL[col_name] - 1
        return (existing[idx] if len(existing) > idx else "").strip()

    # Solo se rellena un campo si el lead lo aporta y la celda esta vacia.
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
    """
    Actualiza un valor concreto en la cache en memoria de una hoja.

    Mantiene _values_cache coherente con las escrituras hechas en Google
    Sheets, ampliando la matriz cacheada (filas/columnas) si hace falta para
    poder fijar la celda indicada.

    Parametros:
        name: titulo de la hoja en la cache.
        row_idx: fila 1-based.
        col_idx: columna 1-based.
        val: valor a guardar.

    No devuelve valor (si la hoja no esta cacheada, no hace nada).
    """
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
    """
    Actualiza varias celdas de una fila en una hoja de prospectos.

    Por cada par (nombre de columna -> valor) escribe la celda correspondiente
    en Google Sheets, respetando el throttle entre escrituras y manteniendo la
    cache sincronizada con _cache_set.

    Parametros:
        feuille: nombre de la hoja.
        row_idx: fila 1-based a actualizar.
        col_values: dict {nombre_de_columna_de_config.COL: valor}.

    No devuelve valor (si la hoja no existe, no hace nada).
    """
    ws = _get_worksheet(feuille)
    if ws is None:
        return
    for col_name, val in col_values.items():
        col_idx = config.COL[col_name]
        _write_throttle()
        ws.update_cell(row_idx, col_idx, val)
        _cache_set(ws.title, row_idx, col_idx, val)


def get_cell(feuille, row_idx, col_name):
    """
    Lee el valor de una celda concreta de una hoja de prospectos.

    Usa los valores cacheados (_values_for) y devuelve "" si la fila/columna
    queda fuera de rango.

    Parametros:
        feuille: nombre de la hoja.
        row_idx: fila 1-based.
        col_name: nombre de columna definido en config.COL.

    Devuelve: el contenido de la celda como cadena, o "".
    """
    ws = _get_worksheet(feuille)
    if ws is None:
        return ""
    vals = _values_for(ws)
    idx = config.COL[col_name] - 1
    if row_idx - 1 < len(vals) and idx < len(vals[row_idx - 1]):
        return vals[row_idx - 1][idx] or ""
    return ""


def list_all_prospect_sheets():
    """
    Devuelve los nombres de todas las hojas de prospectos.

    Excluye cualquier pestana cuyo titulo sea o contenga "config" (la pestana
    de configuracion no es una hoja de prospectos).

    Devuelve: lista de nombres de hoja (cadenas).
    """
    names = []
    for ws in _all_worksheets():
        if ws.title == config.CONFIG_SHEET_NAME or "config" in ws.title.lower():
            continue
        names.append(ws.title)
    return names


def setup_estado_validation():
    """
    Configura la columna "Estado final" (col L) en todas las hojas de prospectos.

    En una sola llamada batch_update aplica, por cada hoja:
      - Una lista desplegable (validacion de datos ONE_OF_LIST) con las opciones
        de config.ESTADO_FINAL_OPTIONS.
      - Un formato condicional que pinta de rojo claro (#FF6B6B) las celdas
        cuyo valor sea config.ERROR_WA_STATE ("Error envío WA").
    Se omiten las pestanas Config y de menu. La validacion es idempotente
    (reemplaza la existente). El rango empieza en la fila 4 (DATA_START_ROW),
    dejando intactas las filas 1-3.

    Devuelve: lista de los nombres de hoja procesados.
    """
    ss = _get_spreadsheet()
    options = config.ESTADO_FINAL_OPTIONS
    col = config.COL["estado_final"] - 1  # col L -> indice 11 (0-based)
    requests = []
    done = []
    for ws in _all_worksheets():
        t = ws.title.strip().lower()
        # Salta las pestanas de configuracion y de menu.
        if "config" in t or t in ("menú", "menu"):
            continue
        # Rango de la columna L desde la fila 4 hasta el final de la hoja.
        rng = {
            "sheetId": ws.id,
            "startRowIndex": config.DATA_START_ROW - 1,  # fila 4 -> indice 3
            "endRowIndex": ws.row_count or 1000,
            "startColumnIndex": col,
            "endColumnIndex": col + 1,
        }
        # Lista desplegable (ONE_OF_LIST) con las opciones de estado.
        requests.append({"setDataValidation": {"range": rng, "rule": {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": v} for v in options]},
            "showCustomUi": True, "strict": False}}})
        # Formato condicional por estado (colores definidos en config.ESTADO_COLORS).
        for estado, spec in config.ESTADO_COLORS.items():
            r, g, b = spec["bg"]
            fmt = {"backgroundColor": {"red": r, "green": g, "blue": b}}
            if spec.get("white"):
                fmt["textFormat"] = {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}
            requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": [rng],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": estado}]},
                    "format": fmt}},
                "index": 0}})
        done.append(ws.title)
    if requests:
        _write_throttle()
        ss.batch_update({"requests": requests})
    print(f"[sheets] listes déroulantes appliquées sur {len(done)} feuilles: {done}")
    return done


# ---------------------------------------------------------------------------
# Gestion des biens (ajout / clôture)
# Gestion de bienes (alta / cierre)
# ---------------------------------------------------------------------------
# Regex para extraer una secuencia de 6 o mas digitos (la referencia de un
# anuncio dentro de su URL). Nombre y patron NO se modifican.
_RE_URL_DIGITS = re.compile(r"(\d{6,})")
# Titulo de la hoja plantilla que se duplica al dar de alta un bien.
BIEN_TEMPLATE = os.environ.get("BIEN_TEMPLATE", "Hoja ejemplo")


def _ref_from_url(url):
    """
    Extrae la referencia del anuncio a partir de su URL.

    Toma la ultima secuencia de 6 o mas digitos que aparece en la URL
    (_RE_URL_DIGITS), que suele ser el identificador del anuncio.

    Parametros:
        url: URL del anuncio.

    Devuelve: la referencia (cadena de digitos), o "" si no hay ninguna.
    """
    if not url:
        return ""
    found = _RE_URL_DIGITS.findall(url)
    return found[-1] if found else ""


def bien_exists(name):
    """Indica si un bien ya existe (en la pestaña Config o como hoja)."""
    target = (name or "").strip().lower()
    if not target:
        return False
    for row in load_config_rows():
        if (row[0] if row else "").strip().lower() == target:
            return True
    return worksheet_exists(name)


def list_active_biens():
    """
    Devuelve los nombres de los bienes activos.

    Lee la columna A de la pestana Config y excluye los bienes ya cerrados,
    es decir, los que llevan el prefijo "VEND " (vendidos).

    Devuelve: lista de nombres de bien activos (cadenas).
    """
    out = []
    for row in load_config_rows():
        a = (row[0] if row else "").strip()
        if a and not a.lower().startswith("vend "):
            out.append(a)
    return out


def add_bien(data):
    """
    Da de alta un bien nuevo: crea su fila en Config y su hoja de prospectos.

    Pasos:
      1) Valida que el nombre y la descripcion no esten vacios y que el bien no
         exista ya (ni en Config ni como hoja).
      2) Duplica la hoja plantilla BIEN_TEMPLATE y la renombra con el nombre del
         bien; luego la limpia (borra los datos de la fila 4 en adelante,
         conservando las cabeceras de las filas 1-3) y escribe la descripcion
         en A2.
      3) Anade una fila en la pestana Config con las URLs y sus referencias
         (extraidas con _ref_from_url).
      4) Reinicia la cache (reset_cache) para reflejar la nueva hoja.

    Parametros:
        data: dict con "nom", "description" y las URLs por portal
              (url_idealista, url_fotocasa, url_habitaclia, url_iad).

    Devuelve: mensaje de confirmacion (cadena).
    Lanza ValueError si faltan datos o el bien ya existe.
    """
    nom = (data.get("nom") or "").strip()
    desc = (data.get("description") or "").strip()
    if not nom or not desc:
        raise ValueError("Nom de la feuille et description obligatoires")
    url_idea = (data.get("url_idealista") or "").strip()
    url_foto = (data.get("url_fotocasa") or "").strip()
    url_habi = (data.get("url_habitaclia") or "").strip()
    url_iad = (data.get("url_iad") or "").strip()

    ss = _get_spreadsheet()
    target = nom.strip().lower()
    for row in load_config_rows():
        if (row[0] if row else "").strip().lower() == target:
            raise ValueError(f"Le bien '{nom}' existe déjà dans l'onglet Config")
    if _get_worksheet(nom) is not None:
        raise ValueError(f"Une feuille '{nom}' existe déjà")

    template = _get_worksheet(BIEN_TEMPLATE)
    if template is None:
        raise ValueError(f"Feuille template '{BIEN_TEMPLATE}' introuvable")

    # Duplica la hoja plantilla y la renombra con el nombre del bien.
    new_ws = ss.duplicate_sheet(template.id, new_sheet_name=nom)
    # Vacia los datos (fila 4 en adelante) y conserva las cabeceras (filas 1-3).
    _write_throttle()
    new_ws.batch_clear([f"A{config.DATA_START_ROW}:L{new_ws.row_count}"])
    # En la fila 2 (A2) se escribe la descripcion como titulo del bien.
    _write_throttle()
    new_ws.update(range_name="A2", values=[[desc]], value_input_option="USER_ENTERED")

    # Formato del titulo (fila 2): fusion A2:L2, fondo amarillo, negrita, centrado.
    gid = new_ws.id
    title_range = {"sheetId": gid, "startRowIndex": 1, "endRowIndex": 2,
                   "startColumnIndex": 0, "endColumnIndex": 12}
    fmt_reqs = [
        {"unmergeCells": {"range": title_range}},
        {"mergeCells": {"mergeType": "MERGE_ALL", "range": title_range}},
        {"repeatCell": {"range": title_range,
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": _rgb("FFFF00"),
                            "textFormat": {"bold": True},
                            "horizontalAlignment": "CENTER"}},
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
    ]
    # Cabecera de navegacion (fila 1) + ref N1 para la hoja recien creada.
    menu_gid = next((w.id for w in _all_worksheets()
                     if w.title.strip().lower() in ("menú", "menu")), None)
    fmt_reqs += _layout_requests(gid, menu_gid, url_idea, url_foto, _ref_from_url(url_idea))
    try:
        _write_throttle()
        ss.batch_update({"requests": fmt_reqs})
    except Exception as e:  # noqa: BLE001
        print(f"[add_bien] mise en page échouée pour '{nom}': {e}")

    # Enriquecimiento automático: busca el anuncio IAD correspondiente (best-effort).
    # Rellena URL/Ref IAD (I/J) y superficie/terreno/habitaciones (M/N/O) si se encuentra.
    surf_hab = surf_terr = habs = matched_ref = ""
    extra = []
    try:
        import iad_scraper
        import database as dbm
        parsed = config.parse_bien_info(nom, dbm.get_city_names())
        listings, err = iad_scraper.scrape_iad_listings(dbm.get_iad_profile_url())
        if err:
            extra.append(f"⚠️ scraping IAD: {err}")
        else:
            match = iad_scraper.find_match(
                listings, price=_price_to_num(parsed["price"]),
                city=parsed["city"], type_code=parsed["type_code"])
            if match:
                url_iad = url_iad or match.get("url", "")
                matched_ref = match.get("ref", "")
                surf_hab = match.get("surface_hab", "")
                surf_terr = match.get("surface_terreno", "")
                habs = match.get("habitaciones", "")
                extra.append(f"✅ URL IAD trouvée ({matched_ref})")
                if surf_hab:
                    extra.append(f"{surf_hab} m²")
                if habs:
                    extra.append(f"{habs} hab.")
            else:
                extra.append("⚠️ Aucune annonce IAD correspondante")
    except Exception as e:  # noqa: BLE001
        extra.append(f"⚠️ enrichissement IAD échoué: {e}")

    ref_iad_val = _ref_from_url(url_iad) or matched_ref

    # Fila correspondiente en la pestana Config (A..P).
    config_ws = _get_config_worksheet()
    new_row = [""] * 16
    new_row[0] = nom
    new_row[1] = desc
    new_row[2] = url_idea
    new_row[3] = _ref_from_url(url_idea)
    new_row[4] = url_foto
    new_row[5] = _ref_from_url(url_foto)
    new_row[6] = url_habi
    new_row[7] = _ref_from_url(url_habi)
    new_row[8] = url_iad
    new_row[9] = ref_iad_val
    new_row[12] = surf_hab       # M
    new_row[13] = surf_terr      # N
    new_row[14] = habs           # O
    _write_throttle()
    config_ws.append_row(new_row, value_input_option="USER_ENTERED")
    reset_cache()
    msg = f"Bien ajouté : {nom}"
    if extra:
        msg += " — " + ", ".join(extra)
    return msg


def close_bien(nom):
    """
    Cierra (marca como vendido) un bien.

    Renombra su hoja anteponiendo el prefijo "VEND " al titulo, hace lo mismo
    con el nombre en la pestana Config y vacia las URLs de ese bien en el
    Config. Reinicia la cache al terminar.

    Parametros:
        nom: nombre del bien a cerrar.

    Devuelve: mensaje de confirmacion (cadena).
    Lanza ValueError si falta el nombre, el bien ya esta cerrado o su hoja no
    existe.
    """
    nom = (nom or "").strip()
    if not nom:
        raise ValueError("Nom du bien requis")
    if nom.lower().startswith("vend "):
        raise ValueError("Ce bien est déjà clôturé")
    ws = _get_worksheet(nom)
    if ws is None:
        raise ValueError(f"Feuille '{nom}' introuvable")

    # Nuevo titulo con el prefijo de bien vendido.
    new_title = "VEND " + nom
    _write_throttle()
    ws.update_title(new_title)

    # Refleja el cierre en la pestana Config: renombra y vacia las URLs.
    config_ws = _get_config_worksheet()
    cc = config.CONFIG_COL
    values = config_ws.get_all_values()
    target = nom.strip().lower()
    for i, row in enumerate(values[1:], start=2):
        if (row[0] if row else "").strip().lower() == target:
            _write_throttle()
            config_ws.update_cell(i, cc["feuille"] + 1, new_title)
            # Borra las cuatro URLs del bien (ya no esta publicado).
            for urlcol in ("url_idealista", "url_fotocasa", "url_habitaclia", "url_iad"):
                _write_throttle()
                config_ws.update_cell(i, cc[urlcol] + 1, "")
            break
    reset_cache()
    return f"Bien clôturé : {new_title}"


# ---------------------------------------------------------------------------
# Sincronización Config <-> hojas + cabecera de navegación (fila 1) + ref (N1)
# ---------------------------------------------------------------------------
def _rgb(hexs):
    """Convierte un color hex 'RRGGBB' en el dict {red,green,blue} (0-1) de la API."""
    return {"red": int(hexs[0:2], 16) / 255,
            "green": int(hexs[2:4], 16) / 255,
            "blue": int(hexs[4:6], 16) / 255}


def _cell(text, bg_hex, white=True, size=None, link=None):
    """Construye un dict de celda (texto + formato) para updateCells.

    Si se pasa `link`, el hipervínculo se aplica mediante textFormatRuns.link.uri
    (NO con la fórmula HYPERLINK, que da #ERROR! en celdas fusionadas).
    """
    fg = _rgb("FFFFFF" if white else "000000")
    tf = {"bold": True, "foregroundColor": fg}
    if size:
        tf["fontSize"] = size
    cell = {
        "userEnteredValue": {"stringValue": text},
        "userEnteredFormat": {"backgroundColor": _rgb(bg_hex),
                              "textFormat": tf, "horizontalAlignment": "CENTER",
                              "verticalAlignment": "MIDDLE"},
    }
    if link:
        # El enlace se define como un "run" de formato sobre todo el texto.
        run_fmt = {"link": {"uri": link}, "bold": True, "foregroundColor": fg, "underline": False}
        cell["textFormatRuns"] = [{"startIndex": 0, "format": run_fmt}]
    return cell


def _update_cell_req(gid, col_index, cell):
    """Genera un request updateCells para una sola celda (fila 1)."""
    return {"updateCells": {
        "rows": [{"values": [cell]}],
        "fields": "userEnteredValue,userEnteredFormat,textFormatRuns",
        "start": {"sheetId": gid, "rowIndex": 0, "columnIndex": col_index}}}


def _layout_requests(gid, menu_gid, url_idea, url_foto, ref_idea):
    """Construye los requests de batch_update para la fila 1 (navegación) y N1 (ref)."""
    reqs = []
    row1 = {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 12}
    # 1) Deshacer la fusión actual A1:L1 (si la hay) y crear dos fusiones nuevas.
    reqs.append({"unmergeCells": {"range": row1}})
    reqs.append({"mergeCells": {"mergeType": "MERGE_ALL", "range": {
        "sheetId": gid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 5}}})
    reqs.append({"mergeCells": {"mergeType": "MERGE_ALL", "range": {
        "sheetId": gid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 5, "endColumnIndex": 12}}})
    # 2) A1:E1 -> "Volver al Menú" con enlace interno (#gid=...) vía textFormatRuns.
    menu_link = f"#gid={menu_gid}" if menu_gid is not None else None
    reqs.append(_update_cell_req(gid, 0, _cell("🏠 Volver al Menú", "1F4E78", white=True, link=menu_link)))
    # 3) F1:L1 -> "Ver anuncio". Prioridad Idealista, luego Fotocasa; si no, gris sin enlace.
    if url_idea:
        anuncio = _cell("🔗 Ver anuncio en Idealista", "00B1EB", white=True, link=url_idea)
    elif url_foto:
        anuncio = _cell("🔗 Ver anuncio en Fotocasa", "00B1EB", white=True, link=url_foto)
    else:
        anuncio = _cell("🔗 Ver anuncio", "CCCCCC", white=False)
    reqs.append(_update_cell_req(gid, 5, anuncio))
    # 4) N1 -> "Ref. Idealista: XXXX" fondo azul claro, texto blanco, tamaño 9 (sin enlace).
    if ref_idea:
        reqs.append(_update_cell_req(gid, 13, _cell(f"Ref. Idealista: {ref_idea}", "00B1EB",
                                                    white=True, size=9)))
    return reqs


def sync_sheets(stats):
    """Sincroniza, en cada run completo: descripción Config (col B) = fila 2 de la hoja,
    y reescribe la cabecera de navegación (fila 1) + la celda N1 con la ref de Idealista.
    Aísla cada hoja en try/except para que un fallo de formato no bloquee el pipeline.
    """
    ss = _get_spreadsheet()
    cc = config.CONFIG_COL
    menu_gid = next((w.id for w in _all_worksheets()
                     if w.title.strip().lower() in ("menú", "menu")), None)
    config_ws = _get_config_worksheet()
    cfg_values = config_ws.get_all_values()
    cfg_index = {}
    for i, row in enumerate(cfg_values[1:], start=2):
        name = (row[cc["feuille"]] if cc["feuille"] < len(row) else "").strip()
        if name:
            cfg_index[name.lower()] = (i, row)

    for ws in _all_worksheets():
        t = ws.title
        if "config" in t.lower() or t.strip().lower() in ("menú", "menu"):
            continue
        try:
            entry = cfg_index.get(t.strip().lower())
            cfg_row = entry[1] if entry else []

            def g(idx):
                return (cfg_row[idx] if idx < len(cfg_row) else "").strip()

            # A) Sincroniza la descripción (col B Config) con la fila 2 (A2) de la hoja.
            vals = _values_for(ws)
            title2 = (vals[1][0] if len(vals) > 1 and vals[1] else "").strip()
            if entry and title2 and title2 != g(cc["description"]):
                _write_throttle()
                config_ws.update_cell(entry[0], cc["description"] + 1, title2)
                print(f"[config] mise à jour description '{t}'")
                stats.setdefault("details", []).append(f"Config descripción actualizada: {t}")

            # B+C) Cabecera de navegación (fila 1) + ref en N1.
            reqs = _layout_requests(ws.id, menu_gid,
                                    g(cc["url_idealista"]), g(cc["url_fotocasa"]), g(cc["ref_idealista"]))
            _write_throttle()
            ss.batch_update({"requests": reqs})
        except Exception as e:  # noqa: BLE001
            stats.setdefault("errors", []).append(f"sync_sheets {t}: {e}")
            print(f"[sync] échec mise en page '{t}': {e}")


def sync_all_sheets():
    """Sincroniza la navegación (fila 1) y la descripción (col B Config) en TODAS las hojas.

    Para cada hoja de inmueble (ignora Menú, Config, Leads sin clasificar, Hoja ejemplo):
      - reescribe la cabecera de navegación de la fila 1 (A1:E1 Menú / F1:L1 Ver anuncio),
      - actualiza la col B de Config con el título de la fila 2 si difiere.
    Devuelve un resumen: {status, sheets_updated, navigation_updated, config_updated}.
    """
    ss = _get_spreadsheet()
    cc = config.CONFIG_COL
    ignore = {"menú", "menu", "leads sin clasificar", "hoja ejemplo"}
    menu_gid = next((w.id for w in _all_worksheets()
                     if w.title.strip().lower() in ("menú", "menu")), None)
    config_ws = _get_config_worksheet()
    cfg_values = config_ws.get_all_values()
    cfg_index = {}
    for i, row in enumerate(cfg_values[1:], start=2):
        name = (row[cc["feuille"]] if cc["feuille"] < len(row) else "").strip()
        if name:
            cfg_index[name.lower()] = (i, row)

    sheets_updated = navigation_updated = config_updated = 0
    for ws in _all_worksheets():
        t = ws.title
        tl = t.strip().lower()
        if tl in ignore or "config" in tl:
            continue
        try:
            entry = cfg_index.get(tl)
            cfg_row = entry[1] if entry else []

            def g(idx):
                return (cfg_row[idx] if idx < len(cfg_row) else "").strip()

            touched = False
            # 1) Sincroniza col B (descripción) con la fila 2 (A2) de la hoja.
            vals = _values_for(ws)
            title2 = (vals[1][0] if len(vals) > 1 and vals[1] else "").strip()
            if entry and title2 and title2 != g(cc["description"]):
                _write_throttle()
                config_ws.update_cell(entry[0], cc["description"] + 1, title2)
                config_updated += 1
                touched = True
                print(f"[sync] {t} → Config col B mis à jour : \"{title2}\"")

            # 2) Cabecera de navegación (fila 1) + ref N1.
            reqs = _layout_requests(ws.id, menu_gid,
                                    g(cc["url_idealista"]), g(cc["url_fotocasa"]), g(cc["ref_idealista"]))
            _write_throttle()
            ss.batch_update({"requests": reqs})
            navigation_updated += 1
            touched = True
            print(f"[sync] {t} → navigation mise à jour")

            if touched:
                sheets_updated += 1
        except Exception as e:  # noqa: BLE001
            print(f"[sync] échec '{t}': {e}")

    return {"status": "ok", "sheets_updated": sheets_updated,
            "navigation_updated": navigation_updated, "config_updated": config_updated}


def _price_to_num(p):
    """Convierte un precio abreviado ('55k', '12 500') en su valor numérico en texto."""
    p = (p or "").strip().lower()
    if not p:
        return ""
    digits = "".join(c for c in p if c.isdigit())
    if p.endswith("k") and digits:
        return str(int(digits) * 1000)
    return digits


def sync_iad_urls(profile_url=None):
    """Sincroniza las URLs/datos de los anuncios IAD con la pestaña Config.

    Scrapea el perfil IAD, empareja cada anuncio con una fila de Config (por ref IAD
    en col J o, si no, por precio+tipo+ciudad) y actualiza las columnas I (URL IAD),
    J (Ref IAD), M (sup. habitable), N (sup. terreno), O (habitaciones).
    Devuelve un informe dict con: updated, iad_no_match, config_no_iad, scraped, error.
    """
    import iad_scraper
    import database as dbm
    profile_url = profile_url or dbm.get_iad_profile_url()
    listings, err = iad_scraper.scrape_iad_listings(profile_url)
    report = {"updated": [], "iad_no_match": [], "config_no_iad": [],
              "scraped": len(listings), "error": err}
    if err:
        return report

    cc = config.CONFIG_COL
    config_ws = _get_config_worksheet()
    values = config_ws.get_all_values()
    city_names = dbm.get_city_names()
    matched = set()

    for i, row in enumerate(values[1:], start=2):
        def g(idx):
            return (row[idx] if idx < len(row) else "").strip()
        feuille = g(cc["feuille"])
        if not feuille or feuille.lower().startswith("vend "):
            continue
        parsed = config.parse_bien_info(feuille, city_names)
        ref_iad = g(cc["ref_iad"])
        match = None
        # 1) por referencia IAD ya registrada
        if ref_iad:
            for it in listings:
                if it.get("ref") and "".join(filter(str.isdigit, it["ref"])) == \
                        "".join(filter(str.isdigit, ref_iad)):
                    match = it
                    break
        # 2) por precio + tipo + ciudad
        if not match:
            match = iad_scraper.find_match(
                listings, price=_price_to_num(parsed["price"]),
                city=parsed["city"], type_code=parsed["type_code"])
        if not match:
            report["config_no_iad"].append(feuille)
            continue
        matched.add(id(match))
        updates = {}
        if match.get("url"):
            updates[cc["url_iad"] + 1] = match["url"]
        if match.get("ref"):
            updates[cc["ref_iad"] + 1] = match["ref"]
        if match.get("surface_hab"):
            updates[cc["surface_hab"] + 1] = match["surface_hab"]
        if match.get("surface_terreno"):
            updates[cc["surface_terreno"] + 1] = match["surface_terreno"]
        if match.get("habitaciones"):
            updates[cc["habitaciones"] + 1] = match["habitaciones"]
        for colnum, val in updates.items():
            _write_throttle()
            config_ws.update_cell(i, colnum, val)
        report["updated"].append({"feuille": feuille, "ref": match.get("ref"),
                                  "surface": match.get("surface_hab"),
                                  "habitaciones": match.get("habitaciones")})

    for it in listings:
        if id(it) not in matched:
            report["iad_no_match"].append({"ref": it.get("ref"), "url": it.get("url"),
                                           "price": it.get("price")})
    reset_cache()
    return report


def diag():
    """
    Diagnostico: comprueba el acceso a Google Sheets y resume su contenido.

    Verifica que se puede abrir la hoja de calculo, lista sus pestanas, cuenta
    las filas del Config (con una muestra de hasta 10 filas) y comprueba la
    presencia de la hoja de repli (FALLBACK_SHEET, que nunca se crea de forma
    automatica). Captura cualquier excepcion y la registra en el resultado.

    Devuelve: un dict con la informacion de diagnostico (titulo de la hoja,
    pestanas, numero de filas de Config, muestra, hoja de repli y posibles
    errores).
    """
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
    # Presencia de la hoja de repli (nunca se crea automaticamente).
    try:
        ws = _get_worksheet(config.FALLBACK_SHEET)
        out["fallback_sheet"] = ws.title if ws else "ABSENTE (à créer manuellement)"
    except Exception as e:  # noqa: BLE001
        out["error_write"] = f"{type(e).__name__}: {e}"
    return out


def iter_prospects(feuille):
    """
    Generador que recorre todos los prospectos de una hoja.

    Por cada fila de datos (a partir de la fila 4) produce una tupla
    (row_idx, dict_columnas) con el indice 1-based de la fila y un diccionario
    con todas las columnas del prospecto (nombre, telefono, email, fuente,
    notas, presupuesto, etc.).

    Parametros:
        feuille: nombre de la hoja de prospectos.

    Devuelve/Genera: pares (row_idx, dict). Si la hoja no existe, no produce
    nada (return temprano).
    """
    ws = _get_worksheet(feuille)
    if ws is None:
        return
    values = _values_for(ws)
    # Los datos empiezan en la fila 4; las filas 1-3 estan reservadas.
    start = config.DATA_START_ROW
    for i, row in enumerate(values[start - 1:], start=start):
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
