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
        if signal.metadata.get('deadline'):
            try:
                dl = datetime.fromisoformat(signal.metadata['deadline'])
                hu = (dl - signal.timestamp).total_seconds() / 3600
                if hu < 24: score += 0.5
                elif hu < 72: score += 0.3
                elif hu < 168: score += 0.1
            except: pass
        return min(1.0, max(0.0, score))

    def _extract_emotion(self, signal):
        cl = signal.content.lower()
        em = {'stressed': ['stress','débordé','overwhelmed','panique','anxieux','urgent'],
              'frustrated': ['énervé','frustré','inacceptable','ridicule','problème'],
              'excited': ['super','génial','excellent','opportunité','gagnant'],
              'sad': ['désolé','triste','difficile','perdu','abandonné']}
        sc = {k: sum(1 for w in v if w in cl) for k, v in em.items()}
        if max(sc.values()) == 0: return 'neutral'
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
                if m.get('deadline') and datetime.fromisoformat(m['deadline']) > datetime.now(): n += 1
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

class PredictionEngine:
    def __init__(self, context_engine):
        self.context = context_engine
        self.prediction_history = []
        self.accuracy_log = []

    def generate_predictions(self, horizon_hours=24):
        p = []
        p.extend(self._predict_deadline_actions())
        p.extend(self._predict_communication_needs())
        p.extend(self._predict_wellbeing_actions())
        p.extend(self._predict_opportunity_actions())
        p.extend(self._predict_habit_based_actions())
        return self._filter_and_score(p)

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
                dl = datetime.fromisoformat(m['deadline'])
                hu = (dl - now).total_seconds() / 3600
                s = ContextSignal(source=x[0],content=x[1],timestamp=datetime.fromisoformat(x[2]),metadata=m,emotional_tone=x[4],urgency_indicator=x[5])
                if 0 < hu < 4:
                    a = NexusAction(action_id=self._gen_id(),action_type=ActionType.REMINDER_SET,description=f"URGENT: Deadline dans {int(hu)}h — {s.content[:80]}...",target_context={'deadline':m['deadline']},confidence=0.95,priority=Priority.CRITICAL,reasoning_chain=[f"Deadline: {m['deadline']}",f"Temps restant: {int(hu)}h","Action: Rappel immédiat"])
                    p.append(PredictedNeed(self._gen_id(),f"Gestion urgente: {s.content[:60]}",now,timedelta(hours=1),0.95,[s.source],a))
                elif 4 <= hu < 24:
                    a = NexusAction(action_id=self._gen_id(),action_type=ActionType.TASK_CREATE,description=f"Planifier: {s.content[:80]}... (deadline dans {int(hu)}h)",target_context={'deadline':m['deadline']},confidence=0.85,priority=Priority.HIGH,reasoning_chain=[f"Deadline dans {int(hu)}h","Action: Création de tâche"])
                    p.append(PredictedNeed(self._gen_id(),f"Planification: {s.content[:60]}",now,timedelta(hours=4),0.85,[s.source],a))
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
                    a = NexusAction(action_id=self._gen_id(),action_type=ActionType.EMAIL_DRAFT,description=f"Brouillon réponse à {sender} ({len(signals)} messages non traités)",target_context={'sender':sender,'count':len(signals)},confidence=0.75,priority=Priority.HIGH,reasoning_chain=[f"{len(signals)} messages de {sender}",f"Dernier il y a {int(hs)}h","Action: Brouillon de réponse"])
                    p.append(PredictedNeed(self._gen_id(),f"Réponse attendue par {sender}",now,timedelta(hours=2),0.75,[s.source for s in signals],a))
        return p

    def _predict_wellbeing_actions(self):
        p = []; now = datetime.now()
        recent = self.context.get_recent_signals(hours=6)
        stress = [s for s in recent if s.emotional_tone == 'stressed']
        if len(stress) >= 2:
            a = NexusAction(action_id=self._gen_id(),action_type=ActionType.FOCUS_BLOCK,description="Bloc de focus recommandé — stress élevé détecté",target_context={'stress_count':len(stress)},confidence=0.80,priority=Priority.HIGH,reasoning_chain=[f"{len(stress)} signaux de stress","Charge cognitive élevée","Action: Bloc de focus 25min"])
            p.append(PredictedNeed(self._gen_id(),"Gestion du stress — pause",now,timedelta(minutes=30),0.80,[s.source for s in stress],a))
        if len(recent) > 30:
            a = NexusAction(action_id=self._gen_id(),action_type=ActionType.INFO_DIGEST,description=f"Digest d'information — {len(recent)} signaux en 6h",target_context={'count':len(recent)},confidence=0.70,priority=Priority.MEDIUM,reasoning_chain=[f"{len(recent)} signaux en 6h","Surcharge informationnelle","Action: Synthèse auto"],auto_executable=True,requires_approval=False)
            p.append(PredictedNeed(self._gen_id(),"Digest — surcharge",now,timedelta(minutes=15),0.70,[s.source for s in recent[:5]],a))
        return p

    def _predict_opportunity_actions(self):
        p = []; now = datetime.now()
        kw = ['opportunité','opportunity','offre','offer','promotion','invitation','partenariat','partnership','collaboration','nouveau projet','new project']
        for s in self.context.get_recent_signals(hours=48):
            if any(w in s.content.lower() for w in kw) and s.urgency_indicator and s.urgency_indicator > 0.3:
                a = NexusAction(action_id=self._gen_id(),action_type=ActionType.DECISION_PROMPT,description=f"Opportunité: {s.content[:100]}...",target_context={'content':s.content[:200]},confidence=0.65,priority=Priority.MEDIUM,reasoning_chain=["Mot-clé opportunité",f"Urgence: {s.urgency_indicator}","Action: Prompt décision"])
                p.append(PredictedNeed(self._gen_id(),f"Opportunité: {s.content[:60]}",now,timedelta(hours=12),0.65,[s.source],a))
        return p

    def _predict_habit_based_actions(self):
        p = []; now = datetime.now(); ch = now.hour
        ph = self.context.profile.learned_preferences.get('productivity_hours',[9,10,14,15])
        if ch in ph:
            b = [s for s in self.context.get_recent_signals(hours=1) if s.source == 'browsing' and any(w in s.content.lower() for w in ['youtube','reddit','twitter','instagram','facebook'])]
            if len(b) > 5:
                a = NexusAction(action_id=self._gen_id(),action_type=ActionType.FOCUS_BLOCK,description="Heure de productivité — redirection suggérée",target_context={'hour':ch,'distractions':len(b)},confidence=0.60,priority=Priority.LOW,reasoning_chain=[f"Heure productive: {ch}h",f"{len(b)} distractions","Action: Focus block"])
                p.append(PredictedNeed(self._gen_id(),"Redirection productivité",now,timedelta(minutes=15),0.60,[s.source for s in b],a))
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
# MOTEUR D'ACTION
# ============================================================

