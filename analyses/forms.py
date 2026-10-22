from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from analyses.services.errors import AnalysisError
from analyses.services.github import parse_repo_url

INPUT = (
    'w-full bg-surface-container-lowest text-on-surface font-body-md text-body-md '
    'rounded-lg px-3.5 py-2.5 shadow-sm border border-transparent '
    'focus:border-primary/40 focus:outline-none focus:ring-2 focus:ring-primary/25 '
    'transition-colors placeholder:text-outline'
)


class AnalysisRequestForm(forms.Form):
    """Collects and validates a GitHub repository URL before any network call."""

    repository_url = forms.CharField(
        label=_('GitHub repository URL'),
        max_length=300,
        widget=forms.URLInput(attrs={'placeholder': 'https://github.com/owner/repository'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.help_text = None
            widget = field.widget
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {INPUT}'.strip()

    def clean_repository_url(self):
        url = (self.cleaned_data.get('repository_url') or '').strip()
        if not url:
            raise ValidationError(_('A repository URL is required.'))
        try:
            owner, repo = parse_repo_url(url)
        except AnalysisError as exc:
            raise ValidationError(_(str(exc)))
        # Normalize to the canonical form we will actually query.
        return f'https://github.com/{owner}/{repo}'
