"""A minimal, dependency-free LLM client for Google's Gemini API
(``generateContent`` REST endpoint), built on ``urllib``.

Free-tier only: the model id must be one that the project's Google AI Studio
dashboard lists as available without billing. The model is *required* via the
``GEMINI_MODEL`` environment variable rather than hard-coded, because Google
renders the current free-tier roster and its (dynamic) RPM/RPD quotas per
project in AI Studio and does not publish a stable hard number.

The API key is sent only through the ``x-goog-api-key`` header (never in the
URL) and is never written into logs, exceptions, or user-facing messages. The
request deadline is ``GEMINI_TIMEOUT`` seconds (default ``LLM_TIMEOUT``) so an
operator can widen it where Free-Tier latency needs more room, without the code
silently holding a jury's request open forever.
"""

import json
import os
import re
import urllib.error
import urllib.request
from urllib.request import Request

from analyses.services import rubric
from analyses.services.errors import (
    LLMConfigError,
    LLMError,
    LLMRateLimitError,
    LLMRejectedError,
    LLMTimeoutError,
    MalformedResponseError,
    NetworkError,
)

DEFAULT_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'
DEFAULT_TEMPERATURE = 0.2
LLM_TIMEOUT = 60

# Bounds for GEMINI_TIMEOUT. A value outside them is ignored rather than
# honoured: below MIN, a normal answer is cut off before it lands; above MAX, a
# dead connection would hold a jury's request open for minutes.
MIN_TIMEOUT = 15
MAX_TIMEOUT = 300


def resolve_timeout(default=LLM_TIMEOUT):
    """Seconds to wait for Gemini, from ``GEMINI_TIMEOUT``.

    The deadline stays an operator's choice instead of a constant in the code:
    Free-Tier latency varies, and the value that was too short for one run is
    not automatically too short for the next one. An unusable value falls back
    to the default, because ``urlopen`` would otherwise be handed a negative or
    absurd timeout and fail in a way that looks like an AI outage.
    """
    raw = os.environ.get('GEMINI_TIMEOUT', '')
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if MIN_TIMEOUT <= value <= MAX_TIMEOUT else default

# Model ids are simple slugs; anything else is rejected before hitting the URL.
_MODEL_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')

