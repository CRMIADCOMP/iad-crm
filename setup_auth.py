"""
A LANZAR UNA SOLA VEZ EN LOCAL para generar token.json a partir de credentials.json.

Uso:
    1. Coloca credentials.json (cliente OAuth de escritorio de Gmail) en esta carpeta.
    2. python setup_auth.py
    3. Se abre una ventana del navegador -> conéctate con thibaut.montalat@iadespana.es
    4. Se crea token.json en esta carpeta.
    5. Codifica credentials.json y token.json en base64 para Railway:
         - macOS/Linux:  base64 -i credentials.json | pbcopy   (luego pega en GMAIL_CREDENTIALS)
                         base64 -i token.json      | pbcopy   (luego pega en GMAIL_TOKEN)
       El script también muestra los valores base64 al final.
"""
import base64
import os

from google_auth_oauthlib.flow import InstalledAppFlow

# Permisos OAuth (SCOPES): lectura+modificación de emails (leads + papelera)
# mediante gmail.modify, y envío del informe diario mediante gmail.send.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def main():
    """
    Ejecuta el flujo de autorización OAuth de Gmail una sola vez en local.

    Comprueba que existe credentials.json, lanza el flujo OAuth con
    InstalledAppFlow (abre el navegador para que el usuario inicie sesión),
    guarda las credenciales resultantes en token.json y, por último, imprime
    los valores base64 de credentials.json y token.json para pegarlos en las
    variables de entorno GMAIL_CREDENTIALS y GMAIL_TOKEN de Railway.
    """
    # Comprueba que credentials.json está presente antes de continuar
    if not os.path.exists("credentials.json"):
        raise SystemExit(
            "credentials.json introuvable. Place-le dans ce dossier avant de lancer le script."
        )

    # Flujo OAuth: carga las credenciales y abre un servidor local para que el
    # usuario autorice la aplicación en el navegador (run_local_server)
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)

    # Guarda las credenciales autorizadas en token.json
    with open("token.json", "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print("\n✅ token.json généré avec succès.\n")
    print("=" * 70)
    print("Colle ces valeurs dans les variables d'environnement Railway :\n")
    # Imprime el valor base64 de credentials.json para la variable GMAIL_CREDENTIALS de Railway
    with open("credentials.json", "rb") as f:
        print("GMAIL_CREDENTIALS =")
        print(base64.b64encode(f.read()).decode())
    print()
    # Imprime el valor base64 de token.json para la variable GMAIL_TOKEN de Railway
    with open("token.json", "rb") as f:
        print("GMAIL_TOKEN =")
        print(base64.b64encode(f.read()).decode())
    print("=" * 70)


if __name__ == "__main__":
    main()
