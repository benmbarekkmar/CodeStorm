from django.contrib import admin

from resources.models import Resource


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ('title', 'type', 'category', 'created_by', 'created_at')
    list_filter = ('type', 'category')
    search_fields = ('title', 'description')
    readonly_fields = ('created_at', 'updated_at')
