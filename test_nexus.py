"""
Suite de tests pour NEXUS v2.0.

Lancement :
    pip install pytest --break-system-packages
    pytest test_nexus.py -v

Le fichier nexus_v2.py doit être dans le même dossier (ou sur le PYTHONPATH).
"""
import importlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nexus_v2 as nx


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_nexus.db")


@pytest.fixture
def db(db_path):
    """Une base SQLite neuve et isolée par test."""
    return nx.NexusDatabase(db_path)


@pytest.fixture
def nexus(db):
    """Un NexusSystem sur un utilisateur de test, base isolée."""
    return nx.NexusSystem("test_user", db)


@pytest.fixture
def client(db_path):
    """
    Client de test Flask, connecté à une base isolée par test, avec les
    instances NexusSystem et les rate limiters réinitialisés pour éviter
    toute fuite d'état entre tests.
    """
    if not nx.FLASK_AVAILABLE:
        pytest.skip("Flask n'est pas installé")

    nx.db = nx.NexusDatabase(db_path)
    nx.nexus_instances.clear()
    nx.user_limiter = nx.RateLimiter(max_requests=60, window_seconds=60)
    nx.register_limiter = nx.RateLimiter(max_requests=5, window_seconds=3600)
    nx.auth_fail_limiter = nx.RateLimiter(max_requests=10, window_seconds=60)
    nx.app.testing = True
    return nx.app.test_client()


def register(client, user_id="alice"):
    r = client.post("/api/v1/register", json={"user_id": user_id})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["api_key"]


# ------------------------------------------------------------------
# Couche base de données
# ------------------------------------------------------------------

class TestDatabase:

    def test_create_user_returns_usable_key(self, db):
        api_key = db.create_user("bob", json.dumps({}))
        assert isinstance(api_key, str) and len(api_key) > 20
        assert db.verify_api_key(api_key) == "bob"

    def test_api_key_never_stored_in_plaintext(self, db_path, db):
        api_key = db.create_user("bob", json.dumps({}))
        import sqlite3
        conn = sqlite3.connect(db_path)
        stored = conn.execute("SELECT api_key FROM users WHERE user_id='bob'").fetchone()[0]
        conn.close()
        assert stored != api_key
        assert len(stored) == 64  # hash SHA-256 en hexadécimal

    def test_duplicate_user_returns_none(self, db):
        db.create_user("bob", json.dumps({}))
        assert db.create_user("bob", json.dumps({})) is None

    def test_verify_wrong_key_returns_none(self, db):
        db.create_user("bob", json.dumps({}))
        assert db.verify_api_key("clé-inventée") is None

    def test_get_action_unknown_returns_none(self, db):
        assert db.get_action("bob", "id-inexistant") is None

    def test_get_action_wrong_user_returns_none(self, db, nexus):
        # Une action créée pour test_user ne doit pas être visible pour un autre user_id.
        nexus.ingest("email", "URGENT: deadline demain",
                     {"deadline": (datetime.now() + timedelta(hours=5)).isoformat()})
        nexus.run_cycle()
        pending = nexus.action_engine.get_pending_actions()
        assert pending, "le scénario doit produire au moins une action en attente"
        action_id = pending[0].action_id
        assert db.get_action("un_autre_user", action_id) is None
        assert db.get_action("test_user", action_id) == "pending"


# ------------------------------------------------------------------
# ContextEngine : extraction d'urgence et d'émotion
# ------------------------------------------------------------------

class TestContextEngine:

    def test_urgency_high_keyword(self, nexus):
        s = nx.ContextSignal(source="email", content="C'est urgent, réponds ASAP",
                              timestamp=datetime.now(), metadata={})
        score = nexus.context_engine._extract_urgency(s)
        assert score > 0.5

    def test_urgency_low_keyword_reduces_score(self, nexus):
        s = nx.ContextSignal(source="email", content="pas pressé, quand tu peux",
                              timestamp=datetime.now(), metadata={})
        score = nexus.context_engine._extract_urgency(s)
        assert score == 0.0  # clampé à 0, jamais négatif

    def test_urgency_near_deadline_increases_score(self, nexus):
        near = nx.ContextSignal(source="email", content="Rapport",
                                 timestamp=datetime.now(),
                                 metadata={"deadline": (datetime.now() + timedelta(hours=3)).isoformat()})
        far = nx.ContextSignal(source="email", content="Rapport",
                                timestamp=datetime.now(),
                                metadata={"deadline": (datetime.now() + timedelta(days=20)).isoformat()})
        assert nexus.context_engine._extract_urgency(near) > nexus.context_engine._extract_urgency(far)

    def test_emotion_stressed_detected(self, nexus):
        s = nx.ContextSignal(source="messaging", content="je suis débordé, c'est le stress total",
                              timestamp=datetime.now(), metadata={})
        assert nexus.context_engine._extract_emotion(s) == "stressed"

    def test_emotion_neutral_default(self, nexus):
        s = nx.ContextSignal(source="messaging", content="on se voit à 15h",
                              timestamp=datetime.now(), metadata={})
        assert nexus.context_engine._extract_emotion(s) == "neutral"

    def test_ingest_persists_signal(self, nexus, db):
        nexus.ingest("email", "test message", {})
        signals = db.get_signals("test_user", hours=24)
        assert len(signals) == 1
        assert signals[0].content == "test message"


