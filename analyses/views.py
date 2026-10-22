from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from analyses.forms import AnalysisRequestForm
from analyses.models import AIReport
from analyses.services.analyzer import analyze_repository
from analyses.services.errors import AnalysisError, LLMConfigError
from analyses.services.llm import GeminiClient

ORGANIZER = 'organizer'

# Access note (v1): only organizers use this module. A reliable way to scope
# which reports a given jury member may see depends on the not-yet-built
# Project/Evaluation assignment model, so reports are intentionally NOT exposed
# to jury (or participant) accounts until that integration step. This is the
# safe default rather than granting jury blanket access to every report.


def organizer_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if getattr(request.user, 'role', None) != ORGANIZER:
            messages.error(request, _lazy('You do not have permission to access analyses.'))
            return redirect('dashboard')
        return view(request, *args, **kwargs)
    return wrapper


def _report_or_404(request, pk):
    # Ownership is enforced at the query level: organizers only ever reach their
    # own reports, including on direct URL access.
    return get_object_or_404(AIReport, pk=pk, created_by=request.user)


@login_required
@organizer_required
def analysis_list(request):
    reports = AIReport.objects.filter(created_by=request.user)
    return render(request, 'analyses/analysis_list.html', {
        'reports': reports,
        'form': AnalysisRequestForm(),
        'action_create': reverse('analyses:create'),
    })


@login_required
@organizer_required
def analysis_detail(request, pk):
    report = _report_or_404(request, pk)
    return render(request, 'analyses/analysis_detail.html', {'report': report})


@login_required
@organizer_required
def analysis_create(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def respond(error_ctx, status=200):
        error_ctx.setdefault('mode', 'create')
        error_ctx.setdefault('action_url', reverse('analyses:create'))
        if is_ajax:
            return render(request, 'analyses/_analysis_form.html', error_ctx, status=status)
        return render(request, 'analyses/analysis_form.html', {
            **error_ctx, 'mode': 'create', 'action_url': reverse('analyses:create'),
        }, status=status)

    if request.method != 'POST':
        return respond({'form': AnalysisRequestForm()})

    form = AnalysisRequestForm(request.POST)
    if not form.is_valid():
        return respond({'form': form})

    repository_url = form.cleaned_data['repository_url']

    # Fail fast, before creating any row, if the AI service is unconfigured.
    try:
        client = GeminiClient()
        if not client.api_key or not client.model:
            raise LLMConfigError()
    except LLMConfigError:
        messages.error(request, _lazy(
            'The AI service is not configured. Set the GEMINI_API_KEY and '
            'GEMINI_MODEL environment variables before launching an analysis.'))
        return respond({'form': AnalysisRequestForm(initial={'repository_url': repository_url})})

    report = AIReport.objects.create(
        created_by=request.user, repository_url=repository_url,
        status=AIReport.Status.RUNNING, model_id=client.model,
    )
    try:
        bundle, data = analyze_repository(repository_url, llm_client=client)
        report.commit_ref = bundle.commit_ref
        report.file_count = len(bundle.files)
        report.analyzed_bytes = bundle.total_bytes
        report.report = data
        report.status = AIReport.Status.DONE
        report.save()
        messages.success(request, _lazy('Analysis completed.'))
    except AnalysisError as exc:
        safe_message = getattr(exc, 'message', None) or str(exc)
        report.status = AIReport.Status.FAILED
        report.error_message = safe_message
        report.save()
        messages.error(request, _(safe_message))
    detail_url = reverse('analyses:detail', args=[report.pk])
    if is_ajax:
        return JsonResponse({'redirect': detail_url})
    return redirect(detail_url)


@login_required
@organizer_required
def analysis_delete(request, pk):
    report = _report_or_404(request, pk)
    if request.method != 'POST':
        return render(request, 'analyses/analysis_confirm.html', {'report': report})
    report.delete()
    messages.success(request, _lazy('Report deleted.'))
    return redirect('analyses:home')
