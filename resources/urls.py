from django.urls import path

from resources import views

app_name = 'resources'

urlpatterns = [
    path('', views.resource_list, name='home'),
    path('create/', views.resource_create, name='create'),
    path('<int:pk>/', views.resource_detail, name='detail'),
    path('<int:pk>/edit/', views.resource_edit, name='edit'),
    path('<int:pk>/delete/', views.resource_delete, name='delete'),
]
