from django.contrib.auth.views import redirect_to_login

EXEMPT_PREFIXES = ('/accounts/login/', '/accounts/register/', '/static/', '/admin/')


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
