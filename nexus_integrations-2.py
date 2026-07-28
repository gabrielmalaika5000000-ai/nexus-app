"""
Connecteur Gmail pour NEXUS.

Implémenté en appels HTTP directs (requests) plutôt qu'avec les SDK Google
officiels (google-auth-oauthlib, google-api-python-client) : ce sont de
simples API REST, et éviter le SDK réduit les dépendances sans rien perdre
en fiabilité.

Flux :
1. GmailOAuth.build_auth_url()      -> URL à donner à l'utilisateur
2. GmailOAuth.exchange_code()       -> échange le code contre des tokens
3. SupabaseTokenStore.save()        -> stocke les tokens (jamais en clair côté client)
4. GmailClient.fetch_unread()       -> lit les emails non lus
5. GmailClient.create_draft()       -> crée un VRAI brouillon (jamais d'envoi auto)

Portée volontairement limitée pour la V1 :
- lecture seule des emails non lus (scope gmail.readonly)
- création de brouillons uniquement (scope gmail.compose) — jamais gmail.send
- pas de gestion des pièces jointes, pas de parsing multipart complet (on
  utilise le "snippet" fourni par Gmail, suffisant pour la priorisation,
  mais pas pour reproduire le corps exact d'un email complexe)
"""
import os
import base64
import hashlib
import hmac
import time
import secrets as _secrets_mod
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")

# Scopes volontairement minimaux : lecture + brouillons, jamais l'envoi direct.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")


# ------------------------------------------------------------------
# État OAuth signé — protection CSRF sans session ni cookie serveur.
#
# L'endpoint /auth/gmail/start est atteint par simple navigation du
# navigateur (clic sur un lien), donc il ne peut pas exiger un header
# personnalisé comme X-API-Key. À la place, le user_id + une expiration +
# un nonce sont signés avec une clé secrète connue seulement du serveur ;
# le paramètre `state` renvoyé par Google au callback est vérifié avant
# de faire confiance au user_id qu'il contient. Un state modifié, expiré,
# ou non signé par ce serveur est rejeté.
# ------------------------------------------------------------------

# Générée une fois par processus si non fournie : suffisant puisque tout le
# cycle start -> callback se déroule en quelques minutes, dans le même
# déploiement. La définir explicitement en env var évite juste d'invalider
# un flux OAuth en cours pile au moment d'un redéploiement.
_OAUTH_STATE_SECRET = os.environ.get("OAUTH_STATE_SECRET") or _secrets_mod.token_hex(32)

_STATE_TTL_SECONDS = 600  # 10 minutes : largement assez pour un consentement Google


def sign_oauth_state(user_id):
    nonce = _secrets_mod.token_urlsafe(8)
    expiry = int(time.time()) + _STATE_TTL_SECONDS
    payload = f"{user_id}:{expiry}:{nonce}"
    sig = hmac.new(_OAUTH_STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_oauth_state(state_token):
    """Renvoie le user_id si le state est valide (signature correcte, non
    expiré), sinon lève ValueError avec un message explicite."""
    try:
        raw = base64.urlsafe_b64decode(state_token.encode()).decode()
        user_id, expiry_str, nonce, sig = raw.rsplit(":", 3)
    except Exception:
        raise ValueError("state OAuth invalide ou corrompu")

    expected_payload = f"{user_id}:{expiry_str}:{nonce}"
    expected_sig = hmac.new(_OAUTH_STATE_SECRET.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("signature du state OAuth invalide (possible falsification)")

    if int(time.time()) > int(expiry_str):
        raise ValueError("state OAuth expiré, relancez la connexion Gmail")

    return user_id


def gmail_oauth_configured():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI)


def supabase_configured():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


# ------------------------------------------------------------------
# OAuth
# ------------------------------------------------------------------

class GmailOAuth:
    @staticmethod
    def build_auth_url(state):
        """state doit contenir l'identité de l'utilisateur NEXUS (ex: user_id
        signé) pour retrouver à qui appartient le code au retour."""
        if not gmail_oauth_configured():
            raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI manquants")
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(GMAIL_SCOPES),
            "access_type": "offline",   # nécessaire pour obtenir un refresh_token
            "prompt": "consent",        # force le refresh_token même si déjà autorisé avant
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    @staticmethod
    def exchange_code(code, timeout=15):
        """Échange le code d'autorisation contre (access_token, refresh_token, expiry, scope).
        Lève une exception explicite en cas d'échec (contrairement à backup_db_to_gist,
        ici l'échec doit être visible : sans token, l'intégration ne peut pas continuer)."""
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"échec de l'échange OAuth : HTTP {resp.status_code} — {resp.text[:300]}")
        data = resp.json()
        expiry = datetime.now(timezone.utc).timestamp() + data.get("expires_in", 3600)
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),  # absent si déjà autorisé sans prompt=consent
            "token_expiry": datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
            "scope": data.get("scope", " ".join(GMAIL_SCOPES)),
        }

    @staticmethod
    def refresh_access_token(refresh_token, timeout=15):
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"échec du rafraîchissement OAuth : HTTP {resp.status_code} — {resp.text[:300]}")
        data = resp.json()
        expiry = datetime.now(timezone.utc).timestamp() + data.get("expires_in", 3600)
        return {
            "access_token": data["access_token"],
            "token_expiry": datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
        }


