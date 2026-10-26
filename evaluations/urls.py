from django.urls import path

from . import views

app_name = 'evaluations'

urlpatterns = [
    # Main evaluations dashboard
    path('', views.evaluations_home, name='home'),

    # Hackathons + projects for the start picker
    path('catalog/', views.evaluation_catalog, name='catalog'),

    # Start an evaluation for a chosen project
    path('start/', views.evaluation_start, name='start'),

    # Fetch evaluation data (AJAX)
    path('project/<int:project_id>/detail/', views.evaluation_detail, name='detail'),

    # Submit or save-draft (AJAX POST, JSON body)
    path('submit/', views.evaluation_submit, name='submit'),

    # Add comment (AJAX POST, form data)
    path('<int:evaluation_id>/comment/', views.evaluation_add_comment, name='add_comment'),

    # Leaderboard / Results page
    path('results/', views.results_leaderboard, name='results'),

    # CSV export
    path('export/', views.export_csv, name='export'),
]
