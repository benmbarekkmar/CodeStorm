"""Explainable participant <-> team compatibility scoring.

This module is intentionally independent of Django views, URLs and templates:
it only *reads* model instances and never writes to the database. It exposes a
pure scoring engine that can be reused by a recommendation view later.

Scoring model (heuristic, NOT a prediction of a team's future success):
    final = round(
        SKILLS_WEIGHT        * skills_score
      + INTERESTS_WEIGHT     * interests_score
      + COMPLEMENTARITY_WEIGHT * complementarity_score
    )

Each component score is a normalised value in ``0..100`` (a percentage of what
the team asked for that the participant satisfies). The weighted contribution
(``component_score * weight``) is what actually feeds the final ``0..100``
``score``. Both are returned separately so callers never confuse them.

Matching is a plain, case-insensitive set comparison on comma-separated text
fields. No synonym table is consulted: "JS" and "JavaScript" are treated as
different skills unless the underlying data already says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Weights are the initial, adjustable heuristic. Keep them summing to 1.0.
SKILLS_WEIGHT = 0.5
INTERESTS_WEIGHT = 0.3
COMPLEMENTARITY_WEIGHT = 0.2

_SKILLS_MISSING = 'The team has not listed any required skills yet.'
_INTERESTS_MISSING = 'The team has not listed any interests yet.'
_ALL_SKILLS_COVERED = 'The team already covers every required skill with its current members.'
_INSUFFICIENT_TEAM_DATA = (
    'The team provides neither required skills nor interests, so this score is '
    'not a reliable assessment.'
)


def normalize(raw: str | None) -> list[str]:
    """Split a comma-separated field into an ordered, de-duplicated, lower-cased
    list of trimmed tokens. Empty values are dropped. The original stored string
    is never mutated (this only reads it)."""
    if not raw:
        return []
    seen: set[str] = set()
    tokens: list[str] = []
    for part in str(raw).split(','):
        token = part.strip().lower()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


@dataclass
class MatchResult:
    """Full, explainable outcome of scoring one participant against one team.

    * ``skills_score`` / ``interests_score`` / ``complementarity_score`` are
      normalised ``0..100`` percentages (what share of the team's requirement is
      met).
    * ``contributions`` maps each component to its *weighted* share of the final
      ``score`` (component score * its weight).
    * ``score`` is the final integer ``0..100``.
    * ``sufficient_data`` is ``False`` when the team declares neither required
      skills nor interests; the score is then a placeholder, not an assessment.
    """

    team_id: int | None
    user_id: int | None
    score: int
    skills_score: float
    interests_score: float
    complementarity_score: float
    contributions: dict[str, float]
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    uncovered_team_skills: list[str] = field(default_factory=list)
    matched_interests: list[str] = field(default_factory=list)
    sufficient_data: bool = True
    explanations: list[str] = field(default_factory=list)

    def to_context(self) -> dict:
        """Flatten the result for a template context without exposing internals."""
        return {
            'score': self.score,
            'skills_score': round(self.skills_score),
            'interests_score': round(self.interests_score),
            'complementarity_score': round(self.complementarity_score),
            'matched_skills': self.matched_skills,
            'missing_skills': self.missing_skills,
            'uncovered_team_skills': self.uncovered_team_skills,
            'matched_interests': self.matched_interests,
            'sufficient_data': self.sufficient_data,
            'explanations': self.explanations,
        }


def _member_skill_sets(team, exclude_user_id=None, preloaded=None):
    """Return the set of skills covered by the team's *current members*.

    When ``preloaded`` (a mapping team_id -> iterable of users, built by
    :func:`rank_teams`) is supplied it is reused so callers avoid a per-team
    query. Otherwise a single ``select_related`` query loads members with their
    user rows (no N+1). ``exclude_user_id`` drops the participant themself so a
    member is never credited for their own skills.
    """
    if preloaded is not None:
        users = preloaded.get(getattr(team, 'pk', None), ())
    else:
        users = (m.user for m in team.memberships.select_related('user'))

    covered: set[str] = set()
    for user in users:
        if exclude_user_id is not None and user.pk == exclude_user_id:
            continue
        covered.update(normalize(getattr(user, 'skills', '')))
    return covered


def score_team(user, team, *, member_skills=None) -> MatchResult:
    """Score ``user`` compatibility with ``team`` (0..100) with full breakdown.

    Reads only: ``user.skills`` / ``user.interests`` and the team's
    ``required_skills`` / ``interests`` plus its current members' skills. Writes
    nothing to the database.

    ``member_skills`` may be passed pre-computed (a set of normalised tokens, or
    the raw iterable of member users produced by :func:`rank_teams`) to skip the
    membership lookup; when omitted the members are loaded in one efficient
    query.
    """
    user_skills = set(normalize(getattr(user, 'skills', '')))
    user_interests = set(normalize(getattr(user, 'interests', '')))

    required = normalize(getattr(team, 'required_skills', ''))
    team_interests = normalize(getattr(team, 'interests', ''))

    if member_skills is None:
        covered = _member_skill_sets(team, exclude_user_id=getattr(user, 'pk', None))
    else:
        # Accept either a ready set or an iterable of member users.
        if isinstance(member_skills, set):
            covered = member_skills
        else:
            covered = set()
            for member in member_skills:
                covered.update(normalize(getattr(member, 'skills', '')))

    explanations: list[str] = []

    # A. Skills match (50%): share of the team's required skills the user has.
    if required:
        matched_skills = [s for s in required if s in user_skills]
        missing_skills = [s for s in required if s not in user_skills]
        skills_score = 100.0 * len(matched_skills) / len(required)
    else:
        matched_skills, missing_skills = [], []
        skills_score = 0.0
        explanations.append(_SKILLS_MISSING)

    # B. Interests match (30%): share of the team's interests the user shares.
    if team_interests:
        matched_interests = [s for s in team_interests if s in user_interests]
        interests_score = 100.0 * len(matched_interests) / len(team_interests)
    else:
        matched_interests = []
        interests_score = 0.0
        explanations.append(_INTERESTS_MISSING)

    # C. Complementarity (20%): how the user fills skills still missing from the
    # current members. 0 when nothing is required or everything is covered.
    uncovered_team_skills = [s for s in required if s not in covered]
    if uncovered_team_skills:
        filled = [s for s in uncovered_team_skills if s in user_skills]
        complementarity_score = 100.0 * len(filled) / len(uncovered_team_skills)
    else:
        complementarity_score = 0.0
        if required:
            explanations.append(_ALL_SKILLS_COVERED)

    contributions = {
        'skills': SKILLS_WEIGHT * skills_score,
        'interests': INTERESTS_WEIGHT * interests_score,
        'complementarity': COMPLEMENTARITY_WEIGHT * complementarity_score,
    }
    score = int(max(0, min(100, round(sum(contributions.values())))))

    sufficient_data = bool(required or team_interests)
    if not sufficient_data:
        explanations.append(_INSUFFICIENT_TEAM_DATA)

    return MatchResult(
        team_id=getattr(team, 'pk', None),
        user_id=getattr(user, 'pk', None),
        score=score,
        skills_score=skills_score,
        interests_score=interests_score,
        complementarity_score=complementarity_score,
        contributions=contributions,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        uncovered_team_skills=uncovered_team_skills,
        matched_interests=matched_interests,
        sufficient_data=sufficient_data,
        explanations=explanations,
    )


def rank_teams(user, teams) -> list[MatchResult]:
    """Score ``user`` against many ``teams`` and return results sorted by score
    (desc), then team id for a stable order.

    Members and their skills are loaded in a *single* query shared across all
    teams, so scoring N teams does not issue N membership queries. Pass a plain
    iterable of :class:`~teams.models.Team` instances.
    """
    teams = list(teams)
    team_ids = [t.pk for t in teams]
    if not team_ids:
        return []

    # One query: every membership (with its user) for the whole candidate set.
    from teams.models import TeamMembership  # local import keeps module import-light

    preloaded: dict[int, list] = {tid: [] for tid in team_ids}
    for membership in TeamMembership.objects.filter(team_id__in=team_ids).select_related('user'):
        preloaded.setdefault(membership.team_id, []).append(membership.user)

    results = [score_team(user, team, member_skills=preloaded.get(team.pk, [])) for team in teams]
    results.sort(key=lambda r: (r.score, r.team_id if r.team_id is not None else 0), reverse=True)
    return results
