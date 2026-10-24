from django.contrib import admin
from .models import Hackathon, Challenge


class ChallengeInline(admin.TabularInline):
    model = Challenge
    extra = 0


@admin.register(Hackathon)
class HackathonAdmin(admin.ModelAdmin):
    list_display = ('title', 'organizer', 'category', 'status', 'start_date', 'end_date')
    list_filter = ('status', 'category', 'event_format')
    search_fields = ('title', 'description')
    inlines = [ChallengeInline]


@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    list_display = ('title', 'hackathon', 'difficulty', 'is_published')
    list_filter = ('is_published', 'difficulty')
