"""
Base de datos SQLite del CRM "iad-crm".

Almacena las respuestas de WhatsApp recibidas a través del webhook de UltraMsg,
junto con el estado de las ejecuciones (runs) del pipeline: el timestamp del
último run completado y el timestamp del último chequeo de respuestas.

Este modulo expone funciones de acceso a dos tablas:
  - incoming_messages: cada mensaje entrante recibido por el webhook.
  - run_state: pares clave/valor que guardan el estado del pipeline.
"""
import sqlite3
import json
import time
from contextlib import contextmanager

from config import DB_PATH


@contextmanager
def _conn():
    """Gestor de contexto que abre una conexion a la base SQLite.

    Configura la conexion para que cada fila se devuelva como sqlite3.Row
    (acceso por nombre de columna). Al salir del bloque without errores
    confirma (commit) la transaccion y siempre cierra la conexion.

    Parametros:
        Ninguno.

    Devuelve:
        Un objeto de conexion sqlite3.Connection (mediante yield), listo
        para ejecutar consultas dentro del bloque ``with``.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row  # filas accesibles por nombre de columna
    try:
        yield conn
        conn.commit()  # confirma los cambios si no hubo excepciones
    finally:
        conn.close()  # cierra la conexion pase lo que pase


def init_db():
    """Crea las tablas y los indices de la base si aun no existen.

    Es idempotente: puede llamarse en cada arranque sin riesgo, ya que
    todas las sentencias usan ``IF NOT EXISTS``. Crea la tabla
    ``incoming_messages`` (mensajes entrantes de WhatsApp), sus indices y
    la tabla clave/valor ``run_state``.

    Parametros:
        Ninguno.

    Devuelve:
        None.
    """
    with _conn() as c:
        # Crea la tabla de mensajes entrantes si no existe.
        # Columnas: id autoincremental, telefono, cuerpo del mensaje,
        # fecha de recepcion (timestamp), bandera de procesado y JSON crudo.
        c.execute("""
            CREATE TABLE IF NOT EXISTS incoming_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                phone       TEXT NOT NULL,
                body        TEXT,
                received_at REAL NOT NULL,
                processed   INTEGER NOT NULL DEFAULT 0,
                raw         TEXT
            )
        """)
        # Indice por telefono: acelera la busqueda de mensajes de un numero.
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_phone ON incoming_messages(phone)")
        # Indice por bandera de procesado: acelera filtrar los no procesados.
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_processed ON incoming_messages(processed)")
        # Crea la tabla de estado del pipeline (almacen clave/valor) si no existe.
        c.execute("""
            CREATE TABLE IF NOT EXISTS run_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)


# ---------------------------------------------------------------------------
# Mensajes de WhatsApp entrantes
# ---------------------------------------------------------------------------
def save_incoming_message(phone, body, raw=None, received_at=None):
    """Guarda un mensaje recibido a traves del webhook de UltraMsg.

    Normaliza el telefono y establece la fecha de recepcion al instante
    actual si no se indica. El mensaje se inserta como no procesado.

    Parametros:
        phone (str): numero de telefono del remitente (se normaliza).
        body (str): texto del mensaje recibido.
        raw (opcional): carga util cruda del webhook; se serializa a JSON.
        received_at (float, opcional): timestamp de recepcion; si es None
            se usa time.time().

    Devuelve:
        int: el id (lastrowid) de la fila insertada.
    """
    phone = normalize_phone(phone)
    received_at = received_at or time.time()
    with _conn() as c:
        # Inserta el mensaje entrante; processed se fija a 0 (no procesado)
        # y raw se guarda como JSON solo si se recibio carga cruda.
        cur = c.execute(
            "INSERT INTO incoming_messages (phone, body, received_at, processed, raw) "
            "VALUES (?, ?, ?, 0, ?)",
            (phone, body, received_at, json.dumps(raw) if raw else None),
        )
        return cur.lastrowid


