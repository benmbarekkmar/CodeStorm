from django.urls import path

from projects import views

app_name = 'projects'

urlpatterns = [
    path('', views.project_list, name='home'),
    path('create/', views.project_create, name='create'),
    path('export/', views.project_export, name='export'),
    path('<int:pk>/', views.project_detail, name='detail'),
    path('<int:pk>/edit/', views.project_edit, name='edit'),
    path('<int:pk>/delete/', views.project_delete, name='delete'),
    path('<int:pk>/submit/', views.project_submit, name='submit'),
    path('<int:pk>/duplicate/', views.project_duplicate, name='duplicate'),
    path('<int:pk>/reopen/', views.project_reopen, name='reopen'),
    path('<int:pk>/review/', views.project_review, name='review'),
    path('<int:pk>/evaluated/', views.project_mark_evaluated, name='mark_evaluated'),
]
