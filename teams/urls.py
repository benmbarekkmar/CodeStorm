from django.urls import path

from teams import views

app_name = 'teams'

urlpatterns = [
    path('', views.team_list, name='home'),
    path('create/', views.team_create, name='create'),
    path('invitations/', views.invitations, name='invitations'),

    path('<int:pk>/', views.team_detail, name='detail'),
    path('<int:pk>/edit/', views.team_edit, name='edit'),
    path('<int:pk>/leave/', views.team_leave, name='leave'),
    path('<int:pk>/request-join/', views.team_request_join, name='request_join'),
    path('<int:pk>/invite/', views.team_send_invitation, name='send_invitation'),
    path('<int:pk>/members/<int:membership_id>/remove/', views.team_remove_member, name='remove_member'),
    path('<int:pk>/transfer/', views.team_transfer_captain, name='transfer_captain'),
    path('<int:pk>/disband/', views.team_disband, name='disband'),

    path('invitations/<int:invitation_id>/accept/', views.invitation_accept, name='invitation_accept'),
    path('invitations/<int:invitation_id>/decline/', views.invitation_decline, name='invitation_decline'),
    path('invitations/<int:invitation_id>/cancel/', views.invitation_cancel, name='invitation_cancel'),

    path('requests/<int:request_id>/respond/', views.join_request_respond, name='join_request_respond'),
    path('requests/<int:request_id>/cancel/', views.join_request_cancel, name='join_request_cancel'),
]
