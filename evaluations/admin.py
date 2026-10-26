from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (
    Evaluation, EvaluationComment, EvaluationCriterion, EvaluationScore,
)


# ── Criteria ────────────────────────────────────────────────────────────────

@admin.register(EvaluationCriterion)
class EvaluationCriterionAdmin(admin.ModelAdmin):
    list_display = ('name', 'hackathon', 'weight', 'order')
    list_editable = ('weight', 'order')
    list_filter = ('hackathon',)
    search_fields = ('name',)


# ── Evaluation ──────────────────────────────────────────────────────────────

class EvaluationScoreInline(admin.TabularInline):
    model = EvaluationScore
    extra = 0
    fields = ('criterion', 'score')


class EvaluationCommentInline(admin.TabularInline):
    model = EvaluationComment
    extra = 0
    fields = ('author', 'text', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(Evaluation)
class EvaluationAdmin(admin.ModelAdmin):
    list_display = ('project', 'jury', 'status', 'total_score_display', 'updated_at')
    list_filter = ('status', 'project__hackathon')
    search_fields = ('project__title', 'project__team__name', 'jury__username')
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