# ------------------------------------------------------------------
# Stockage des tokens dans Supabase (REST/PostgREST, via la clé service_role)
# ------------------------------------------------------------------

class SupabaseTokenStore:
    def __init__(self, url=None, service_key=None, timeout=15):
        self.url = (url or SUPABASE_URL or "").rstrip("/")
        self.service_key = service_key or SUPABASE_SERVICE_KEY
        self.timeout = timeout

    def _headers(self):
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",  # upsert
        }

    def save(self, user_id, tokens):
        """tokens: dict avec access_token, refresh_token (optionnel si déjà connu),
        token_expiry, scope. Fait un upsert (insert ou update) sur user_id."""
        payload = {"user_id": user_id, **tokens}
        resp = requests.post(
            f"{self.url}/rest/v1/google_oauth_credentials",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"échec sauvegarde token Supabase : HTTP {resp.status_code} — {resp.text[:300]}")
        return True

    def get(self, user_id):
        resp = requests.get(
            f"{self.url}/rest/v1/google_oauth_credentials",
            headers=self._headers(),
            params={"user_id": f"eq.{user_id}", "select": "*"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"échec lecture token Supabase : HTTP {resp.status_code} — {resp.text[:300]}")
        rows = resp.json()
        return rows[0] if rows else None

    def update_access_token(self, user_id, access_token, token_expiry):
        resp = requests.patch(
            f"{self.url}/rest/v1/google_oauth_credentials",
            headers=self._headers(),
            params={"user_id": f"eq.{user_id}"},
            json={"access_token": access_token, "token_expiry": token_expiry},
            timeout=self.timeout,
        )
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"échec mise à jour token Supabase : HTTP {resp.status_code} — {resp.text[:300]}")
        return True

    def update_last_sync(self, user_id, when=None):
        when = when or datetime.now(timezone.utc).isoformat()
        requests.patch(
            f"{self.url}/rest/v1/google_oauth_credentials",
            headers=self._headers(),
            params={"user_id": f"eq.{user_id}"},
            json={"last_sync_at": when},
            timeout=self.timeout,
        )

    def delete(self, user_id):
        resp = requests.delete(
            f"{self.url}/rest/v1/google_oauth_credentials",
            headers=self._headers(),
            params={"user_id": f"eq.{user_id}"},
            timeout=self.timeout,
        )
        return resp.status_code in (200, 204)


