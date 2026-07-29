import re
from datetime import datetime, timedelta

# 'demain', 'aujourd'hui', 'dans X heures/jours' ne peuvent JAMAIS
# grammaticalement désigner le passé en français — ces motifs restent
# fiables tels quels. Seuls les noms de jours ('lundi', 'vendredi'...) sont
# ambigus entre passé et futur, d'où un garde-fou ciblé uniquement sur eux.
_PAST_TENSE_MARKERS = [
    r'\bavait\b', r'\bavais\b', r'\bétait\b', r"\bc'était\b", r'\bdernier\b',
    r'\bdernière\b', r'\bpassé\b', r'\bpassée\b', r'\bmerci pour\b',
    r'\bpas de réponse\b', r"\bje t'ai\b", r'\bon avait\b',
]


def _looks_like_past(text_lower):
    return any(re.search(m, text_lower) for m in _PAST_TENSE_MARKERS)


class DeadlineExtractor:
    def __init__(self):
        self.days_map = {
            'lundi': 0, 'mardi': 1, 'mercredi': 2, 'jeudi': 3,
            'vendredi': 4, 'samedi': 5, 'dimanche': 6,
            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
            'friday': 4, 'saturday': 5, 'sunday': 6,
        }
        self.patterns = [
            (r'demain\s*[àa]?\s*(\d{1,2})[hH](\d{2})?', 1),
            (r'(aujourd\'hui|ce soir)\s*[àa]?\s*(\d{1,2})[hH](\d{2})?', 0),
            (r'(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*[àa]?\s*(\d{1,2})[hH](\d{2})?', None),
            (r'dans\s+(\d+)\s+(heure|jour)s?', None),
        ]

    def extract(self, text, current_time=None):
        if not text:
            return None
        # Normalise l'apostrophe typographique (’, U+2019) vers l'apostrophe
        # droite (') — très fréquente dans les emails réels (iPhone Mail,
        # Word, certains clients web) et invisible à l'œil, mais elle casse
        # silencieusement tout match sur "aujourd'hui" sans normalisation.
        text_lower = text.lower().replace('\u2019', "'")
        now = current_time or datetime.now()
        for pattern, offset_type in self.patterns:
            match = re.search(pattern, text_lower)
            if not match:
                continue
            # Garde-fou anti-faux-positif : seuls les noms de jours sont
            # grammaticalement ambigus entre passé et futur ("lundi" peut
            # désigner lundi dernier ou lundi prochain). 'demain', 'ce soir',
            # 'dans X heures' ne peuvent jamais désigner le passé, donc ils
            # ne passent pas par ce filtre.
            if offset_type is None and match.group(1) in self.days_map and _looks_like_past(text_lower):
                continue
            return self._parse_match(match, now, offset_type)
        return None

    def _parse_match(self, match, now, offset_type):
        if offset_type == 1:
            hour = int(match.group(1))
            minute = int(match.group(2)) if match.group(2) else 0
            target_date = now + timedelta(days=1)
            return target_date.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
        elif offset_type == 0:
            hour = int(match.group(2))
            minute = int(match.group(3)) if match.group(3) else 0
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
        elif match.group(1) in self.days_map:
            day_str = match.group(1)
            hour = int(match.group(2))
            minute = int(match.group(3)) if match.group(3) else 0
            target_day = self.days_map[day_str]
            current_day = now.weekday()
            days_ahead = target_day - current_day
            if days_ahead <= 0:
                days_ahead += 7
            target_date = now + timedelta(days=days_ahead)
            return target_date.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
        elif match.group(1) and match.group(2):
            amount = int(match.group(1))
            unit = match.group(2)
            if 'heure' in unit:
                return (now + timedelta(hours=amount)).isoformat()
            elif 'jour' in unit:
                return (now + timedelta(days=amount)).isoformat()
        return None

def test_nlp_extraction():
    extractor = DeadlineExtractor()
    now = datetime(2023, 11, 1, 10, 0, 0)  # mercredi 1er nov, 10h
    tests = [
        "On peut se voir demain à 14h ?",
        "URGENT: livrable pour vendredi à 9h",
        "Ce soir à 20h pour l'apéro",
        "Dans 2 heures j'ai besoin du rapport",
    ]
    print("=== Test d'extraction NLP de Deadlines ===")
    for text in tests:
        deadline = extractor.extract(text, now)
        print(f"Texte: '{text}'")
        print(f"Deadline extraite: {deadline}\n")

if __name__ == "__main__":
    test_nlp_extraction()
