"""
Pipeline d'ingestion Gmail pour NEXUS.

Architecture décidée collectivement :
- Connecteur CONCRET (pas de SourceConnector abstrait) — Rule of Three,
  on n'abstrait qu'au deuxième connecteur réel (Calendar, pas avant).
- EventBus minimal : découple la collecte de l'affichage (terminal
  aujourd'hui, WebSocket/console web demain), sans dépendance circulaire.
- Instrumentation à CHAQUE étape (fetch / parse / engine / decision),
  pour diagnostiquer sans deviner quand un cycle ne produit rien.

Ce module ne connaît PAS NexusSystem par import direct au niveau module
(seulement au moment de l'appel), pour rester testable sans base de données.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from nexus_nlp_extractor import DeadlineExtractor


# ------------------------------------------------------------------
# EventBus — découple la collecte de l'affichage
# ------------------------------------------------------------------

class EventBus:
    def __init__(self):
        self._subscribers = []
        self.history = []  # conservé pour les tests et le débogage

    def subscribe(self, fn: Callable):
        self._subscribers.append(fn)

    def publish(self, event_type, data=None):
        event = {"type": event_type, "data": data or {}, "timestamp": datetime.now().isoformat()}
        self.history.append(event)
        for fn in self._subscribers:
            fn(event)


def terminal_logger(event):
    print(f"[{event['timestamp'][11:19]}] {event['type']} — {event['data']}")


# ------------------------------------------------------------------
# Filtrage du bruit (newsletters, notifications automatiques)
# ------------------------------------------------------------------

_NOISE_MARKERS = ["no-reply", "noreply", "unsubscribe", "newsletter", "notifications@"]


def is_noise(sender, subject):
    blob = f"{sender} {subject}".lower()
    return any(marker in blob for marker in _NOISE_MARKERS)


# ------------------------------------------------------------------
# Connecteur Gmail concret
# ------------------------------------------------------------------

class GmailConnector:
    """
    Enveloppe autour de nexus_integrations.GmailClient, avec émission
    d'événements de progression. Séparé de nexus_integrations.py pour que
    ce module reste testable avec un simple objet "raw_client" simulé,
    sans dépendre du vrai HTTP.
    """
    def __init__(self, event_bus: EventBus, raw_client):
        self.event_bus = event_bus
        self.raw_client = raw_client  # doit exposer .fetch_unread(max_results)

    def fetch_raw(self, max_results=10):
        self.event_bus.publish("fetch_start", {})
        started = datetime.now()
        try:
            raw_emails = self.raw_client.fetch_unread(max_results=max_results)
        except Exception as e:
            self.event_bus.publish("fetch_error", {"error": str(e)})
            raise
        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        self.event_bus.publish("fetch_complete", {"count": len(raw_emails), "duration_ms": duration_ms})
        return raw_emails


# ------------------------------------------------------------------
# Parsing : RawEmail -> ContextSignal-compatible dict, avec extraction NLP
# ------------------------------------------------------------------

class EmailParser:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.extractor = DeadlineExtractor()

    def parse(self, raw_emails):
        self.event_bus.publish("parse_start", {"count": len(raw_emails)})
        signals = []
        ignored_noise = 0
        deadlines_found = 0

        for raw in raw_emails:
            metadata = raw.get("metadata", {})
            sender = metadata.get("from", "expéditeur inconnu")
            content = raw.get("content", "")
            # Le sujet est encodé dans `content` par GmailClient ("Objet: ...");
            # pour le filtrage anti-bruit on regarde tout le contenu, plus robuste
            # que d'extraire le sujet seul.
            if is_noise(sender, content):
                ignored_noise += 1
                continue

            deadline = self.extractor.extract(content)
            sig_metadata = dict(metadata)
            if deadline:
                sig_metadata["deadline"] = deadline
                deadlines_found += 1

            signals.append({
                "source": raw.get("source", "email"),
                "content": content,
                "metadata": sig_metadata,
            })

        self.event_bus.publish("parse_complete", {
            "total_received": len(raw_emails),
            "ignored_noise": ignored_noise,
            "signals_created": len(signals),
            "deadlines_detected": deadlines_found,
        })
        return signals


# ------------------------------------------------------------------
# Pipeline complet — instrumenté à chaque étape
# ------------------------------------------------------------------

@dataclass
class PipelineMetrics:
    emails_fetched: int = 0
    fetch_duration_ms: int = 0
    ignored_noise: int = 0
    signals_created: int = 0
    deadlines_detected: int = 0
    predictions_generated: int = 0
    actions_pending: int = 0
    actions_by_priority: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


class IngestionPipeline:
    def __init__(self, connector: GmailConnector, parser: EmailParser, event_bus: EventBus):
        self.connector = connector
        self.parser = parser
        self.event_bus = event_bus

    def run(self, nexus_system, max_results=10):
        """
        Exécute fetch -> parse -> ingest -> cycle, avec métriques à chaque
        étape. nexus_system est injecté (pas importé au niveau module) pour
        que ce pipeline reste testable indépendamment de NexusSystem.
        """
        self.event_bus.publish("pipeline_start", {})
        metrics = PipelineMetrics()

        try:
            raw_emails = self.connector.fetch_raw(max_results=max_results)
        except Exception as e:
            metrics.errors.append(f"fetch: {e}")
            self.event_bus.publish("pipeline_end", {"status": "error", "stage": "fetch"})
            return metrics

        metrics.emails_fetched = len(raw_emails)

        signals = self.parser.parse(raw_emails)
        metrics.ignored_noise = len(raw_emails) - len(signals)
        metrics.signals_created = len(signals)
        metrics.deadlines_detected = sum(1 for s in signals if s["metadata"].get("deadline"))

        self.event_bus.publish("engine_start", {"signals_to_ingest": len(signals)})
        for sig in signals:
            nexus_system.ingest(sig["source"], sig["content"], sig["metadata"])

        result = nexus_system.run_cycle()
        metrics.predictions_generated = result["predictions"]
        metrics.actions_pending = result["pending"]

        pending = nexus_system.action_engine.get_pending_actions()
        for a in pending:
            key = a.priority.name
            metrics.actions_by_priority[key] = metrics.actions_by_priority.get(key, 0) + 1

        self.event_bus.publish("engine_complete", {
            "predictions": metrics.predictions_generated,
            "pending": metrics.actions_pending,
            "by_priority": metrics.actions_by_priority,
        })
        self.event_bus.publish("pipeline_end", {"status": "success"})
        return metrics
