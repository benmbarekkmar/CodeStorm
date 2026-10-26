from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Resource(models.Model):
    """A curated learning/utility resource shown on the Resources page.

    Intentionally small and self-contained: it reuses the existing custom User
    model for the author, and the hackathon concept is expressed through the
    ``Links`` category rather than a foreign key, so this module stays
    independent from the other apps.
    """

    class Type(models.TextChoices):
        DOCUMENTATION = 'documentation', _('Documentation')
        TUTORIAL = 'tutorial', _('Tutorial')
        TOOL = 'tool', _('Tool')
        TEMPLATE = 'template', _('Template')
        VIDEO = 'video', _('Video')

    class Category(models.TextChoices):
        AI_ML = 'ai_ml', _('AI & Machine Learning')
        WEB = 'web', _('Web Development')
        DESIGN = 'design', _('Design')
        SECURITY = 'security', _('Cybersecurity')
        BUSINESS = 'business', _('Business')
        LINKS = 'links', _('Useful Hackathon Links')
        GENERAL = 'general', _('General')

    # Material Symbols icon shown on cards when no image is attached.
    ICONS = {
        Type.DOCUMENTATION: 'description',
        Type.TUTORIAL: 'school',
        Type.TOOL: 'build',
        Type.TEMPLATE: 'dashboard_customize',
        Type.VIDEO: 'videocam',
    }

    title = models.CharField(_('Title'), max_length=200)
    description = models.TextField(_('Description'), blank=True)
    type = models.CharField(
        _('Type'), max_length=20, choices=Type.choices, default=Type.DOCUMENTATION,
    )
    category = models.CharField(
        _('Category'), max_length=20, choices=Category.choices, default=Category.GENERAL,
    )
    url = models.URLField(_('Link'), blank=True, help_text=_('External URL.'))
    file = models.FileField(
        _('File'), upload_to='resources/', blank=True, null=True,
        help_text=_('Optional downloadable attachment.'),
    )
    image = models.ImageField(
        _('Image'), upload_to='resources/images/', blank=True, null=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('Author'),
        on_delete=models.PROTECT,
        related_name='resources_created',
    )
    created_at = models.DateTimeField(_('Published at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

    class Meta:
        verbose_name = _('Resource')
        verbose_name_plural = _('Resources')
        ordering = ('-created_at', '-id')

    def __str__(self):
        return self.title

    @property
    def icon(self):
        return self.ICONS.get(self.type, 'menu_book')

    @property
    def author_display(self):
        return self.created_by.get_full_name() or self.created_by.username

    @property
    def has_asset(self):
        """Whether the resource can actually be opened (a link or a file)."""
        return bool(self.url or self.file)

    @property
    def asset_kind(self):
        if self.file:
            return 'file'
        if self.url:
            return 'url'
        return ''

    @property
    def action_label(self):
        """Primary CTA text, adapted to the resource type/asset."""
        if self.asset_kind == 'file':
            return _('Download')
        return {
            self.Type.VIDEO: _('Watch video'),
            self.Type.TOOL: _('Open tool'),
            self.Type.TEMPLATE: _('Get template'),
            self.Type.TUTORIAL: _('Read tutorial'),
        }.get(self.type, _('Open resource'))
