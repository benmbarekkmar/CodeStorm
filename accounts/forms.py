from django.contrib.auth.forms import UserCreationForm, PasswordChangeForm
from django import forms

from accounts.models import User

INPUT_CLASSES = (
    'field-input w-full bg-surface-container-lowest rounded-lg px-4 py-3 '
    'font-body-md text-body-md text-on-surface border border-transparent '
    'focus:border-primary/40 focus:outline-none focus:ring-2 focus:ring-primary/30 '
    'shadow-[0_1px_8px_rgba(0,0,0,0.04)] placeholder:text-outline'
)


class RegisterForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'role')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Username'
        self.fields['username'].widget.attrs.update({'placeholder': 'kmar_mbarek', 'autocomplete': 'username'})
        self.fields['first_name'].label = 'First name'
        self.fields['first_name'].widget.attrs.update({'placeholder': 'Kmar'})
        self.fields['last_name'].label = 'Last name'
        self.fields['last_name'].widget.attrs.update({'placeholder': 'Ben Mbarek'})
        self.fields['email'].label = 'Email'
        self.fields['email'].widget.attrs.update({'placeholder': 'kmar@example.com', 'autocomplete': 'email'})
        self.fields['role'].label = 'I am a'
        self.fields['role'].empty_label = None
        self.fields['password1'].label = 'Password'
        self.fields['password2'].label = 'Confirm password'
        self.fields['password1'].widget.attrs.update({'placeholder': '••••••••', 'autocomplete': 'new-password'})
        self.fields['password2'].widget.attrs.update({'placeholder': '••••••••', 'autocomplete': 'new-password'})

        for name, field in self.fields.items():
            field.help_text = None
            extra = ' pr-12' if name in ('password1', 'password2') else ''
            field.widget.attrs['class'] = INPUT_CLASSES + extra


PROFILE_INPUT = (
    'w-full bg-surface-container-lowest text-on-surface font-body-md text-body-md '
    'rounded-lg px-3.5 py-2.5 shadow-sm border border-transparent '
    'focus:border-primary/40 focus:outline-none focus:ring-2 focus:ring-primary/25 '
    'transition-colors placeholder:text-outline'
)

LANGUAGE_CHOICES = [
    ('en-US', 'English (United States)'),
    ('fr-FR', 'Français (France)'),
    ('ar-TN', 'العربية (Tunisie)'),
]


class PersonalInfoForm(forms.ModelForm):
    class Meta:
        model = User
        fields = (
            'first_name', 'last_name', 'email', 'username', 'phone',
            'institution', 'field_of_study', 'bio', 'avatar',
        )
        widgets = {
            'bio': forms.Textarea(attrs={'rows': 3, 'maxlength': 250}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholders = {
            'first_name': 'Kmar',
            'last_name': 'Ben Mbarek',
            'email': 'kmar@example.com',
            'username': 'kmar_mbarek',
            'phone': '+216 22 123 456',
            'institution': 'Polytechnic Institute of Technology',
            'field_of_study': 'Computer Science & AI',
            'bio': 'Tell the community what you build…',
        }
        for name, field in self.fields.items():
            field.help_text = None
            field.required = name in ('first_name', 'last_name', 'email', 'username')
            widget = field.widget
            base = PROFILE_INPUT
            if name in placeholders:
                widget.attrs['placeholder'] = placeholders[name]
            if name == 'bio':
                base += ' resize-none'
            if name == 'avatar':
                base = PROFILE_INPUT.replace('py-2.5', 'py-2 px-3 text-sm file:mr-3 file:rounded-md file:border-0 file:bg-primary-container file:text-on-primary-container file:px-3 file:py-1.5 file:font-semibold')
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = f'{existing} {base}'.strip()
        self.fields['institution'].label = 'University / Institution'
        self.fields['field_of_study'].label = 'Field of study / Specialization'


class ChangePasswordForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholders = {
            'old_password': 'Current password',
            'new_password1': 'Enter new password',
            'new_password2': 'Re-type new password',
        }
        for name, field in self.fields.items():
            field.help_text = None
            field.widget.attrs['class'] = PROFILE_INPUT
            if name in placeholders:
                field.widget.attrs['placeholder'] = placeholders[name]


class NotificationForm(forms.ModelForm):
    class Meta:
        model = User
        fields = (
            'notif_in_app', 'notif_email', 'notif_hackathon', 'notif_team',
            'notif_deadlines', 'notif_eval', 'notif_prizes', 'notif_weekly',
        )
        labels = {
            'notif_in_app': 'In-App Notifications',
            'notif_email': 'Email Notifications',
            'notif_hackathon': 'Hackathon Announcements',
            'notif_team': 'Team Invitations',
            'notif_deadlines': 'Project Deadline Reminders',
            'notif_eval': 'Evaluation Results',
            'notif_prizes': 'Prizes & Certificates',
            'notif_weekly': 'Weekly Summary Email',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.help_text = None
            field.widget.attrs['class'] = 'sr-only peer'


class AppearanceForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('theme', 'accent', 'density', 'language')
        widgets = {
            'theme': forms.HiddenInput(),
            'accent': forms.HiddenInput(),
            'density': forms.HiddenInput(),
            'language': forms.Select(choices=LANGUAGE_CHOICES, attrs={'class': PROFILE_INPUT}),
        }
