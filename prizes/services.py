"""Business rules for preparing and validating official prize results."""

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from evaluations.models import EvaluationCriterion
from hackathons.models import Hackathon
from projects.models import Project

from .models import LeaderboardEntry, Prize, PrizeAward


class LeaderboardNotReady(Exception):
    """Raised when current evaluations are insufficient for a reliable ranking."""


def _two_decimals(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def compute_leaderboard(hackathon):
    """Return the ranking using Evaluation's existing weighted-score formula.

    The evaluation app has no separate jury-assignment table, so the evaluations
    already recorded for each submitted project are the assignments this service
    can validate. Every recorded evaluation must be complete and cover the full
    rubric before a result can be prepared.
    """
    criteria = list(EvaluationCriterion.objects.filter(hackathon=hackathon))
    if not criteria:
        raise LeaderboardNotReady(_('Add evaluation criteria before preparing results.'))
    if any(criterion.weight < 0 or criterion.weight > 100 for criterion in criteria):
        raise LeaderboardNotReady(_('The evaluation rubric contains an invalid weight.'))
    if len({criterion.name.casefold() for criterion in criteria}) != len(criteria):
        raise LeaderboardNotReady(_('The evaluation rubric contains duplicate criterion names.'))

    projects = list(
        Project.objects.filter(
            hackathon=hackathon,
            status__in=(
                Project.Status.SUBMITTED,
                Project.Status.UNDER_REVIEW,
                Project.Status.EVALUATED,
            ),
        ).select_related('team').order_by('pk')
    )
    if not projects:
        raise LeaderboardNotReady(_('There are no submitted projects to rank.'))

    expected_criterion_ids = {criterion.pk for criterion in criteria}
    project_scores = []
    for project in projects:
        if project.status != Project.Status.EVALUATED:
            raise LeaderboardNotReady(
                _('Project “%(project)s” has not been marked as evaluated.') % {
                    'project': project.title
                }
            )

        evaluations = list(
            project.evaluations.prefetch_related('scores__criterion').all()
        )
        if not evaluations:
            raise LeaderboardNotReady(
                _('Project “%(project)s” has no jury evaluations.') % {
                    'project': project.title
                }
            )

        jury_scores = []
        breakdown = {criterion.name: [] for criterion in criteria}
        for evaluation in evaluations:
            if not evaluation.is_complete:
                raise LeaderboardNotReady(
                    _('At least one evaluation for “%(project)s” is still incomplete.') % {
                        'project': project.title
                    }
                )

            score_rows = list(evaluation.scores.select_related('criterion'))
            score_criterion_ids = {row.criterion_id for row in score_rows}
            if (score_criterion_ids != expected_criterion_ids or
                    any(row.criterion.hackathon_id != hackathon.pk for row in score_rows) or
                    any(row.score < 0 or row.score > 10 for row in score_rows)):
                raise LeaderboardNotReady(
                    _('An evaluation for “%(project)s” does not cover this hackathon’s full rubric.') % {
                        'project': project.title
                    }
                )

            # Keep the Evaluation model's formula as the single source of truth.
            weighted_score = evaluation.get_weighted_score()
            if weighted_score is None:
                raise LeaderboardNotReady(
                    _('An evaluation for “%(project)s” has no usable weighted score.') % {
                        'project': project.title
                    }
                )
            jury_scores.append(Decimal(str(weighted_score)))
            for row in score_rows:
                breakdown[row.criterion.name].append(Decimal(str(row.score)))

        average_score = _two_decimals(sum(jury_scores) / len(jury_scores))
        average_breakdown = {
            name: str(_two_decimals(sum(values) / len(values)))
            for name, values in breakdown.items()
        }
        project_scores.append({
            'project': project,
            'score': average_score,
            'breakdown': average_breakdown,
        })

    project_scores.sort(key=lambda item: (-item['score'], item['project'].pk))
    ranked = []
    previous_score = None
    previous_rank = None
    for index, item in enumerate(project_scores, start=1):
        rank = previous_rank if item['score'] == previous_score else index
        ranked.append({**item, 'rank': rank})
        previous_score = item['score']
        previous_rank = rank
    return ranked


@transaction.atomic
def save_leaderboard(hackathon, ranked):
    """Replace the draft ranking in one transaction."""
    locked_hackathon = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
    if locked_hackathon.results_published:
        raise LeaderboardNotReady(_('Unpublish the results before changing the draft ranking.'))
    if any(item['project'].hackathon_id != locked_hackathon.pk for item in ranked):
        raise LeaderboardNotReady(_('A project in this ranking belongs to another hackathon.'))
    LeaderboardEntry.objects.filter(hackathon=hackathon).delete()
    LeaderboardEntry.objects.bulk_create([
        LeaderboardEntry(
            hackathon=hackathon,
            project=item['project'],
            rank=item['rank'],
            final_score=item['score'],
            score_breakdown=item['breakdown'],
        )
        for item in ranked
    ])


def validate_saved_leaderboard(hackathon, ranked):
    """Reject publishing a draft that no longer matches completed evaluations."""
    saved = list(
        LeaderboardEntry.objects.filter(hackathon=hackathon)
        .order_by('project_id').values_list('project_id', 'rank', 'final_score')
    )
    expected = sorted(
        (item['project'].pk, item['rank'], item['score']) for item in ranked
    )
    if len(saved) != len(expected):
        raise LeaderboardNotReady(
            _('The saved ranking is out of date. Prepare it again before publishing.')
        )
    for actual, wanted in zip(saved, expected):
        project_id, rank, score = actual
        expected_project_id, expected_rank, expected_score = wanted
        if (project_id != expected_project_id or rank != expected_rank or
                score != expected_score):
            raise LeaderboardNotReady(
                _('The saved ranking is out of date. Prepare it again before publishing.')
            )


def validate_prize_awards(hackathon):
    """Validate prize assignments before results become official."""
    prizes = Prize.objects.filter(hackathon=hackathon).prefetch_related('awards__project')
    ranked_project_ids = set(
        LeaderboardEntry.objects.filter(hackathon=hackathon)
        .values_list('project_id', flat=True)
    )
    for prize in prizes:
        awards = list(prize.awards.all())
        positions = [award.position for award in awards]
        if prize.status in (Prize.Status.ASSIGNED, Prize.Status.AWARDED) and not awards:
            raise LeaderboardNotReady(
                _('Prize “%(prize)s” is marked assigned but has no winning project.') % {
                    'prize': prize.name
                }
            )
        if awards and prize.status == Prize.Status.AVAILABLE:
            raise LeaderboardNotReady(
                _('Prize “%(prize)s” has winning projects but is still marked available.') % {
                    'prize': prize.name
                }
            )
        if len(awards) > prize.winner_slots:
            raise LeaderboardNotReady(
                _('Prize “%(prize)s” has more winners than its configured slots.') % {
                    'prize': prize.name
                }
            )
        for award in awards:
            if award.position < 1 or award.position > prize.winner_slots:
                raise LeaderboardNotReady(
                    _('Prize “%(prize)s” has a winner outside its configured slots.') % {
                        'prize': prize.name
                    }
                )
            if (award.project.hackathon_id != hackathon.pk or
                    award.project_id not in ranked_project_ids):
                raise LeaderboardNotReady(
                    _('Prize “%(prize)s” is assigned to a project outside the saved ranking.') % {
                        'prize': prize.name
                    }
                )
        if awards and positions != list(range(1, len(awards) + 1)):
            raise LeaderboardNotReady(
                _('Prize “%(prize)s” has gaps in its winner slots.') % {
                    'prize': prize.name
                }
            )
        if awards and prize.winning_project_id != awards[0].project_id:
            raise LeaderboardNotReady(
                _('Prize “%(prize)s” has inconsistent primary winner data.') % {
                    'prize': prize.name
                }
            )
        if not awards and prize.winning_project_id:
            raise LeaderboardNotReady(
                _('Prize “%(prize)s” has a legacy winner without an award assignment.') % {
                    'prize': prize.name
                }
            )
        if prize.status == Prize.Status.AWARDED and not hackathon.results_published:
            raise LeaderboardNotReady(
                _('Prize “%(prize)s” is marked awarded before results are published.') % {
                    'prize': prize.name
                }
            )
