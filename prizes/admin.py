from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (
    Certificate, LeaderboardEntry, Prize, PrizeAward, PrizeCategory, Sponsor,
)


@admin.register(Sponsor)
class SponsorAdmin(admin.ModelAdmin):
    list_display = ('name', 'website', 'contact_email', 'created_by', 'created_at')
    search_fields = ('name', 'description')
    ordering = ('name',)
    readonly_fields = ('created_at',)


@admin.register(PrizeCategory)
class PrizeCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'icon', 'order')
    list_editable = ('order',)
    search_fields = ('name',)
    ordering = ('order', 'name')


class PrizeAwardInline(admin.TabularInline):
    model = PrizeAward
    extra = 0
    raw_id_fields = ('project',)


@admin.register(Prize)
class PrizeAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'hackathon', 'category', 'sponsor', 'amount',
        'status', 'winning_project', 'created_at',
    )
    list_filter = ('status', 'hackathon', 'category')
    search_fields = ('name', 'hackathon__name', 'sponsor__name')
    autocomplete_fields = ('sponsor',)
    raw_id_fields = ('winning_project',)
    ordering = ('hackathon', '-amount', 'name')
    readonly_fields = ('created_at', 'updated_at')
    inlines = (PrizeAwardInline,)
    fieldsets = (
        (_('Basic Information'), {
            'fields': ('hackathon', 'name', 'description', 'category', 'status')
        }),
        (_('Reward'), {
            'fields': ('amount', 'reward', 'winner_slots', 'eligibility_criteria')
        }),
        (_('Associations'), {'fields': ('sponsor', 'winning_project')}),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(PrizeAward)
class PrizeAwardAdmin(admin.ModelAdmin):
    list_display = ('prize', 'project', 'position', 'created_at')
    list_filter = ('prize__hackathon',)
    search_fields = ('prize__name', 'project__title', 'project__team__name')
    autocomplete_fields = ('prize',)
    raw_id_fields = ('project',)
    readonly_fields = ('created_at',)


@admin.register(LeaderboardEntry)
class LeaderboardEntryAdmin(admin.ModelAdmin):
    list_display = ('rank', 'project', 'hackathon', 'final_score', 'created_at')
    list_filter = ('hackathon',)
    ordering = ('hackathon', 'rank')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = (
        'uid', 'cert_type', 'recipient', 'hackathon', 'prize', 'rank', 'issued_at'
    )
    list_filter = ('cert_type', 'hackathon')
    search_fields = ('recipient__username', 'recipient__email', 'uid')
    readonly_fields = ('uid', 'issued_at')
