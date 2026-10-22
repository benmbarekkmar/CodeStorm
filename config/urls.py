from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include
from django.views.generic import TemplateView

from accounts.views import register, profile_settings
from config.views import dashboard
from django.conf import settings
from django.conf.urls.static import static

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
    path(
        'notifications/',
        TemplateView.as_view(template_name='notifications.html'),
        name='notifications',
    ),
    path(
        'profile/',
        profile_settings,
        name='profile_settings',
    ),

    # Apps (chaque app définit app_name et name='home' dans son urls.py)
    path('hackathons/', include('hackathons.urls')),
    path('teams/', include('teams.urls')),
    path('projects/', include('projects.urls')),
    path('prizes/', include('prizes.urls')),
    path('evaluations/', include('evaluations.urls')),
    path('resources/', include('resources.urls')),
    path('analyses/', include('analyses.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)