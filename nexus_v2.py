#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
NEXUS v2.0 — SYSTÈME D'IA ANTICIPATIVE ET ACTIONNABLE
================================================================================

Auteur: Kimi (Moonshot AI)
Version: 2.0.0 (corrigée)
Date: 2026-07-23

CORRECTIONS v2.0:
  ✓ Persistance SQLite (données conservées après redémarrage)
  ✓ Authentification par API key (header X-API-Key)
  ✓ Gestion d'erreurs sur tous les endpoints
  ✓ Tout en un seul fichier (pas de bug d'import)
  ✓ Profil utilisateur chargé/sauvegardé automatiquement

ARCHITECTURE:
  ContextEngine → PredictionEngine → ActionEngine → TransparencyEngine
  ↓ SQLite      ↓ SQLite          ↓ SQLite       ↓ SQLite

DÉPLOIEMENT:
  pip install flask flask-cors
  python nexus_v2.py

UTILISATION API:
  1. POST /api/v1/register → récupérer API key
  2. Header X-API-Key sur tous les autres endpoints
"""

import sqlite3
import os
import re
import sys
import json
import hashlib
import random
import secrets
import threading
import time
import base64
import signal
import atexit
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from collections import Counter, defaultdict, deque

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = os.environ.get("NEXUS_DB_PATH", "nexus.db")


def parse_iso_naive(date_string):
    """
    Parse une date ISO 8601 et retourne toujours un datetime "naïf" (sans
    fuseau horaire), pour rester comparable avec datetime.now() utilisé
    partout ailleurs dans le code.

    Nécessaire car les navigateurs envoient souvent des dates avec un
    suffixe 'Z' ou un offset explicite (ex: via Date.toISOString() en JS),
    ce qui produit un datetime "conscient du fuseau" — le comparer
    directement à datetime.now() (naïf) lève un TypeError.
    """
    dt = datetime.fromisoformat(date_string)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt

# ============================================================
# SAUVEGARDE / RESTAURATION VIA GIST GITHUB PRIVÉ
# ============================================================
# Contourne l'absence de disque persistant sur Render (plan gratuit) : à
# chaque redéploiement, le système de fichiers repart de zéro. On restaure
# donc la base depuis un Gist privé au démarrage, et on la ré-uploade
# périodiquement + juste avant l'arrêt (SIGTERM envoyé par Render au moment
# du redéploiement).
#
# Limite assumée : toute donnée écrite entre la dernière sauvegarde et un
# arrêt brutal (crash, kill -9, coupure réseau pendant le SIGTERM) est perdue.
# Ce n'est pas un vrai substitut à un disque persistant ou une vraie base
# managée (Postgres) — c'est un filet de sécurité pour un usage à faible
# enjeu, pas une garantie de durabilité.

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_GIST_ID = os.environ.get("GITHUB_GIST_ID")
GIST_BACKUP_FILENAME = "nexus_db_backup.b64"
BACKUP_INTERVAL_SECONDS = int(os.environ.get("BACKUP_INTERVAL_SECONDS", "300"))


def backup_enabled():
    return bool(REQUESTS_AVAILABLE and GITHUB_TOKEN and GITHUB_GIST_ID)


def _gist_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def backup_db_to_gist(db_path=None, timeout=15):
    """
    Encode la base SQLite en base64 et l'envoie dans le Gist configuré.
    Ne lève jamais d'exception : un échec de sauvegarde ne doit jamais
    faire planter le serveur. Retourne True/False.
    """
    path = db_path or DB_PATH
    if not backup_enabled():
        return False
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        resp = requests.patch(
            f"{GITHUB_API}/gists/{GITHUB_GIST_ID}",
            headers=_gist_headers(),
            json={"files": {GIST_BACKUP_FILENAME: {"content": encoded}}},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return True
        print(f"[backup] échec sauvegarde Gist : HTTP {resp.status_code} — {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[backup] erreur sauvegarde Gist : {e}")
        return False


def restore_db_from_gist(db_path=None, timeout=15):
    """
    Télécharge la dernière sauvegarde depuis le Gist et l'écrit sur disque.
    Ne fait rien si un fichier local existe déjà (ne jamais écraser une base
    qui aurait par ailleurs survécu). Retourne True/False, ne lève jamais.
    """
    path = db_path or DB_PATH
    if not backup_enabled():
        return False
    if os.path.exists(path):
        return False
    try:
        resp = requests.get(f"{GITHUB_API}/gists/{GITHUB_GIST_ID}", headers=_gist_headers(), timeout=timeout)
        if resp.status_code != 200:
            print(f"[restore] échec lecture Gist : HTTP {resp.status_code}")
            return False
        files = resp.json().get("files", {})
        file_info = files.get(GIST_BACKUP_FILENAME)
        content = file_info.get("content") if file_info else None
        if not content or content.startswith("placeholder"):
            print("[restore] aucune sauvegarde exploitable dans le Gist (premier démarrage ?)")
            return False
        raw = base64.b64decode(content)
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        print(f"[restore] base restaurée depuis le Gist ({len(raw)} octets)")
        return True
    except Exception as e:
        print(f"[restore] erreur restauration Gist : {e}")
        return False


def _periodic_backup_loop():
    while True:
        time.sleep(BACKUP_INTERVAL_SECONDS)
        backup_db_to_gist()


def start_backup_thread():
    if not backup_enabled():
        print("[backup] désactivé (GITHUB_TOKEN / GITHUB_GIST_ID absents) — la base ne survivra pas à un redéploiement")
        return
    t = threading.Thread(target=_periodic_backup_loop, daemon=True)
    t.start()
    print(f"[backup] sauvegarde automatique activée (toutes les {BACKUP_INTERVAL_SECONDS}s)")


def _handle_shutdown_signal(signum, frame):
    print("[backup] signal d'arrêt reçu, sauvegarde finale avant coupure...")
    backup_db_to_gist()
    sys.exit(0)


def register_shutdown_backup():
    if not backup_enabled():
        return
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    atexit.register(backup_db_to_gist)


# ============================================================
# UTILITAIRES TEXTE — partagés par le lexique adaptatif et le moteur
# d'anticipation par motifs (voir plus bas)
# ============================================================

_STOPWORDS = {
    'le','la','les','un','une','des','de','du','et','ou','à','au','aux','en',
    'ce','ces','cet','cette','que','qui','quoi','dont','pour','par','sur',
    'avec','sans','pas','plus','moins','très','ça','se','son','sa','ses',
    'je','tu','il','elle','on','nous','vous','ils','elles','est','sont',
    'the','a','an','of','to','in','on','for','and','or','is','are','it',
    'this','that','be','have','has','was','were','i','you','he','she','we',
}

def tokenize(text):
    """Tokenisation simple : minuscules, mots de 3+ lettres, stopwords retirés."""
    words = re.findall(r"[a-zàâäéèêëïîôöùûüç]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}

def jaccard(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


# ============================================================
# STRUCTURES DE DONNÉES
# ============================================================

class ActionType(Enum):
    EMAIL_DRAFT = "email_draft"
    CALENDAR_SCHEDULE = "calendar_schedule"
    REMINDER_SET = "reminder_set"
    TASK_CREATE = "task_create"
    INFO_DIGEST = "info_digest"
    CONTACT_NUDGE = "contact_nudge"
    FOCUS_BLOCK = "focus_block"
    DECISION_PROMPT = "decision_prompt"

class Priority(Enum):
    CRITICAL = 5
    HIGH = 4
    MEDIUM = 3
    LOW = 2
    MINIMAL = 1

@dataclass
class ContextSignal:
    source: str
    content: str
    timestamp: datetime
    metadata: dict = field(default_factory=dict)
    emotional_tone: str = None
    urgency_indicator: float = None

@dataclass
class UserProfile:
    user_id: str
    created_at: datetime
    communication_style: str = "professional"
    peak_productivity_hours: list = field(default_factory=lambda: [9, 10, 14, 15])
    stress_triggers: list = field(default_factory=list)
    relationship_map: dict = field(default_factory=dict)
    decision_patterns: dict = field(default_factory=dict)
    learned_preferences: dict = field(default_factory=dict)
    api_key: str = ""

@dataclass
class PredictedNeed:
    need_id: str
    description: str
    predicted_at: datetime
    expected_window: timedelta
    confidence: float
    source_signals: list
    suggested_action: 'NexusAction'

@dataclass
class NexusAction:
    action_id: str
    action_type: ActionType
    description: str
    target_context: dict
    confidence: float
    priority: Priority
    reasoning_chain: list
    auto_executable: bool = False
    requires_approval: bool = True
    executed_at: datetime = None
    status: str = "pending"

@dataclass
class DecisionLog:
    log_id: str
    timestamp: datetime
    input_signals: list
    reasoning: str
    decision: str
    confidence: float
    outcome: str = None
    user_feedback: str = None


# ============================================================
# BASE DE DONNÉES SQLITE
# ============================================================

class NexusDatabase:
    """Couche de persistance SQLite. Tout est stocké, rien ne disparaît."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        # Crée le dossier parent si le chemin configuré en pointe un qui n'existe
        # pas (ex: NEXUS_DB_PATH mal réglé sur un disque non attaché). Évite un
        # crash total au démarrage pour une simple erreur de configuration.
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                print(f"[db] impossible de créer le dossier '{parent}' ({e}) — "
                      f"repli sur 'nexus.db' dans le dossier courant")
                self.db_path = "nexus.db"
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, api_key TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL, profile_json TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS signals (
            signal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            source TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT NOT NULL,
            metadata_json TEXT, emotional_tone TEXT, urgency_indicator REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS actions (
            action_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, action_type TEXT NOT NULL,
            description TEXT NOT NULL, target_context_json TEXT, confidence REAL NOT NULL,
            priority TEXT NOT NULL, reasoning_chain TEXT, auto_executable INTEGER DEFAULT 0,
            requires_approval INTEGER DEFAULT 1, status TEXT DEFAULT 'pending',
            executed_at TEXT, created_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS decision_logs (
            log_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, timestamp TEXT NOT NULL,
            reasoning TEXT, decision TEXT NOT NULL, confidence REAL,
            outcome TEXT, user_feedback TEXT)''')
        # Lexique adaptatif : poids appris par mot à partir des approbations/rejets
        # réels de l'utilisateur, en complément (pas en remplacement) des listes de
        # mots-clés statiques. category = 'urgency' ou 'emotion:<tone>'.
        c.execute('''CREATE TABLE IF NOT EXISTS word_feedback (
            user_id TEXT NOT NULL, word TEXT NOT NULL, category TEXT NOT NULL,
            pos INTEGER DEFAULT 0, neg INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, word, category))''')
        # Motifs précurseurs : associe un ensemble de mots significatifs (issus
        # d'un signal ayant mené à une action) au type d'action qui a suivi, avec
        # le nombre d'occurrences et le nombre de fois où l'action a été approuvée.
        # C'est ce qui permet d'anticiper sur un signal qui RESSEMBLE à un motif
        # passé, même sans mot-clé explicite (deadline, "urgent", etc.).
        c.execute('''CREATE TABLE IF NOT EXISTS precursor_patterns (
            user_id TEXT NOT NULL, signature TEXT NOT NULL, action_type TEXT NOT NULL,
            occurrences INTEGER DEFAULT 0, hits INTEGER DEFAULT 0, last_seen TEXT,
            PRIMARY KEY (user_id, signature, action_type))''')
        conn.commit(); conn.close()

    def _hash_key(self, api_key):
        # Hash SHA-256 de la clé — seul le hash est stocké en base, jamais la clé en clair.
        return hashlib.sha256(api_key.encode()).hexdigest()

    def create_user(self, user_id, profile_json):
        # Si l'utilisateur existe déjà, on ne peut pas retrouver son ancienne clé
        # en clair (elle n'est jamais stockée) : on renvoie une info claire plutôt
        # que de recréer silencieusement une clé qui casserait les anciennes intégrations.
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if c.fetchone():
            conn.close()
            return None  # signale à l'appelant : utilisateur déjà enregistré

        api_key = secrets.token_urlsafe(32)
        key_hash = self._hash_key(api_key)
        try:
            c.execute("INSERT INTO users VALUES (?,?,?,?)", (user_id, key_hash, datetime.now().isoformat(), profile_json))
            conn.commit()
            return api_key  # la clé en clair n'est renvoyée qu'une seule fois, à la création
        finally:
            conn.close()

    def verify_api_key(self, api_key):
        key_hash = self._hash_key(api_key)
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE api_key=?", (key_hash,))
        r = c.fetchone(); conn.close()
        return r[0] if r else None

    def get_user_api_key(self, user_id):
        # Le hash n'est pas une clé utilisable : on ne peut plus "récupérer" la clé
        # d'un utilisateur existant, seulement vérifier une clé fournie (verify_api_key).
        # Cette méthode est conservée pour compatibilité mais renvoie toujours "".
        return ""

    def save_signal(self, user_id, source, content, timestamp, metadata, emotional_tone, urgency):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO signals (user_id,source,content,timestamp,metadata_json,emotional_tone,urgency_indicator) VALUES (?,?,?,?,?,?,?)",
                  (user_id, source, content, timestamp.isoformat(), json.dumps(metadata), emotional_tone, urgency))
        conn.commit(); conn.close()

    def get_signals(self, user_id, hours=24):
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT source,content,timestamp,metadata_json,emotional_tone,urgency_indicator FROM signals WHERE user_id=? AND timestamp>? ORDER BY timestamp DESC", (user_id, cutoff))
        r = c.fetchall(); conn.close()
        return [ContextSignal(source=x[0],content=x[1],timestamp=datetime.fromisoformat(x[2]),metadata=json.loads(x[3]) if x[3] else {},emotional_tone=x[4],urgency_indicator=x[5]) for x in r]

    def save_action(self, user_id, action):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (action.action_id, user_id, action.action_type.value, action.description,
                   json.dumps(action.target_context), action.confidence, action.priority.name,
                   json.dumps(action.reasoning_chain), int(action.auto_executable), int(action.requires_approval),
                   action.status, None, datetime.now().isoformat()))
        conn.commit(); conn.close()

    def get_action(self, user_id, action_id):
        """Retourne (status) de l'action si elle appartient bien à cet utilisateur, sinon None."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT status FROM actions WHERE action_id=? AND user_id=?", (action_id, user_id))
        r = c.fetchone(); conn.close()
        return r[0] if r else None

    def get_pending_actions(self, user_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT action_id,action_type,description,target_context_json,confidence,priority,reasoning_chain,auto_executable,requires_approval,status FROM actions WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 10", (user_id,))
        r = c.fetchall(); conn.close()
        return [NexusAction(action_id=x[0],action_type=ActionType(x[1]),description=x[2],target_context=json.loads(x[3]) if x[3] else {},confidence=x[4],priority=Priority[x[5]],reasoning_chain=json.loads(x[6]) if x[6] else [],auto_executable=bool(x[7]),requires_approval=bool(x[8]),status=x[9]) for x in r]

    def update_action_status(self, action_id, status, executed_at=None):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        if executed_at:
            c.execute("UPDATE actions SET status=?,executed_at=? WHERE action_id=?", (status, executed_at.isoformat(), action_id))
        else:
            c.execute("UPDATE actions SET status=? WHERE action_id=?", (status, action_id))
        conn.commit(); conn.close()

    def save_decision_log(self, user_id, log):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO decision_logs VALUES (?,?,?,?,?,?,?,?)",
                  (log.log_id, user_id, log.timestamp.isoformat(), log.reasoning, log.decision, log.confidence, log.outcome, log.user_feedback))
        conn.commit(); conn.close()

    def get_decision_logs(self, user_id, hours=24):
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT log_id,timestamp,reasoning,decision,confidence,outcome,user_feedback FROM decision_logs WHERE user_id=? AND timestamp>? ORDER BY timestamp DESC", (user_id, cutoff))
        r = c.fetchall(); conn.close(); return r

    def get_stats(self, user_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM actions WHERE user_id=?", (user_id,))
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM actions WHERE user_id=? AND status='executed'", (user_id,))
        executed = c.fetchone()[0]
        c.execute("SELECT action_type,COUNT(*) FROM actions WHERE user_id=? GROUP BY action_type ORDER BY COUNT(*) DESC LIMIT 5", (user_id,))
        types = dict(c.fetchall()); conn.close()
        return {'total_actions':total,'executed_actions':executed,'approval_rate':f"{executed/max(total,1):.1%}",'action_types':types}

    def get_action_full(self, user_id, action_id):
        """Version complète de get_action : renvoie tout ce qu'il faut pour
        apprendre d'une décision (approve/reject), y compris après un
        redémarrage où l'action n'est plus en mémoire (pending_approvals)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""SELECT action_type, target_context_json, priority, status
                     FROM actions WHERE action_id=? AND user_id=?""", (action_id, user_id))
        r = c.fetchone(); conn.close()
        if not r: return None
        return {'action_type': r[0], 'target_context': json.loads(r[1]) if r[1] else {},
                'priority': r[2], 'status': r[3]}

    def get_approval_stats(self, user_id, action_type, priority):
        """Historique réel d'approbation pour un (type d'action, priorité) donné,
        limité aux actions qui exigeaient une approbation humaine (on ne calibre
        pas sur les digests auto-exécutés, qui ne reflètent pas un choix)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""SELECT status, COUNT(*) FROM actions
                     WHERE user_id=? AND action_type=? AND priority=? AND requires_approval=1
                       AND status IN ('executed','rejected')
                     GROUP BY status""", (user_id, action_type, priority))
        counts = dict(c.fetchall()); conn.close()
        approved = counts.get('executed', 0)
        rejected = counts.get('rejected', 0)
        return approved, rejected

    def record_word_feedback(self, user_id, words, category, positive):
        """Renforce (positive=True) ou affaiblit (positive=False) le poids d'un
        ensemble de mots pour une catégorie donnée ('urgency' ou 'emotion:xxx'),
        suite à une décision réelle de l'utilisateur (approve/reject)."""
        if not words: return
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        col = 'pos' if positive else 'neg'
        for w in words:
            c.execute(f"""INSERT INTO word_feedback (user_id, word, category, {col})
                          VALUES (?,?,?,1)
                          ON CONFLICT(user_id, word, category)
                          DO UPDATE SET {col} = {col} + 1""", (user_id, w, category))
        conn.commit(); conn.close()

    def get_word_weights(self, user_id, category):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT word, pos, neg FROM word_feedback WHERE user_id=? AND category=?", (user_id, category))
        r = c.fetchall(); conn.close()
        return {w: (pos, neg) for w, pos, neg in r}

    def record_precursor_outcome(self, user_id, signature_words, action_type, hit):
        """Enregistre qu'un signal avec ce jeu de mots significatifs a mené à ce
        type d'action, et si l'utilisateur l'a validée (hit=True) ou non. La
        signature est stockée comme chaîne triée pour être réutilisable comme clé."""
        if not signature_words: return
        signature = ",".join(sorted(signature_words))[:500]
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT INTO precursor_patterns (user_id, signature, action_type, occurrences, hits, last_seen)
                     VALUES (?,?,?,1,?,?)
                     ON CONFLICT(user_id, signature, action_type)
                     DO UPDATE SET occurrences = occurrences + 1, hits = hits + excluded.hits, last_seen = excluded.last_seen""",
                  (user_id, signature, action_type, 1 if hit else 0, datetime.now().isoformat()))
        conn.commit(); conn.close()

    def get_precursor_patterns(self, user_id, min_occurrences=3):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""SELECT signature, action_type, occurrences, hits FROM precursor_patterns
                     WHERE user_id=? AND occurrences>=?""", (user_id, min_occurrences))
        r = c.fetchall(); conn.close()
        return [{'words': set(sig.split(",")) if sig else set(), 'action_type': at,
                  'occurrences': occ, 'hits': hits} for sig, at, occ, hits in r]


# ============================================================
# MOTEUR DE CONTEXTE
# ============================================================

class ContextEngine:
    def __init__(self, user_id, db):
        self.user_id = user_id; self.db = db
        self.profile = UserProfile(user_id=user_id, created_at=datetime.now(), api_key=db.get_user_api_key(user_id) or "")
        self._load_profile()

    def _load_profile(self):
        conn = sqlite3.connect(self.db.db_path)
        c = conn.cursor()
        c.execute("SELECT profile_json FROM users WHERE user_id=?", (self.user_id,))
        r = c.fetchone(); conn.close()
        if r:
            d = json.loads(r[0])
            self.profile.communication_style = d.get('communication_style', 'professional')
            self.profile.learned_preferences = d.get('learned_preferences', {})
            self.profile.stress_triggers = d.get('stress_triggers', [])

    def _save_profile(self):
        d = {'communication_style': self.profile.communication_style, 'learned_preferences': self.profile.learned_preferences, 'stress_triggers': self.profile.stress_triggers}
        conn = sqlite3.connect(self.db.db_path)
        c = conn.cursor()
        c.execute("UPDATE users SET profile_json=? WHERE user_id=?", (json.dumps(d), self.user_id))
        conn.commit(); conn.close()

    def ingest_signal(self, signal):
        signal.urgency_indicator = self._extract_urgency(signal)
        signal.emotional_tone = self._extract_emotion(signal)
        self.db.save_signal(self.user_id, signal.source, signal.content, signal.timestamp, signal.metadata, signal.emotional_tone, signal.urgency_indicator)
        self._update_profile_from_signal(signal); self._save_profile()

    def _extract_urgency(self, signal):
        # Base statique : liste de mots-clés figée, sert de point de départ à
        # froid (aucun historique nécessaire) mais casse sur les synonymes et
        # les formulations imprévues ("pas de panique, mais...").
        markers = {'high': ['urgent','asap','deadline','emergency','critical','immediately',"aujourd'hui",'demain','ce soir','ce matin'],
                   'medium': ['bientôt','prochainement','cette semaine','dans les jours'],
                   'low': ['quand tu peux','pas pressé','pas urgent','à ta convenance']}
        score = 0.0; cl = signal.content.lower()
        for w in markers['high']:
            if w in cl: score += 0.4
        for w in markers['medium']:
            if w in cl: score += 0.2
        for w in markers['low']:
            if w in cl: score -= 0.3

        # Lexique adaptatif : ajuste le score avec des mots que CE utilisateur a,
        # par ses propres approbations/rejets passés, associés à de l'urgence
        # réelle — même si ces mots n'apparaissent dans aucune liste statique.
        # Poids = log-odds lissé, borné pour qu'un seul mot ne domine pas le score.
        learned = self.db.get_word_weights(self.user_id, 'urgency')
        if learned:
            for w in tokenize(signal.content):
                if w in learned:
                    pos, neg = learned[w]
                    weight = (pos - neg) / (pos + neg + 3)  # lissage : proche de 0 tant que peu de preuves
                    score += max(-0.3, min(0.3, weight))

        if signal.metadata.get('deadline'):
            try:
                dl = parse_iso_naive(signal.metadata['deadline'])
                hu = (dl - signal.timestamp).total_seconds() / 3600
                if hu < 24: score += 0.5
                elif hu < 72: score += 0.3
                elif hu < 168: score += 0.1
            except: pass
        return min(1.0, max(0.0, score))

    _NEGATION_WORDS = ['rien', 'pas', 'aucun', 'aucune', 'sans', 'jamais', 'ni']

    def _negated_before(self, text_lower, keyword, window_chars=20):
        """Vrai si un mot de négation précède ce mot-clé de près (ex: 'rien
        d'urgent'). Naïf par nature (fenêtre de caractères, pas d'analyse
        syntaxique réelle), mais couvre les formulations les plus courantes
        sans faux positifs constatés dans les tests."""
        for m in re.finditer(re.escape(keyword), text_lower):
            start = max(0, m.start() - window_chars)
            preceding = text_lower[start:m.start()]
            if any(re.search(r'\b' + re.escape(neg) + r'\b', preceding) for neg in self._NEGATION_WORDS):
                return True
        return False

    def _extract_emotion(self, signal):
        cl = signal.content.lower()
        em = {'stressed': ['stress','débordé','overwhelmed','panique','anxieux','urgent'],
              'frustrated': ['énervé','frustré','inacceptable','ridicule','problème'],
              'excited': ['super','génial','excellent','opportunité','gagnant'],
              'sad': ['désolé','triste','difficile','perdu','abandonné']}
        # Un mot-clé précédé d'une négation ('rien d'urgent', 'aucun problème')
        # ne doit pas compter — sinon un email qui dit explicitement qu'il n'y
        # a PAS de stress finit par déclencher une action de gestion du stress.
        sc = {k: float(sum(1 for w in v if w in cl and not self._negated_before(cl, w))) for k, v in em.items()}

        # Même principe que pour l'urgence : des mots appris spécifiques à cet
        # utilisateur peuvent renforcer une émotion sans figurer dans la liste statique.
        tokens = tokenize(signal.content)
        for emotion in em:
            learned = self.db.get_word_weights(self.user_id, f'emotion:{emotion}')
            if not learned: continue
            for w in tokens:
                if w in learned:
                    pos, neg = learned[w]
                    sc[emotion] += max(-0.5, min(0.5, (pos - neg) / (pos + neg + 3)))

        if max(sc.values()) <= 0: return 'neutral'
        return max(sc, key=sc.get)

    def _update_profile_from_signal(self, signal):
        h = signal.timestamp.hour
        if signal.source in ['email_sent','task_completed','focus_session']:
            if 'productivity_hours' not in self.profile.learned_preferences:
                self.profile.learned_preferences['productivity_hours'] = []
            self.profile.learned_preferences['productivity_hours'].append(h)
        if signal.emotional_tone == 'stressed':
            for w in signal.content.lower().split():
                if len(w) > 4 and w not in ['avoir','être','faire','pouvoir'] and w not in self.profile.stress_triggers:
                    self.profile.stress_triggers.append(w)

    def get_recent_signals(self, hours=24, source_filter=None):
        s = self.db.get_signals(self.user_id, hours)
        if source_filter: s = [x for x in s if x.source.startswith(source_filter)]
        return s

    def get_context_summary(self):
        recent = self.get_recent_signals(hours=24)
        return {'total_signals_24h': len(recent),
                'urgency_level': sum(s.urgency_indicator or 0 for s in recent) / max(len(recent), 1),
                'emotional_state': self._dominant_emotion(recent),
                'top_sources': dict(Counter(s.source for s in recent).most_common(5)),
                'pending_deadlines': self._count_pending_deadlines(),
                'unread_communications': len([s for s in recent if s.source == 'email' and not s.metadata.get('read')]),
                'productivity_score': self._calculate_productivity_score(recent)}

    def _dominant_emotion(self, signals):
        e = [s.emotional_tone for s in signals if s.emotional_tone]
        return max(set(e), key=e.count) if e else 'neutral'

    def _count_pending_deadlines(self):
        conn = sqlite3.connect(self.db.db_path)
        c = conn.cursor()
        c.execute("SELECT metadata_json FROM signals WHERE user_id=? AND metadata_json LIKE '%deadline%'", (self.user_id,))
        r = c.fetchall(); conn.close()
        n = 0
        for x in r:
            try:
                m = json.loads(x[0])
                if m.get('deadline') and parse_iso_naive(m['deadline']) > datetime.now(): n += 1
            except: pass
        return n

    def _calculate_productivity_score(self, signals):
        if not signals: return 0.5
        c = len([s for s in signals if 'completed' in s.source or 'done' in s.content.lower()])
        t = len([s for s in signals if 'task' in s.source])
        return min(1.0, c / max(t, 1)) if t else 0.5


# ============================================================
# MOTEUR DE PRÉDICTION
# ============================================================

MIN_CALIBRATION_SAMPLES = 5  # en dessous de ce nombre de décisions passées, on garde le prior par défaut

class PredictionEngine:
    def __init__(self, context_engine):
        self.context = context_engine
        self.prediction_history = []
        self.accuracy_log = []
        self.anticipation = AnticipationEngine(context_engine)

    def generate_predictions(self, horizon_hours=24):
        p = []
        p.extend(self._predict_deadline_actions())
        p.extend(self._predict_communication_needs())
        p.extend(self._predict_wellbeing_actions())
        p.extend(self._predict_opportunity_actions())
        p.extend(self._predict_habit_based_actions())
        # Anticipation par motifs : signaux qui ressemblent à des précurseurs
        # historiques, même sans mot-clé/deadline explicite. Voir AnticipationEngine.
        p.extend(self.anticipation.predict(rule_based_so_far=p))
        return self._filter_and_score(p)

    def _confidence(self, action_type, priority, default, reasoning_chain):
        """Renvoie une confiance calibrée sur l'historique réel d'approbation de
        CET utilisateur pour ce (type d'action, priorité), avec repli explicite
        sur le prior par défaut quand il n'y a pas encore assez de décisions
        passées (cold start honnête plutôt que chiffre inventé)."""
        approved, rejected = self.context.db.get_approval_stats(self.context.user_id, action_type.value, priority.name)
        total = approved + rejected
        if total >= MIN_CALIBRATION_SAMPLES:
            calibrated = (approved + 1) / (total + 2)  # lissage de Laplace, prior neutre 0.5
            reasoning_chain.append(f"Confiance calibrée sur {total} décisions passées similaires ({approved} approuvées, {rejected} rejetées)")
            return round(calibrated, 2)
        reasoning_chain.append(f"Confiance par défaut — seulement {total} décision(s) passée(s) similaire(s), pas assez pour calibrer")
        return default

    def _predict_deadline_actions(self):
        p = []; now = datetime.now()
        conn = sqlite3.connect(self.context.db.db_path)
        c = conn.cursor()
        c.execute("SELECT source,content,timestamp,metadata_json,emotional_tone,urgency_indicator FROM signals WHERE user_id=? AND metadata_json LIKE '%deadline%'", (self.context.user_id,))
        r = c.fetchall(); conn.close()
        for x in r:
            try:
                m = json.loads(x[3])
                if not m.get('deadline'): continue
                dl = parse_iso_naive(m['deadline'])
                hu = (dl - now).total_seconds() / 3600
                s = ContextSignal(source=x[0],content=x[1],timestamp=datetime.fromisoformat(x[2]),metadata=m,emotional_tone=x[4],urgency_indicator=x[5])
                if 0 < hu < 4:
                    rc = [f"Deadline: {m['deadline']}",f"Temps restant: {int(hu)}h","Action: Rappel immédiat"]
                    conf = self._confidence(ActionType.REMINDER_SET, Priority.CRITICAL, 0.95, rc)
                    a = NexusAction(action_id=self._gen_id(),action_type=ActionType.REMINDER_SET,description=f"URGENT: Deadline dans {int(hu)}h — {s.content[:80]}...",target_context={'deadline':m['deadline'],'trigger_text':s.content},confidence=conf,priority=Priority.CRITICAL,reasoning_chain=rc)
                    p.append(PredictedNeed(self._gen_id(),f"Gestion urgente: {s.content[:60]}",now,timedelta(hours=1),conf,[s.source],a))
                elif 4 <= hu < 24:
                    rc = [f"Deadline dans {int(hu)}h","Action: Création de tâche"]
                    conf = self._confidence(ActionType.TASK_CREATE, Priority.HIGH, 0.85, rc)
                    a = NexusAction(action_id=self._gen_id(),action_type=ActionType.TASK_CREATE,description=f"Planifier: {s.content[:80]}... (deadline dans {int(hu)}h)",target_context={'deadline':m['deadline'],'trigger_text':s.content},confidence=conf,priority=Priority.HIGH,reasoning_chain=rc)
                    p.append(PredictedNeed(self._gen_id(),f"Planification: {s.content[:60]}",now,timedelta(hours=4),conf,[s.source],a))
            except: pass
        return p

    def _predict_communication_needs(self):
        p = []; now = datetime.now()
        recent = self.context.get_recent_signals(hours=48)
        unread = [s for s in recent if s.source in ['email','messaging','social'] and not s.metadata.get('responded')]
        by_sender = defaultdict(list)
        for s in unread: by_sender[s.metadata.get('from','unknown')].append(s)
        for sender, signals in by_sender.items():
            if len(signals) >= 3:
                latest = max(signals, key=lambda x: x.timestamp)
                hs = (now - latest.timestamp).total_seconds() / 3600
                if hs > 6:
                    rc = [f"{len(signals)} messages de {sender}",f"Dernier il y a {int(hs)}h","Action: Brouillon de réponse"]
                    conf = self._confidence(ActionType.EMAIL_DRAFT, Priority.HIGH, 0.75, rc)
                    trigger_text = " | ".join(sg.content for sg in signals[-3:])
                    a = NexusAction(action_id=self._gen_id(),action_type=ActionType.EMAIL_DRAFT,description=f"Brouillon réponse à {sender} ({len(signals)} messages non traités)",target_context={'sender':sender,'count':len(signals),'trigger_text':trigger_text},confidence=conf,priority=Priority.HIGH,reasoning_chain=rc)
                    p.append(PredictedNeed(self._gen_id(),f"Réponse attendue par {sender}",now,timedelta(hours=2),conf,[s.source for s in signals],a))
        return p

    def _predict_wellbeing_actions(self):
        p = []; now = datetime.now()
        recent = self.context.get_recent_signals(hours=6)
        stress = [s for s in recent if s.emotional_tone == 'stressed']
        if len(stress) >= 2:
            rc = [f"{len(stress)} signaux de stress","Charge cognitive élevée","Action: Bloc de focus 25min"]
            conf = self._confidence(ActionType.FOCUS_BLOCK, Priority.HIGH, 0.80, rc)
            trigger_text = " | ".join(sg.content for sg in stress[-3:])
            a = NexusAction(action_id=self._gen_id(),action_type=ActionType.FOCUS_BLOCK,description="Bloc de focus recommandé — stress élevé détecté",target_context={'stress_count':len(stress),'trigger_text':trigger_text},confidence=conf,priority=Priority.HIGH,reasoning_chain=rc)
            p.append(PredictedNeed(self._gen_id(),"Gestion du stress — pause",now,timedelta(minutes=30),conf,[s.source for s in stress],a))
        if len(recent) > 30:
            a = NexusAction(action_id=self._gen_id(),action_type=ActionType.INFO_DIGEST,description=f"Digest d'information — {len(recent)} signaux en 6h",target_context={'count':len(recent)},confidence=0.70,priority=Priority.MEDIUM,reasoning_chain=[f"{len(recent)} signaux en 6h","Surcharge informationnelle","Action: Synthèse auto"],auto_executable=True,requires_approval=False)
            p.append(PredictedNeed(self._gen_id(),"Digest — surcharge",now,timedelta(minutes=15),0.70,[s.source for s in recent[:5]],a))
        return p

    def _predict_opportunity_actions(self):
        p = []; now = datetime.now()
        kw = ['opportunité','opportunity','offre','offer','promotion','invitation','partenariat','partnership','collaboration','nouveau projet','new project']
        for s in self.context.get_recent_signals(hours=48):
            if any(w in s.content.lower() for w in kw) and s.urgency_indicator and s.urgency_indicator > 0.3:
                rc = ["Mot-clé opportunité",f"Urgence: {s.urgency_indicator}","Action: Prompt décision"]
                conf = self._confidence(ActionType.DECISION_PROMPT, Priority.MEDIUM, 0.65, rc)
                a = NexusAction(action_id=self._gen_id(),action_type=ActionType.DECISION_PROMPT,description=f"Opportunité: {s.content[:100]}...",target_context={'content':s.content[:200],'trigger_text':s.content},confidence=conf,priority=Priority.MEDIUM,reasoning_chain=rc)
                p.append(PredictedNeed(self._gen_id(),f"Opportunité: {s.content[:60]}",now,timedelta(hours=12),conf,[s.source],a))
        return p

    def _predict_habit_based_actions(self):
        p = []; now = datetime.now(); ch = now.hour
        ph = self.context.profile.learned_preferences.get('productivity_hours',[9,10,14,15])
        if ch in ph:
            b = [s for s in self.context.get_recent_signals(hours=1) if s.source == 'browsing' and any(w in s.content.lower() for w in ['youtube','reddit','twitter','instagram','facebook'])]
            if len(b) > 5:
                rc = [f"Heure productive: {ch}h",f"{len(b)} distractions","Action: Focus block"]
                conf = self._confidence(ActionType.FOCUS_BLOCK, Priority.LOW, 0.60, rc)
                a = NexusAction(action_id=self._gen_id(),action_type=ActionType.FOCUS_BLOCK,description="Heure de productivité — redirection suggérée",target_context={'hour':ch,'distractions':len(b)},confidence=conf,priority=Priority.LOW,reasoning_chain=rc)
                p.append(PredictedNeed(self._gen_id(),"Redirection productivité",now,timedelta(minutes=15),conf,[s.source for s in b],a))
        return p

    def _filter_and_score(self, predictions):
        seen = set(); filtered = []
        for p in predictions:
            k = p.description[:50]
            if k not in seen:
                seen.add(k); filtered.append(p)
        po = {Priority.CRITICAL:5,Priority.HIGH:4,Priority.MEDIUM:3,Priority.LOW:2,Priority.MINIMAL:1}
        filtered.sort(key=lambda x:(po.get(x.suggested_action.priority,0),x.confidence),reverse=True)
        return filtered[:10]

    def _gen_id(self):
        return hashlib.md5(f"{datetime.now().isoformat()}{random.random()}".encode()).hexdigest()[:12]


# ============================================================
# MOTEUR D'ANTICIPATION — motifs précurseurs appris de l'historique
# ============================================================
# Ce que ce moteur fait réellement : il compare le vocabulaire d'un nouveau
# signal aux "signatures" de mots qui, par le passé, ont précédé une action
# que CET utilisateur a validée. Si le recouvrement est suffisant ET que le
# motif a déjà été observé plusieurs fois avec un taux d'approbation correct,
# il propose l'action AVANT qu'un mot-clé explicite (deadline, "urgent", etc.)
# n'apparaisse. C'est une vraie généralisation lexicale (similarité d'ensembles
# de mots, pas correspondance exacte) — mais elle reste bornée par l'historique
# réel de l'utilisateur : sans preuve accumulée (occurrences >= seuil), rien ne
# se déclenche. Ce n'est pas de la compréhension sémantique, c'est de la
# reconnaissance de motifs récurrents, honnêtement scorée par sa propre preuve.

MIN_PATTERN_EVIDENCE = 3      # nombre minimum d'occurrences passées avant de faire confiance à un motif
MIN_PATTERN_SIMILARITY = 0.35  # recouvrement de Jaccard minimum avec un motif connu
MIN_PATTERN_HIT_RATE = 0.5     # taux d'approbation historique minimum du motif

class AnticipationEngine:
    def __init__(self, context_engine):
        self.context = context_engine

    def predict(self, rule_based_so_far=None, hours=12):
        already_covered = {s for pred in (rule_based_so_far or []) for s in pred.source_signals}
        patterns = self.context.db.get_precursor_patterns(self.context.user_id, min_occurrences=MIN_PATTERN_EVIDENCE)
        if not patterns:
            return []  # pas d'historique -> pas d'anticipation. Cold start honnête, pas de magie.

        p = []; now = datetime.now()
        recent = self.context.get_recent_signals(hours=hours)
        for s in recent:
            sig_words = tokenize(s.content)
            if not sig_words:
                continue
            best = None
            for pat in patterns:
                sim = jaccard(sig_words, pat['words'])
                if sim < MIN_PATTERN_SIMILARITY:
                    continue
                hit_rate = pat['hits'] / pat['occurrences']
                if hit_rate < MIN_PATTERN_HIT_RATE:
                    continue
                score = sim * hit_rate
                if best is None or score > best[0]:
                    best = (score, sim, hit_rate, pat)
            if not best:
                continue
            score, sim, hit_rate, pat = best
            try:
                action_type = ActionType(pat['action_type'])
            except ValueError:
                continue
            confidence = round(0.25 + 0.5 * hit_rate * sim, 2)  # volontairement plus prudent qu'une règle explicite
            priority = Priority.MEDIUM if hit_rate >= 0.7 else Priority.LOW
            rc = [
                f"Aucun mot-clé explicite détecté — signal similaire à un motif passé ({int(sim*100)}% de recouvrement lexical)",
                f"Ce motif a précédé une action « {action_type.value} » {pat['occurrences']} fois, approuvée {pat['hits']} fois ({int(hit_rate*100)}%)",
                "Anticipation fondée sur ton historique, pas sur un mot-clé de la liste statique",
            ]
            a = NexusAction(
                action_id=hashlib.md5(f"{now.isoformat()}{random.random()}".encode()).hexdigest()[:12],
                action_type=action_type,
                description=f"Anticipé (motif reconnu) : {s.content[:80]}...",
                target_context={'trigger_text': s.content, 'anticipated': True, 'similarity': round(sim, 2)},
                confidence=confidence, priority=priority, reasoning_chain=rc,
            )
            p.append(PredictedNeed(
                hashlib.md5(f"{now.isoformat()}{random.random()}anticip".encode()).hexdigest()[:12],
                f"Anticipation motif: {s.content[:60]}", now, timedelta(hours=6), confidence, [s.source], a,
            ))
        return p


# ============================================================
# MOTEUR D'ACTION
# ============================================================

class ActionEngine:
    # Catégorie de lexique adaptatif à nourrir selon le type d'action approuvée/rejetée.
    _WORD_FEEDBACK_CATEGORY = {
        ActionType.REMINDER_SET: 'urgency',
        ActionType.TASK_CREATE: 'urgency',
        ActionType.FOCUS_BLOCK: 'emotion:stressed',
    }

    def __init__(self, context_engine, prediction_engine, db):
        self.context = context_engine; self.predictor = prediction_engine; self.db = db
        self.pending_approvals = []; self.executed_actions = []

    def process_predictions(self, predictions):
        for pred in predictions:
            a = pred.suggested_action
            self.db.save_action(self.context.user_id, a)
            if a.auto_executable and not a.requires_approval:
                self._execute(a)
            else:
                self.pending_approvals.append(a)

    def _execute(self, action):
        action.status = "executed"; action.executed_at = datetime.now()
        self.db.update_action_status(action.action_id, "executed", action.executed_at)
        self.executed_actions.append(action)
        self._learn_from_feedback(action.action_type, action.target_context, approved=True)
        return {'status':'executed','action_id':action.action_id}

    def _learn_from_feedback(self, action_type, target_context, approved):
        """Point d'entrée unique de la boucle d'apprentissage : appelé après
        CHAQUE décision humaine (approbation ou rejet), qu'elle vienne d'une
        action encore en mémoire ou retrouvée en base après redémarrage.
        Alimente à la fois le lexique adaptatif (mots -> urgence/émotion) et
        les motifs précurseurs (l'anticipation de AnticipationEngine)."""
        target_context = target_context or {}
        trigger_text = target_context.get('trigger_text', '')
        if not trigger_text:
            return
        words = tokenize(trigger_text)
        if not words:
            return
        category = self._WORD_FEEDBACK_CATEGORY.get(action_type)
        if category:
            self.db.record_word_feedback(self.context.user_id, words, category, positive=approved)
        # Un motif précurseur se construit indépendamment du type d'action :
        # même chose qui n'alimente pas le lexique (ex: EMAIL_DRAFT) peut
        # néanmoins devenir un motif reconnu par AnticipationEngine.
        significant = list(words)[:8]  # signature bornée pour rester comparable
        self.db.record_precursor_outcome(self.context.user_id, significant, action_type.value, hit=approved)

    def approve_action(self, action_id):
        for a in self.pending_approvals:
            if a.action_id == action_id:
                self.pending_approvals.remove(a); return self._execute(a)
        # Pas en mémoire (ex: après redémarrage) : vérifier que l'action existe
        # vraiment en base, appartient à cet utilisateur, et est encore en attente,
        # avant de la marquer comme exécutée.
        full = self.db.get_action_full(self.context.user_id, action_id)
        if full is None:
            return {'status': 'not_found', 'action_id': action_id}
        if full['status'] != 'pending':
            return {'status': 'error', 'message': f"action déjà '{full['status']}', ne peut pas être approuvée", 'action_id': action_id}
        self.db.update_action_status(action_id, "executed", datetime.now())
        try:
            self._learn_from_feedback(ActionType(full['action_type']), full['target_context'], approved=True)
        except ValueError:
            pass
        return {'status': 'executed', 'action_id': action_id}

    def reject_action(self, action_id, reason=""):
        for a in self.pending_approvals:
            if a.action_id == action_id:
                a.status = "rejected"; self.pending_approvals.remove(a)
                self.db.update_action_status(action_id, "rejected")
                self.predictor.accuracy_log.append({'action_id':action_id,'outcome':'rejected','reason':reason,'timestamp':datetime.now()})
                self._learn_from_feedback(a.action_type, a.target_context, approved=False)
                return {'status':'rejected'}
        full = self.db.get_action_full(self.context.user_id, action_id)
        if full is None:
            return {'status': 'not_found', 'action_id': action_id}
        if full['status'] != 'pending':
            return {'status': 'error', 'message': f"action déjà '{full['status']}', ne peut pas être rejetée", 'action_id': action_id}
        self.db.update_action_status(action_id, "rejected")
        try:
            self._learn_from_feedback(ActionType(full['action_type']), full['target_context'], approved=False)
        except ValueError:
            pass
        return {'status': 'rejected', 'action_id': action_id}

    def get_pending_actions(self):
        return self.db.get_pending_actions(self.context.user_id)


# ============================================================
# MOTEUR DE TRANSPARENCE
# ============================================================

class TransparencyEngine:
    def __init__(self, context_engine, prediction_engine, action_engine, db):
        self.context = context_engine; self.predictor = prediction_engine
        self.action_engine = action_engine; self.db = db; self.decision_logs = []

    def log_decision(self, action, context_signals, reasoning):
        log = DecisionLog(log_id=self._gen_id(),timestamp=datetime.now(),input_signals=context_signals,reasoning=reasoning,decision=action.description,confidence=action.confidence)
        self.db.save_decision_log(self.context.user_id, log); self.decision_logs.append(log); return log

    def generate_transparency_report(self, action_id=None, time_range_hours=24):
        logs = self.db.get_decision_logs(self.context.user_id, time_range_hours)
        return {'generated_at':datetime.now().isoformat(),'period_hours':time_range_hours,'total_decisions':len(logs),'decisions':[{'log_id':l[0],'timestamp':l[1][:19],'decision':l[3],'confidence':l[4],'reasoning':l[2]} for l in logs]}

    def get_user_stats(self):
        st = self.db.get_stats(self.context.user_id)
        ph = self.context.profile.learned_preferences.get('productivity_hours',[])
        return {'total_actions':st['total_actions'],'approval_rate':st['approval_rate'],'actions_by_type':st['action_types'],'productivity_hours':list(set(ph)) if ph else [9,10,14,15],'stress_triggers':self.context.profile.stress_triggers[-5:],'context_summary':self.context.get_context_summary()}

    def _gen_id(self):
        return hashlib.md5(f"{datetime.now().isoformat()}{random.random()}".encode()).hexdigest()[:12]


# ============================================================
# ORCHESTRATEUR NEXUS
# ============================================================

class NexusSystem:
    def __init__(self, user_id, db):
        self.user_id = user_id; self.db = db
        self.context_engine = ContextEngine(user_id, db)
        self.prediction_engine = PredictionEngine(self.context_engine)
        self.action_engine = ActionEngine(self.context_engine, self.prediction_engine, db)
        self.transparency_engine = TransparencyEngine(self.context_engine, self.prediction_engine, self.action_engine, db)
        self.is_running = False; self.last_cycle = None

    def ingest(self, source, content, metadata=None, timestamp=None):
        if metadata is None: metadata = {}
        if timestamp is None: timestamp = datetime.now()
        s = ContextSignal(source=source, content=content, timestamp=timestamp, metadata=metadata)
        self.context_engine.ingest_signal(s)
        return {'status':'ingested'}

    def run_cycle(self):
        self.is_running = True; self.last_cycle = datetime.now()
        predictions = self.prediction_engine.generate_predictions()
        self.action_engine.process_predictions(predictions)
        for pred in predictions:
            self.transparency_engine.log_decision(pred.suggested_action, [], "; ".join(pred.suggested_action.reasoning_chain))
        self.is_running = False
        return {'predictions':len(predictions),'pending':len(self.action_engine.get_pending_actions()),'executed':len([a for a in self.action_engine.executed_actions if a.executed_at and a.executed_at > self.last_cycle - timedelta(seconds=1)])}

    def get_dashboard(self):
        pending = self.action_engine.get_pending_actions()
        return {'user_id':self.user_id,'status':'running' if self.is_running else 'idle','last_cycle':self.last_cycle.isoformat() if self.last_cycle else None,'context':self.context_engine.get_context_summary(),'pending_actions':[{'id':a.action_id,'type':a.action_type.value,'description':a.description,'priority':a.priority.name,'confidence':a.confidence,'reasoning':a.reasoning_chain} for a in pending[:5]],'stats':self.transparency_engine.get_user_stats()}

    def approve(self, action_id): return self.action_engine.approve_action(action_id)
    def reject(self, action_id, reason=""): return self.action_engine.reject_action(action_id, reason)
    def get_report(self): return self.transparency_engine.generate_transparency_report()


# ============================================================
# API REST FLASK
# ============================================================

try:
    from flask import Flask, request, jsonify, redirect, Response
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

try:
    from flask_cors import CORS
    FLASK_CORS_AVAILABLE = True
except ImportError:
    FLASK_CORS_AVAILABLE = False


class RateLimiter:
    """
    Rate limiter en mémoire à fenêtre glissante, thread-safe.
    Suffisant pour un déploiement mono-process ; pour du multi-instance
    (plusieurs workers/serveurs), il faudrait un backend partagé (Redis).
    """
    def __init__(self, max_requests, window_seconds):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key):
        """Retourne (autorisé: bool, secondes_avant_retry: float)."""
        now = time.time()
        with self._lock:
            q = self._hits[key]
            cutoff = now - self.window_seconds
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                retry_after = self.window_seconds - (now - q[0])
                return False, max(retry_after, 0)
            q.append(now)
            return True, 0.0


if FLASK_AVAILABLE:
    app = Flask(__name__)
    if FLASK_CORS_AVAILABLE:
        CORS(app)
    else:
        # Fallback minimal si flask-cors n'est pas installé : autorise les requêtes
        # cross-origin sur nos propres endpoints JSON sans dépendance externe.
        @app.after_request
        def add_cors_headers(response):
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-API-Key'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            return response

    # Avant toute chose : si une sauvegarde existe sur le Gist et qu'aucun
    # fichier local n'est présent (cas typique après un redéploiement sur
    # Render sans disque persistant), on la restaure.
    restore_db_from_gist()

    db = NexusDatabase()
    nexus_instances = {}

    start_backup_thread()
    register_shutdown_backup()

    # Limites : par utilisateur authentifié (endpoints normaux), par IP pour
    # /register (empêcher la création massive de comptes), et par IP sur les
    # tentatives de clé invalide (ralentir le brute-force de clés API).
    user_limiter = RateLimiter(max_requests=60, window_seconds=60)      # 60 req/min/utilisateur
    register_limiter = RateLimiter(max_requests=5, window_seconds=3600)  # 5 inscriptions/h/IP
    auth_fail_limiter = RateLimiter(max_requests=10, window_seconds=60)  # 10 échecs/min/IP

    def get_nexus(user_id):
        if user_id not in nexus_instances:
            nexus_instances[user_id] = NexusSystem(user_id, db)
        return nexus_instances[user_id]

    def client_ip():
        # X-Forwarded-For si derrière un proxy/load balancer, sinon IP directe.
        forwarded = request.headers.get('X-Forwarded-For', '')
        return forwarded.split(',')[0].strip() if forwarded else request.remote_addr

    def rate_limited_response(retry_after):
        resp = jsonify({'status': 'error', 'message': 'trop de requêtes, réessayez plus tard'})
        resp.status_code = 429
        resp.headers['Retry-After'] = str(int(retry_after) + 1)
        return resp

    def require_auth():
        api_key = request.headers.get('X-API-Key')
        if not api_key:
            return None, jsonify({'status': 'error', 'message': 'API key manquante (header X-API-Key)'}), 401

        user_id = db.verify_api_key(api_key)
        if not user_id:
            # On ne consomme le budget anti brute-force que sur un échec réel,
            # pour ne jamais pénaliser un client légitime qui envoie sa bonne clé.
            ok, retry_after = auth_fail_limiter.check(client_ip())
            if not ok:
                return None, rate_limited_response(retry_after), 429
            return None, jsonify({'status': 'error', 'message': 'API key invalide'}), 401

        allowed, retry_after = user_limiter.check(user_id)
        if not allowed:
            return None, rate_limited_response(retry_after), 429

        return user_id, None, None

    @app.route('/api/v1/register', methods=['POST'])
    def register():
        allowed, retry_after = register_limiter.check(client_ip())
        if not allowed:
            return rate_limited_response(retry_after)
        data = request.json or {}
        user_id = data.get('user_id')
        if not user_id or not isinstance(user_id, str) or not user_id.strip():
            return jsonify({'status': 'error', 'message': 'user_id requis (chaîne non vide)'}), 400
        api_key = db.create_user(user_id.strip(), json.dumps({
            'communication_style': 'professional', 'learned_preferences': {}, 'stress_triggers': []
        }))
        if api_key is None:
            return jsonify({
                'status': 'error',
                'message': "user_id déjà enregistré — la clé API originale ne peut pas être récupérée "
                           "(elle n'est jamais stockée en clair). Choisissez un autre user_id."
            }), 409
        return jsonify({'status': 'success', 'user_id': user_id, 'api_key': api_key}), 201

    @app.route('/api/v1/ingest', methods=['POST'])
    def ingest_signal():
        user_id, err, code = require_auth()
        if err: return err, code
        data = request.json or {}
        source = data.get('source')
        content = data.get('content')
        if not source or not isinstance(source, str):
            return jsonify({'status': 'error', 'message': 'source requis (chaîne non vide)'}), 400
        if not content or not isinstance(content, str):
            return jsonify({'status': 'error', 'message': 'content requis (chaîne non vide)'}), 400
        metadata = data.get('metadata', {})
        if not isinstance(metadata, dict):
            return jsonify({'status': 'error', 'message': 'metadata doit être un objet JSON'}), 400
        nexus = get_nexus(user_id)
        result = nexus.ingest(source, content, metadata)
        return jsonify({'status': 'success', 'result': result})

    @app.route('/api/v1/cycle', methods=['POST'])
    def run_cycle():
        user_id, err, code = require_auth()
        if err: return err, code
        nexus = get_nexus(user_id)
        result = nexus.run_cycle()
        return jsonify({'status': 'success', 'cycle_result': result})

    @app.route('/api/v1/dashboard', methods=['GET'])
    def get_dashboard():
        user_id, err, code = require_auth()
        if err: return err, code
        nexus = get_nexus(user_id)
        return jsonify({'status': 'success', 'dashboard': nexus.get_dashboard()})

    @app.route('/api/v1/actions/<action_id>/approve', methods=['POST'])
    def approve_action(action_id):
        user_id, err, code = require_auth()
        if err: return err, code
        nexus = get_nexus(user_id)
        result = nexus.approve(action_id)
        status_code = {'executed': 200, 'not_found': 404, 'error': 400}.get(result.get('status'), 200)
        return jsonify({'status': 'success' if status_code == 200 else 'error', 'result': result}), status_code

    @app.route('/api/v1/actions/<action_id>/reject', methods=['POST'])
    def reject_action(action_id):
        user_id, err, code = require_auth()
        if err: return err, code
        data = request.json or {}
        reason = data.get('reason', '')
        nexus = get_nexus(user_id)
        result = nexus.reject(action_id, reason)
        status_code = {'rejected': 200, 'not_found': 404, 'error': 400}.get(result.get('status'), 200)
        return jsonify({'status': 'success' if status_code == 200 else 'error', 'result': result}), status_code

    @app.route('/api/v1/transparency', methods=['GET'])
    def get_transparency():
        user_id, err, code = require_auth()
        if err: return err, code
        nexus = get_nexus(user_id)
        return jsonify({'status': 'success', 'report': nexus.get_report()})

    @app.route('/api/v1/auth/gmail/start', methods=['GET'])
    def gmail_auth_start():
        """
        Atteint par navigation directe du navigateur (lien cliqué), donc
        authentifié par user_id + api_key en paramètres d'URL plutôt que
        par le header X-API-Key habituel — un simple lien ne peut pas
        poser de header personnalisé.
        """
        try:
            import nexus_integrations as ni
        except ImportError:
            return jsonify({'status': 'error', 'message': "module nexus_integrations manquant sur le serveur"}), 500

        if not ni.gmail_oauth_configured():
            return jsonify({'status': 'error',
                             'message': "Gmail non configuré côté serveur (GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI manquants)"}), 503

        user_id = request.args.get('user_id')
        api_key = request.args.get('api_key')
        if not user_id or not api_key:
            return jsonify({'status': 'error', 'message': 'user_id et api_key requis en paramètres d\'URL'}), 400

        verified_user = db.verify_api_key(api_key)
        if not verified_user or verified_user != user_id:
            return jsonify({'status': 'error', 'message': 'authentification invalide'}), 401

        state = ni.sign_oauth_state(user_id)
        auth_url = ni.GmailOAuth.build_auth_url(state=state)
        return redirect(auth_url, code=302)

    @app.route('/api/v1/auth/gmail/callback', methods=['GET'])
    def gmail_auth_callback():
        """Google redirige ici après consentement (ou refus) de l'utilisateur."""
        try:
            import nexus_integrations as ni
        except ImportError:
            return _oauth_result_page(False, "Erreur serveur : module d'intégration manquant.")

        error = request.args.get('error')
        if error:
            # L'utilisateur a refusé l'autorisation, ou Google a rejeté la demande.
            return _oauth_result_page(False, f"Connexion Gmail annulée ({error}). Tu peux réessayer.")

        code = request.args.get('code')
        state = request.args.get('state')
        if not code or not state:
            return _oauth_result_page(False, "Réponse Google incomplète (code ou state manquant).")

        try:
            user_id = ni.verify_oauth_state(state)
        except ValueError as e:
            return _oauth_result_page(False, f"Échec de vérification de sécurité : {e}")

        try:
            tokens = ni.GmailOAuth.exchange_code(code)
        except Exception as e:
            return _oauth_result_page(False, f"Échec de l'échange avec Google : {e}")

        try:
            store = ni.SupabaseTokenStore()
            store.save(user_id, tokens)
        except Exception as e:
            return _oauth_result_page(False, f"Gmail autorisé mais échec de sauvegarde du token "
                                              f"(vérifie SUPABASE_URL / SUPABASE_SERVICE_KEY sur le serveur) : {e}")

        return _oauth_result_page(True, "Gmail connecté avec succès. Tu peux fermer cet onglet et retourner sur la console NEXUS.")

    def _oauth_result_page(success, message):
        """Petite page HTML autonome — le callback OAuth est ouvert dans
        l'onglet du navigateur, pas appelé via fetch(), donc une réponse
        JSON brute serait peu lisible pour l'utilisateur."""
        color = '#38D9A9' if success else '#FF5D5D'
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>body{{background:#0B0F1A;color:#E4E9F5;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:20px;text-align:center}}
p{{border-left:3px solid {color};padding-left:12px;max-width:420px}}</style></head>
<body><p>{message}</p></body></html>"""
        return Response(html, mimetype='text/html')

    @app.route('/api/v1/gmail/sync', methods=['POST'])
    def gmail_sync():
        """Authentifié normalement (X-API-Key) puisqu'appelé via fetch()
        depuis la console, pas par navigation directe."""
        user_id, err, code = require_auth()
        if err: return err, code

        try:
            import nexus_integrations as ni
            import nexus_pipeline as npl
        except ImportError as e:
            return jsonify({'status': 'error', 'message': f'modules manquants: {e}'}), 500

        try:
            store = ni.SupabaseTokenStore()
            access_token = ni.get_valid_access_token(user_id, store=store)
        except LookupError:
            return jsonify({'status': 'error', 'message': 'gmail_not_connected',
                             'auth_url': f'/api/v1/auth/gmail/start?user_id={user_id}&api_key={request.headers.get("X-API-Key")}'}), 409
        except Exception as e:
            return jsonify({'status': 'error',
                             'message': f'échec de rafraîchissement du token '
                                        f'(vérifie SUPABASE_URL / SUPABASE_SERVICE_KEY) : {e}'}), 502

        raw_client = ni.GmailClient(access_token)
        bus = npl.EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))
        connector = npl.GmailConnector(bus, raw_client)
        parser = npl.EmailParser(bus)
        pipeline = npl.IngestionPipeline(connector, parser, bus)

        nexus = get_nexus(user_id)
        try:
            metrics = pipeline.run(nexus, max_results=25)
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'échec de synchronisation : {e}'}), 502

        store.update_last_sync(user_id)
        return jsonify({
            'status': 'success',
            'metrics': {
                'emails_fetched': metrics.emails_fetched,
                'ignored_noise': metrics.ignored_noise,
                'signals_created': metrics.signals_created,
                'deadlines_detected': metrics.deadlines_detected,
                'predictions_generated': metrics.predictions_generated,
                'actions_pending': metrics.actions_pending,
                'actions_by_priority': metrics.actions_by_priority,
            },
            'events': events,
        })

    @app.route('/api/v1/gmail/debug', methods=['GET'])
    def gmail_debug():
        """
        Diagnostic en LECTURE SEULE : ne touche jamais à la base NEXUS
        (aucun ingest, aucun cycle). Montre, pour chaque email non lu
        récupéré, le sujet exact renvoyé par Gmail, sa date interne, s'il
        est classé comme bruit, et si une deadline en a été extraite —
        pour diagnostiquer sans deviner plutôt que d'empiler des hypothèses.
        """
        user_id, err, code = require_auth()
        if err: return err, code

        try:
            import nexus_integrations as ni
            import nexus_pipeline as npl
        except ImportError as e:
            return jsonify({'status': 'error', 'message': f'modules manquants: {e}'}), 500

        try:
            store = ni.SupabaseTokenStore()
            access_token = ni.get_valid_access_token(user_id, store=store)
        except LookupError:
            return jsonify({'status': 'error', 'message': 'gmail_not_connected'}), 409
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'échec de rafraîchissement du token : {e}'}), 502

        max_results = min(int(request.args.get('max_results', 50)), 100)
        raw_client = ni.GmailClient(access_token)
        try:
            raw_signals = raw_client.fetch_unread(max_results=max_results)
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'échec de récupération Gmail : {e}'}), 502

        extractor = npl.DeadlineExtractor()
        details = []
        for sig in raw_signals:
            content = sig['content']
            sender = sig['metadata'].get('from', '')
            noise = npl.is_noise(sender, content)
            deadline = None if noise else extractor.extract(content)
            internal_date = sig['metadata'].get('gmail_internal_date')
            details.append({
                'gmail_message_id': sig['metadata'].get('gmail_message_id'),
                'from': sender,
                'content_preview': content[:200],
                'gmail_internal_date_ms': internal_date,
                'gmail_internal_date_readable': (
                    datetime.fromtimestamp(int(internal_date) / 1000).isoformat()
                    if internal_date else None
                ),
                'classified_as_noise': noise,
                'deadline_extracted': deadline,
            })

        return jsonify({
            'status': 'success',
            'total_fetched': len(details),
            'max_results_requested': max_results,
            'messages': details,
        })

    @app.route('/api/v1/health', methods=['GET'])
    def health_check():
        return jsonify({
            'status': 'healthy', 'service': 'NEXUS API v2.0', 'version': '2.0.0',
            'backup_enabled': backup_enabled()
        })

    @app.route('/api/v1/backup/now', methods=['POST'])
    def trigger_backup():
        # Volontairement protégé par une clé API valide (n'importe laquelle) plutôt
        # que laissé ouvert : évite qu'un tiers déclenche des sauvegardes à volonté.
        user_id, err, code = require_auth()
        if err: return err, code
        if not backup_enabled():
            return jsonify({'status': 'error', 'message': 'sauvegarde désactivée (GITHUB_TOKEN / GITHUB_GIST_ID absents)'}), 400
        ok = backup_db_to_gist()
        return jsonify({'status': 'success' if ok else 'error', 'backed_up': ok})

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'status': 'error', 'message': 'endpoint inconnu'}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({'status': 'error', 'message': 'erreur interne'}), 500


# ============================================================
# DÉMONSTRATION
# ============================================================

def run_demo():
    print("=" * 60)
    print("NEXUS v2.0 — Démonstration")
    print("=" * 60)

    demo_db = NexusDatabase()
    user_id = "demo_user"
    api_key = demo_db.create_user(user_id, json.dumps({'communication_style':'professional','learned_preferences':{},'stress_triggers':[]}))
    if api_key is None:
        print(f"Utilisateur '{user_id}' déjà enregistré (base existante) — clé API non ré-affichable, "
              f"supprimez nexus.db pour repartir de zéro et voir une nouvelle clé.")
    else:
        print(f"API Key: {api_key}")

    nexus = NexusSystem(user_id, demo_db)

    nexus.ingest("email", "URGENT: Présentation demain 14h", {'deadline':(datetime.now()+timedelta(hours=14)).isoformat()})
    nexus.ingest("messaging", "Hey réponds-moi !", {'from':'Marie','responded':False})
    nexus.ingest("messaging", "Toujours pas de réponse...", {'from':'Marie','responded':False})
    nexus.ingest("messaging", "Bon ok je choisis seul", {'from':'Marie','responded':False})
    nexus.ingest("browsing", "youtube.com stress management", {})
    nexus.ingest("browsing", "reddit.com/r/stress burnout", {})

    result = nexus.run_cycle()
    print(f"\nPrédictions: {result['predictions']}")
    print(f"Actions en attente: {result['pending']}")

    dash = nexus.get_dashboard()
    print(f"\nContexte: {dash['context']}")
    for a in dash['pending_actions']:
        print(f"  [{a['priority']}] {a['type']}: {a['description'][:50]}...")

    # Démonstration du comportement sur un ID d'action inexistant (non-régression du fix)
    fake_result = nexus.approve("id-inexistant")
    print(f"\nApprove sur ID inexistant (doit être 'not_found'): {fake_result}")


if __name__ == "__main__":
    import sys
    if "--serve" in sys.argv:
        if not FLASK_AVAILABLE:
            print("Flask n'est pas installé. Lancez : pip install flask flask-cors")
            sys.exit(1)
        print("NEXUS API v2.0 démarrée sur http://0.0.0.0:5000")
        print("  POST /api/v1/register   {\"user_id\": \"...\"} -> récupère une clé API")
        print("  Puis header X-API-Key sur tous les autres endpoints.")
        app.run(host='0.0.0.0', port=5000, debug=False)
    else:
        run_demo()
        print("\n(Pour lancer le serveur API à la place : python nexus_v2.py --serve)")
