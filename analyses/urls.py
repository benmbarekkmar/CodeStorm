from django.urls import path

from analyses import views

app_name = 'analyses'

urlpatterns = [
    path('', views.analysis_list, name='home'),
    path('create/', views.analysis_create, name='create'),
    path('<int:pk>/', views.analysis_detail, name='detail'),
    path('<int:pk>/relaunch/', views.analysis_relaunch, name='relaunch'),
    path('<int:pk>/delete/', views.analysis_delete, name='delete'),
]
