"""
Análisis de las respuestas de WhatsApp con Claude Haiku (API de Anthropic).

Este módulo recibe los mensajes de respuesta enviados por un prospecto
inmobiliario y los envía al modelo Claude Haiku de Anthropic para extraer
información estructurada. A partir del análisis de la IA se obtienen tres
datos clave que se vuelcan en la hoja de cálculo del CRM:

- el presupuesto del prospecto (columna F),
- el tiempo que lleva buscando inmueble (columna G),
- el estado de su financiación / pago validado (columna H).

La función pública principal es ``analyze_replies``, que devuelve un
diccionario normalizado con todos los campos extraídos. El resto de
funciones (``_get_client`` y ``_parse_json``) son utilidades internas.
"""
import json
import re

import anthropic

import config

_client = None


def _get_client():
    """Crea (una sola vez) y devuelve el cliente de la API de Anthropic.

    Utiliza un patrón de inicialización perezosa (lazy): el cliente se
    almacena en la variable global ``_client`` y solo se construye la
    primera vez que se invoca esta función; las siguientes llamadas
    reutilizan la misma instancia.

    Parámetros:
        (ninguno)

    Devuelve:
        anthropic.Anthropic: la instancia del cliente lista para usarse.

    Lanza:
        RuntimeError: si no hay una clave de API configurada
        (``config.ANTHROPIC_API_KEY`` vacía o ausente).
    """
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY manquant.")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# Prompt de sistema (en español) que define el rol y las reglas de la IA. NO MODIFICAR su contenido.
SYSTEM_PROMPT = (
    "Eres un asistente que analiza respuestas de WhatsApp de prospectos inmobiliarios en español. "
    "Extrae únicamente lo que el prospecto dice explícitamente. No inventes datos. "
    "Devuelve SIEMPRE un objeto JSON válido y nada más."
)

# Plantilla del mensaje de usuario (en español) que pide a la IA extraer los campos en formato JSON.
# Contiene el marcador {messages}, que se sustituye en tiempo de ejecución por los mensajes del
# prospecto mediante .format(). NO MODIFICAR su contenido.
USER_TEMPLATE = """Analiza este/estos mensaje(s) de un prospecto inmobiliario y extrae:

- "presupuesto": el presupuesto/budget mencionado (texto corto, ej "250.000€" o "" si no se menciona)
- "tiempo_busqueda_texto": cuánto tiempo lleva buscando, tal cual lo dice (ej "6 meses", "más de un año", "" si no se menciona)
- "tiempo_busqueda_meses": número entero de meses estimado a partir del texto (0 si no se menciona)
- "pago_validado": estado de la financiación. EXACTAMENTE uno de: "Sí - Validado" (banco/bróker ya aprobó o tiene financiación lista), "En curso" (está hablando con banco/bróker o en trámite), "No - Rechazado" (le han denegado la financiación), "Pendiente" (aún no ha hecho nada / no ha empezado), "" (no se menciona)
- "interesado": true/false/null — si sigue interesado en el inmueble (null si no está claro)
- "resumen": resumen breve (1 frase) de la respuesta

Mensaje(s) del prospecto:
\"\"\"
{messages}
\"\"\"

Responde solo con el JSON."""


def _parse_json(text):
    """Extrae y parsea el objeto JSON contenido en la respuesta de la IA.

    La IA debería devolver únicamente un objeto JSON, pero a veces lo
    acompaña de texto adicional (saltos de línea, comentarios, bloques de
    código, etc.). Esta función localiza el primer objeto JSON dentro del
    texto y lo convierte en un diccionario de Python.

    Parámetros:
        text (str): el texto bruto devuelto por el modelo.

    Devuelve:
        dict: el diccionario resultante del JSON, o un diccionario vacío
        ``{}`` si no se encuentra JSON válido o el parseo falla.
    """
    text = text.strip()
    # Busca el primer objeto JSON {...} dentro del texto: el patrón \{.*\}
    # captura desde la primera llave de apertura hasta la última de cierre.
    # El flag re.S (DOTALL) hace que el punto "." también case con saltos de
    # línea, de modo que el JSON puede ocupar varias líneas.
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        # Si se encontró, nos quedamos solo con el fragmento JSON (descartando
        # cualquier texto que la IA haya escrito antes o después).
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        # Si el contenido no es un JSON válido, devolvemos un dict vacío para
        # que el llamante pueda continuar con los valores por defecto.
        return {}


def analyze_replies(messages):
    """Analiza con la IA las respuestas de un prospecto y las normaliza.

    Une todos los mensajes recibidos, los envía a Claude Haiku junto con el
    prompt de sistema y la plantilla de usuario, parsea el JSON devuelto y
    construye un diccionario con todos los campos esperados, aplicando
    valores por defecto cuando faltan datos.

    Parámetros:
        messages (list[str]): lista de cadenas con las respuestas del
            prospecto. Las cadenas vacías se ignoran al unirlas.

    Devuelve:
        dict: diccionario normalizado con las claves "presupuesto",
        "tiempo_busqueda_texto", "tiempo_busqueda_meses", "pago_validado",
        "interesado" y "resumen". Si no hay mensajes con contenido, o si la
        llamada a la IA falla, se devuelve el diccionario por defecto (en
        caso de error, con el motivo del fallo en "resumen").
    """
    # Une los mensajes no vacíos separándolos con un delimitador "---".
    joined = "\n---\n".join(m for m in messages if m)
    # Diccionario por defecto: define todas las claves esperadas con sus
    # valores neutros, que se usan si no hay datos o si la IA falla.
    default = {
        "presupuesto": "",
        "tiempo_busqueda_texto": "",
        "tiempo_busqueda_meses": 0,
        "pago_validado": "",
        "interesado": None,
        "resumen": "",
    }
    # Si tras unir y recortar no queda texto, no merece la pena llamar a la IA:
    # devolvemos directamente el diccionario por defecto.
    if not joined.strip():
        return default

    # Obtiene (o crea) el cliente de Anthropic.
    client = _get_client()
    try:
        # Llamada a la API de Anthropic: envía el prompt de sistema y el
        # mensaje del usuario (la plantilla con los mensajes ya insertados) al
        # modelo configurado, limitando la respuesta a 400 tokens.
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": USER_TEMPLATE.format(messages=joined)}],
        )
        # La respuesta viene como una lista de bloques de contenido; nos quedamos
        # solo con los bloques de tipo "text" y concatenamos su texto para
        # reconstruir la respuesta completa de la IA.
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        # Extrae el objeto JSON del texto y lo convierte en diccionario.
        data = _parse_json(text)
    except Exception as e:  # noqa: BLE001
        # Ante cualquier fallo (red, API, parseo...) no interrumpimos el flujo:
        # devolvemos el diccionario por defecto anotando el error en "resumen".
        default["resumen"] = f"(análisis IA falló: {e})"
        return default

    # Parte de una copia del diccionario por defecto y la actualiza con los
    # valores devueltos por la IA, usando el valor por defecto para las claves
    # que la IA no haya proporcionado.
    result = dict(default)
    result.update({k: data.get(k, default[k]) for k in default})
    # Normalización del número de meses: la IA puede devolver el valor como
    # cadena, None u otro tipo. Lo convertimos a entero; si la conversión falla
    # (ValueError/TypeError) o el valor es vacío, se usa 0 como respaldo.
    try:
        result["tiempo_busqueda_meses"] = int(result["tiempo_busqueda_meses"] or 0)
    except (ValueError, TypeError):
        result["tiempo_busqueda_meses"] = 0
    return result
