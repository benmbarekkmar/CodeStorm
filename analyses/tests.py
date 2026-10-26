import base64
import io
import json
import os
import urllib.error
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from analyses import permissions, rubrics
from analyses.forms import AnalysisRequestForm
from analyses.models import AIReport
from analyses.services import github, llm, rubric, suggestions
from analyses.services.analyzer import analyze_repository
from analyses.services.errors import (
    GitHubNotFoundError,
    GitHubPrivateError,
    GitHubRateLimitError,
    LLMConfigError,
    LLMError,
    LLMRateLimitError,
    LLMRejectedError,
    LLMTimeoutError,
    MalformedResponseError,
    NetworkError,
    OversizedRepoError,
    RequestTimeoutError,
)
from evaluations.models import Evaluation, EvaluationCriterion, EvaluationScore
from hackathons.models import Hackathon
from projects.models import Project
from teams.models import Team

User = get_user_model()
P = User.Role.PARTICIPANT
O = User.Role.ORGANIZER
J = User.Role.JURY


def make_user(username, role=P):
    return User.objects.create_user(
        username=username, email=f'{username}@ex.com', password='pass12345', role=role,
    )


def b64(text):
    return base64.b64encode(text.encode('utf-8')).decode('utf-8')


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = body.encode('utf-8') if isinstance(body, str) else body
        self._headers = headers or {}

    def read(self):
        return self._body

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code):
    return urllib.error.HTTPError(url, code, 'err', {}, None)


def http_error_body(code, error):
    """HTTPError carrying a realistic Gemini error body (``{"error": {...}}``)."""
    fp = io.BytesIO(json.dumps({'error': error}).encode('utf-8'))
    return urllib.error.HTTPError('https://generativelanguage.googleapis.com/x', code, 'err', {}, fp)


def url_opener(routes, default):
    """Build a fake ``_open`` that dispatches on substrings of the request URL."""
    def _fake(request, timeout=None):
        url = request.full_url
        for needle, payload in routes:
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(json.dumps(payload))
        if isinstance(default, Exception):
            raise default
        return FakeResponse(json.dumps(default))
    return _fake


META = {
    'private': False, 'size': 100, 'default_branch': 'main',
    'sha': 'deadbeefcafe', 'description': 'Demo app', 'language': 'Python',
}
TREE = {
    'truncated': False,
    'tree': [
        {'path': 'README.md', 'type': 'blob'},
        {'path': 'LICENSE', 'type': 'blob'},
        {'path': 'package.json', 'type': 'blob'},
        {'path': 'app.py', 'type': 'blob'},
        {'path': 'tests/test_app.py', 'type': 'blob'},
        {'path': 'node_modules/lib/index.js', 'type': 'blob'},
        {'path': 'assets/logo.png', 'type': 'blob'},
        {'path': 'requirements.txt', 'type': 'blob'},
    ],
}
CONTENTS = {'encoding': 'base64', 'content': b64('print("hi")\n')}


