"""Typed exceptions for the analyses services.

Every error message here is *safe to show*: it must never contain secrets
(API keys) or raw upstream response bodies. Views store ``error_message``
from these exceptions on a FAILED report.
"""


class AnalysisError(Exception):
    """Base class for every recoverable analysis failure."""

    # Default user-facing fallback when no message is provided.
    default_message = 'The analysis could not be completed.'

    def __init__(self, message=None):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class GitHubError(AnalysisError):
    """Problem talking to GitHub (bad URL is caught earlier by validation)."""

    default_message = 'The GitHub repository could not be read.'


class GitHubNotFoundError(GitHubError):
    default_message = 'The repository was not found. Make sure it is public.'


class GitHubPrivateError(GitHubError):
    default_message = 'This repository is private. Only public repositories can be analyzed.'


class GitHubRateLimitError(GitHubError):
    default_message = 'GitHub rate limit reached. Please try again in a moment.'


class OversizedRepoError(GitHubError):
    default_message = 'This repository is too large to analyze.'


class NetworkError(AnalysisError):
    default_message = 'A network error occurred while analyzing the repository.'


class RequestTimeoutError(NetworkError):
    default_message = 'The request timed out. Please try again.'


class LLMTimeoutError(RequestTimeoutError):
    """The Gemini request, not the GitHub one, reached its deadline.

    Subclassing ``RequestTimeoutError`` keeps every existing ``except`` branch
    working, while the message names the service at fault and the one knob
    (``GEMINI_TIMEOUT``) that can move the deadline.
    """

    default_message = (
        'The AI service did not answer before the request deadline, so no '
        'analysis was saved. The deadline can be raised with the GEMINI_TIMEOUT '
        'environment variable.'
    )


class LLMError(AnalysisError):
    """Problem talking to the Gemini generateContent endpoint."""

    default_message = 'The AI service could not be reached.'


class LLMConfigError(LLMError):
    default_message = (
        'The AI service is not configured. Ask an administrator to set '
        'the GEMINI_API_KEY and GEMINI_MODEL environment variables.'
    )


class LLMRateLimitError(LLMError):
    default_message = 'The AI free-tier quota has been reached. Please try again later.'


class LLMRejectedError(LLMError):
    default_message = 'The AI service rejected the request. Please try again.'


class MalformedResponseError(AnalysisError):
    """The model answered, but not with a valid, well-formed report."""

    default_message = 'The AI returned an unexpected result. Please try again.'
