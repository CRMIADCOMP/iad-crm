"""
Scraping de la página del agente IAD para sincronizar las URLs y los datos de los anuncios.

Extrae, para cada anuncio publicado por el agente en su perfil IAD:
  - la URL completa del anuncio,
  - la referencia (ej. "r926287" -> "926287"),
  - el tipo, la ciudad y el precio (best-effort),
  - la superficie habitable (m²), la superficie del terreno (m²) y las habitaciones si aparecen.

FILTRADO:
  - se ignoran los anuncios con la etiqueta "Bajo compromiso",
  - se ignora la sección "Últimas operaciones" (ya vendidos).

NOTA: el HTML de IAD puede cambiar; este parser es "best-effort" y nunca lanza
excepción hacia fuera (devuelve lo que consigue extraer). Requiere validación en producción.
"""
import re
import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

_RE_REF = re.compile(r"r?(\d{4,})")
_RE_PRICE = re.compile(r"([\d][\d\.\s]{2,})\s*€")
_RE_SURFACE = re.compile(r"(\d+)\s*m²")
_RE_HAB = re.compile(r"(\d+)\s*(?:hab|habitaci)", re.I)
_RE_BANO = re.compile(r"(\d+)\s*(?:bañ|baño|wc)", re.I)

# Palabras clave de tipo de inmueble -> código interno.
_TYPE_KW = [
    ("terreno", "T", "Terreno"), ("parcela", "T", "Terreno"),
    ("casa", "C", "Casa"), ("chalet", "C", "Casa"),
    ("piso", "P", "Piso"), ("apartamento", "P", "Piso"), ("ático", "P", "Piso"),
    ("garaje", "Pa", "Parking"), ("parking", "Pa", "Parking"), ("plaza", "Pa", "Parking"),
    ("local", "L", "Local"), ("nave", "L", "Local"),
]


def _digits(s):
    return "".join(ch for ch in str(s) if ch.isdigit())


def _detect_type(text):
    t = (text or "").lower()
    for kw, code, name in _TYPE_KW:
        if kw in t:
            return code, name
    return "", ""


def _parse_card(a_tag, base="https://www.iadespana.es"):
    """Extrae los datos de una tarjeta de anuncio a partir de su enlace y su entorno."""
    href = a_tag.get("href", "")
    url = href if href.startswith("http") else base + href
    # Texto de la tarjeta: empieza por el propio enlace y, si hay poco texto, sube
    # como máximo 2 niveles SIN llegar a <body>/<html> (para no mezclar otras tarjetas).
    container = a_tag
    txt = container.get_text(" ", strip=True)
    hops = 0
    while (len(txt) < 25 and container.parent is not None
           and getattr(container.parent, "name", "") not in ("body", "html", "[document]")
           and hops < 2):
        container = container.parent
        txt = container.get_text(" ", strip=True)
        hops += 1
    text = " ".join(txt.split())

    # Referencia: de la URL (rXXXXXX) o del texto.
    ref = ""
    m = re.search(r"/r?(\d{4,})(?:[/\-_]|$)", href) or _RE_REF.search(href)
    if m:
        ref = m.group(1)
    if not ref:
        m = re.search(r"[Rr]ef[\.\s:]*r?(\d{4,})", text)
        if m:
            ref = m.group(1)

    price = ""
    mp = _RE_PRICE.search(text)
    if mp:
        price = _digits(mp.group(1))

    surfaces = _RE_SURFACE.findall(text)  # puede haber 1 o 2 (habitable / terreno)
    surface_hab = surfaces[0] if surfaces else ""
    surface_terreno = surfaces[1] if len(surfaces) > 1 else ""

    mh = _RE_HAB.search(text)
    habitaciones = mh.group(1) if mh else ""
    mb = _RE_BANO.search(text)
    banos = mb.group(1) if mb else ""

    code, name = _detect_type(text)

    return {
        "url": url, "ref": ref, "price": price,
        "type_code": code, "type": name,
        "surface_hab": surface_hab, "surface_terreno": surface_terreno,
        "habitaciones": habitaciones, "banos": banos,
        "bajo_compromiso": "bajo compromiso" in text.lower(),
        "raw": text[:200],
    }


def scrape_iad_listings(profile_url):
    """Descarga la página del agente IAD y devuelve la lista de anuncios activos.

    Devuelve (listings, error): listings es una lista de dicts; error es None o un mensaje.
    Filtra los anuncios "Bajo compromiso" y la sección "Últimas operaciones".
    """
    try:
        resp = requests.get(profile_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        if not resp.ok:
            return [], f"HTTP {resp.status_code}"
        html = resp.text
    except Exception as e:  # noqa: BLE001
        return [], f"excepción de red: {e}"

    # Corta el HTML antes de la sección "Últimas operaciones" (anuncios ya vendidos).
    low = html.lower()
    idx = low.find("últimas operaciones")
    if idx == -1:
        idx = low.find("ultimas operaciones")
    if idx != -1:
        html = html[:idx]

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:  # noqa: BLE001
        return [], f"error parseando HTML: {e}"

    # Recoge los enlaces a fichas de inmueble (href con /inmueble/ o referencia rXXXX).
    seen = set()
    listings = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/inmueble" not in href and not re.search(r"/r?\d{5,}", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        card = _parse_card(a)
        if card["bajo_compromiso"]:
            continue  # ignora los anuncios bajo compromiso
        if not card["ref"] and not card["price"]:
            continue  # tarjeta sin datos útiles
        listings.append(card)
    return listings, None


def find_match(listings, price="", city="", type_code=""):
    """Busca en los anuncios IAD una coincidencia aproximada por precio + (ciudad/tipo).

    Devuelve el dict del anuncio coincidente o None.
    """
    price_n = _digits(price)
    city_l = (city or "").lower()
    best = None
    for it in listings:
        if price_n and it.get("price") and _digits(it["price"]) == price_n:
            # precio coincide: refuerza con tipo si está disponible
            if type_code and it.get("type_code") and it["type_code"] != type_code:
                continue
            best = it
            break
        if city_l and city_l in (it.get("raw", "").lower()):
            best = best or it
    return best