def get_broker():
    """Devuelve (nombre, teléfono) del bróker, con override de la base de datos.

    Lee los valores guardados desde el dashboard; si no hay, usa los valores
    por defecto de config (BROKER_NAME / BROKER_PHONE).
    """
    import config
    name = get_state("broker_name") or config.BROKER_NAME
    phone = get_state("broker_phone") or config.BROKER_PHONE
    return name, phone


def set_broker(name=None, phone=None):
    """Guarda el nombre y/o el teléfono del bróker (override del dashboard)."""
    if name:
        set_state("broker_name", name)
    if phone:
        set_state("broker_phone", phone)


def get_custom_cities():
    """Devuelve el dict de ciudades añadidas desde el dashboard (abreviatura->nombre)."""
    raw = get_state("custom_cities")
    return json.loads(raw) if raw else {}


def add_custom_city(abbrev, full):
    """Añade (o actualiza) una ciudad personalizada en la base de datos."""
    cities = get_custom_cities()
    cities[abbrev.strip().lower()] = full.strip()
    set_state("custom_cities", json.dumps(cities, ensure_ascii=False))


def get_city_names():
    """Devuelve el mapeo de ciudades fusionando config.CITY_NAMES + ciudades personalizadas."""
    import config
    merged = dict(config.CITY_NAMES)
    merged.update(get_custom_cities())
    return merged


