"""
Lecture des emails Gmail et extraction des leads Idealista / Fotocasa / Habitaclia.

Authentification : OAuth (token.json généré par setup_auth.py).
Sur Railway, credentials.json et token.json sont reconstruits depuis
les variables d'env GMAIL_CREDENTIALS / GMAIL_TOKEN (base64) via config.py.
"""
import re
import base64
import datetime
from email.utils import parsedate_to_datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config

# Lecture des emails + envoi du rapport
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Domaines / mots-clés permettant d'attribuer une source à un email
SOURCE_SIGNATURES = {
    "Idealista": ["idealista.com", "idealista"],
    "Fotocasa": ["fotocasa.es", "fotocasa"],
    "Habitaclia": ["habitaclia.com", "habitaclia"],
}


def _get_service():
    config.gmail_credentials_path()
    token_path = config.gmail_token_path()
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(to_addr, subject, body_text):
    """Envoie un email texte simple depuis le compte Gmail authentifié."""
    import email.mime.text
    service = _get_service()
    mime = email.mime.text.MIMEText(body_text, _charset="utf-8")
    mime["to"] = to_addr
    mime["from"] = config.GMAIL_ADDRESS
    mime["subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _decode_part(part):
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_body(payload):
    """Renvoie le texte brut + html concaténés du message."""
    texts = []

    def walk(p):
        mime = p.get("mimeType", "")
        if mime in ("text/plain", "text/html"):
            texts.append(_decode_part(p))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(texts)


def _strip_html(html):
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    # Convertit les balises de bloc en sauts de ligne pour préserver la structure
    html = re.sub(r"(?i)<\s*br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</\s*(p|div|tr|li|h[1-6]|td|table)\s*>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#?\w+;", " ", text)
    # Nettoie chaque ligne (espaces superflus) mais conserve les sauts de ligne
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _detect_source(sender, subject, body):
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()

    # Fotocasa et Habitaclia partagent l'expéditeur cliente@fotocasa.pro :
    # on distingue via le sujet OU le titre/corps du mail (selon le type).
    if "cliente@fotocasa.pro" in sender_l:
        haystack = subject_l + " " + (body or "").lower()
        if "habitaclia" in haystack:
            return "Habitaclia"
        return "Fotocasa"

    # Match par adresse d'expéditeur
    for addr, source in config.PORTAL_SENDERS.items():
        if addr.lower() in sender_l:
            return source

    # Fallback par mots-clés / domaines (sujet inclus)
    haystack = (sender_l + " " + subject_l + " " + body).lower()
    for source, sigs in SOURCE_SIGNATURES.items():
        if any(sig in haystack for sig in sigs):
            return source
    return None


# Regex génériques de parsing
RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
RE_PHONE = re.compile(r"(?:\+?34[\s.\-]?)?(?:[6789]\d{2}[\s.\-]?\d{3}[\s.\-]?\d{3})")
RE_URL = re.compile(r"https?://(?:www\.)?(?:idealista|fotocasa|habitaclia)\.[a-z.]+/[^\s\"'>)]+", re.I)
# Référence d'annonce : suites de chiffres longues, ou patterns "ref. XXXX"
RE_REF = re.compile(r"(?:ref(?:erencia)?\.?\s*[:#]?\s*)([A-Za-z0-9\-]{4,})", re.I)
RE_NUM_IN_URL = re.compile(r"/(\d{6,})")
# Nom : après "nombre", "name", ou ligne de salutation
RE_NAME = re.compile(
    r"(?:nombre|name|nom|contacto|cliente|interesad[oa])\s*[:\-]\s*([A-Za-zÀ-ÿ'’\.\s]{2,40})",
    re.I,
)


# --- Parsing des corps de mails ----------------------------------------------
# Structure du corps : "...espera tu respuesta [NOM] [TÉL] [EMAIL] [MESSAGE]"
RE_IDEALISTA_MARKER = re.compile(r"espera tu respuesta", re.I)
# Référence dans le sujet Fotocasa/Habitaclia : "... con referencia 926287 - De ..."
RE_SUBJECT_REF = re.compile(r"con referencia\s+([A-Za-z0-9\-]+)", re.I)
# Suite de chiffres séparés par espaces, avec éventuel préfixe +/indicatif
RE_PHONE_RUN = re.compile(r"\+?\d[\d\s]{6,}\d")


def _clean_phone(raw):
    """Supprime espaces et '+' — ne garde que les chiffres."""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def _is_phone_line(line):
    """Ligne composée uniquement d'un numéro (chiffres/espaces/+, sans lettres)."""
    if any(ch.isalpha() for ch in line):
        return False
    return len(_clean_phone(line)) >= 7


def _extract_idealista_lead(text):
    """
    Extrait (nombre, telefono, email, message) d'un mail Idealista.
    Structure (ligne par ligne) après 'espera tu respuesta' :
        [NOM] / [TÉLÉPHONE] / [EMAIL] / [MESSAGE...]
    """
    m = RE_IDEALISTA_MARKER.search(text)
    segment = text[m.end():] if m else text

    # Si le marqueur termine la ligne, on saute jusqu'à la ligne suivante
    lines = [ln.strip() for ln in segment.splitlines()]
    lines = [ln for ln in lines if ln]

    name = phone = email = message = ""
    email_idx = None

    # Nom = première ligne qui n'est ni un téléphone ni un email
    name_idx = None
    for i, ln in enumerate(lines):
        if RE_EMAIL.search(ln) or _is_phone_line(ln):
            continue
        name = ln
        name_idx = i
        break

    # Téléphone = première ligne "téléphone" (après le nom si trouvé)
    start = (name_idx + 1) if name_idx is not None else 0
    for i in range(start, len(lines)):
        if _is_phone_line(lines[i]):
            phone = _clean_phone(lines[i])
            break

    # Email = première occurrence
    for i, ln in enumerate(lines):
        em = RE_EMAIL.search(ln)
        if em:
            email = em.group(0).strip()
            email_idx = i
            break

    # Message = lignes après l'email
    if email_idx is not None and email_idx + 1 < len(lines):
        message = " ".join(lines[email_idx + 1:]).strip()

    # Repli si structure inattendue (tout sur une ligne)
    if not phone:
        runs = list(RE_PHONE_RUN.finditer(segment))
        if runs:
            phone = _clean_phone(runs[-1].group(0))
    if not email:
        em = RE_EMAIL.search(segment)
        if em:
            email = em.group(0).strip()

    return name.strip(), phone.strip(), email.strip(), message.strip()


# --- Parsing Fotocasa / Habitaclia (3 types de mails) ------------------------
def _label_value(text, label_pattern):
    """Renvoie la valeur après 'label:' (jusqu'à la fin de la ligne)."""
    m = re.search(label_pattern + r"\s*:\s*(.+)", text, re.I)
    return m.group(1).strip() if m else ""


def _extract_fotocasa_habitaclia(subject, full):
    """
    Gère les 3 types de mails Fotocasa/Habitaclia. Renvoie un dict
    {nombre, telefono, email, message, ref}.
    """
    low = full.lower()

    # Référence : sujet d'abord (TYPE 1), sinon corps (TYPE 3)
    ref = ""
    mref = RE_SUBJECT_REF.search(subject) or RE_SUBJECT_REF.search(full)
    if mref:
        ref = mref.group(1).strip()

    # TYPE 2 — appel téléphonique
    if "datos de la llamada" in low or "has recibido una llamada" in low:
        phone = _clean_phone(_label_value(full, r"Tel[eé]fono"))
        duracion = _label_value(full, r"Duraci[oó]n")
        notas = ("Llamada recibida " + duracion).strip()
        return {"nombre": "", "telefono": phone, "email": "", "message": notas, "ref": ref}

    # TYPE 3 — contact structuré (labels Nombre/Teléfono/Email/Mensaje)
    if "nuevo contacto de" in low or ("nombre:" in low and "email:" in low):
        nombre = _label_value(full, r"Nombre")
        phone = _clean_phone(_label_value(full, r"Tel[eé]fono"))
        email_raw = _label_value(full, r"Email")
        em = RE_EMAIL.search(email_raw)
        email = em.group(0).strip() if em else email_raw.strip()
        mensaje = _label_value(full, r"Mensaje")
        if mensaje.strip().lower().startswith("no especificado") or not mensaje.strip():
            mensaje = ""
        return {"nombre": nombre.strip(), "telefono": phone, "email": email,
                "message": mensaje.strip(), "ref": ref}

    # TYPE 1 — message écrit (même structure qu'Idealista)
    name, phone, email, message = _extract_idealista_lead(full)
    return {"nombre": name, "telefono": phone, "email": email, "message": message, "ref": ref}


def _clean_name(raw):
    if not raw:
        return ""
    name = raw.strip().strip(".,;:")
    # coupe sur sauts de mots parasites fréquents
    name = re.split(r"\b(te ha|ha contactado|está interesad|email|tel|phone|móvil)\b", name, flags=re.I)[0]
    name = re.sub(r"\s+", " ", name).strip()
    # rejette si ça ressemble à une phrase complète
    if len(name.split()) > 5:
        return ""
    return name


def _extract_lead(source, body, sender_email):
    """Extrait les champs d'un lead depuis le corps de l'email."""
    emails = [e for e in RE_EMAIL.findall(body)
              if not any(d in e.lower() for d in ("idealista", "fotocasa", "habitaclia", "noreply", "no-reply"))]
    phones = RE_PHONE.findall(body)
    urls = RE_URL.findall(body)

    ref = ""
    m = RE_REF.search(body)
    if m:
        ref = m.group(1)
    elif urls:
        mnum = RE_NUM_IN_URL.search(urls[0])
        if mnum:
            ref = mnum.group(1)

    name = ""
    mname = RE_NAME.search(body)
    if mname:
        name = _clean_name(mname.group(1))

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


def diag():
    """Diagnostic Gmail : vérifie l'auth et compte les mails correspondant à la requête."""
    out = {}
    try:
        service = _get_service()
    except Exception as e:  # noqa: BLE001
        out["error_auth"] = f"{type(e).__name__}: {e}"
        return out
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


def fetch_new_leads(after_ts):
    """
    Renvoie la liste des leads détectés dans les emails reçus après `after_ts`
    (timestamp Unix). Filtre sur les sources Idealista/Fotocasa/Habitaclia.
    """
    service = _get_service()
    # Boîte de réception uniquement + dernières 24h + expéditeurs connus.
    # On refiltre ensuite par timestamp (internalDate) pour ne traiter que le nouveau.
    senders = " OR ".join(config.PORTAL_SENDERS.keys())
    query = f"label:INBOX newer_than:1d from:({senders})"
    print(f"[gmail] requête: {query}")

    leads = []
    seen_ids = set()
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=100
        ).execute()
        for ref in resp.get("messages", []):
            mid = ref["id"]
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            msg = service.users().messages().get(userId="me", id=mid, format="full").execute()

            internal_ts = int(msg.get("internalDate", "0")) / 1000.0
            if internal_ts <= after_ts:
                continue

            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            sender = headers.get("from", "")
            subject = headers.get("subject", "")

            raw_body = _extract_body(msg["payload"])
            body = _strip_html(raw_body) if "<" in raw_body else raw_body
            full = subject + "\n" + body

            source = _detect_source(sender, subject, full)
            if not source:
                continue

            if source == "Idealista":
                name, phone, email, message = _extract_idealista_lead(full)
                urls = RE_URL.findall(full)
                ref = ""
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
                lead = _extract_lead(source, full, sender)
                lead["message"] = ""

            lead["gmail_id"] = mid
            lead["received_at"] = internal_ts
            lead["subject"] = subject

            # Logs détaillés pour vérification du parsing
            print(
                f"[gmail] lead {source} | nombre='{lead['nombre']}' "
                f"telefono='{lead['telefono']}' email='{lead['email']}' "
                f"ref='{lead.get('ref','')}' url='{lead.get('url','')}'"
            )
            print(f"[gmail]   message='{(lead.get('message') or '')[:160]}'")

            # On ne garde que les leads ayant au moins un téléphone OU un email
            if lead["telefono"] or lead["email"]:
                leads.append(lead)
            else:
                print(f"[gmail]   ignoré (ni téléphone ni email) — subject='{subject}'")

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return leads
