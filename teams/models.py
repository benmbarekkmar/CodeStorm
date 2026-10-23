from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


class Team(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', _('Active')
        ARCHIVED = 'archived', _('Archived')

    class Visibility(models.TextChoices):
        PUBLIC = 'public', _('Public')
        PRIVATE = 'private', _('Private')

    name = models.CharField(_('Name'), max_length=120)
    slug = models.SlugField(_('Slug'), max_length=140, unique=True, blank=True)
    description = models.TextField(_('Description'), blank=True)
    required_skills = models.TextField(_('Required skills'), blank=True, default='')
    interests = models.TextField(_('Team interests'), blank=True, default='')
    hackathon = models.ForeignKey(
        'hackathons.Hackathon',
        verbose_name=_('Hackathon'),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='teams',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('Created by'),
        on_delete=models.PROTECT,
        related_name='teams_created',
    )
    captain = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('Captain'),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='teams_captaining',
    )
    status = models.CharField(
        _('Status'), max_length=20, choices=Status.choices, default=Status.ACTIVE,
    )
    visibility = models.CharField(
        _('Visibility'), max_length=10, choices=Visibility.choices, default=Visibility.PUBLIC,
    )
    max_members = models.PositiveIntegerField(_('Max members'), default=5)
    allow_join_requests = models.BooleanField(_('Allow join requests'), default=True)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='TeamMembership', related_name='teams',
    )

    class Meta:
        verbose_name = _('Team')
        verbose_name_plural = _('Teams')
        ordering = ('-created_at',)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:130] or 'team'
            slug, n = base, 1
            while Team.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                n += 1
                slug = f'{base}-{n}'[:140]
            self.slug = slug
        super().save(*args, **kwargs)

    # --- Queries ---------------------------------------------------------
    def member_count(self):
        return self.memberships.count()

    def remaining_slots(self):
        return max(self.max_members - self.member_count(), 0)

    def is_full(self):
        return self.member_count() >= self.max_members

    def has_member(self, user):
        if not user or not user.is_authenticated:
            return False
        return self.memberships.filter(user=user).exists()

    def membership_for(self, user):
        return self.memberships.filter(user=user).first()

    def is_captain(self, user):
        return bool(user and user.is_authenticated and self.captain_id == user.pk)

    def can_manage(self, user):
        return self.is_captain(user)

    def is_joinable(self, user):
        """Server-side eligibility for a participant joining this team.

        This is the single source of truth reused by the request-join view and
        the captain's approval, so every join path enforces the same rules.
        """
        if self.status != self.Status.ACTIVE:
            return False, _('This team is not open for joining.')
        if self.visibility != self.Visibility.PUBLIC:
            return False, _('This team is private; an invitation is required.')
        if not self.allow_join_requests:
            return False, _('This team does not accept join requests.')
        if self.hackathon and not self.hackathon.accepts_participants:
            return False, _('The hackathon is no longer accepting participants.')
        if self.is_full():
            return False, _('This team is full.')
        if self.has_member(user):
            return False, _('You are already a member of this team.')
        if self.hackathon and not self.hackathon.allow_multiple_teams:
            if Team.objects.filter(
                hackathon=self.hackathon, memberships__user=user,
            ).exists():
                return False, _('You already have a team in this hackathon.')
        return True, ''

    # --- Mutations (call inside a transaction) ---------------------------
    @transaction.atomic
    def add_member(self, user, role=None):
        if role is None:
            role = TeamMembership.Role.MEMBER
        membership, created = TeamMembership.objects.select_related('team').get_or_create(
            team=self, user=user, defaults={'role': role},
        )
        if not created and membership.role != role:
            membership.role = role
            membership.save(update_fields=['role'])
        return membership

    @transaction.atomic
    def lock_for_capacity(self):
        """Row lock so concurrent joins cannot exceed capacity."""
        return Team.objects.select_for_update().get(pk=self.pk)


class TeamMembership(models.Model):
    class Role(models.TextChoices):
        CAPTAIN = 'captain', _('Captain')
        MEMBER = 'member', _('Member')

    team = models.ForeignKey(Team, verbose_name=_('Team'), on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('User'), on_delete=models.CASCADE, related_name='memberships',
    )
    role = models.CharField(_('Role'), max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(_('Joined at'), auto_now_add=True)

    class Meta:
        verbose_name = _('Team membership')
        verbose_name_plural = _('Team memberships')
        constraints = [
            models.UniqueConstraint(fields=['team', 'user'], name='uniq_team_member'),
        ]
        ordering = ('joined_at',)

    def __str__(self):
        return f'{self.user} @ {self.team} ({self.get_role_display()})'


class TeamInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        ACCEPTED = 'accepted', _('Accepted')
        DECLINED = 'declined', _('Declined')
        CANCELLED = 'cancelled', _('Cancelled')
        EXPIRED = 'expired', _('Expired')

    team = models.ForeignKey(Team, verbose_name=_('Team'), on_delete=models.CASCADE, related_name='invitations')
    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('Inviter'),
        on_delete=models.PROTECT, related_name='team_invitations_sent',
    )
    invitee = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('Invitee'),
        on_delete=models.PROTECT, related_name='team_invitations_received',
    )
    message = models.TextField(_('Message'), blank=True)
    status = models.CharField(_('Status'), max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    expires_at = models.DateTimeField(_('Expires at'), null=True, blank=True)
    responded_at = models.DateTimeField(_('Responded at'), null=True, blank=True)

    class Meta:
        verbose_name = _('Team invitation')
        verbose_name_plural = _('Team invitations')
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'invitee'],
                condition=models.Q(status='pending'),
                name='uniq_pending_invitation',
            ),
        ]

    def __str__(self):
        return f'{self.invitee} -> {self.team}'

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    def is_valid(self):
        return self.status == self.Status.PENDING and not self.is_expired

    def mark_expired_if_due(self):
        if self.status == self.Status.PENDING and self.is_expired:
            self.status = self.Status.EXPIRED
            self.responded_at = timezone.now()
            self.save(update_fields=['status', 'responded_at'])
        return self.status


class JoinRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        ACCEPTED = 'accepted', _('Accepted')
        REJECTED = 'rejected', _('Rejected')
        CANCELLED = 'cancelled', _('Cancelled')

    team = models.ForeignKey(Team, verbose_name=_('Team'), on_delete=models.CASCADE, related_name='join_requests')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_('User'), on_delete=models.CASCADE, related_name='join_requests',
    )
    message = models.TextField(_('Message'), blank=True)
    status = models.CharField(_('Status'), max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    responded_at = models.DateTimeField(_('Responded at'), null=True, blank=True)

    class Meta:
        verbose_name = _('Join request')
        verbose_name_plural = _('Join requests')
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'user'],
                condition=models.Q(status='pending'),
                name='uniq_pending_join_request',
            ),
        ]

    def __str__(self):
        return f'{self.user} requests {self.team}'
