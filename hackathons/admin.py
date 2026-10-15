from django.contrib import admin

from hackathons.models import Hackathon


@admin.register(Hackathon)
class HackathonAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'max_team_size', 'allow_multiple_teams', 'created_by', 'created_at')
    list_filter = ('status', 'allow_multiple_teams')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')