class ActionEngine:
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
        return {'status':'executed','action_id':action.action_id}

    def approve_action(self, action_id):
        for a in self.pending_approvals:
            if a.action_id == action_id:
                self.pending_approvals.remove(a); return self._execute(a)
        # Pas en mémoire (ex: après redémarrage) : vérifier que l'action existe
        # vraiment en base, appartient à cet utilisateur, et est encore en attente,
        # avant de la marquer comme exécutée.
        status = self.db.get_action(self.context.user_id, action_id)
        if status is None:
            return {'status': 'not_found', 'action_id': action_id}
        if status != 'pending':
            return {'status': 'error', 'message': f"action déjà '{status}', ne peut pas être approuvée", 'action_id': action_id}
        self.db.update_action_status(action_id, "executed", datetime.now())
        return {'status': 'executed', 'action_id': action_id}

    def reject_action(self, action_id, reason=""):
        for a in self.pending_approvals:
            if a.action_id == action_id:
                a.status = "rejected"; self.pending_approvals.remove(a)
                self.db.update_action_status(action_id, "rejected")
                self.predictor.accuracy_log.append({'action_id':action_id,'outcome':'rejected','reason':reason,'timestamp':datetime.now()})
                return {'status':'rejected'}
        status = self.db.get_action(self.context.user_id, action_id)
        if status is None:
            return {'status': 'not_found', 'action_id': action_id}
        if status != 'pending':
            return {'status': 'error', 'message': f"action déjà '{status}', ne peut pas être rejetée", 'action_id': action_id}
        self.db.update_action_status(action_id, "rejected")
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
    from flask import Flask, request, jsonify
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
