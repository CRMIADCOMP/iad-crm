"""
À LANCER UNE SEULE FOIS EN LOCAL pour générer token.json à partir de credentials.json.

Usage :
    1. Place credentials.json (OAuth client desktop Gmail) dans ce dossier.
    2. python setup_auth.py
    3. Une fenêtre navigateur s'ouvre -> connecte-toi avec thibaut.montalat@iadespana.es
    4. token.json est créé dans ce dossier.
    5. Encode credentials.json et token.json en base64 pour Railway :
         - macOS/Linux :  base64 -i credentials.json | pbcopy   (puis colle dans GMAIL_CREDENTIALS)
                          base64 -i token.json      | pbcopy   (puis colle dans GMAIL_TOKEN)
       Le script affiche aussi les valeurs base64 à la fin.
"""
import base64
import os

from google_auth_oauthlib.flow import InstalledAppFlow

# Lecture+modif des emails (leads + corbeille) + envoi (rapport quotidien).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def main():
    if not os.path.exists("credentials.json"):
        raise SystemExit(
            "credentials.json introuvable. Place-le dans ce dossier avant de lancer le script."
        )

    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)

    with open("token.json", "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print("\n✅ token.json généré avec succès.\n")
    print("=" * 70)
    print("Colle ces valeurs dans les variables d'environnement Railway :\n")
    with open("credentials.json", "rb") as f:
        print("GMAIL_CREDENTIALS =")
        print(base64.b64encode(f.read()).decode())
    print()
    with open("token.json", "rb") as f:
        print("GMAIL_TOKEN =")
        print(base64.b64encode(f.read()).decode())
    print("=" * 70)


if __name__ == "__main__":
    main()