# ------------------------------------------------------------------
# PredictionEngine + ActionEngine : bout en bout
# ------------------------------------------------------------------

class TestPredictionAndAction:

    def test_deadline_signal_produces_pending_action(self, nexus):
        nexus.ingest("email", "URGENT: présentation demain",
                     {"deadline": (datetime.now() + timedelta(hours=3)).isoformat()})
        result = nexus.run_cycle()
        assert result["predictions"] >= 1
        assert result["pending"] >= 1

    def test_no_signal_no_prediction(self, nexus):
        result = nexus.run_cycle()
        assert result["predictions"] == 0
        assert result["pending"] == 0

    def test_approve_existing_pending_action(self, nexus):
        nexus.ingest("email", "URGENT: deadline demain",
                     {"deadline": (datetime.now() + timedelta(hours=4)).isoformat()})
        nexus.run_cycle()
        pending = nexus.action_engine.get_pending_actions()
        assert pending
        result = nexus.approve(pending[0].action_id)
        assert result["status"] == "executed"

    def test_approve_unknown_action_returns_not_found(self, nexus):
        result = nexus.approve("id-qui-nexiste-pas")
        assert result["status"] == "not_found"

    def test_reject_unknown_action_returns_not_found(self, nexus):
        result = nexus.reject("id-qui-nexiste-pas")
        assert result["status"] == "not_found"

    def test_cannot_approve_already_rejected_action(self, nexus):
        nexus.ingest("email", "URGENT: deadline demain",
                     {"deadline": (datetime.now() + timedelta(hours=4)).isoformat()})
        nexus.run_cycle()
        pending = nexus.action_engine.get_pending_actions()
        assert pending
        action_id = pending[0].action_id
        reject_result = nexus.reject(action_id)
        assert reject_result["status"] == "rejected"
        # tentative d'approuver une action déjà rejetée -> doit échouer proprement
        approve_result = nexus.approve(action_id)
        assert approve_result["status"] == "error"

    def test_communication_pattern_after_unanswered_messages(self, nexus):
        # Le prédicteur ne se déclenche que si le dernier message non répondu
        # date de plus de 6h (sinon on laisse le temps de répondre normalement).
        old = datetime.now() - timedelta(hours=8)
        for i, msg in enumerate(["Hey réponds-moi !", "Toujours rien ?", "Bon je fais sans toi"]):
            nexus.ingest("messaging", msg, {"from": "Marie", "responded": False},
                         timestamp=old + timedelta(minutes=i))
        result = nexus.run_cycle()
        assert result["predictions"] >= 1


# ------------------------------------------------------------------
# API REST : auth, validation, flux complet, rate limiting
# ------------------------------------------------------------------

