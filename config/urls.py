from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include

from accounts.views import register, profile_settings
from config.views import dashboard, healthz, media_file, notifications
from django.conf import settings

# Declared explicitly: templates/404.html, 403.html and 500.html are the answer a
# visitor gets, so a permission failure never shows a bare stack trace.
handler404 = 'django.views.defaults.page_not_found'
handler403 = 'django.views.defaults.permission_denied'
handler500 = 'django.views.defaults.server_error'

urlpatterns = [
    path('admin/', admin.site.urls),

    # Dashboard (page d'accueil)
    path('', dashboard, name='dashboard'),

    # Authentification
    path(
        'accounts/login/',
        auth_views.LoginView.as_view(template_name='login.html', redirect_authenticated_user=True),
        name='login',
    ),
    path('accounts/register/', register, name='register'),
    path(
        'accounts/logout/',
        auth_views.LogoutView.as_view(),
        name='logout',
    ),

    # Pages simples (sans app dédiée)
    path('healthz/', healthz, name='healthz'),
    path('notifications/', notifications, name='notifications'),
    path(
        'profile/',
        profile_settings,
        name='profile_settings',
    ),

    # Uploads. One route for every environment, so a banner that shows locally
    # is the same code path that shows in production -- and it is not a public
    # directory anymore.
    path(f'{settings.MEDIA_URL.lstrip("/")}<path:uploaded_path>', media_file, name='media'),

    # Apps (chaque app définit app_name et name='home' dans son urls.py)
    path('hackathons/', include('hackathons.urls')),
    path('teams/', include('teams.urls')),
    path('projects/', include('projects.urls')),
    path('prizes/', include('prizes.urls')),
    path('evaluations/', include('evaluations.urls')),
    path('resources/', include('resources.urls')),
    path('analyses/', include('analyses.urls')),
]