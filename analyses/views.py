from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from analyses import permissions
from analyses.forms import AnalysisRequestForm
from analyses.models import AIReport
from analyses.rubrics import rubric_for_project
from analyses.services.analyzer import analyze_repository
from analyses.services.errors import AnalysisError, LLMConfigError
from analyses.services.llm import GeminiClient

# Access: an organizer works on the hackathons they run, a jury member reads and
# launches analyses for the projects they are allowed to score, a participant
# never reaches this module. Scoping is delegated to
# ``analyses.permissions``, which reuses the project visibility rules already
# applied by ``projects``.


def analysis_access_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not permissions.has_module_access(request.user):
            messages.error(request, _lazy('You do not have permission to access analyses.'))
            return redirect('dashboard')
        return view(request, *args, **kwargs)
    return wrapper


def _visible_report(request, pk):
    # Ownership and scoping are enforced at the query level: a report outside
    # this user's scope answers 404, even on a direct URL.
    return get_object_or_404(permissions.visible_reports(request.user), pk=pk)


def _form_context(request, form, *, mode, error_ctx=None):
    return {
        'form': form,
        'mode': mode,
        'action_url': reverse('analyses:create'),
        'can_launch': permissions.can_request_analysis(request.user),
        'has_projects': permissions.can_analyze_anything(request.user),
        **(error_ctx or {}),
    }


def _respond(request, *, is_ajax, form, mode, extra=None):
    if is_ajax:
        return render(request, 'analyses/_analysis_form.html', _form_context(
            request, form, mode=mode, error_ctx=extra))
    return render(request, 'analyses/analysis_form.html', _form_context(
        request, form, mode=mode, error_ctx=extra))


@login_required
@analysis_access_required
def analysis_list(request):
    reports = permissions.visible_reports(request.user)
    # Prefetched relations make this loop query-free; the card only shows the
    # delete entry point for reports this user may actually remove.
    can_delete_ids = {r.pk for r in reports if permissions.can_delete(request.user, r)}
    return render(request, 'analyses/analysis_list.html', {
        'reports': reports,
        'can_delete_ids': can_delete_ids,
        'form': AnalysisRequestForm(user=request.user),
        'action_create': reverse('analyses:create'),
        'can_launch': permissions.can_request_analysis(request.user),
        'has_projects': permissions.can_analyze_anything(request.user),
    })


@login_required
@analysis_access_required
def analysis_detail(request, pk):
    report = _visible_report(request, pk)
    return render(request, 'analyses/analysis_detail.html', {
        'report': report,
        'can_delete': permissions.can_delete(request.user, report),
        'can_relaunch': (
            report.project_id is not None
            and permissions.can_launch(request.user, report.project)
            and report.status != AIReport.Status.RUNNING
        ),
    })


def _run_analysis(request, *, user, project, repository_url):
    """Create the report row, run the analysis, persist exactly what happened.

    Returns the report; a failure is stored as ``failed`` with a typed,
    human-readable message and never as a successful report.
    """
    try:
        client = GeminiClient()
        if not client.api_key or not client.model:
            raise LLMConfigError()
    except LLMConfigError:
        messages.error(request, _lazy(
            'The AI service is not configured. Set the GEMINI_API_KEY and '
            'GEMINI_MODEL environment variables before launching an analysis.'))
        return None

    choice = rubric_for_project(project)
    report = AIReport.objects.create(
        created_by=user, project=project, repository_url=repository_url,
        status=AIReport.Status.RUNNING, model_id=client.model,
    )
    try:
        bundle, data = analyze_repository(
            repository_url, llm_client=client,
            specs=list(choice.specs), score_max=choice.score_max,
        )
        data['rubric_source'] = choice.source
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
    return report


@login_required
@analysis_access_required
def analysis_create(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if request.method == 'POST':
        form = AnalysisRequestForm(request.POST, user=request.user)
        if not form.is_valid():
            return _respond(request, is_ajax=is_ajax, form=form, mode='create')
        project = form.cleaned_data['project']
        repository_url = form.cleaned_data['repository_url']
        # The form queryset already restricts the choices to projects this user
        # may analyze; the check is repeated so the rule lives in one place.
        if not permissions.can_launch(request.user, project):
            messages.error(request, _lazy(
                'You do not have permission to access analyses.'))
            return redirect('analyses:home')
        report = _run_analysis(
            request, user=request.user, project=project,
            repository_url=repository_url)
        if report is None:
            return _respond(request, is_ajax=is_ajax, form=AnalysisRequestForm(
                {'project': project.pk}, user=request.user), mode='create')
        detail_url = reverse('analyses:detail', args=[report.pk])
        if is_ajax:
            return JsonResponse({'redirect': detail_url})
        return redirect(detail_url)

    # GET: a fresh form, optionally pre-selected from the project page.
    initial = {}
    project_id = request.GET.get('project')
    if project_id and str(project_id).isdigit():
        project = permissions.analyzable_projects(request.user).filter(pk=project_id).first()
        if project is not None:
            initial['project'] = project
    form = AnalysisRequestForm(user=request.user, initial=initial)
    return _respond(request, is_ajax=is_ajax, form=form, mode='create')


@login_required
@analysis_access_required
def analysis_relaunch(request, pk):
    """Re-analyze the same repository on an explicit request, as a new report.

    The previous report stays readable: a jury decision may already rest on it,
    so a relaunch adds history instead of overwriting it.
    """
    if request.method != 'POST':
        return redirect('analyses:detail', pk=pk)
    report = _visible_report(request, pk)
    project = report.project
    if project is None:
        # A report recorded before the project link existed has no submission to
        # re-read, so re-analyzing it would mean inventing a repository.
        messages.error(request, _lazy(
            'This report is not linked to a project, so it cannot be re-analyzed. '
            'Launch the analysis from the project page instead.'))
        return redirect('analyses:detail', pk=report.pk)
    if not permissions.can_launch(request.user, project):
        messages.error(request, _lazy('You do not have permission to access analyses.'))
        return redirect('analyses:detail', pk=report.pk)
    if report.status == AIReport.Status.RUNNING:
        messages.error(request, _lazy('An analysis is already running for this repository.'))
        return redirect('analyses:detail', pk=report.pk)
    new_report = _run_analysis(
        request, user=request.user, project=project,
        repository_url=report.repository_url)
    if new_report is None:
        return redirect('analyses:detail', pk=report.pk)
    return redirect('analyses:detail', new_report.pk)


@login_required
@analysis_access_required
def analysis_delete(request, pk):
    report = _visible_report(request, pk)
    if not permissions.can_delete(request.user, report):
        messages.error(request, _lazy('You do not have permission to access analyses.'))
        return redirect('analyses:detail', pk=report.pk)
    if request.method != 'POST':
        return render(request, 'analyses/analysis_confirm.html', {'report': report})
    report.delete()
    messages.success(request, _lazy('Report deleted.'))
    return redirect('analyses:home')
