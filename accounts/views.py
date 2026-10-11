from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

from accounts.forms import (
    AppearanceForm,
    ChangePasswordForm,
    NotificationForm,
    PersonalInfoForm,
    RegisterForm,
)


def register(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect('dashboard')

    return render(request, 'register.html', {'form': form})


@login_required
def profile_settings(request):
    user = request.user
    embed = request.GET.get('embed')
    base_query = '?embed=1' if embed else '?'

    personal_form = PersonalInfoForm(instance=user, prefix='p')
    password_form = ChangePasswordForm(user, prefix='pw')
    notification_form = NotificationForm(instance=user, prefix='n')
    appearance_form = AppearanceForm(instance=user, prefix='a')

    active_tab = 'personal'

    if request.method == 'POST':
        section = request.POST.get('section')
        if section == 'personal':
            active_tab = 'personal'
            personal_form = PersonalInfoForm(request.POST, request.FILES, instance=user, prefix='p')
            if personal_form.is_valid():
                personal_form.save()
                messages.success(request, _('Your personal information has been updated.'))
                return redirect(f'{request.path}{base_query}')
        elif section == 'password':
            active_tab = 'security'
            password_form = ChangePasswordForm(user, request.POST, prefix='pw')
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, _('Your password has been changed successfully.'))
                return redirect(f'{request.path}{base_query}')
        elif section == 'notifications':
            active_tab = 'notifications'
            notification_form = NotificationForm(request.POST, instance=user, prefix='n')
            if notification_form.is_valid():
                notification_form.save()
                messages.success(request, _('Your notification preferences have been saved.'))
                return redirect(f'{request.path}{base_query}')
        elif section == 'appearance':
            active_tab = 'appearance'
            appearance_form = AppearanceForm(request.POST, instance=user, prefix='a')
            if appearance_form.is_valid():
                appearance_form.save()
                messages.success(request, _('Your appearance settings have been applied.'))
                return redirect(f'{request.path}{base_query}')

    context = {
        'profile_user': user,
        'personal_form': personal_form,
        'password_form': password_form,
        'notification_form': notification_form,
        'appearance_form': appearance_form,
        'active_tab': active_tab,
    }
    return render(request, 'profile_settings.html', context)
