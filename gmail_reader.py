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
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"\s+", " ", text).strip()


def _detect_source(sender, body):
    sender_l = (sender or "").lower()
    # 1) match prioritaire par adresse d'expéditeur
    for addr, source in config.PORTAL_SENDERS.items():
        if addr.lower() in sender_l:
            return source
    # 2) fallback par mots-clés / domaines
    haystack = (sender_l + " " + body).lower()
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


def fetch_new_leads(after_ts):
    """
    Renvoie la liste des leads détectés dans les emails reçus après `after_ts`
    (timestamp Unix). Filtre sur les sources Idealista/Fotocasa/Habitaclia.
    """
    service = _get_service()
    # Gmail query : emails reçus après la date (granularité jour) -> on refiltre par timestamp.
    after_date = datetime.datetime.fromtimestamp(max(after_ts - 86400, 0))
    query = f"after:{after_date.strftime('%Y/%m/%d')}"

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

            source = _detect_source(sender, full)
            if not source:
                continue

            lead = _extract_lead(source, full, sender)
            lead["gmail_id"] = mid
            lead["received_at"] = internal_ts
            lead["subject"] = subject
            # On ne garde que les leads ayant au moins un téléphone OU un email
            if lead["telefono"] or lead["email"]:
                leads.append(lead)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return leads
