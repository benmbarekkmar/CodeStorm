import logging
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

RETRYABLE = {429, 500, 503, 504}
_client = None


class AIServiceError(Exception):
    """Erreur lisible par l'utilisateur."""


def _get_client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=12000),  # 12 s max (en ms)
        )
    return _client


def _models():
    primary = settings.GEMINI_MODEL
    fallback = getattr(settings, 'GEMINI_FALLBACK_MODEL', '')
    return [m for m in (primary, fallback) if m]


def generate_description(title, category='', language='en'):
    if not settings.GEMINI_API_KEY:
        raise AIServiceError('The AI service is not configured.')

    cache_key = f'aidesc:{language}:{category}:{title.lower()}'
    cached = cache.get(cache_key)
    if cached:
        return cached

    from google.genai import errors, types

    lang = 'French' if language == 'fr' else 'English'
    prompt = (
        f'Write an engaging description of a hackathon titled "{title}"'
        f'{f" in the category {category}" if category else ""}. '
        f'Maximum 4 lines (about 60 words), plain text, no markdown, no title. '
        f'Write in {lang}.'
    )
    fast = types.GenerateContentConfig(
        max_output_tokens=300,
        temperature=0.7,
        thinking_config=types.ThinkingConfig(thinking_budget=0),  # pas de réflexion = rapide
    )
    plain = types.GenerateContentConfig(max_output_tokens=300, temperature=0.7)

    client = _get_client()
    for model in _models():
        for attempt in range(2):
            started = time.perf_counter()
            try:
                try:
                    resp = client.models.generate_content(model=model, contents=prompt, config=fast)
                except errors.ClientError as exc:
                    if exc.code == 400:  # modèle sans thinking_budget : on réessaie sans
                        resp = client.models.generate_content(model=model, contents=prompt, config=plain)
                    else:
                        raise
                text = (resp.text or '').strip()
                if text:
                    logger.info('Gemini %s OK in %.2fs', model, time.perf_counter() - started)
                    cache.set(cache_key, text, 600)
                    return text
            except errors.APIError as exc:
                logger.warning('Gemini %s attempt %s failed: %s', model, attempt + 1, exc)
                if exc.code not in RETRYABLE:
                    break  # clé invalide / modèle inconnu : inutile de réessayer
                time.sleep(1.5 * (attempt + 1))
            except Exception:
                logger.exception('Gemini unexpected error')
                time.sleep(1)

    raise AIServiceError('The AI service is temporarily unavailable. Please try again.')