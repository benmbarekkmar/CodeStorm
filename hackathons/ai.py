import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class AIServiceError(Exception):
    """Erreur lisible par l'utilisateur."""


def _call_gemini(prompt):
    if not settings.GEMINI_API_KEY:
        raise AIServiceError('The AI service is not configured.')
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


def generate_description(title, category='', language='en'):
    lang = 'French' if language == 'fr' else 'English'
    prompt = (
        f'Write an engaging description of a hackathon titled "{title}"'
        f'{f" in the category {category}" if category else ""}. '
        f'Maximum 4 lines (about 60 words), plain text, no markdown, no title. '
        f'Write in {lang}.'
    )
    return _call_gemini(prompt)


def generate_title_and_description(category='', language='en'):
    """Suggest a hackathon title AND description for a category.

    Returns {'title': ..., 'description': ...}; the model must answer on
    two lines: 'TITLE: <suggested title>' then the description.
    """
    lang = 'French' if language == 'fr' else 'English'
    prompt = (
        f'Suggest a catchy hackathon name'
        f'{f" in the category {category}" if category else ""}. '
        f'Answer on exactly two lines. First line: TITLE: <name only>. '
        f'Second line: an engaging description, maximum 4 lines (about 60 words), '
        f'plain text, no markdown. Write in {lang}.'
    )
    text = _call_gemini(prompt)
    head, sep, tail = text.partition('\n')
    cleaned = head.lstrip('*-# ')
    if sep and cleaned.lower().lstrip().startswith(('title', 'titre')):
        title = cleaned.partition(':')[2].strip()[:150]
        description = tail.strip()
    else:
        title = ''
        description = ''
    if not title or not description:
        raise AIServiceError('The AI returned an unusable answer. Please try again.')
    return {'title': title, 'description': description}