import csv
import re
import statistics
import time

from django.core.management.base import BaseCommand
from django.test.utils import override_settings

from hackathons import ai

SAMPLES = [
    ('AI for Good Hackathon', 'AI & Machine Learning'),
    ('Green City Challenge', 'GreenTech'),
    ('FinTech Innovation Sprint', 'FinTech'),
    ('Capture The Flag Night', 'Cybersecurity'),
    ('Web Dev Marathon 2026', 'Web Development'),
    ('Smart Agriculture Hack', 'GreenTech'),
    ('Chatbot Battle', 'AI & Machine Learning'),
    ('Secure Banking Apps', 'FinTech'),
    ('Open Data Tunisia', 'Web Development'),
    ('Zero Trust Defenders', 'Cybersecurity'),
]
FR = {'le', 'la', 'les', 'des', 'une', 'un', 'et', 'pour', 'de', 'du', 'avec', 'votre'}
EN = {'the', 'and', 'for', 'with', 'your', 'to', 'of', 'a', 'in', 'is'}
LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def has_markdown(text):
    return bool(re.search(r'(\*\*|`|^#|^\s*[-*] )', text, re.M))


def language_ok(text, lang):
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    fr = sum(w in FR for w in words)
    en = sum(w in EN for w in words)
    return fr > en if lang == 'fr' else en >= fr


class Command(BaseCommand):
    help = 'Benchmark de la generation de description par IA'

    @override_settings(CACHES=LOCMEM)   # n'a pas besoin de Redis
    def handle(self, *args, **options):
        rows, latencies = [], []
        for lang in ('en', 'fr'):
            for title, category in SAMPLES:
                started = time.perf_counter()
                try:
                    text, ok = ai.generate_description(title, category, lang), True
                except ai.AIServiceError:
                    text, ok = '', False
                ms = round((time.perf_counter() - started) * 1000)
                words = len(text.split())
                if ok:
                    latencies.append(ms)
                rows.append({
                    'title': title, 'lang': lang, 'success': ok,
                    'latency_ms': ms, 'words': words,
                    'length_ok': ok and words <= 70,
                    'no_markdown': ok and not has_markdown(text),
                    'language_ok': ok and language_ok(text, lang),
                    'relevance_1_5': '',
                    'text': text.replace('\n', ' '),
                })
                self.stdout.write(f'{lang} | {title[:26]:26} | {ms:5} ms | {words} mots')

        with open('ai_metrics.csv', 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        n = len(rows)
        pct = lambda key: 100 * sum(r[key] for r in rows) / n
        if latencies:
            ordered = sorted(latencies)
            p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
            self.stdout.write(self.style.SUCCESS(
                f'\n=== RESULTATS ({n} appels) ===\n'
                f'Taux de succes      : {pct("success"):.0f} %\n'
                f'Latence moyenne     : {statistics.mean(latencies):.0f} ms\n'
                f'Latence mediane     : {statistics.median(latencies):.0f} ms\n'
                f'Latence p95         : {p95} ms\n'
                f'Longueur <= 70 mots : {pct("length_ok"):.0f} %\n'
                f'Sans markdown       : {pct("no_markdown"):.0f} %\n'
                f'Bonne langue        : {pct("language_ok"):.0f} %\n'
                'Detail : ai_metrics.csv'
            ))
        else:
            self.stdout.write(self.style.ERROR('Aucun appel reussi : verifiez GEMINI_API_KEY dans .env'))