def get_unprocessed_messages():
    """Devuelve todos los mensajes que el pipeline aun no ha procesado.

    Parametros:
        Ninguno.

    Devuelve:
        list[dict]: lista de mensajes con processed = 0, ordenados por
        fecha de recepcion ascendente (del mas antiguo al mas reciente).
        Cada mensaje es un dict con las columnas de la tabla.
    """
    with _conn() as c:
        # Selecciona los mensajes sin procesar, del mas antiguo al mas nuevo.
        rows = c.execute(
            "SELECT * FROM incoming_messages WHERE processed = 0 ORDER BY received_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_messages_for_phone(phone, since=0.0):
    """Devuelve los mensajes de un numero a partir de un timestamp.

    Parametros:
        phone (str): numero de telefono a consultar (se normaliza).
        since (float): timestamp minimo de recepcion; por defecto 0.0,
            que devuelve todo el historial del numero.

    Devuelve:
        list[dict]: mensajes del numero con received_at >= since,
        ordenados por fecha de recepcion ascendente. Cada elemento es un
        dict con las columnas de la tabla.
    """
    phone = normalize_phone(phone)
    with _conn() as c:
        # Filtra por telefono y por fecha minima, del mas antiguo al mas nuevo.
        rows = c.execute(
            "SELECT * FROM incoming_messages WHERE phone = ? AND received_at >= ? "
            "ORDER BY received_at ASC",
            (phone, since),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_processed(message_ids):
    """Marca como procesados una lista de mensajes por su id.

    Si la lista esta vacia no hace nada (retorno anticipado).

    Parametros:
        message_ids (Iterable[int]): ids de los mensajes a marcar como
            procesados (processed = 1).

    Devuelve:
        None.
    """
    if not message_ids:
        return
    with _conn() as c:
        # Pone processed = 1 para cada id de la lista en una sola operacion.
        c.executemany(
            "UPDATE incoming_messages SET processed = 1 WHERE id = ?",
            [(mid,) for mid in message_ids],
        )


def phones_with_replies_since(since=0.0):
    """Devuelve el conjunto de numeros que han respondido desde un timestamp.

    Parametros:
        since (float): timestamp minimo de recepcion; por defecto 0.0,
            que considera todo el historial.

    Devuelve:
        set[str]: conjunto de telefonos distintos con al menos un mensaje
        cuyo received_at >= since.
    """
    with _conn() as c:
        # Telefonos distintos con algun mensaje recibido a partir de "since".
        rows = c.execute(
            "SELECT DISTINCT phone FROM incoming_messages WHERE received_at >= ?",
            (since,),
        ).fetchall()
        return {r["phone"] for r in rows}


# ---------------------------------------------------------------------------
# Estado de las ejecuciones (almacen clave/valor)
# ---------------------------------------------------------------------------
def get_state(key, default=None):
    """Lee un valor del almacen clave/valor ``run_state``.

    Parametros:
        key (str): clave a consultar.
        default: valor a devolver si la clave no existe (por defecto None).

    Devuelve:
        El valor asociado a la clave (str), o ``default`` si no existe.
    """
    with _conn() as c:
        # Busca la fila por su clave; devuelve None si no hay coincidencia.
        row = c.execute("SELECT value FROM run_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key, value):
    """Guarda (o actualiza) un valor en el almacen clave/valor ``run_state``.

    Parametros:
        key (str): clave bajo la que se almacena el valor.
        value: valor a guardar; se convierte a texto con str().

    Devuelve:
        None.
    """
    with _conn() as c:
        # Inserta la clave; si ya existe (ON CONFLICT), actualiza su valor
        # con el nuevo (excluded.value es el valor que se intentaba insertar).
        c.execute(
            "INSERT INTO run_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_last_run_ts():
    """Devuelve el timestamp del ultimo run del pipeline completado.

    Parametros:
        Ninguno.

    Devuelve:
        float: timestamp del ultimo run; 0.0 si nunca se ha ejecutado.
    """
    return float(get_state("last_run_ts", 0.0))


def set_last_run_ts(ts=None):
    """Guarda el timestamp del ultimo run del pipeline.

    Parametros:
        ts (float, opcional): timestamp a guardar; si es None se usa el
            instante actual (time.time()).

    Devuelve:
        None.
    """
    # ATENCION: no usar "ts or time.time()": el valor 0 es falsy y seria
    # sobrescrito por time.time(). Por eso se compara explicitamente con None.
    set_state("last_run_ts", time.time() if ts is None else ts)


def get_last_reply_check_ts():
    """Devuelve el timestamp del ultimo chequeo de respuestas.

    Este valor marca el punto a partir del cual se buscan las nuevas
    respuestas entrantes.

    Parametros:
        Ninguno.

    Devuelve:
        float: timestamp del ultimo chequeo; 0.0 si nunca se ha hecho.
    """
    return float(get_state("last_reply_check_ts", 0.0))


def set_last_reply_check_ts(ts=None):
    """Guarda el timestamp del ultimo chequeo de respuestas.

    Parametros:
        ts (float, opcional): timestamp a guardar; si es None se usa el
            instante actual (time.time()).

    Devuelve:
        None.
    """
    # ATENCION: igual que en set_last_run_ts, no usar "ts or time.time()"
    # porque el 0 es falsy y se perderia; se compara con None explicitamente.
    set_state("last_reply_check_ts", time.time() if ts is None else ts)


# ---------------------------------------------------------------------------
# Utilidad de normalizacion de numeros de telefono
# ---------------------------------------------------------------------------
def normalize_phone(phone):
    """Normaliza un numero dejando unicamente sus digitos.

    Elimina el sufijo de WhatsApp (@c.us, @g.us, etc.) y cualquier caracter
    que no sea un digito (el signo +, espacios, guiones, parentesis...).

    Parametros:
        phone (str): numero de telefono en bruto a normalizar.

    Devuelve:
        str: cadena con solo los digitos del numero; cadena vacia si la
        entrada es None o vacia.
    """
    if not phone:
        return ""
    # UltraMsg devuelve el numero con sufijo, p. ej. "34XXXXXXXXX@c.us";
    # se corta en "@" para quedarnos solo con la parte del numero.
    phone = str(phone).split("@")[0]
    # Conserva unicamente los caracteres que son digitos.
    return "".join(ch for ch in phone if ch.isdigit())
