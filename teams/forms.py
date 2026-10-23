from django import forms
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon
from teams.models import Team, TeamInvitation

User = get_user_model()

INPUT = (
    'w-full bg-surface-container-lowest text-on-surface font-body-md text-body-md '
    'rounded-lg px-3.5 py-2.5 shadow-sm border border-transparent '
    'focus:border-primary/40 focus:outline-none focus:ring-2 focus:ring-primary/25 '
    'transition-colors placeholder:text-outline'
)


class TeamForm(forms.ModelForm):
    hackathon = forms.ModelChoiceField(
        label=_('Hackathon'),
        queryset=Hackathon.objects.filter(
            status__in=[Hackathon.Status.REGISTRATION_OPEN, Hackathon.Status.ONGOING],
        ),
        required=False,
        empty_label=_('No hackathon'),
    )

    class Meta:
        model = Team
        fields = (
            'name', 'description', 'required_skills', 'interests', 'hackathon',
            'max_members', 'visibility', 'allow_join_requests',
        )
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': _('Choose a unique team name')}),
            'description': forms.Textarea(attrs={'rows': 3, 'placeholder': _('Describe what your team aims to build.')}),
            'required_skills': forms.TextInput(attrs={'placeholder': _('Skills the team is looking for, separated by commas')}),
            'interests': forms.TextInput(attrs={'placeholder': _('Topics or domains the team focuses on, separated by commas')}),
            'max_members': forms.NumberInput(attrs={'min_value': 2, 'max_value': 50}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.help_text = None
            widget = field.widget
            existing = widget.attrs.get('class', '')
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'w-5 h-5 accent-primary cursor-pointer'
            elif isinstance(widget, forms.Select):
                widget.attrs['class'] = f'{existing} {INPUT}'.strip()
            else:
                widget.attrs['class'] = f'{existing} {INPUT}'.strip()
        self.fields['allow_join_requests'].label = _('Allow join requests')
        self.fields['visibility'].label = _('Visibility')
        self.fields['max_members'].label = _('Max members')

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        qs = Team.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_('A team with this name already exists.'))
        return name

    def clean(self):
        cleaned = super().clean()
        max_members = cleaned.get('max_members')
        instance = self.instance
        if max_members is not None and instance and instance.pk:
            if max_members < instance.member_count():
                raise forms.ValidationError(
                    _('Max members cannot be lower than the current number of members (%(count)s).')
                    % {'count': instance.member_count()}
                )
        return cleaned


class InviteMemberForm(forms.ModelForm):
    invitee = forms.ModelChoiceField(
        label=_('Participant'),
        queryset=User.objects.filter(role=User.Role.PARTICIPANT, is_active=True).order_by('username'),
        empty_label=_('Select a participant'),
    )

    class Meta:
        model = TeamInvitation
        fields = ('invitee', 'message')
        widgets = {
            'message': forms.Textarea(attrs={'rows': 2, 'placeholder': _('Optional message to the invitee.')}),
        }

    def __init__(self, *args, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.team = team
        for field in self.fields.values():
            field.help_text = None
        self.fields['invitee'].widget.attrs['class'] = INPUT
        self.fields['message'].widget.attrs['class'] = INPUT
        if self.team is not None:
            invited = self.team.invitations.filter(
                status=TeamInvitation.Status.PENDING,
            ).values_list('invitee_id', flat=True)
            self.fields['invitee'].queryset = (
                User.objects.filter(role=User.Role.PARTICIPANT, is_active=True)
                .exclude(memberships__team=self.team)
                .exclude(pk__in=list(invited))
                .order_by('username')
            )

    def clean_invitee(self):
        user = self.cleaned_data['invitee']
        if self.team and self.team.has_member(user):
            raise forms.ValidationError(_('This user is already a member of the team.'))
        if self.team and self.team.is_full():
            raise forms.ValidationError(_('This team is full.'))
        if self.team and self.team.invitations.filter(invitee=user, status=TeamInvitation.Status.PENDING).exists():
            raise forms.ValidationError(_('An invitation is already pending for this user.'))
        return user
