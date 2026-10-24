from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from .models import (
    Hackathon, Project, EvaluationCriteria,
    Evaluation, EvaluationScore, EvaluationComment,
)


# ── Hackathon ───────────────────────────────────────────────────────────────

@admin.register(Hackathon)
class HackathonAdmin(admin.ModelAdmin):
    list_display = ('name', 'track', 'deadline', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'track')
    list_editable = ('is_active',)


# ── Project ─────────────────────────────────────────────────────────────────

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'team_name', 'hackathon', 'submitted_at')
    list_filter = ('hackathon',)
    search_fields = ('name', 'team_name')
    autocomplete_fields = ('hackathon',)


# ── Criteria ────────────────────────────────────────────────────────────────

@admin.register(EvaluationCriteria)
class EvaluationCriteriaAdmin(admin.ModelAdmin):
    list_display = ('name', 'hackathon', 'weight', 'order')
    list_editable = ('weight', 'order')
    list_filter = ('hackathon',)
    search_fields = ('name',)


# ── Evaluation ──────────────────────────────────────────────────────────────

class EvaluationScoreInline(admin.TabularInline):
    model = EvaluationScore
    extra = 0
    fields = ('criteria', 'score')


class EvaluationCommentInline(admin.TabularInline):
    model = EvaluationComment
    extra = 0
    fields = ('author', 'text', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(Evaluation)
class EvaluationAdmin(admin.ModelAdmin):
    list_display = ('project', 'evaluator', 'status', 'total_score_display', 'updated_at')
    list_filter = ('status', 'project__hackathon')
    search_fields = ('project__name', 'project__team_name', 'evaluator__username')
    readonly_fields = ('created_at', 'updated_at')
    inlines = [EvaluationScoreInline, EvaluationCommentInline]

    @admin.display(description=_('Total Score'))
    def total_score_display(self, obj):
        return f"{obj.total_score}/100"


@admin.register(EvaluationComment)
class EvaluationCommentAdmin(admin.ModelAdmin):
    list_display = ('evaluation', 'author', 'created_at')
    list_filter = ('evaluation__project__hackathon',)
    search_fields = ('text', 'author__username')
