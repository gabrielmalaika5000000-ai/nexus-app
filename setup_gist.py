#!/usr/bin/env python3
"""
À exécuter UNE SEULE FOIS, en local, pour créer le Gist privé qui servira
de sauvegarde à la base NEXUS.

1. Crée un token GitHub : https://github.com/settings/tokens
   -> "Generate new token (classic)" -> coche uniquement la permission "gist"
2. Lance :
     export GITHUB_TOKEN=ghp_xxxxxxxxxxxx
     python setup_gist.py
3. Copie le GITHUB_GIST_ID affiché dans les variables d'environnement de
   Render (en plus de GITHUB_TOKEN, qui doit aussi y être).

Ne relance JAMAIS ce script une fois le Gist créé : ça créerait un second
Gist vide, différent de celui que ton app utilise déjà.
"""
import os
import sys

try:
    import requests
except ImportError:
    print("Le module 'requests' n'est pas installé : pip install requests")
    sys.exit(1)

GITHUB_API = "https://api.github.com"
GIST_BACKUP_FILENAME = "nexus_db_backup.b64"


def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Erreur : variable d'environnement GITHUB_TOKEN manquante.")
        print("Crée un token sur https://github.com/settings/tokens (permission 'gist' uniquement).")
        sys.exit(1)

    resp = requests.post(
        f"{GITHUB_API}/gists",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        json={
            "description": "Sauvegarde automatique de la base NEXUS — ne pas éditer manuellement",
            "public": False,
            "files": {GIST_BACKUP_FILENAME: {"content": "placeholder — sera remplacé au premier backup"}},
        },
        timeout=15,
    )

    if resp.status_code != 201:
        print(f"Erreur lors de la création du Gist : HTTP {resp.status_code}")
        print(resp.text)
        sys.exit(1)

    gist_id = resp.json()["id"]
    print("Gist créé avec succès.\n")
    print(f"GITHUB_GIST_ID = {gist_id}\n")
    print("Ajoute maintenant sur Render (Settings -> Environment) :")
    print(f"  GITHUB_TOKEN   = {token}")
    print(f"  GITHUB_GIST_ID = {gist_id}")


if __name__ == "__main__":
    main()
