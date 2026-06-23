# IAD CRM — Automatisation leads immobiliers (Railway)

App Flask 24h/24 qui, à **8h / 12h / 18h (heure Madrid)** :
lit les emails Gmail → détecte les leads Idealista/Fotocasa/Habitaclia → matche avec l'onglet
`🔗 Config` du Google Sheets → déduplique → ajoute/met à jour le prospect → envoie un WhatsApp
personnalisé via UltraMsg → traite les réponses reçues (analyse Claude Haiku → colonnes F/G/H) →
relance J+2 → clôture après 7 jours → envoie un rapport par email.

Un webhook (`/webhook`) reçoit les réponses WhatsApp d'UltraMsg et les stocke dans SQLite.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | Flask + APScheduler (8h/12h/18h) + routes `/health`, `/run` |
| `pipeline.py` | Orchestration des 14 étapes |
| `gmail_reader.py` | Lecture des emails + extraction leads + envoi du rapport |
| `sheets_handler.py` | Google Sheets (matching, dédup, insert/update, F/G/H) |
| `whatsapp.py` | Envoi UltraMsg + templates de messages (ES) |
| `ai_analyzer.py` | Analyse des réponses via Claude Haiku |
| `webhook.py` | Réception des réponses WhatsApp |
| `database.py` | SQLite (réponses entrantes + état des runs) |
| `config.py` | Configuration (variables d'env) |
| `setup_auth.py` | **À lancer une fois en local** pour générer `token.json` |
| `requirements.txt`, `Procfile`, `runtime.txt`, `.gitignore` | Déploiement |

---

## Étape 1 — Authentification Gmail (en local, une seule fois)

1. Place ton `credentials.json` (OAuth client *Desktop* Gmail) dans ce dossier.
2. Installe les dépendances et lance le script :
   ```bash
   pip install -r requirements.txt
   python setup_auth.py
   ```
3. Connecte-toi avec **thibaut.montalat@iadespana.es** dans le navigateur.
4. `token.json` est créé. Le script affiche aussi les valeurs **base64** de
   `credentials.json` et `token.json` à coller dans Railway (`GMAIL_CREDENTIALS`, `GMAIL_TOKEN`).

> Pour ré-afficher les base64 plus tard :
> `base64 -i credentials.json` et `base64 -i token.json` (macOS/Linux).

## Étape 2 — Compte de service Google (accès au Sheets)

1. Sur [console.cloud.google.com](https://console.cloud.google.com) → crée (ou réutilise) un projet.
2. Active l'API **Google Sheets** et **Google Drive**.
3. Crée un **compte de service** → génère une clé **JSON**.
4. Ouvre ton Google Sheets → **Partager** → ajoute l'email du compte de service
   (`...@...iam.gserviceaccount.com`) en **Éditeur**.
5. Encode le JSON en base64 pour Railway :
   ```bash
   base64 -i service_account.json
   ```
   → à coller dans la variable `GOOGLE_SERVICE_ACCOUNT`.

## Étape 3 — Pousser sur GitHub (CRMIADCOMP/iad-crm)

Depuis ce dossier :
```bash
git init
git add .
git commit -m "CRM IAD initial"
git branch -M main
git remote add origin https://github.com/CRMIADCOMP/iad-crm.git
git push -u origin main
```
> `.gitignore` exclut déjà `credentials.json`, `token.json`, `service_account.json`, `*.db`, `.env`.
> Ces secrets ne partent **pas** sur GitHub : ils vont dans les variables Railway (étape 4).

## Étape 4 — Déployer sur Railway

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → `CRMIADCOMP/iad-crm`.
2. Railway détecte Python, installe `requirements.txt` et lance le `Procfile` (`web: gunicorn app:app`).
3. Onglet **Variables** → ajoute les variables ci-dessous.
4. Onglet **Settings** → **Networking** → **Generate Domain** pour obtenir l'URL publique
   (ex. `https://iad-crm.up.railway.app`).

### Variables d'environnement Railway

| Variable | Valeur |
|---|---|
| `GMAIL_CREDENTIALS` | base64 de `credentials.json` (affiché par `setup_auth.py`) |
| `GMAIL_TOKEN` | base64 de `token.json` (affiché par `setup_auth.py`) |
| `GOOGLE_SERVICE_ACCOUNT` | base64 du JSON du compte de service |
| `ANTHROPIC_API_KEY` | ta clé API Anthropic (`sk-ant-...`) |
| `GOOGLE_SHEET_ID` | `1WYvelN50Hz_8gCo8o9BtsFpUUdLaxQbzlXAySSY4I9M` |
| `ULTRAMSG_INSTANCE` | `instance181932` |
| `ULTRAMSG_TOKEN` | `00sfoebzfiih9jfa` |
| `GMAIL_ADDRESS` | `thibaut.montalat@iadespana.es` |
| `REPORT_EMAIL` | `thibaut.montalat@iadespana.es` |
| `TIMEZONE` | `Europe/Madrid` *(optionnel, déjà par défaut)* |
| `RUN_HOURS` | `8,12,18` *(optionnel, déjà par défaut)* |
| `RUN_TOKEN` | un mot de passe au choix pour sécuriser `/run` *(optionnel)* |

> Variables optionnelles déjà fournies par défaut dans `config.py` : tu peux les omettre
> sauf si tu veux changer une valeur. Les 4 premières + `ANTHROPIC_API_KEY` sont **obligatoires**.

## Étape 5 — Configurer le webhook UltraMsg

UltraMsg dashboard → **Settings → Webhook** :
- **Webhook URL** : `https://<ton-domaine-railway>/webhook`
  (ex. `https://iad-crm.up.railway.app/webhook`)
- Active **« On Message Received »** (messages entrants).
- Enregistre.

Teste : envoie un WhatsApp au numéro de l'instance → un log `[webhook] réponse reçue...`
apparaît dans les logs Railway et le message est stocké en base.

---

## Vérifier / tester

- `GET https://<domaine>/health` → statut + dernier run.
- `GET https://<domaine>/run?token=<RUN_TOKEN>` → déclenche le pipeline manuellement
  (sans `RUN_TOKEN` défini, `GET /run` suffit).
- Les runs automatiques tournent à 8h, 12h, 18h (Madrid).

## Notes importantes

- **Un message par prospect**, sur son numéro perso. Jamais de groupe.
- Envoi bloqué si `Estado final` ∉ {vide, `Nuevo contacto`, `WhatsApp enviado`}.
- Nom vide → `vecino/a`.
- Déduplication par **téléphone ET email** avant toute insertion.
- L'URL de l'annonce est stockée en colonne **E (Notas)** pour réutilisation lors des relances.
- L'extraction des leads (`gmail_reader.py`) repose sur des regex génériques : si le format
  exact des emails Idealista/Fotocasa/Habitaclia diffère, ajuste les motifs `RE_*` en haut du fichier.
- SQLite (`crm.db`) est local au conteneur Railway. Le redéploiement le réinitialise ; pour
  une persistance durable, ajoute un **Volume** Railway monté sur le dossier et pointe
  `DB_PATH` dessus (ex. `/data/crm.db`).
