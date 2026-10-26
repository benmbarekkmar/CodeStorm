from django.contrib import admin

from teams.models import JoinRequest, Team, TeamInvitation, TeamMembership


class TeamMembershipInline(admin.TabularInline):
    model = TeamMembership
    extra = 0
    raw_id_fields = ('user',)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'hackathon', 'captain', 'status', 'visibility', 'max_members', 'created_at')
    list_filter = ('status', 'visibility', 'hackathon')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')
    inlines = (TeamMembershipInline,)


@admin.register(TeamMembership)
class TeamMembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'team', 'role', 'joined_at')
    list_filter = ('role',)
    search_fields = ('user__username', 'team__name')


@admin.register(TeamInvitation)
class TeamInvitationAdmin(admin.ModelAdmin):
    list_display = ('team', 'invitee', 'inviter', 'status', 'created_at', 'expires_at')
    list_filter = ('status',)
    search_fields = ('team__name', 'invitee__username')


@admin.register(JoinRequest)
class JoinRequestAdmin(admin.ModelAdmin):
    list_display = ('team', 'user', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('team__name', 'user__username')