def get_valid_access_token(user_id, store=None):
    """Renvoie un access_token valide pour cet utilisateur, en le rafraîchissant
    automatiquement si expiré. Lève une exception explicite si l'utilisateur
    n'a jamais connecté Gmail — appelant doit gérer ce cas (proposer la connexion)."""
    store = store or SupabaseTokenStore()
    creds = store.get(user_id)
    if not creds:
        raise LookupError(f"aucun compte Gmail connecté pour {user_id}")

    expiry = datetime.fromisoformat(creds["token_expiry"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if expiry - now > __import__("datetime").timedelta(seconds=60):
        return creds["access_token"]

    # Token expiré ou expire dans moins d'une minute : on rafraîchit.
    refreshed = GmailOAuth.refresh_access_token(creds["refresh_token"])
    store.update_access_token(user_id, refreshed["access_token"], refreshed["token_expiry"])
    return refreshed["access_token"]


# ------------------------------------------------------------------
# Client Gmail (lecture + brouillons)
# ------------------------------------------------------------------

class GmailClient:
    def __init__(self, access_token, timeout=15):
        self.access_token = access_token
        self.timeout = timeout

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    def fetch_unread(self, max_results=10):
        """Renvoie une liste de signaux prêts à être passés à nexus.ingest(),
        limitée volontairement (maxResults) pour éviter un cold-start massif."""
        resp = requests.get(
            f"{GMAIL_API_BASE}/messages",
            headers=self._headers(),
            params={"labelIds": "UNREAD", "maxResults": max_results, "q": "in:inbox"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"échec liste emails Gmail : HTTP {resp.status_code} — {resp.text[:300]}")
        message_refs = resp.json().get("messages", [])

        signals = []
        for ref in message_refs:
            detail = requests.get(
                f"{GMAIL_API_BASE}/messages/{ref['id']}",
                headers=self._headers(),
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                timeout=self.timeout,
            )
            if detail.status_code != 200:
                continue  # un email illisible ne doit pas faire échouer tout le sync
            msg = detail.json()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender = headers.get("From", "expéditeur inconnu")
            subject = headers.get("Subject", "(sans objet)")
            snippet = msg.get("snippet", "")

            signals.append({
                "source": "email",
                "content": f"De: {sender} | Objet: {subject} | {snippet}",
                "metadata": {
                    "from": sender,
                    "responded": False,
                    "gmail_message_id": msg["id"],
                    "gmail_thread_id": msg.get("threadId"),
                },
            })
        return signals

    def create_draft(self, to, subject, body, thread_id=None):
        """Crée un VRAI brouillon dans Gmail. Ne l'envoie jamais — l'utilisateur
        doit ouvrir Gmail et cliquer Envoyer lui-même. C'est un invariant produit,
        pas un détail technique : voir la décision prise plus tôt dans le projet."""
        mime = f"To: {to}\r\nSubject: {subject}\r\n\r\n{body}"
        raw = base64.urlsafe_b64encode(mime.encode("utf-8")).decode("utf-8")
        message = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id

        resp = requests.post(
            f"{GMAIL_API_BASE}/drafts",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"message": message},
            timeout=self.timeout,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"échec création brouillon Gmail : HTTP {resp.status_code} — {resp.text[:300]}")
        return resp.json()


# ------------------------------------------------------------------
# Glue : synchronisation complète pour un utilisateur NEXUS
# ------------------------------------------------------------------

def sync_gmail_for_user(nexus_system, user_id, max_results=10, store=None):
    """Récupère les emails non lus et les pousse dans nexus_system.ingest().
    Renvoie le nombre de signaux effectivement ingérés. Ne lève pas d'exception
    pour un email individuel en échec (voir fetch_unread), mais laisse remonter
    les erreurs d'authentification (utilisateur non connecté, token révoqué)
    pour que l'appelant puisse proposer une reconnexion."""
    store = store or SupabaseTokenStore()
    access_token = get_valid_access_token(user_id, store=store)
    client = GmailClient(access_token)
    signals = client.fetch_unread(max_results=max_results)
    for sig in signals:
        nexus_system.ingest(sig["source"], sig["content"], sig["metadata"])
    store.update_last_sync(user_id)
    return len(signals)
