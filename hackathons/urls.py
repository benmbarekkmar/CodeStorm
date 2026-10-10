from django.urls import path
from django.http import HttpResponse
from django.views.generic import TemplateView

app_name = 'hackathons'

urlpatterns = [
    path('', TemplateView.as_view(template_name='hackathons.html'), name='home'),
]