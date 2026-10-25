from django import forms
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon
from projects.models import Project, normalize_technologies
from projects.permissions import eligible_teams
from teams.models import Team


INPUT = (
    'w-full bg-surface-container-low text-on-surface font-body-md text-body-md '
    'rounded-xl px-3.5 py-2.5 focus:outline-none focus:bg-surface-container-lowest '
    'focus:ring-2 focus:ring-primary/20 transition-all placeholder:text-outline'
)

MAX_BANNER_BYTES = 5 * 1024 * 1024
MAX_DECK_BYTES = 10 * 1024 * 1024


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = (
            'title', 'tagline', 'description', 'hackathon', 'team',
            'technologies', 'repo_url', 'demo_url', 'progress',
            'banner', 'pitch_deck',
        )
        widgets = {
            'title': forms.TextInput(attrs={'placeholder': _('e.g. SmartHealth AI')}),
            'tagline': forms.TextInput(attrs={
                'placeholder': _('One-sentence elevator pitch of your submission'),
            }),
            'description': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': _('Describe the problem, the solution, and who it helps.'),
            }),
            'technologies': forms.TextInput(attrs={
                'placeholder': _('e.g. Python, Django, PostgreSQL'),
            }),
            'repo_url': forms.URLInput(attrs={'placeholder': 'https://github.com/org/repo'}),
            'demo_url': forms.URLInput(attrs={'placeholder': 'https://demo.example.com'}),
            'progress': forms.NumberInput(attrs={'min': 0, 'max': 100, 'step': 1}),
            'banner': forms.ClearableFileInput(attrs={'accept': 'image/png,image/jpeg,image/webp'}),
            'pitch_deck': forms.ClearableFileInput(attrs={'accept': 'application/pdf,.pdf'}),
        }

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        editing = bool(self.instance.pk)
        if editing:
            self.fields['team'].queryset = Team.objects.filter(
                pk=self.instance.team_id,
            ).select_related('hackathon')
            self.fields['hackathon'].queryset = Hackathon.objects.filter(
                pk=self.instance.hackathon_id,
            )
            self.fields['team'].disabled = True
            self.fields['hackathon'].disabled = True
            self.fields['team'].empty_label = None
            self.fields['hackathon'].empty_label = None
        else:
            teams = eligible_teams(user)
            self.fields['team'].queryset = teams
            self.fields['hackathon'].queryset = Hackathon.objects.filter(
                pk__in=teams.values('hackathon_id'),
            ).order_by('title')
            self.fields['team'].empty_label = _('Select a team')
            self.fields['hackathon'].empty_label = _('Select a hackathon')

        self.fields['team'].label_from_instance = _team_label
        self.fields['tagline'].required = True
        labels = {
            'title': _('Project name'),
            'tagline': _('Short pitch'),
            'description': _('Problem and solution'),
            'hackathon': _('Hackathon'),
            'team': _('Team'),
            'technologies': _('Technologies'),
            'repo_url': _('GitHub repository URL'),
            'demo_url': _('Live demo URL'),
            'progress': _('Progress (%)'),
            'banner': _('Banner'),
            'pitch_deck': _('Pitch deck (PDF)'),
        }
        for name, field in self.fields.items():
            field.label = labels.get(name, field.label)
            if name != 'technologies':
                field.help_text = None
            widget = field.widget
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {INPUT}'.strip()

    def clean_title(self):
        title = (self.cleaned_data.get('title') or '').strip()
        if not title:
            raise forms.ValidationError(_('Enter a project name.'))
        return title

    def clean_tagline(self):
        tagline = (self.cleaned_data.get('tagline') or '').strip()
        if not tagline:
            raise forms.ValidationError(_('Enter a short pitch.'))
        return tagline

    def clean_description(self):
        description = (self.cleaned_data.get('description') or '').strip()
        if not description:
            raise forms.ValidationError(_('Describe the project.'))
        return description

    def clean_technologies(self):
        return normalize_technologies(self.cleaned_data.get('technologies'))

    def clean_banner(self):
        banner = self.cleaned_data.get('banner')
        _reject_oversized(banner, MAX_BANNER_BYTES, _('The banner'))
        return banner

    def clean_pitch_deck(self):
        deck = self.cleaned_data.get('pitch_deck')
        _reject_oversized(deck, MAX_DECK_BYTES, _('The pitch deck'))
        return deck


class DuplicateProjectForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.none(), label=_('Copy into team'))

    def __init__(self, teams, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields['team']
        field.queryset = teams
        field.empty_label = _('Select a team')
        field.label_from_instance = _team_label
        field.help_text = None
        field.widget.attrs['class'] = INPUT


def _team_label(team):
    if team.hackathon_id:
        return f'{team.name} — {team.hackathon.title}'
    return team.name


def _reject_oversized(upload, limit, label):
    if upload and getattr(upload, 'size', 0) > limit:
        raise forms.ValidationError(
            _('%(label)s must be %(limit)s MB or smaller.')
            % {'label': label, 'limit': limit // (1024 * 1024)}
        )