# finishReason values that mean the model produced no usable answer.
_BLOCKED_REASONS = {'SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'RECITATION'}


class LLMClient:
    """Interface: turn a system + user prompt into a validated JSON object."""

    def complete_json(self, system_prompt, user_prompt, *, schema=None):
        raise NotImplementedError


class GeminiClient(LLMClient):
    def __init__(self, api_key=None, model=None, base_url=None,
                 temperature=None, timeout=None):
        self.api_key = api_key if api_key is not None else os.environ.get('GEMINI_API_KEY', '')
        self.model = model if model is not None else os.environ.get('GEMINI_MODEL', '')
        self.base_url = base_url or os.environ.get('GEMINI_BASE_URL') or DEFAULT_BASE_URL
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else temperature
        self.timeout = resolve_timeout() if timeout is None else timeout

    # -- request construction ------------------------------------------------
    def build_request(self, system_prompt, user_prompt, *, schema=None):
        """Return the ``urllib.request.Request`` (no network) for inspection/tests."""
        if not self.api_key:
            raise LLMConfigError()
        if not self.model:
            raise LLMConfigError(
                'No Gemini model is configured. Set GEMINI_MODEL to a model '
                'available on your project Free Tier.'
            )
        if not _MODEL_RE.match(self.model):
            raise LLMConfigError('The configured Gemini model id is not valid.')

        body = {
            'systemInstruction': {'parts': [{'text': system_prompt}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_prompt}]}],
            'generationConfig': {
                'temperature': self.temperature,
                # Gemini natively returns JSON when asked for this mime type, and
                # the schema constrains the shape the analyzer expects. The reply
                # is still validated strictly in rubric.validate_report.
                'responseMimeType': 'application/json',
                'responseSchema': schema if schema is not None else rubric.RESPONSE_SCHEMA,
            },
        }
        url = f'{self.base_url.rstrip("/")}/models/{self.model}:generateContent'
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-goog-api-key': self.api_key,
        }
        return Request(url=url, data=json.dumps(body).encode('utf-8'),
                       headers=headers, method='POST')

    # -- network seam (patched by tests) ------------------------------------
    def _open(self, request, timeout):
        return urllib.request.urlopen(request, timeout=timeout)

    def complete_json(self, system_prompt, user_prompt, *, schema=None):
        request = self.build_request(system_prompt, user_prompt, schema=schema)
        try:
            with self._open(request, self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            self._raise_for_status(exc.code, self._safe_error_detail(exc))
        except urllib.error.URLError as exc:
            reason = getattr(exc, 'reason', None)
            if isinstance(reason, TimeoutError):
                raise LLMTimeoutError() from exc
            raise NetworkError() from exc
        except TimeoutError as exc:
            # Gemini accepted the request and then held it past the deadline.
            # Named as an AI timeout, not a generic network stall, so the
            # organizer sees which service to look at.
            raise LLMTimeoutError() from exc

        content = self._extract_content(raw)
        return self._parse_json(content)

    def _safe_error_detail(self, exc):
        """Return a short, secret-free summary of a Gemini error response body.

        Google's errors carry ``error.code``/``error.status``/``error.message``.
        The API key travels only in the ``x-goog-api-key`` header, so it is not
        part of the body; we still redact it defensively and cap the length.
        """
        try:
            payload = json.loads(exc.read().decode('utf-8'))
        except Exception:
            return ''
        if not isinstance(payload, dict):
            return ''
        err = payload.get('error')
        if not isinstance(err, dict):
            return ''
        # Prefer the machine-readable status token (e.g. NOT_FOUND) over free text.
        detail = str(err.get('status') or err.get('message') or '').strip()[:120]
        if self.api_key:
            detail = detail.replace(self.api_key, '[redacted]')
        return detail

    def _raise_for_status(self, code, detail=''):
        # Map every status the AI service can realistically return to a typed,
        # actionable error. Well-known codes keep their exact (translated)
        # message; the diagnostic Gemini status token is only attached where it
        # adds signal without harming i18n (the new 404 path and the fallback).
        if code in (401, 403):
            raise LLMRejectedError('The AI service refused the API key.')
        if code == 404:
            suffix = f' [{detail}]' if detail else ''
            raise LLMConfigError(
                'The configured Gemini model is not available for this API key. '
                'Set GEMINI_MODEL to a model your key can access on the Free Tier.' + suffix)
        if code == 400:
            raise LLMRejectedError('The AI service rejected the request.')
        if code == 429:
            raise LLMRateLimitError()
        if 500 <= code < 600:
            raise LLMError('The AI service is temporarily unavailable.')
        suffix = f' [{detail}]' if detail else ''
        raise LLMRejectedError(f'The AI service returned an error (HTTP {code}).' + suffix)

    @staticmethod
    def _extract_content(raw):
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, AttributeError):
            raise MalformedResponseError('The AI service returned an unreadable response.')
        if not isinstance(data, dict):
            raise MalformedResponseError('The AI response was empty or malformed.')
        if data.get('error'):
            raise MalformedResponseError('The AI service returned an error response.')

        feedback = data.get('promptFeedback') or {}
        if feedback.get('blocked'):
            raise MalformedResponseError('The request was blocked by the safety filters.')

        candidates = data.get('candidates') or []
        if not candidates:
            raise MalformedResponseError('The AI response was empty or malformed.')
        first = candidates[0]
        if first.get('finishReason') in _BLOCKED_REASONS:
            raise MalformedResponseError('The response was filtered by the safety settings.')

        parts = (first.get('content') or {}).get('parts') or []
        text = ''.join(part.get('text', '') for part in parts if isinstance(part, dict))
        if not text.strip():
            raise MalformedResponseError('The AI response was empty or malformed.')
        return text

    @staticmethod
    def _parse_json(content):
        text = (content or '').strip()
        # Even with responseMimeType set, strip code fences defensively.
        if text.startswith('```'):
            text = text.strip('`')
            if text.lower().startswith('json'):
                text = text[4:]
            text = text.strip()
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise MalformedResponseError('The AI response was not valid JSON.') from exc
        if not isinstance(payload, dict):
            raise MalformedResponseError('The AI response was not a JSON object.')
        return payload
