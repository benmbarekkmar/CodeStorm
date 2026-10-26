from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from resources.forms import ResourceForm
from resources.models import Resource
ORGANIZER = 'organizer'

SORT_OPTIONS = {
    'recent': '-created_at',
    'oldest': 'created_at',
    'az': 'title',
    'za': '-title',
}


# --------------------------------------------------------------------------
# Permission helpers
# --------------------------------------------------------------------------
def organizer_required(view):
    """Resource curation (create/edit/delete) is reserved for organizers."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if getattr(request.user, 'role', None) != ORGANIZER:
            messages.error(request, _('You do not have permission to manage resources.'))
            return redirect('resources:home')
        return view(request, *args, **kwargs)
    return wrapper


def _resource_or_404(pk):
    return get_object_or_404(Resource.objects.select_related('created_by'), pk=pk)


def _apply_filters(qs, request):
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    rtype = request.GET.get('type', '').strip()
    if rtype and rtype != 'all' and rtype in dict(Resource.Type.choices):
        qs = qs.filter(type=rtype)
    category = request.GET.get('category', '').strip()
    if category and category != 'all' and category in dict(Resource.Category.choices):
        qs = qs.filter(category=category)
    return qs


def _apply_sort(qs, request):
    return qs.order_by(SORT_OPTIONS.get(request.GET.get('sort', 'recent'), '-created_at'))


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------
@login_required
def resource_list(request):
    is_organizer = getattr(request.user, 'role', None) == ORGANIZER
    resources = _apply_sort(_apply_filters(Resource.objects.all(), request), request)
    context = {
        'resources': resources,
        'is_organizer': is_organizer,
        'types': Resource.Type.choices,
        'categories': Resource.Category.choices,
        'create_form': ResourceForm() if is_organizer else None,
        'action_create': reverse('resources:create'),
        'q': request.GET.get('q', ''),
        'sel_type': request.GET.get('type', 'all'),
        'sel_category': request.GET.get('category', 'all'),
        'sel_sort': request.GET.get('sort', 'recent'),
        'has_filters': bool(
            request.GET.get('q')
            or (request.GET.get('type', 'all') != 'all')
            or (request.GET.get('category', 'all') != 'all')
        ),
    }
    return render(request, 'resources/resource_list.html', context)


@login_required
def resource_detail(request, pk):
    resource = _resource_or_404(pk)
    return render(request, 'resources/resource_detail.html', {
        'resource': resource,
        'is_organizer': getattr(request.user, 'role', None) == ORGANIZER,
    })


@login_required
@organizer_required
def resource_create(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = ResourceForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        resource = form.save(commit=False)
        resource.created_by = request.user
        resource.save()
        messages.success(request, _('Resource created.'))
        if is_ajax:
            return JsonResponse({'redirect': reverse('resources:detail', args=[resource.pk])})
        return redirect('resources:detail', pk=resource.pk)
    if is_ajax:
        return render(request, 'resources/_resource_form.html', {
            'form': form, 'mode': 'create',
            'action_url': reverse('resources:create'),
        })
    return render(request, 'resources/resource_form.html', {
        'form': form, 'mode': 'create', 'action_url': reverse('resources:create'),
    })


@login_required
@organizer_required
def resource_edit(request, pk):
    resource = _resource_or_404(pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = ResourceForm(request.POST or None, request.FILES or None, instance=resource)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Resource updated.'))
        if is_ajax:
            return JsonResponse({'redirect': reverse('resources:detail', args=[resource.pk])})
        return redirect('resources:detail', pk=resource.pk)
    if is_ajax:
        return render(request, 'resources/_resource_form.html', {
            'form': form, 'mode': 'edit', 'resource': resource,
            'action_url': reverse('resources:edit', args=[resource.pk]),
        })
    return render(request, 'resources/resource_form.html', {
        'form': form, 'mode': 'edit', 'resource': resource,
        'action_url': reverse('resources:edit', args=[resource.pk]),
    })


@login_required
@organizer_required
def resource_delete(request, pk):
    resource = _resource_or_404(pk)
    if request.method != 'POST':
        return render(request, 'resources/resource_confirm.html', {'resource': resource})
    resource.delete()
    messages.success(request, _('Resource deleted.'))
    return redirect('resources:home')
