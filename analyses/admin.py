from django.contrib import admin

from analyses.models import AIReport


@admin.register(AIReport)
class AIReportAdmin(admin.ModelAdmin):
    list_display = ('repository_url', 'status', 'created_by', 'file_count', 'created_at')
    list_filter = ('status',)
    search_fields = ('repository_url', 'commit_ref')
    readonly_fields = ('created_at', 'updated_at', 'report', 'error_message')
    ordering = ('-created_at', '-id')
