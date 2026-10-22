from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from resources.models import Resource

INPUT = (
    'w-full bg-surface-container-lowest text-on-surface font-body-md text-body-md '
    'rounded-lg px-3.5 py-2.5 shadow-sm border border-transparent '
    'focus:border-primary/40 focus:outline-none focus:ring-2 focus:ring-primary/25 '
    'transition-colors placeholder:text-outline'
)

# File policy for downloadable attachments.
ALLOWED_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'txt', 'csv', 'md', 'zip',
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_URL_SCHEMES = {'http', 'https'}


class ResourceForm(forms.ModelForm):
    class Meta:
        model = Resource
        fields = ('title', 'description', 'type', 'category', 'url', 'file', 'image')
        widgets = {
            'title': forms.TextInput(attrs={
                'placeholder': _('e.g. Supabase Edge Functions Guide'),
            }),
            'description': forms.Textarea(attrs={
                'rows': 4, 'placeholder': _('Describe what this resource offers.')}),
            'url': forms.URLInput(attrs={'placeholder': 'https://example.com/guide'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.help_text = None
            widget = field.widget
            existing = widget.attrs.get('class', '')
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'w-5 h-5 accent-primary cursor-pointer'
            elif isinstance(widget, forms.ClearableFileInput):
                widget.attrs['class'] = (
                    'w-full text-on-surface font-body-sm text-body-sm rounded-lg '
                    'bg-surface-container-low border border-outline-variant px-3 py-2'
                )
            elif isinstance(widget, forms.Select):
                widget.attrs['class'] = f'{existing} {INPUT}'.strip()
            else:
                widget.attrs['class'] = f'{existing} {INPUT}'.strip()

    def clean_title(self):
        return self.cleaned_data['title'].strip()

    def clean_url(self):
        url = (self.cleaned_data.get('url') or '').strip()
        if not url:
            return ''
        scheme = url.split(':', 1)[0].lower() if ':' in url else ''
        if scheme and scheme not in ALLOWED_URL_SCHEMES:
            raise ValidationError(_('Only http or https links are allowed.'))
        return url

    def clean_file(self):
        upload = self.cleaned_data.get('file')
        if not upload or hasattr(upload, 'path'):
            # Empty submission or an already-stored file kept unchanged.
            return upload
        ext = upload.name.rsplit('.', 1)[-1].lower() if '.' in upload.name else ''
        if ext not in ALLOWED_EXTENSIONS:
            raise ValidationError(
                _('Unsupported file type. Allowed: %(types)s.') % {'types': ', '.join(sorted(ALLOWED_EXTENSIONS))}
            )
        if upload.size > MAX_FILE_SIZE:
            raise ValidationError(
                _('The file is too large (max %(size)s MB).') % {'size': MAX_FILE_SIZE // (1024 * 1024)}
            )
        return upload

    def clean(self):
        cleaned = super().clean()
        # A resource must be openable through either a link or a file.
        if not cleaned.get('url') and not cleaned.get('file'):
            instance = self.instance
            if not (instance and (instance.pk and (instance.url or instance.file))):
                self.add_error('url', _('Provide a link or upload a file.'))
        return cleaned
