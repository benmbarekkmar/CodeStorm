from django.urls import path
from django.http import HttpResponse
from django.views.generic import TemplateView

app_name = 'teams'

urlpatterns = [
    path('', TemplateView.as_view(template_name='teams.html'), name='home'),
]