class TestAPI:

    def test_health_check_no_auth_needed(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.get_json()["status"] == "healthy"

    def test_register_returns_api_key(self, client):
        r = client.post("/api/v1/register", json={"user_id": "alice"})
        assert r.status_code == 201
        assert len(r.get_json()["api_key"]) > 20

    def test_register_missing_user_id(self, client):
        r = client.post("/api/v1/register", json={})
        assert r.status_code == 400

    def test_register_duplicate_returns_409(self, client):
        register(client, "alice")
        r = client.post("/api/v1/register", json={"user_id": "alice"})
        assert r.status_code == 409

    def test_ingest_without_auth_401(self, client):
        r = client.post("/api/v1/ingest", json={"source": "email", "content": "test"})
        assert r.status_code == 401

    def test_ingest_with_wrong_key_401(self, client):
        register(client, "alice")
        r = client.post("/api/v1/ingest", json={"source": "email", "content": "test"},
                         headers={"X-API-Key": "fausse-clé"})
        assert r.status_code == 401

    def test_ingest_missing_content_400(self, client):
        key = register(client, "alice")
        r = client.post("/api/v1/ingest", json={"source": "email"},
                         headers={"X-API-Key": key})
        assert r.status_code == 400

    def test_ingest_bad_metadata_type_400(self, client):
        key = register(client, "alice")
        r = client.post("/api/v1/ingest",
                         json={"source": "email", "content": "test", "metadata": "pas-un-objet"},
                         headers={"X-API-Key": key})
        assert r.status_code == 400

    def test_full_flow_register_ingest_cycle_approve(self, client):
        key = register(client, "alice")
        headers = {"X-API-Key": key}

        r = client.post("/api/v1/ingest", json={
            "source": "email", "content": "URGENT: deadline demain",
            "metadata": {"deadline": (datetime.now() + timedelta(hours=5)).isoformat()}
        }, headers=headers)
        assert r.status_code == 200

        r = client.post("/api/v1/cycle", json={}, headers=headers)
        assert r.status_code == 200
        assert r.get_json()["cycle_result"]["pending"] >= 1

        r = client.get("/api/v1/dashboard", headers=headers)
        assert r.status_code == 200
        pending = r.get_json()["dashboard"]["pending_actions"]
        assert pending

        action_id = pending[0]["id"]
        r = client.post(f"/api/v1/actions/{action_id}/approve", json={}, headers=headers)
        assert r.status_code == 200
        assert r.get_json()["result"]["status"] == "executed"

        r = client.get("/api/v1/transparency", headers=headers)
        assert r.status_code == 200
        assert len(r.get_json()["report"]["decisions"]) >= 1

    def test_approve_unknown_action_404(self, client):
        key = register(client, "alice")
        r = client.post("/api/v1/actions/id-inexistant/approve", json={},
                         headers={"X-API-Key": key})
        assert r.status_code == 404

    def test_user_cannot_approve_another_users_action(self, client):
        key_alice = register(client, "alice")
        key_bob = register(client, "bob")

        client.post("/api/v1/ingest", json={
            "source": "email", "content": "URGENT: deadline demain",
            "metadata": {"deadline": (datetime.now() + timedelta(hours=5)).isoformat()}
        }, headers={"X-API-Key": key_alice})
        client.post("/api/v1/cycle", json={}, headers={"X-API-Key": key_alice})
        dash = client.get("/api/v1/dashboard", headers={"X-API-Key": key_alice}).get_json()
        action_id = dash["dashboard"]["pending_actions"][0]["id"]

        # bob tente d'approuver l'action d'alice -> doit échouer (not_found, pas une fuite d'info)
        r = client.post(f"/api/v1/actions/{action_id}/approve", json={},
                         headers={"X-API-Key": key_bob})
        assert r.status_code == 404

    def test_register_rate_limit(self, client):
        nx.register_limiter = nx.RateLimiter(max_requests=3, window_seconds=3600)
        codes = [client.post("/api/v1/register", json={"user_id": f"u{i}"}).status_code
                 for i in range(5)]
        assert codes == [201, 201, 201, 429, 429]

    def test_user_endpoint_rate_limit(self, client):
        key = register(client, "alice")
        nx.user_limiter = nx.RateLimiter(max_requests=3, window_seconds=60)
        codes = [client.get("/api/v1/dashboard", headers={"X-API-Key": key}).status_code
                 for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]

    def test_auth_brute_force_rate_limit(self, client):
        register(client, "alice")
        nx.auth_fail_limiter = nx.RateLimiter(max_requests=3, window_seconds=60)
        codes = [client.get("/api/v1/dashboard", headers={"X-API-Key": "mauvaise"}).status_code
                 for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]

    def test_rate_limit_response_has_retry_after_header(self, client):
        nx.register_limiter = nx.RateLimiter(max_requests=1, window_seconds=3600)
        client.post("/api/v1/register", json={"user_id": "a"})
        r = client.post("/api/v1/register", json={"user_id": "b"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ------------------------------------------------------------------
# RateLimiter isolé (sans Flask)
# ------------------------------------------------------------------

class TestRateLimiter:

    def test_allows_up_to_max_requests(self):
        rl = nx.RateLimiter(max_requests=3, window_seconds=60)
        results = [rl.check("k")[0] for _ in range(4)]
        assert results == [True, True, True, False]

    def test_different_keys_independent(self):
        rl = nx.RateLimiter(max_requests=1, window_seconds=60)
        assert rl.check("a")[0] is True
        assert rl.check("b")[0] is True  # clé différente, budget séparé
        assert rl.check("a")[0] is False

    def test_window_expiry_frees_budget(self):
        rl = nx.RateLimiter(max_requests=1, window_seconds=0.2)
        assert rl.check("k")[0] is True
        assert rl.check("k")[0] is False
        time.sleep(0.25)
        assert rl.check("k")[0] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
