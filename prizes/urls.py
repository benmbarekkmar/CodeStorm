from django.urls import path
from . import views

app_name = 'prizes'

urlpatterns = [
    # ── Vue principale (dashboard selon le rôle) ──────────────────────────────
    path('', views.prizes_home, name='home'),

    # ── CRUD Prix ─────────────────────────────────────────────────────────────
    path('create/', views.prize_create, name='prize_create'),
    path('<int:pk>/edit/', views.prize_edit, name='prize_edit'),
    path('<int:pk>/delete/', views.prize_delete, name='prize_delete'),
    path('<int:pk>/', views.prize_detail, name='prize_detail'),
    path('<int:pk>/assign-winner/', views.prize_assign_winner, name='prize_assign_winner'),

    # ── Sponsors ───────────────────────────────────────────────────────────────
    path('sponsors/', views.sponsor_list, name='sponsor_list'),
    path('sponsors/create/', views.sponsor_create, name='sponsor_create'),
    path('sponsors/<int:pk>/edit/', views.sponsor_edit, name='sponsor_edit'),

    # ── Catégories ─────────────────────────────────────────────────────────────
    path('categories/', views.category_list, name='category_list'),
    path('categories/<int:pk>/edit/', views.category_edit, name='category_edit'),
    path('categories/<int:pk>/delete/', views.category_delete, name='category_delete'),

    # ── Classement ─────────────────────────────────────────────────────────────
    path(
        'hackathon/<int:hackathon_pk>/leaderboard/prepare/',
        views.leaderboard_prepare,
        name='leaderboard_prepare',
    ),
    path(
        'hackathon/<int:hackathon_pk>/leaderboard/publish/',
        views.leaderboard_publish,
        name='leaderboard_publish',
    ),
    path(
        'hackathon/<int:hackathon_pk>/leaderboard/unpublish/',
        views.leaderboard_unpublish,
        name='leaderboard_unpublish',
    ),

    # ── Certificats ────────────────────────────────────────────────────────────
    path(
        'hackathon/<int:hackathon_pk>/certificates/generate/',
        views.certificate_generate,
        name='certificate_generate',
    ),
    path(
        'hackathon/<int:hackathon_pk>/certificates/',
        views.certificate_list,
        name='certificate_list',
    ),
    path(
        'certificates/<uuid:uid>/download/',
        views.certificate_download,
        name='certificate_download',
    ),
    path(
        'certificates/<uuid:uid>/preview/',
        views.certificate_preview,
        name='certificate_preview',
    ),
]
