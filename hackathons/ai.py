import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class AIServiceError(Exception):
    """Erreur lisible par l'utilisateur."""


def generate_description(title, category='', language='en'):
    if not settings.GEMINI_API_KEY:
        raise AIServiceError('The AI service is not configured.')

    lang = 'French' if language == 'fr' else 'English'
    prompt = (
        f'Write an engaging description of a hackathon titled "{title}"'
        f'{f" in the category {category}" if category else ""}. '
        f'Maximum 4 lines (about 60 words), plain text, no markdown, no title. '
        f'Write in {lang}.'
    )
    try:
        from google import genai
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        response = client.models.generate_content(model=settings.GEMINI_MODEL, contents=prompt)
        text = (response.text or '').strip()
    except Exception as exc:  # réseau, quota, clé invalide...
        logger.exception('Gemini call failed')
        raise AIServiceError('The AI service is temporarily unavailable. Please try again.') from exc

    if not text:
        raise AIServiceError('The AI returned an empty answer. Please try again.')
    return text