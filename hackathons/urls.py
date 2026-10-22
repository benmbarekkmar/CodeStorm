from django.urls import path
from . import views

app_name = 'hackathons'

urlpatterns = [
    path('', views.hackathon_list, name='home'),
    path('create/', views.hackathon_create, name='create'),
    path('<int:pk>/', views.hackathon_detail, name='detail'),
    path('<int:pk>/edit/', views.hackathon_update, name='update'),
    path('<int:pk>/delete/', views.hackathon_delete, name='delete'),
    path('<int:pk>/publish/', views.hackathon_toggle_publish, name='toggle_publish'),
    path('<int:pk>/cancel/', views.hackathon_cancel, name='cancel'),

    path('<int:hackathon_pk>/challenges/create/', views.challenge_create, name='challenge_create'),
    path('challenges/<int:pk>/edit/', views.challenge_update, name='challenge_update'),
    path('challenges/<int:pk>/delete/', views.challenge_delete, name='challenge_delete'),
    path('challenges/<int:pk>/publish/', views.challenge_toggle_publish, name='challenge_toggle_publish'),

    path('ai/generate-description/', views.generate_description, name='generate_description'),
]