# --------------------------------------------------------------------------
# GitHub URL validation & file selection (no network at all)
# --------------------------------------------------------------------------
class ParseRepoUrlTests(TestCase):
    def test_valid(self):
        self.assertEqual(github.parse_repo_url('https://github.com/foo/bar'), ('foo', 'bar'))
        self.assertEqual(github.parse_repo_url('https://github.com/foo/bar.git'), ('foo', 'bar'))
        self.assertEqual(github.parse_repo_url('https://github.com/foo/bar/'), ('foo', 'bar'))

    def test_rejects_bad_urls(self):
        bad = [
            'http://github.com/foo/bar',            # not https
            'https://gitlab.com/foo/bar',           # wrong host
            'https://evil.com/foo/bar',             # malicious host
            'https://github.com:8080/foo/bar',      # port
            'https://user:***@github.com/foo/bar',  # credentials
            'https://github.com/foo/bar?token=***',    # query
            'https://github.com/foo',               # missing repo
            'https://github.com/foo/bar#frag',      # fragment
            'https://github.com/foo/..',            # traversal
            'not a url',
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(github.GitHubError):
                    github.parse_repo_url(url)


class NoNetworkOnBadHostTests(TestCase):
    def test_malicious_url_never_reaches_network(self):
        patcher = mock.patch.object(github, '_open')
        mock_open = patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(github.GitHubError):
            github.fetch_repository('https://evil.example.com/x/y')
        mock_open.assert_not_called()


class SelectCandidatePathsTests(TestCase):
    def test_filters_and_orders(self):
        paths = github.select_candidate_paths(TREE['tree'])
        # Excluded: vendor + binary.
        self.assertNotIn('node_modules/lib/index.js', paths)
        self.assertNotIn('assets/logo.png', paths)
        # Included docs/code.
        self.assertIn('README.md', paths)
        self.assertIn('app.py', paths)
        self.assertIn('requirements.txt', paths)
        # Priority docs (README/LICENSE/package.json) come before plain source.
        self.assertLess(paths.index('README.md'), paths.index('app.py'))


# --------------------------------------------------------------------------
# Repository fetch behaviour (network mocked)
# --------------------------------------------------------------------------
class FetchRepositoryTests(TestCase):
    def patch_open(self, routes, default):
        patcher = mock.patch.object(github, '_open', url_opener(routes, default))
        m = patcher.start()
        self.addCleanup(patcher.stop)
        return m

    def _standard_routes(self, **over):
        meta = dict(META); meta.update(over.get('meta', {}))
        tree = over.get('tree', TREE)
        contents = over.get('contents', CONTENTS)
        return [
            ('/git/trees/', tree),
            ('/contents/', contents),
            ('/repos/foo/bar', meta),
        ]

    def test_success_builds_bundle(self):
        self.patch_open(self._standard_routes(), META)
        bundle = github.fetch_repository('https://github.com/foo/bar')
        self.assertEqual(bundle.owner, 'foo')
        self.assertEqual(bundle.commit_ref, 'deadbeefcafe')
        self.assertTrue(bundle.files)
        self.assertNotIn('node_modules/lib/index.js', [f.path for f in bundle.files])
        context = bundle.build_context()
        self.assertIn('REPO_DATA_START', context)
        self.assertIn('REPO_DATA_END', context)

    def test_private_repo_rejected(self):
        self.patch_open(self._standard_routes(meta={'private': True}), META)
        with self.assertRaises(GitHubPrivateError):
            github.fetch_repository('https://github.com/foo/bar')

    def test_oversized_repo_rejected(self):
        self.patch_open(self._standard_routes(meta={'size': 10_000_000}), META)
        with self.assertRaises(OversizedRepoError):
            github.fetch_repository('https://github.com/foo/bar')

    def test_not_found(self):
        self.patch_open([], http_error('x', 404))
        with self.assertRaises(GitHubNotFoundError):
            github.fetch_repository('https://github.com/foo/bar')

    def test_rate_limit_via_forbidden(self):
        self.patch_open([], http_error('x', 403))
        with self.assertRaises(GitHubRateLimitError):
            github.fetch_repository('https://github.com/foo/bar')

    def test_rate_limit_via_remaining_header(self):
        def _fake(request, timeout=None):
            return FakeResponse(json.dumps(META), headers={'X-RateLimit-Remaining': '0'})
        patcher = mock.patch.object(github, '_open', _fake)
        patcher.start(); self.addCleanup(patcher.stop)
        with self.assertRaises(GitHubRateLimitError):
            github.fetch_repository('https://github.com/foo/bar')

    def test_timeout_maps(self):
        def _fake(request, timeout=None):
            raise urllib.error.URLError(TimeoutError())
        patcher = mock.patch.object(github, '_open', _fake)
        patcher.start(); self.addCleanup(patcher.stop)
        with self.assertRaises(RequestTimeoutError):
            github.fetch_repository('https://github.com/foo/bar')

    def test_file_cap(self):
        many = {'truncated': False,
                'tree': [{'path': f'src/f{i}.py', 'type': 'blob'} for i in range(50)]}
        self.patch_open(self._standard_routes(tree=many, contents=CONTENTS), META)
        with mock.patch.object(github, 'MAX_FILES', 5):
            bundle = github.fetch_repository('https://github.com/foo/bar')
        self.assertLessEqual(len(bundle.files), 5)
        self.assertTrue(any(r == 'file cap reached' for _, r in bundle.ignored_files))

    def test_per_file_truncation(self):
        big = {'encoding': 'base64', 'content': b64('x' * 5000)}
        self.patch_open(self._standard_routes(contents=big), META)
        with mock.patch.object(github, 'MAX_FILE_BYTES', 100):
            bundle = github.fetch_repository('https://github.com/foo/bar')
        self.assertTrue(bundle.truncated_files)
        for f in bundle.files:
            self.assertLessEqual(len(f.content.encode('utf-8')), 100)


# --------------------------------------------------------------------------
# Optional GitHub token (Authorization header), no network
# --------------------------------------------------------------------------
class GitHubTokenTests(TestCase):
    def _capture_request(self, env):
        """Call _http_get_json with github._open patched; return the Request."""
        captured = {}

        def _fake(request, timeout=None):
            captured['request'] = request
            return FakeResponse(json.dumps({'ok': True}))

        patcher = mock.patch.object(github, '_open', _fake)
        patcher.start(); self.addCleanup(patcher.stop)
        with mock.patch.dict('os.environ', env, clear=True):
            github._http_get_json('https://api.github.com/repos/foo/bar')
        return captured['request']

    def test_no_token_stays_anonymous(self):
        request = self._capture_request({})
        self.assertIsNone(request.get_header('Authorization'))
        # Public-repo defaults are preserved.
        self.assertEqual(request.get_header('Accept'), 'application/vnd.github+json')

    def test_token_sets_bearer_authorization(self):
        request = self._capture_request({'GITHUB_TOKEN': 'gh-secret-123'})
        self.assertEqual(request.get_header('Authorization'), 'Bearer gh-secret-123')
        # The token never travels in the URL.
        self.assertNotIn('gh-secret-123', request.full_url)

    def test_blank_token_treated_as_absent(self):
        for value in ('', '   '):
            with self.subTest(value=repr(value)):
                request = self._capture_request({'GITHUB_TOKEN': value})
                self.assertIsNone(request.get_header('Authorization'))

    def test_token_never_leaks_into_error(self):
        def _fake(request, timeout=None):
            raise http_error('https://api.github.com/repos/foo/bar', 403)
        patcher = mock.patch.object(github, '_open', _fake)
        patcher.start(); self.addCleanup(patcher.stop)
        with mock.patch.dict('os.environ', {'GITHUB_TOKEN': 'gh-secret-123'}, clear=True):
            with self.assertRaises(GitHubRateLimitError) as ctx:
                github._http_get_json('https://api.github.com/repos/foo/bar')
        self.assertNotIn('gh-secret-123', str(ctx.exception))


# --------------------------------------------------------------------------
# Gemini client
# --------------------------------------------------------------------------
class ValidReport:
    @staticmethod
    def payload():
        return {
            'summary': 'A demo.',
            'technologies': ['Python'],
            'strengths': ['Clear README'],
            'concerns': ['No tests'],
            'criteria': [
                {'key': 'relevance', 'score': 3, 'rationale': 'ok', 'evidence': ['README.md:1']},
                {'key': 'technical_quality', 'score': 4, 'rationale': 'good', 'evidence': ['app.py']},
                {'key': 'functionality', 'score': 2, 'rationale': 'partial', 'evidence': []},
                {'key': 'innovation', 'score': 3, 'rationale': 'fine', 'evidence': []},
                {'key': 'documentation', 'score': 5, 'rationale': 'great', 'evidence': ['README.md']},
            ],
            'limitations': 'Only listed files considered.',
        }


def gemini_response(text):
    return FakeResponse(json.dumps(
        {'candidates': [{'content': {'parts': [{'text': text}]}, 'finishReason': 'STOP'}]}
    ))


class LLMClientTests(TestCase):
    def make_client(self, key='secret-key', model='gemini-test-model'):
        return llm.GeminiClient(api_key=key, model=model)

    def test_build_request_headers_and_body(self):
        client = self.make_client()
        request = client.build_request('sys', 'user')
        # Key travels only in the header, never in the URL.
        self.assertEqual(request.get_header('X-goog-api-key'), 'secret-key')
        self.assertNotIn('secret-key', request.full_url)
        self.assertIn('/models/gemini-test-model:generateContent', request.full_url)
        body = json.loads(request.data.decode('utf-8'))
        self.assertEqual(body['systemInstruction']['parts'][0]['text'], 'sys')
        self.assertEqual(body['contents'][0]['role'], 'user')
        self.assertEqual(body['contents'][0]['parts'][0]['text'], 'user')
        gen = body['generationConfig']
        self.assertEqual(gen['responseMimeType'], 'application/json')
        self.assertLessEqual(gen['temperature'], 0.5)
        self.assertEqual(gen['responseSchema']['properties']['criteria']['items']
                         ['properties']['key']['enum'], list(rubric.EXPECTED_KEYS))
        self.assertNotIn('secret-key', json.dumps(body))

    def test_model_from_env(self):
        with mock.patch.dict('os.environ', {'GEMINI_MODEL': 'gemini-env-model'}):
            self.assertEqual(llm.GeminiClient(api_key='k').model, 'gemini-env-model')

    def test_missing_key_raises_config_error(self):
        client = llm.GeminiClient(api_key='', model='gemini-test-model')
        with self.assertRaises(LLMConfigError) as ctx:
            client.complete_json('s', 'u')
        self.assertNotIn('secret', str(ctx.exception).lower())

    def test_missing_model_raises_config_error(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            client = llm.GeminiClient(api_key='k', model='')
            with self.assertRaises(LLMConfigError):
                client.complete_json('s', 'u')

    def test_invalid_model_rejected_before_network(self):
        patcher = mock.patch('urllib.request.urlopen')
        mock_urlopen = patcher.start(); self.addCleanup(patcher.stop)
        client = self.make_client(model='bad model/../x')
        with self.assertRaises(LLMConfigError):
            client.complete_json('s', 'u')
        mock_urlopen.assert_not_called()

    def test_complete_json_success(self):
        client = self.make_client()
        with mock.patch.object(client, '_open', return_value=gemini_response('{"a":1}')):
            self.assertEqual(client.complete_json('s', 'u'), {'a': 1})

    def test_complete_json_joins_multiple_parts(self):
        client = self.make_client()
        resp = FakeResponse(json.dumps({'candidates': [{'content': {'parts': [
            {'text': '{"a"'}, {'text': ':2}'}]}}]}))
        with mock.patch.object(client, '_open', return_value=resp):
            self.assertEqual(client.complete_json('s', 'u'), {'a': 2})

    def test_complete_json_strips_code_fence(self):
        client = self.make_client()
        with mock.patch.object(client, '_open',
                               return_value=gemini_response('```json\n{"a": 2}\n```')):
            self.assertEqual(client.complete_json('s', 'u'), {'a': 2})

    def test_blocked_prompt_feedback(self):
        client = self.make_client()
        resp = FakeResponse(json.dumps({'promptFeedback': {'blocked': {'reason': 'SPII'}}}))
        with mock.patch.object(client, '_open', return_value=resp):
            with self.assertRaises(MalformedResponseError):
                client.complete_json('s', 'u')

    def test_safety_finish_reason(self):
        client = self.make_client()
        resp = FakeResponse(json.dumps(
            {'candidates': [{'content': {'parts': [{'text': 'x'}]}, 'finishReason': 'SAFETY'}]}))
        with mock.patch.object(client, '_open', return_value=resp):
            with self.assertRaises(MalformedResponseError):
                client.complete_json('s', 'u')

    def test_empty_candidates(self):
        client = self.make_client()
        resp = FakeResponse(json.dumps({'candidates': []}))
        with mock.patch.object(client, '_open', return_value=resp):
            with self.assertRaises(MalformedResponseError):
                client.complete_json('s', 'u')

    def test_http_error_mapping(self):
        cases = {400: LLMRejectedError, 401: LLMRejectedError, 403: LLMRejectedError,
                 404: LLMConfigError, 429: LLMRateLimitError, 500: LLMError, 503: LLMError}
        client = self.make_client()
        for code, exc in cases.items():
            with self.subTest(code=code), mock.patch.object(
                    client, '_open', side_effect=http_error('x', code)):
                with self.assertRaises(exc):
                    client.complete_json('s', 'u')

    def test_unavailable_model_is_named_not_generic(self):
        # Regression: a 404 from Gemini used to collapse into the useless
        # "The AI service returned an error." generic. It must now surface the
        # real Gemini status token so the organizer can act on the config.
        client = self.make_client(model='gemini-2.5-flash')
        body = {'code': 404, 'status': 'NOT_FOUND',
                'message': 'models/gemini-2.5-flash is not found for API version v1beta'}
        with mock.patch.object(client, '_open', side_effect=http_error_body(404, body)):
            with self.assertRaises(LLMConfigError) as ctx:
                client.complete_json('s', 'u')
        msg = str(ctx.exception)
        self.assertIn('NOT_FOUND', msg)
        self.assertIn('model', msg.lower())

    def test_error_detail_never_leaks_key(self):
        client = llm.GeminiClient(api_key='supersecret', model='gemini-test-model')
        body = {'status': 'UNAUTHENTICATED', 'message': 'API key supersecret is invalid'}
        with mock.patch.object(client, '_open', side_effect=http_error_body(401, body)):
            with self.assertRaises(LLMError) as ctx:
                client.complete_json('s', 'u')
        self.assertNotIn('supersecret', str(ctx.exception))

    def test_timeout(self):
        client = self.make_client()
        with mock.patch.object(client, '_open', side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaises(RequestTimeoutError):
                client.complete_json('s', 'u')

    def test_gemini_timeout_is_named_as_an_ai_failure(self):
        # The diagnosis of the one real call was a Gemini-side stall, so the
        # saved error message must point at the AI service and at the knob that
        # moves its deadline, not at a generic network problem.
        client = self.make_client()
        for exc in (urllib.error.URLError(TimeoutError()), TimeoutError()):
            with self.subTest(exc=type(exc).__name__), mock.patch.object(
                    client, '_open', side_effect=exc):
                with self.assertRaises(LLMTimeoutError) as ctx:
                    client.complete_json('s', 'u')
                self.assertIn('GEMINI_TIMEOUT', str(ctx.exception))
                # Still catchable by every pre-existing RequestTimeoutError branch.
                self.assertIsInstance(ctx.exception, RequestTimeoutError)
    def test_github_timeout_stays_a_generic_request_timeout(self):
        # Only the Gemini path is renamed; a GitHub stall must not tell the
        # organizer to raise the AI deadline.
        client = self.make_client()
        with mock.patch.object(client, '_open', side_effect=urllib.error.URLError('dns fail')):
            with self.assertRaises(NetworkError) as ctx:
                client.complete_json('s', 'u')
        self.assertNotIsInstance(ctx.exception, LLMTimeoutError)

    def test_timeout_defaults_to_the_documented_constant(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llm.GeminiClient(api_key='k', model='m').timeout, llm.LLM_TIMEOUT)

    def test_configured_timeout_reaches_the_transport(self):
        with mock.patch.dict(os.environ, {'GEMINI_TIMEOUT': '120'}):
            client = llm.GeminiClient(api_key='k', model='gemini-test-model')
            self.assertEqual(client.timeout, 120)
            with mock.patch.object(client, '_open',
                                   return_value=gemini_response('{"a":1}')) as mock_open:
                client.complete_json('s', 'u')
        self.assertEqual(mock_open.call_args.args[1], 120)

    def test_unusable_timeout_falls_back_to_the_default(self):
        # A negative, tiny, huge or non-numeric value must not be handed to
        # urlopen: it would fail in a way that looks like an AI outage.
        for raw in ('5', '10000', '-1', 'abc', '', '  ', '90.5'):
            with self.subTest(GEMINI_TIMEOUT=raw), mock.patch.dict(
                    os.environ, {'GEMINI_TIMEOUT': raw}):
                self.assertEqual(llm.GeminiClient(api_key='k', model='m').timeout,
                                 llm.LLM_TIMEOUT)

    def test_boundary_timeouts_are_honoured(self):
        for raw, expected in ((str(llm.MIN_TIMEOUT), llm.MIN_TIMEOUT),
                              (str(llm.MAX_TIMEOUT), llm.MAX_TIMEOUT)):
            with self.subTest(GEMINI_TIMEOUT=raw), mock.patch.dict(
                    os.environ, {'GEMINI_TIMEOUT': raw}):
                self.assertEqual(llm.GeminiClient(api_key='k', model='m').timeout, expected)

    def test_error_never_leaks_key(self):
        client = llm.GeminiClient(api_key='supersecret', model='gemini-test-model')
        request = client.build_request('s', 'u')
        self.assertNotIn('supersecret', request.full_url)
        with mock.patch.object(client, '_open', side_effect=http_error('x', 401)):
            with self.assertRaises(LLMError) as ctx:
                client.complete_json('s', 'u')
            self.assertNotIn('supersecret', str(ctx.exception))


# --------------------------------------------------------------------------
# Rubric validation
# --------------------------------------------------------------------------
class RubricValidationTests(TestCase):
    def test_valid_and_ordered(self):
        report = rubric.validate_report(ValidReport.payload())
        keys = [c['key'] for c in report['criteria']]
        self.assertEqual(keys, list(rubric.EXPECTED_KEYS))

    def test_missing_criterion(self):
        payload = ValidReport.payload()
        payload['criteria'] = payload['criteria'][:3]
        with self.assertRaises(MalformedResponseError):
            rubric.validate_report(payload)

    def test_duplicate_criterion(self):
        payload = ValidReport.payload()
        payload['criteria'][1] = dict(payload['criteria'][0])
        with self.assertRaises(MalformedResponseError):
            rubric.validate_report(payload)

    def test_out_of_range_score(self):
        for score in (6, -1):
            payload = ValidReport.payload()
            payload['criteria'][0]['score'] = score
            with self.subTest(score=score), self.assertRaises(MalformedResponseError):
                rubric.validate_report(payload)

    def test_non_integer_score(self):
        payload = ValidReport.payload()
        payload['criteria'][0]['score'] = 'four'
        with self.assertRaises(MalformedResponseError):
            rubric.validate_report(payload)

    def test_unknown_key(self):
        payload = ValidReport.payload()
        payload['criteria'][0]['key'] = 'made_up'
        with self.assertRaises(MalformedResponseError):
            rubric.validate_report(payload)

    def test_not_a_dict(self):
        with self.assertRaises(MalformedResponseError):
            rubric.validate_report([1, 2, 3])

    def test_empty_summary(self):
        payload = ValidReport.payload()
        payload['summary'] = '   '
        with self.assertRaises(MalformedResponseError):
            rubric.validate_report(payload)


# --------------------------------------------------------------------------
# Analyzer orchestration (no services hit network)
# --------------------------------------------------------------------------
class AnalyzerTests(TestCase):
    def test_success(self):
        bundle = github.RepoBundle(owner='f', repo='b', default_branch='main',
                                   commit_ref='x', description='', primary_language='',
                                   size_kb=1, files=[github.SourceFile('a.py', 'x')])
        client = mock.Mock()
        client.complete_json.return_value = ValidReport.payload()
        out_bundle, report = analyze_repository(
            'https://github.com/f/b', llm_client=client, fetcher=lambda u: bundle)
        self.assertEqual(out_bundle, bundle)
        self.assertEqual([c['key'] for c in report['criteria']], list(rubric.EXPECTED_KEYS))

    def test_invalid_json_not_reported_as_success(self):
        bundle = github.RepoBundle(owner='f', repo='b', default_branch='main',
                                   commit_ref='x', description='', primary_language='',
                                   size_kb=1, files=[])
        client = mock.Mock()
        client.complete_json.return_value = {'summary': 'ok'}  # missing criteria
        with self.assertRaises(MalformedResponseError):
            analyze_repository('https://github.com/f/b', llm_client=client,
                               fetcher=lambda u: bundle)


# --------------------------------------------------------------------------
# Views / permissions / CSRF / isolation
# --------------------------------------------------------------------------
class ViewsBase(TestCase):
    def setUp(self):
        self.org = make_user('an_org', O)
        self.org2 = make_user('an_org2', O)
        self.participant = make_user('an_part', P)
        self.jury = make_user('an_jury', J)

    def login(self, user):
        self.client.force_login(user)

    def make_report(self, owner, status=AIReport.Status.DONE):
        return AIReport.objects.create(
            created_by=owner, repository_url='https://github.com/foo/bar',
            status=status, commit_ref='deadbeef', model_id='test/model:free',
            file_count=2, analyzed_bytes=100, report=ValidReport.payload(),
        )


class AccessGatingTests(ViewsBase):
    def test_anonymous_redirected(self):
        resp = self.client.get(reverse('analyses:home'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

    def test_participant_blocked(self):
        self.login(self.participant)
        resp = self.client.get(reverse('analyses:home'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('dashboard'), resp.url)

    def test_jury_allowed_on_scoped_reports(self):
        # Jury access exists now that report scoping does: a juré reaches the
        # module but only sees analyses of the projects they may score.
        self.login(self.jury)
        resp = self.client.get(reverse('analyses:home'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context['reports']), [])

    def test_organizer_allowed(self):
        self.login(self.org)
        resp = self.client.get(reverse('analyses:home'))
        self.assertEqual(resp.status_code, 200)


class OwnershipIsolationTests(ViewsBase):
    def test_cannot_view_others_report_direct_url(self):
        report = self.make_report(self.org2)
        self.login(self.org)
        resp = self.client.get(reverse('analyses:detail', args=[report.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_delete_others_report(self):
        report = self.make_report(self.org2)
        self.login(self.org)
        resp = self.client.post(reverse('analyses:delete', args=[report.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(AIReport.objects.filter(pk=report.pk).exists())

    def test_list_only_own(self):
        own = self.make_report(self.org)
        other = self.make_report(self.org2)
        self.login(self.org)
        resp = self.client.get(reverse('analyses:home'))
        pks = [r.pk for r in resp.context['reports']]
        self.assertIn(own.pk, pks)
        self.assertNotIn(other.pk, pks)


class DetailRenderTests(ViewsBase):
    def test_disclaimer_and_no_final_grade(self):
        report = self.make_report(self.org)
        self.login(self.org)
        resp = self.client.get(reverse('analyses:detail', args=[report.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'final grade belongs to the jury')
        # Indicative scores render, but there is no "final grade" label.
        self.assertNotContains(resp, 'Final grade')

    def test_failed_report_shows_error_not_success(self):
        report = self.make_report(self.org, status=AIReport.Status.FAILED)
        report.report = {}
        report.error_message = 'The repository was not found. Make sure it is public.'
        report.save()
        self.login(self.org)
        resp = self.client.get(reverse('analyses:detail', args=[report.pk]))
        self.assertContains(resp, 'could not be completed')
        self.assertNotContains(resp, 'Indicative scores')


class NoFinalGradeFieldTests(TestCase):
    def test_model_has_no_grade_field(self):
        names = {f.name for f in AIReport._meta.get_fields()}
        for forbidden in ('grade', 'final_grade', 'score', 'mark'):
            self.assertNotIn(forbidden, names)


class CreateFlowTests(ViewsBase):
    def setUp(self):
        super().setUp()
        # Provide key + model so the config guard passes.
        patcher = mock.patch.dict('os.environ',
                                  {'GEMINI_API_KEY': 'test-key', 'GEMINI_MODEL': 'gemini-test-model'})
        patcher.start(); self.addCleanup(patcher.stop)
        hackathon = make_hackathon(self.org)
        self.project = make_project(hackathon, self.participant,
                                    repo_url='https://github.com/foo/bar')

    def _payload(self):
        bundle = github.RepoBundle(owner='foo', repo='bar', default_branch='main',
                                   commit_ref='cafe', description='', primary_language='',
                                   size_kb=1, files=[github.SourceFile('a.py', 'x')])
        return bundle, ValidReport.payload()

    def test_organizer_create_done(self):
        self.login(self.org)
        with mock.patch('analyses.views.analyze_repository', return_value=self._payload()):
            resp = self.client.post(reverse('analyses:create'), {'project': self.project.pk})
        self.assertEqual(resp.status_code, 302)
        report = AIReport.objects.get(created_by=self.org)
        self.assertEqual(report.project_id, self.project.pk)
        self.assertEqual(report.repository_url, 'https://github.com/foo/bar')
        self.assertEqual(report.status, AIReport.Status.DONE)
        self.assertEqual(report.commit_ref, 'cafe')

    def test_project_without_a_submitted_url_is_not_accepted(self):
        # The URL is no longer something the browser supplies, so an unusable
        # submission is refused before any network call.
        self.login(self.org)
        blank = make_project(self.project.hackathon, self.participant,
                             title='No repo', repo_url='')
        resp = self.client.post(reverse('analyses:create'), {'project': blank.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('project', resp.context['form'].errors)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_analysis_failure_persisted_as_failed(self):
        self.login(self.org)
        with mock.patch('analyses.views.analyze_repository',
                        side_effect=GitHubNotFoundError()):
            resp = self.client.post(reverse('analyses:create'), {'project': self.project.pk})
        self.assertEqual(resp.status_code, 302)
        report = AIReport.objects.get(created_by=self.org)
        self.assertEqual(report.status, AIReport.Status.FAILED)
        self.assertTrue(report.error_message)

    def test_missing_key_shows_config_error(self):
        self.login(self.org)
        patcher = mock.patch.dict('os.environ', {'GEMINI_API_KEY': ''})
        patcher.start(); self.addCleanup(patcher.stop)
        resp = self.client.post(reverse('analyses:create'), {'project': self.project.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_missing_model_shows_config_error(self):
        self.login(self.org)
        patcher = mock.patch.dict('os.environ', {'GEMINI_MODEL': ''})
        patcher.start(); self.addCleanup(patcher.stop)
        resp = self.client.post(reverse('analyses:create'), {'project': self.project.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_participant_cannot_create_direct(self):
        self.login(self.participant)
        resp = self.client.post(reverse('analyses:create'), {'project': self.project.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.count(), 0)


class CsrfTests(ViewsBase):
    def test_post_without_csrf_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.org)
        resp = csrf_client.post(reverse('analyses:create'), {'project': 1})
        self.assertEqual(resp.status_code, 403)


class DeleteFlowTests(ViewsBase):
    def test_get_shows_confirmation(self):
        report = self.make_report(self.org)
        self.login(self.org)
        resp = self.client.get(reverse('analyses:delete', args=[report.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Delete this report')

    def test_post_deletes(self):
        report = self.make_report(self.org)
        self.login(self.org)
        resp = self.client.post(reverse('analyses:delete', args=[report.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AIReport.objects.filter(pk=report.pk).exists())


# --------------------------------------------------------------------------
# Project + hackathon-rubric binding
#
# These tests build the real shared models (hackathons, projects, teams,
# evaluations) so the bridge is checked against the contracts the other modules
# actually expose, not against a stand-in.
# --------------------------------------------------------------------------
def make_hackathon(organizer, **overrides):
    now = timezone.now()
    values = {
        'organizer': organizer,
        'title': 'HackFlow Cup',
        'description': 'Projects are judged against the published rubric.',
        'category': Hackathon.Category.AI,
        'event_format': Hackathon.Format.ONLINE,
        'start_date': now + timedelta(days=1),
        'end_date': now + timedelta(days=3),
        'registration_deadline': now,
        'submission_deadline': now + timedelta(days=2),
        'status': Hackathon.Status.PUBLISHED,
    }
    values.update(overrides)
    return Hackathon.objects.create(**values)


def make_project(hackathon, member, **overrides):
    values = {
        'hackathon': hackathon,
        'team': Team.objects.create(
            name=f'{hackathon.title} team', hackathon=hackathon,
            created_by=member, captain=member, max_members=5,
        ),
        'submitted_by': member,
        'title': 'Smart Health AI',
        'description': 'A judged project.',
        'status': Project.Status.SUBMITTED,
        'repo_url': 'https://github.com/example/repo',
    }
    values.update(overrides)
    return Project.objects.create(**values)


def make_criteria(hackathon):
    return [
        EvaluationCriterion.objects.create(
            hackathon=hackathon, name=name, description=desc, weight=weight, order=order)
        for name, desc, weight, order in (
            ('Innovation', 'Novelty of the concept.', 40, 1),
            ('Technical Implementation', 'Architecture and code quality.', 30, 2),
            ('Impact', 'Usefulness of the result.', 30, 3),
        )
    ]


def project_payload(specs, score=8, evidence=('app.py',)):
    """A model reply shaped like the requested rubric, on the requested scale."""
    return {
        'summary': 'A demo.',
        'technologies': ['Python'],
        'strengths': ['Clear README'],
        'concerns': ['No tests'],
        'criteria': [
            {'key': spec.key, 'score': score, 'rationale': 'observed',
             'evidence': list(evidence)}
            for spec in specs
        ],
        'limitations': 'Only listed files considered.',
    }


def bundle_with(paths=('app.py', 'README.md'), ignored=(), truncated=()):
    return github.RepoBundle(
        owner='f', repo='b', default_branch='main', commit_ref='cafe',
        description='', primary_language='Python', size_kb=2,
        files=[github.SourceFile(path, 'code') for path in paths],
        truncated_files=list(truncated), ignored_files=list(ignored), total_bytes=200,
    )


def stored_report(creator, project=None, specs=None, criteria=(), status=None, **extra):
    """Persist a validated project-bound report on the jury's 0-10 scale."""
    chosen = specs if specs is not None else rubric.project_specs(criteria)
    report = rubric.validate_report(
        project_payload(chosen), chosen, score_max=rubric.PROJECT_SCORE_MAX,
        allowed_paths=['app.py', 'README.md'])
    return AIReport.objects.create(
        created_by=creator, project=project,
        repository_url=(project.repo_url if project else 'https://github.com/foo/bar'),
        status=status or AIReport.Status.DONE, model_id='test/model', file_count=2,
        analyzed_bytes=200, report=report, **extra
    )


class DynamicRubricTests(TestCase):
    def setUp(self):
        self.org = make_user('dr_org', O)
        self.hackathon = make_hackathon(self.org)
        self.criteria = make_criteria(self.hackathon)
        self.specs = rubric.project_specs(self.criteria)

    def test_specs_carry_real_ids_weights_and_jury_scale(self):
        self.assertEqual([spec.key for spec in self.specs],
                         [f'c{c.pk}' for c in self.criteria])
        self.assertEqual([spec.label for spec in self.specs], [c.name for c in self.criteria])
        self.assertEqual([spec.criterion_id for spec in self.specs], [c.pk for c in self.criteria])
        self.assertEqual([spec.weight for spec in self.specs], [40.0, 30.0, 30.0])
        self.assertEqual(rubric.PROJECT_SCORE_MAX, 10)

    def test_scale_and_identity_are_stored_with_the_report(self):
        report = rubric.validate_report(
            project_payload(self.specs), self.specs, score_max=rubric.PROJECT_SCORE_MAX)
        self.assertEqual(report['scale'], {'min': 0, 'max': 10})
        entries = report['criteria']
        self.assertEqual([e['criterion_id'] for e in entries], [c.pk for c in self.criteria])
        self.assertEqual([e['weight'] for e in entries], [40.0, 30.0, 30.0])

    def test_full_jury_range_is_accepted(self):
        for score in (0, 10):
            payload = project_payload(self.specs, score=score)
            with self.subTest(score=score):
                report = rubric.validate_report(
                    payload, self.specs, score_max=rubric.PROJECT_SCORE_MAX)
                self.assertEqual(report['criteria'][0]['score'], score)

    def test_generic_scale_still_rejects_six(self):
        specs = rubric.generic_specs()
        with self.assertRaises(MalformedResponseError):
            rubric.validate_report(project_payload(specs, score=6), specs,
                                   score_max=rubric.SCORE_MAX)

    def test_prompt_and_schema_follow_the_rubric(self):
        keys = rubric.spec_keys(self.specs)
        prompt = rubric.system_prompt(self.specs, rubric.PROJECT_SCORE_MAX)
        for key in keys:
            self.assertIn(key, prompt)
        self.assertIn('between 0 and 10', prompt)
        self.assertNotIn('relevance', prompt)
        schema = rubric.response_schema(self.specs, rubric.PROJECT_SCORE_MAX)
        self.assertEqual(
            schema['properties']['criteria']['items']['properties']['key']['enum'], list(keys))

    def test_generic_rubric_still_available(self):
        self.assertIn('relevance', rubric.SYSTEM_PROMPT)
        schema = rubric.response_schema()
        self.assertEqual(
            schema['properties']['criteria']['items']['properties']['key']['enum'],
            list(rubric.EXPECTED_KEYS))


class EvidenceGroundingTests(TestCase):
    def setUp(self):
        self.specs = rubric.generic_specs()

    def test_invented_file_is_dropped_and_counted(self):
        specs = self.specs[:1]
        payload = project_payload(specs, score=3,
                                  evidence=('app.py', 'src/never_fetched.py'))
        report = rubric.validate_report(
            payload, specs, allowed_paths=['app.py', 'README.md'])
        self.assertEqual(report['criteria'][0]['evidence'], ['app.py'])
        self.assertEqual(report['unverified_evidence'], 1)

    def test_line_reference_to_a_real_file_is_kept(self):
        specs = self.specs[:1]
        payload = project_payload(specs, score=3, evidence=('README.md:12-20',))
        report = rubric.validate_report(payload, specs, allowed_paths=['README.md'])
        self.assertEqual(report['criteria'][0]['evidence'], ['README.md:12-20'])
        self.assertEqual(report['unverified_evidence'], 0)

    def test_all_evidence_can_be_dropped_without_losing_the_score(self):
        specs = self.specs[:1]
        payload = project_payload(specs, score=3, evidence=('imaginary.txt',))
        report = rubric.validate_report(payload, specs, allowed_paths=['app.py'])
        self.assertEqual(report['criteria'][0]['evidence'], [])
        self.assertEqual(report['criteria'][0]['score'], 3)
        self.assertEqual(report['unverified_evidence'], 1)

    def test_no_grounding_when_paths_are_unknown(self):
        report = rubric.validate_report(
            project_payload(self.specs, score=3, evidence=('whatever.py',)), self.specs)
        self.assertEqual(report['criteria'][0]['evidence'], ['whatever.py'])


class CoverageTests(TestCase):
    def test_coverage_records_what_was_read_and_skipped(self):
        bundle = bundle_with(paths=('app.py',), ignored=[('logo.png', 'binary file')],
                            truncated=['big.py'])
        client = mock.Mock()
        client.complete_json.return_value = ValidReport.payload()
        _, report = analyze_repository('https://github.com/f/b', llm_client=client,
                                      fetcher=lambda u: bundle)
        self.assertEqual(report['coverage']['read_files'], ['app.py'])
        self.assertEqual(report['coverage']['truncated_files'], ['big.py'])
        self.assertEqual(report['coverage']['ignored_files'],
                         [{'path': 'logo.png', 'reason': 'binary file'}])

    def test_only_fetched_files_can_be_cited(self):
        bundle = bundle_with(paths=('app.py',))
        client = mock.Mock()
        client.complete_json.return_value = ValidReport.payload()
        _, report = analyze_repository('https://github.com/f/b', llm_client=client,
                                      fetcher=lambda u: bundle)
        # ValidReport cites README.md and app.py; README.md was never fetched.
        self.assertGreaterEqual(report['unverified_evidence'], 1)
        for entry in report['criteria']:
            for item in entry['evidence']:
                self.assertIn('app.py', item)


class RubricChoiceTests(TestCase):
    def setUp(self):
        self.org = make_user('rc_org', O)
        self.hackathon = make_hackathon(self.org)
        self.project = make_project(self.hackathon, make_user('rc_part', P))

    def test_project_rubric_is_the_hackathon_one_on_the_jury_scale(self):
        criteria = make_criteria(self.hackathon)
        choice = rubrics.rubric_for_project(self.project)
        self.assertEqual(choice.source, rubrics.SOURCE_HACKATHON)
        self.assertEqual(choice.score_max, 10)
        self.assertEqual([s.criterion_id for s in choice.specs], [c.pk for c in criteria])

    def test_missing_rubric_falls_back_to_generic(self):
        choice = rubrics.rubric_for_project(self.project)
        self.assertEqual(choice.source, rubrics.SOURCE_GENERIC)
        self.assertEqual(choice.score_max, 5)

    def test_choosing_a_rubric_never_creates_one(self):
        # ensure_rubric() writes default criteria; the analysis bridge must not.
        rubrics.rubric_for_project(self.project)
        self.assertEqual(EvaluationCriterion.objects.filter(hackathon=self.hackathon).count(), 0)


class ReportScalePropertyTests(TestCase):
    def make(self, report, project=None, creator=None):
        return AIReport.objects.create(
            created_by=creator or make_user('rp_org', O), project=project,
            repository_url='https://github.com/foo/bar', status=AIReport.Status.DONE,
            report=report,
        )

    def test_legacy_report_reads_as_five_point_and_unweighted(self):
        report = self.make(ValidReport.payload())
        self.assertEqual(report.scale_max, 5)
        self.assertIsNone(report.weighted_indicative)
        self.assertEqual(report.suggestion_by_criterion, {})
        self.assertFalse(report.is_project_bound)

    def test_weighted_uses_the_same_formula_as_the_jury(self):
        org = make_user('rp2_org', O)
        hackathon = make_hackathon(org)
        project = make_project(hackathon, make_user('rp2_part', P))
        specs = rubric.project_specs(make_criteria(hackathon))
        stored = rubric.validate_report(project_payload(specs), specs, score_max=10)
        report = self.make(stored, project=project, creator=org)
        self.assertEqual(report.scale_max, 10)
        self.assertTrue(report.is_project_bound)
        # (8*40 + 8*30 + 8*30) / 100, i.e. Evaluation.get_weighted_score's formula
        self.assertEqual(report.weighted_indicative, 8.0)
        self.assertEqual(sorted(report.suggestion_by_criterion),
                         sorted(c['criterion_id'] for c in stored['criteria']))


class ModulePermissionsTests(TestCase):
    def setUp(self):
        self.org = make_user('mp_org', O)
        self.other_org = make_user('mp_org2', O)
        self.jury = make_user('mp_jury', J)
        self.participant = make_user('mp_part', P)
        self.admin = User.objects.create_superuser(
            username='mp_admin', email='mp_admin@ex.com', password='pass12345')
        self.hackathon = make_hackathon(self.org)
        # A draft event is not open for scoring, so its projects stay outside
        # the jury's scope and can prove the scoping is real.
        self.other_hackathon = make_hackathon(self.other_org, title='Other Cup',
                                             status=Hackathon.Status.DRAFT)
        self.project = make_project(self.hackathon, self.participant)
        self.other_project = make_project(self.other_hackathon, self.participant,
                                         title='Other project')

    def report(self, creator, project=None, status=AIReport.Status.DONE):
        return AIReport.objects.create(
            created_by=creator, project=project, repository_url='https://github.com/foo/bar',
            status=status, report=ValidReport.payload(),
        )

    def test_anonymous_and_participant_see_nothing(self):
        self.report(self.org, self.project)
        self.assertEqual(list(permissions.visible_reports(None)), [])
        self.assertEqual(list(permissions.visible_reports(self.participant)), [])
        self.assertFalse(permissions.has_module_access(self.participant))
        self.assertFalse(permissions.has_module_access(None))

    def test_organizer_sees_own_reports_and_their_event_reports(self):
        own = self.report(self.org)
        theirs = self.report(self.other_org, self.other_project)
        pks = [r.pk for r in permissions.visible_reports(self.org)]
        self.assertIn(own.pk, pks)
        self.assertNotIn(theirs.pk, pks)

    def test_jury_sees_only_reports_of_projects_they_can_score(self):
        bound = self.report(self.org, self.project)
        self.report(self.org, self.other_project)
        unbound = self.report(self.org)
        pks = [r.pk for r in permissions.visible_reports(self.jury)]
        self.assertEqual(pks, [bound.pk])
        self.assertTrue(permissions.can_view(self.jury, bound))
        self.assertFalse(permissions.can_view(self.jury, unbound))
        self.assertTrue(permissions.has_module_access(self.jury))

    def test_superuser_reads_all_but_never_launches(self):
        bound = self.report(self.org, self.project)
        self.assertEqual([r.pk for r in permissions.visible_reports(self.admin)], [bound.pk])
        self.assertFalse(permissions.can_launch(self.admin, self.project))
        self.assertFalse(permissions.can_analyze_anything(self.admin))

    def test_launch_rights_always_need_a_project(self):
        self.assertTrue(permissions.can_launch(self.org, self.project))
        self.assertFalse(permissions.can_launch(self.org, self.other_project))
        self.assertTrue(permissions.can_launch(self.jury, self.project))
        self.assertFalse(permissions.can_launch(self.participant, self.project))
        # A report from before the project link has nothing submitted to re-read.
        self.assertFalse(permissions.can_launch(self.org, None))

    def test_entry_point_follows_the_projects_there_are_to_analyze(self):
        # The "new analysis" offer is now exactly "you have a project whose
        # repository can be fetched", because that is the only thing the view
        # will accept.
        self.assertTrue(permissions.can_analyze_anything(self.org))
        self.assertTrue(permissions.can_analyze_anything(self.jury))
        self.assertFalse(permissions.can_analyze_anything(self.participant))
        self.assertFalse(permissions.can_analyze_anything(None))

    def test_analyzable_projects_requires_a_github_url(self):
        def pks(user):
            return [pk for pk in permissions.analyzable_projects(user).values_list('pk', flat=True)]

        self.assertIn(self.project.pk, pks(self.org))
        draft = make_project(self.hackathon, self.participant, title='No repo', repo_url='')
        self.assertNotIn(draft.pk, pks(self.org))
        gitlab = make_project(self.hackathon, self.participant, title='GitLab',
                             repo_url='https://gitlab.com/foo/bar')
        self.assertNotIn(gitlab.pk, pks(self.org))
        self.assertEqual(pks(self.participant), [])
        self.assertIn(self.project.pk, pks(self.jury))

    def test_jury_never_deletes(self):
        bound = self.report(self.org, self.project)
        self.assertTrue(permissions.can_delete(self.org, bound))
        self.assertFalse(permissions.can_delete(self.jury, bound))
        self.assertFalse(permissions.can_delete(self.participant, bound))

    def test_creator_deletes_own(self):
        report = self.report(self.jury, self.project)
        self.assertTrue(permissions.can_delete(self.jury, report))


class ProjectBoundCreateTests(TestCase):
    def setUp(self):
        self.org = make_user('pbc_org', O)
        self.jury = make_user('pbc_jury', J)
        self.participant = make_user('pbc_part', P)
        self.hackathon = make_hackathon(self.org)
        self.criteria = make_criteria(self.hackathon)
        self.project = make_project(self.hackathon, self.participant)
        patcher = mock.patch.dict('os.environ',
                                 {'GEMINI_API_KEY': 'test-key', 'GEMINI_MODEL': 'gemini-test'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def post_create(self, **post):
        """POST to the create view with the analyzer mocked; capture its kwargs."""
        captured = {}

        def fake_analyze(url, **kwargs):
            captured['url'] = url
            captured.update(kwargs)
            report = rubric.validate_report(
                project_payload(kwargs['specs'], score=kwargs['score_max']),
                kwargs['specs'], score_max=kwargs['score_max'],
                allowed_paths=['app.py', 'README.md'])
            return bundle_with(), report

        with mock.patch('analyses.views.analyze_repository', side_effect=fake_analyze):
            resp = self.client.post(reverse('analyses:create'), post)
        return resp, captured

    def test_project_bound_analysis_uses_the_hackathon_rubric(self):
        self.client.force_login(self.org)
        resp, captured = self.post_create(project=self.project.pk)
        self.assertEqual(resp.status_code, 302)
        report = AIReport.objects.get(project=self.project)
        self.assertEqual(captured['score_max'], 10)
        self.assertEqual([s.key for s in captured['specs']], [f'c{c.pk}' for c in self.criteria])
        self.assertEqual(captured['url'], self.project.repo_url)
        self.assertEqual(report.repository_url, 'https://github.com/example/repo')
        self.assertEqual(report.scale_max, 10)
        self.assertEqual(report.status, AIReport.Status.DONE)
        self.assertEqual([e['criterion_id'] for e in report.criteria], [c.pk for c in self.criteria])
        # A stored report from before the coverage metadata stays readable.
        self.assertEqual(report.coverage, {})

    def test_typed_url_cannot_redirect_a_project_analysis(self):
        self.client.force_login(self.org)
        _, captured = self.post_create(project=self.project.pk,
                                      repository_url='https://github.com/attacker/repo')
        self.assertEqual(captured['url'], 'https://github.com/example/repo')
        self.assertEqual(AIReport.objects.get().repository_url,
                         'https://github.com/example/repo')

    def test_project_without_a_rubric_is_still_a_project_analysis(self):
        # No criteria defined yet for this event: the report is generic and on
        # the 0-5 scale, but it stays bound to the project it was launched from.
        self.client.force_login(self.org)
        EvaluationCriterion.objects.filter(hackathon=self.hackathon).delete()
        resp, captured = self.post_create(project=self.project.pk)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(captured['specs'], rubric.generic_specs())
        report = AIReport.objects.get()
        self.assertEqual(report.project_id, self.project.pk)
        self.assertEqual(report.scale_max, 5)
        self.assertEqual(report.weighted_indicative, None)

    def test_jury_can_analyze_a_project_they_score(self):
        self.client.force_login(self.jury)
        resp, _ = self.post_create(project=self.project.pk)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.get().created_by, self.jury)

    def test_no_project_means_no_analysis(self):
        """There is no URL left to type, so a POST without a project is refused."""
        for user in (self.org, self.jury):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                resp = self.client.post(reverse('analyses:create'),
                                        {'repository_url': 'https://github.com/foo/bar'})
                self.assertEqual(resp.status_code, 200)
                self.assertIn('project', resp.context['form'].errors)
                self.assertEqual(AIReport.objects.count(), 0)

    def test_form_offers_no_url_field(self):
        for user in (self.org, self.jury):
            form = AnalysisRequestForm(user=user)
            self.assertNotIn('repository_url', form.fields)
            self.assertTrue(form.fields['project'].required)
            self.assertEqual(str(form.fields['project'].empty_label), 'Select a project')

    def test_project_of_another_event_is_not_accepted(self):
        alien = make_project(make_hackathon(make_user('pbc_org2', O), title='Alien Cup'),
                            self.participant, title='Alien')
        self.client.force_login(self.org)
        resp = self.client.post(reverse('analyses:create'), {'project': alien.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('project', resp.context['form'].errors)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_form_only_offers_fetchable_projects(self):
        make_project(self.hackathon, self.participant, title='No repo', repo_url='')
        form = AnalysisRequestForm(user=self.org)
        self.assertEqual([p.pk for p in form.fields['project'].queryset], [self.project.pk])
        self.assertEqual(
            list(AnalysisRequestForm(user=self.participant).fields['project'].queryset), [])

    def test_get_prefills_from_a_project(self):
        self.client.force_login(self.org)
        resp = self.client.get(reverse('analyses:create'), {'project': self.project.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['form'].initial.get('project'), self.project)

    def test_non_github_project_url_is_rejected_before_any_fetch(self):
        make_criteria(self.hackathon)
        project = make_project(self.hackathon, self.participant, title='Elsewhere',
                              repo_url='https://gitlab.com/foo/bar')
        self.client.force_login(self.org)
        resp = self.client.post(reverse('analyses:create'), {'project': project.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AIReport.objects.count(), 0)


class ProjectPageAnalysisTests(TestCase):
    """The project page is where an analysis is launched from."""

    def setUp(self):
        self.org = make_user('pp_org', O)
        self.jury = make_user('pp_jury', J)
        self.participant = make_user('pp_part', P)
        self.hackathon = make_hackathon(self.org)
        self.project = make_project(self.hackathon, self.participant)

    def test_organizer_and_jury_get_the_button(self):
        for user in (self.org, self.jury):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                resp = self.client.get(reverse('projects:detail', args=[self.project.pk]))
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.context['can_analyze'])
                self.assertContains(resp, 'data-ai-launch')
                self.assertContains(resp, 'value="%s"' % self.project.pk)

    def test_participant_sees_no_analysis_entry(self):
        # The team's own member reads the project but is not part of the
        # decision-support loop, so no launch form reaches their browser.
        self.client.force_login(self.participant)
        resp = self.client.get(reverse('projects:detail', args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['can_analyze'])
        self.assertNotContains(resp, 'name="project"')

    def test_a_project_without_a_submitted_repository_offers_nothing(self):
        project = make_project(self.hackathon, self.participant, title='No repo',
                              repo_url='')
        self.client.force_login(self.org)
        resp = self.client.get(reverse('projects:detail', args=[project.pk]))
        self.assertFalse(resp.context['can_analyze'])
        self.assertNotContains(resp, 'name="project"')

    def test_existing_reports_of_this_project_are_listed(self):
        mine = stored_report(self.org, self.project, criteria=make_criteria(self.hackathon))
        other = make_project(self.hackathon, self.participant, title='Other project',
                             repo_url='https://github.com/example/other')
        hidden = stored_report(self.org, other, criteria=make_criteria(self.hackathon))
        self.client.force_login(self.org)
        resp = self.client.get(reverse('projects:detail', args=[self.project.pk]))
        self.assertEqual([r.pk for r in resp.context['ai_reports']], [mine.pk])
        self.assertNotIn(hidden.pk, [r.pk for r in resp.context['ai_reports']])
        self.assertContains(resp, reverse('analyses:detail', args=[mine.pk]))


class RelaunchTests(TestCase):
    def setUp(self):
        self.org = make_user('rl_org', O)
        self.jury = make_user('rl_jury', J)
        self.participant = make_user('rl_part', P)
        self.hackathon = make_hackathon(self.org)
        self.criteria = make_criteria(self.hackathon)
        self.project = make_project(self.hackathon, self.participant)
        self.report = stored_report(self.org, self.project, criteria=self.criteria)
        patcher = mock.patch.dict('os.environ',
                                 {'GEMINI_API_KEY': 'test-key', 'GEMINI_MODEL': 'gemini-test'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def relaunch(self, user):
        self.client.force_login(user)
        specs = rubric.project_specs(self.criteria)
        with mock.patch('analyses.views.analyze_repository') as analyze:
            analyze.return_value = (bundle_with(), rubric.validate_report(
                project_payload(specs), specs, score_max=10,
                allowed_paths=['app.py', 'README.md']))
            return self.client.post(reverse('analyses:relaunch', args=[self.report.pk]))

    def test_get_never_relaunches(self):
        self.client.force_login(self.org)
        resp = self.client.get(reverse('analyses:relaunch', args=[self.report.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.count(), 1)

    def test_relaunch_adds_history_and_keeps_the_old_report(self):
        resp = self.relaunch(self.org)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.count(), 2)
        fresh = AIReport.objects.exclude(pk=self.report.pk).get()
        self.assertEqual(fresh.project_id, self.project.pk)
        self.assertEqual(fresh.created_by_id, self.org.pk)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, AIReport.Status.DONE)

    def test_running_report_is_not_relaunched(self):
        self.report.status = AIReport.Status.RUNNING
        self.report.save()
        resp = self.relaunch(self.org)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.count(), 1)

    def test_jury_may_relaunch_for_a_scoreable_project(self):
        resp = self.relaunch(self.jury)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.count(), 2)

    def test_jury_cannot_delete_a_visible_report(self):
        self.client.force_login(self.jury)
        resp = self.client.post(reverse('analyses:delete', args=[self.report.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(AIReport.objects.filter(pk=self.report.pk).exists())

    def test_outsider_scope(self):
        self.client.force_login(self.participant)
        self.assertEqual(self.client.get(
            reverse('analyses:detail', args=[self.report.pk])).status_code, 302)
        other_org = make_user('rl_org2', O)
        self.client.force_login(other_org)
        self.assertEqual(self.client.get(
            reverse('analyses:detail', args=[self.report.pk])).status_code, 404)


class AnalysisNeverWritesGradesTests(TestCase):
    """The essential rule: AI assists the jury, it never replaces its decision."""

    def setUp(self):
        self.org = make_user('nw_org', O)
        self.jury = make_user('nw_jury', J)
        self.participant = make_user('nw_part', P)
        self.hackathon = make_hackathon(self.org)
        self.criteria = make_criteria(self.hackathon)
        self.project = make_project(self.hackathon, self.participant)
        self.evaluation = Evaluation.objects.create(
            project=self.project, jury=self.jury,
            status=Evaluation.Status.COMPLETED, is_complete=True,
        )
        for criterion, score in zip(self.criteria, (9, 7, 5)):
            EvaluationScore.objects.create(
                evaluation=self.evaluation, criterion=criterion, score=score)
        patcher = mock.patch.dict('os.environ',
                                 {'GEMINI_API_KEY': 'test-key', 'GEMINI_MODEL': 'gemini-test'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot(self):
        self.evaluation.refresh_from_db()
        return (
            self.evaluation.status,
            self.evaluation.is_complete,
            self.evaluation.total_score,
            sorted(EvaluationScore.objects.filter(evaluation=self.evaluation)
                   .values_list('criterion_id', 'score')),
        )

    def test_analysis_leaves_a_submitted_evaluation_untouched(self):
        before = self.snapshot()
        self.client.force_login(self.org)
        specs = rubric.project_specs(self.criteria)
        with mock.patch('analyses.views.analyze_repository') as analyze:
            analyze.return_value = (bundle_with(), rubric.validate_report(
                project_payload(specs, score=2), specs, score_max=10,
                allowed_paths=['app.py', 'README.md']))
            resp = self.client.post(reverse('analyses:create'), {'project': self.project.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.count(), 1)
        self.assertEqual(self.snapshot(), before)

    def test_suggestions_are_readable_without_being_adopted(self):
        before = self.snapshot()
        report = stored_report(self.org, self.project, criteria=self.criteria)
        mapping = suggestions.suggestions_for_user(self.jury, self.project)
        self.assertEqual(sorted(mapping), [c.pk for c in self.criteria])
        self.assertEqual(mapping[self.criteria[0].pk]['indicative_score'], 8)
        self.assertEqual(self.snapshot(), before)
        report.delete()

    def test_participant_is_outside_the_decision_support_loop(self):
        stored_report(self.org, self.project, criteria=self.criteria)
        self.assertEqual(suggestions.suggestions_for_user(self.participant, self.project), {})
        self.assertIsNone(suggestions.analysis_state_for(self.participant, self.project))


class SuggestionsPayloadTests(TestCase):
    def setUp(self):
        self.org = make_user('sp_org', O)
        self.jury = make_user('sp_jury', J)
        self.participant = make_user('sp_part', P)
        self.hackathon = make_hackathon(self.org)
        self.criteria = make_criteria(self.hackathon)
        self.project = make_project(self.hackathon, self.participant)

    def make_report(self, status):
        return stored_report(self.org, self.project, criteria=self.criteria, status=status)

    def test_done_report_exposes_suggestions_and_report_link(self):
        report = self.make_report(AIReport.Status.DONE)
        state = suggestions.analysis_state_for(self.jury, self.project)
        self.assertEqual(state['status'], 'done')
        self.assertEqual(state['scale_max'], 10)
        self.assertIn(f'/analyses/{report.pk}/', state['report_url'])
        self.assertEqual(len(state['criteria']), 3)
        self.assertEqual(state['criteria'][0]['criterion_id'], self.criteria[0].pk)
        self.assertEqual(state['weighted_indicative'], 8.0)

    def test_running_report_carries_only_its_state(self):
        report = self.make_report(AIReport.Status.RUNNING)
        report.report = {}
        report.save()
        state = suggestions.analysis_state_for(self.jury, self.project)
        self.assertEqual(state['status'], 'running')
        self.assertNotIn('criteria', state)

    def test_failed_report_explains_itself(self):
        report = self.make_report(AIReport.Status.FAILED)
        report.report = {}
        report.error_message = 'The repository was not found. Make sure it is public.'
        report.save()
        state = suggestions.analysis_state_for(self.jury, self.project)
        self.assertEqual(state['status'], 'failed')
        self.assertIn('not found', state['error_message'])
        self.assertEqual(suggestions.suggestions_for_user(self.jury, self.project), {})

    def test_most_recent_report_is_the_one_shown(self):
        old = self.make_report(AIReport.Status.DONE)
        new = self.make_report(AIReport.Status.RUNNING)
        state = suggestions.analysis_state_for(self.jury, self.project)
        self.assertEqual(state['report_id'], new.pk)
        old.delete()
        new.delete()

    def test_out_of_scope_project_has_no_state(self):
        self.make_report(AIReport.Status.DONE)
        other = make_project(make_hackathon(make_user('sp_org2', O), title='Elsewhere'),
                            self.participant, title='Other')
        self.assertIsNone(suggestions.analysis_state_for(self.jury, other))
        self.assertIsNone(suggestions.analysis_state_for(self.jury, None))
