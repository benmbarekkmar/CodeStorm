from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Hackathon, Challenge

INPUT_CLASS = (
    'w-full bg-surface text-on-surface rounded-lg px-3.5 py-2.5 border '
    'border-outline-variant focus:outline-none focus:ring-2 focus:ring-primary'
)
DT_FORMAT = '%Y-%m-%dT%H:%M'


def datetime_widget():
    return forms.DateTimeInput(attrs={'type': 'datetime-local'}, format=DT_FORMAT)


class StyledModelForm(forms.ModelForm):
    """Applique le style Tailwind du projet à tous les champs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = INPUT_CLASS


class HackathonForm(StyledModelForm):
    class Meta:
        model = Hackathon
        fields = [
            'title', 'category', 'description', 'event_format', 'location', 'banner',
            'start_date', 'end_date', 'registration_deadline', 'submission_deadline',
            'max_participants', 'min_team_size', 'max_team_size',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 5}),
            'start_date': datetime_widget(),
            'end_date': datetime_widget(),
            'registration_deadline': datetime_widget(),
            'submission_deadline': datetime_widget(),
        }

    def clean_start_date(self):
        start = self.cleaned_data['start_date']
        if not self.instance.pk and start < timezone.now():
            raise forms.ValidationError(_('The start date cannot be in the past.'))
        return start

    def clean_max_participants(self):
        value = self.cleaned_data['max_participants']
        if value < 1:
            raise forms.ValidationError(_('There must be at least 1 participant.'))
        return value


class ChallengeForm(StyledModelForm):
    class Meta:
        model = Challenge
        fields = ['title', 'description', 'difficulty', 'deadline']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 5}),
            'deadline': datetime_widget(),
        }