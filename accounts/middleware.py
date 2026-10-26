from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.utils import translation

# /healthz/ is the platform's own probe: it has to answer before anyone signs in.
EXEMPT_PREFIXES = (
    '/accounts/login/', '/accounts/register/', '/static/', '/admin/', '/healthz/',
)


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            not request.user.is_authenticated
            and not request.path.startswith(EXEMPT_PREFIXES)
        ):
            return redirect_to_login(request.get_full_path())
        return self.get_response(request)


class UserLanguageMiddleware:
    """Activate the logged-in user's chosen display language for the request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        code = getattr(request.user, 'language', None) if request.user.is_authenticated else None
        if code:
            code = code.split('-')[0].lower()
        if code and code in dict(settings.LANGUAGES):
            translation.activate(code)
            request.LANGUAGE_CODE = code
        return self.get_response(request)
