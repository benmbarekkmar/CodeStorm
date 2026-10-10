# resources/urls.py
from django.urls import path
from django.views.generic import TemplateView

app_name = 'resources'

urlpatterns = [
    path('', TemplateView.as_view(template_name='resources.html'), name='home'),
]