from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from projects.models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'team', 'hackathon', 'status', 'progress', 'submitted_by', 'submitted_at',
    )
    list_filter = ('status', 'hackathon')
    search_fields = ('title', 'team__name', 'hackathon__title', 'technologies', 'repo_url')
    list_select_related = ('team', 'hackathon', 'submitted_by')
    raw_id_fields = ('team', 'hackathon', 'submitted_by')
    readonly_fields = ('created_at', 'updated_at', 'submitted_at')
    fieldsets = (
        (None, {
            'fields': ('title', 'tagline', 'description', 'hackathon', 'team', 'submitted_by'),
        }),
        (_('Submission'), {
            'fields': (
                'status', 'progress', 'technologies', 'repo_url', 'demo_url',
                'banner', 'pitch_deck', 'submitted_at',
            ),
        }),
        (_('Dates'), {'fields': ('created_at', 'updated_at')}),
    )
