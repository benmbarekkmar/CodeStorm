from django import forms
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

from .models import Prize, PrizeCategory, Sponsor, LeaderboardEntry
from hackathons.models import Hackathon
from projects.models import Project


class PrizeCategoryForm(forms.ModelForm):
    """Formulaire de création / modification d'une catégorie de prix."""

    class Meta:
        model = PrizeCategory
        fields = ('name', 'description', 'icon', 'order')
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none focus:ring-2 '
                         'focus:ring-primary/40',
                'placeholder': _('e.g. Grand Prize, Best Innovation...'),
            }),
            'description': forms.Textarea(attrs={
                'class': 'w-full p-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none resize-none',
                'rows': 2,
            }),
            'icon': forms.TextInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'placeholder': 'emoji_events',
            }),
            'order': forms.NumberInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'min': 0,
            }),
        }

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        matches = PrizeCategory.objects.filter(name__iexact=name)
        if self.instance.pk:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise ValidationError(_('A category with this name already exists.'))
        return name


class SponsorForm(forms.ModelForm):
    """Formulaire de création / modification d'un sponsor."""

    class Meta:
        model = Sponsor
        fields = ('name', 'description', 'website', 'contact_email', 'logo')
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'placeholder': _('Sponsor organization name'),
            }),
            'description': forms.Textarea(attrs={
                'class': 'w-full p-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none resize-none',
                'rows': 2,
            }),
            'website': forms.URLInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'placeholder': 'https://example.com',
            }),
            'contact_email': forms.EmailInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
            }),
        }


class PrizeForm(forms.ModelForm):
    """
    Formulaire de création / modification d'un prix.
    L'organisateur ne peut créer un prix que pour les hackathons dont il est responsable.
    On lui passe le queryset hackathon filtré dans la vue.
    """

    class Meta:
        model = Prize
        fields = (
            'hackathon', 'name', 'description', 'category', 'sponsor',
            'amount', 'reward', 'winner_slots', 'eligibility_criteria',
        )
        widgets = {
            'hackathon': forms.Select(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none cursor-pointer',
            }),
            'name': forms.TextInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'placeholder': _('e.g. Grand Champion Award'),
            }),
            'description': forms.Textarea(attrs={
                'class': 'w-full p-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none resize-none',
                'rows': 2,
            }),
            'category': forms.Select(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none cursor-pointer',
            }),
            'sponsor': forms.Select(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none cursor-pointer',
            }),
            'amount': forms.NumberInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'placeholder': '5000',
                'min': '0',
                'step': '0.01',
            }),
            'reward': forms.TextInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'placeholder': _('e.g. Cloud credits, internship, mentoring...'),
            }),
            'winner_slots': forms.NumberInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'min': '1',
            }),
            'eligibility_criteria': forms.Textarea(attrs={
                'class': 'w-full p-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none resize-none',
                'rows': 2,
                'placeholder': _('Who can receive this prize?'),
            }),
        }

    def __init__(self, *args, organizer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].required = True
        # Filtrer les hackathons par organisateur
        if organizer is not None:
            self.fields['hackathon'].queryset = Hackathon.objects.filter(
                organizer=organizer
            )
            if self.instance.pk:
                self.fields['hackathon'].disabled = True
                self.initial['hackathon'] = self.instance.hackathon_id
            # Sponsor details are public records, so a sponsor can back awards
            # across events; editing sponsor details remains creator-restricted.
            self.fields['sponsor'].queryset = Sponsor.objects.all()

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is not None and amount < 0:
            raise ValidationError(_('Prize amount cannot be negative.'))
        return amount

    def clean(self):
        cleaned_data = super().clean()
        # Vérifier l'unicité hackathon + name (en dehors de l'instance en cours)
        hackathon = cleaned_data.get('hackathon')
        name = cleaned_data.get('name')
        if hackathon and name:
            name = name.strip()
            cleaned_data['name'] = name
            qs = Prize.objects.filter(hackathon=hackathon, name__iexact=name)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(
                    _('A prize with this name already exists for this hackathon.')
                )
        winner_slots = cleaned_data.get('winner_slots')
        if (self.instance.pk and winner_slots is not None and
                self.instance.awards.count() > winner_slots):
            self.add_error(
                'winner_slots',
                _('Winner slots cannot be fewer than the current winner assignments.'),
            )
        return cleaned_data


class AssignWinnerForm(forms.Form):
    """
    Formulaire permettant à l'organisateur d'assigner un projet gagnant à un prix.
    Filtre les projets au hackathon concerné.
    """

    projects = forms.ModelMultipleChoiceField(
        queryset=Project.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label=_('Winning projects'),
        help_text=_('Select no more projects than this prize has winner slots.'),
    )

    def __init__(self, *args, prize=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.prize = prize
        if prize is not None:
            self.fields['projects'].queryset = Project.objects.filter(
                hackathon=prize.hackathon,
                status=Project.Status.EVALUATED,
                leaderboard_entries__hackathon=prize.hackathon,
            ).distinct().order_by('title')
            if not self.is_bound:
                self.initial['projects'] = prize.awards.values_list('project_id', flat=True)

    def clean_projects(self):
        projects = self.cleaned_data['projects']
        if self.prize is not None and projects.count() > self.prize.winner_slots:
            raise ValidationError(
                _('Select at most %(count)d winning project(s).') % {
                    'count': self.prize.winner_slots
                }
            )
        return projects


class LeaderboardEntryForm(forms.ModelForm):
    """Formulaire pour créer / mettre à jour une entrée de classement."""

    class Meta:
        model = LeaderboardEntry
        fields = ('project', 'rank', 'final_score', 'notes')
        widgets = {
            'project': forms.Select(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none cursor-pointer',
            }),
            'rank': forms.NumberInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'min': 1,
            }),
            'final_score': forms.NumberInput(attrs={
                'class': 'w-full h-10 px-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none',
                'step': '0.01',
                'min': 0,
                'max': 100,
            }),
            'notes': forms.Textarea(attrs={
                'class': 'w-full p-3 rounded-lg bg-surface-container-low '
                         'text-on-surface focus:outline-none resize-none',
                'rows': 2,
                'placeholder': _('Private organizer notes...'),
            }),
        }

    def __init__(self, *args, hackathon=None, **kwargs):
        super().__init__(*args, **kwargs)
        if hackathon is not None:
            self.fields['project'].queryset = Project.objects.filter(
                hackathon=hackathon
            ).select_related('team')
