from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from analyses.permissions import analyzable_projects
from analyses.services.errors import AnalysisError
from analyses.services.github import parse_repo_url
from projects.models import Project

SELECT = (
    'w-full bg-surface-container-lowest text-on-surface font-body-md text-body-md '
    'rounded-lg px-3.5 py-2.5 shadow-sm border border-transparent '
    'focus:border-primary/40 focus:outline-none focus:ring-2 focus:ring-primary/25 '
    'transition-colors'
)


class AnalysisRequestForm(forms.Form):
    """Pick the project to analyze; its submitted URL is the only repository used.

    There is deliberately no URL field. Typing one would either analyze a
    repository the team never submitted, or point the report at some other
    project's code, and the report is decision support for a real jury score.
    """

    project = forms.ModelChoiceField(
        label=_('Project'),
        queryset=Project.objects.none(),
        required=True,
        empty_label=_('Select a project'),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['project'].queryset = analyzable_projects(user)
        widget = self.fields['project'].widget
        widget.attrs['class'] = SELECT

    def clean(self):
        data = super().clean()
        project = data.get('project')
        if project is None:
            # clean() on the field already reported "this choice is required".
            return data
        try:
            owner, repo = parse_repo_url(project.repo_url)
        except AnalysisError:
            # The chosen project's own submission is unusable; say so about the
            # project instead of echoing a URL-field message nobody can act on.
            self.add_error('project', ValidationError(_(
                'This project has no public github.com repository to analyze. '
                'The team must fix its submission first.')))
            return data
        # Normalized to the canonical form the GitHub service actually queries.
        data['repository_url'] = f'https://github.com/{owner}/{repo}'
        return data
