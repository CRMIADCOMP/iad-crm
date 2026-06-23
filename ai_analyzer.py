"""
Analyse des réponses WhatsApp avec Claude Haiku (API Anthropic).
Extrait : budget (F), temps de recherche (G), statut financement (H).
"""
import json
import re

import anthropic

import config

_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY manquant.")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


SYSTEM_PROMPT = (
    "Eres un asistente que analiza respuestas de WhatsApp de prospectos inmobiliarios en español. "
    "Extrae únicamente lo que el prospecto dice explícitamente. No inventes datos. "
    "Devuelve SIEMPRE un objeto JSON válido y nada más."
)

USER_TEMPLATE = """Analiza este/estos mensaje(s) de un prospecto inmobiliario y extrae:

- "presupuesto": el presupuesto/budget mencionado (texto corto, ej "250.000€" o "" si no se menciona)
- "tiempo_busqueda_texto": cuánto tiempo lleva buscando, tal cual lo dice (ej "6 meses", "más de un año", "" si no se menciona)
- "tiempo_busqueda_meses": número entero de meses estimado a partir del texto (0 si no se menciona)
- "pago_validado": estado de la financiación. Uno de: "Validado" (banco/bróker ya aprobó o tiene financiación lista), "En proceso" (está hablando con banco/bróker), "No iniciado" (aún no ha hecho nada), "" (no se menciona)
- "interesado": true/false/null — si sigue interesado en el inmueble (null si no está claro)
- "resumen": resumen breve (1 frase) de la respuesta

Mensaje(s) del prospecto:
\"\"\"
{messages}
\"\"\"

Responde solo con el JSON."""


def _parse_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {}


def analyze_replies(messages):
    """
    messages : liste de chaînes (réponses du prospect).
    Renvoie un dict normalisé.
    """
    joined = "\n---\n".join(m for m in messages if m)
    default = {
        "presupuesto": "",
        "tiempo_busqueda_texto": "",
        "tiempo_busqueda_meses": 0,
        "pago_validado": "",
        "interesado": None,
        "resumen": "",
    }
    if not joined.strip():
        return default

    client = _get_client()
    try:
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": USER_TEMPLATE.format(messages=joined)}],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        data = _parse_json(text)
    except Exception as e:  # noqa: BLE001
        default["resumen"] = f"(análisis IA falló: {e})"
        return default

    result = dict(default)
    result.update({k: data.get(k, default[k]) for k in default})
    # normalisation du nombre de mois
    try:
        result["tiempo_busqueda_meses"] = int(result["tiempo_busqueda_meses"] or 0)
    except (ValueError, TypeError):
        result["tiempo_busqueda_meses"] = 0
    return result
