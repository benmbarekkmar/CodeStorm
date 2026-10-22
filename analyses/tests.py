import base64
import io
import json
import urllib.error
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from analyses.models import AIReport
from analyses.services import github, llm, rubric
from analyses.services.analyzer import analyze_repository
from analyses.services.errors import (
    GitHubNotFoundError,
    GitHubPrivateError,
    GitHubRateLimitError,
    LLMConfigError,
    LLMError,
    LLMRateLimitError,
    LLMRejectedError,
    MalformedResponseError,
    OversizedRepoError,
    RequestTimeoutError,
)

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

    def test_jury_blocked_v1(self):
        # Jury is intentionally not granted access until report scoping exists.
        self.login(self.jury)
        resp = self.client.get(reverse('analyses:home'))
        self.assertEqual(resp.status_code, 302)

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

    def _payload(self):
        bundle = github.RepoBundle(owner='foo', repo='bar', default_branch='main',
                                   commit_ref='cafe', description='', primary_language='',
                                   size_kb=1, files=[github.SourceFile('a.py', 'x')])
        return bundle, ValidReport.payload()

    def test_organizer_create_done(self):
        self.login(self.org)
        with mock.patch('analyses.views.analyze_repository', return_value=self._payload()):
            resp = self.client.post(reverse('analyses:create'),
                                    {'repository_url': 'https://github.com/foo/bar'})
        self.assertEqual(resp.status_code, 302)
        report = AIReport.objects.get(created_by=self.org)
        self.assertEqual(report.status, AIReport.Status.DONE)
        self.assertEqual(report.commit_ref, 'cafe')

    def test_invalid_url_no_report_created(self):
        self.login(self.org)
        resp = self.client.post(reverse('analyses:create'),
                                {'repository_url': 'https://evil.example.com/a/b'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('repository_url', resp.context['form'].errors)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_analysis_failure_persisted_as_failed(self):
        self.login(self.org)
        with mock.patch('analyses.views.analyze_repository',
                        side_effect=GitHubNotFoundError()):
            resp = self.client.post(reverse('analyses:create'),
                                    {'repository_url': 'https://github.com/foo/bar'})
        self.assertEqual(resp.status_code, 302)
        report = AIReport.objects.get(created_by=self.org)
        self.assertEqual(report.status, AIReport.Status.FAILED)
        self.assertTrue(report.error_message)

    def test_missing_key_shows_config_error(self):
        self.login(self.org)
        patcher = mock.patch.dict('os.environ', {'GEMINI_API_KEY': ''})
        patcher.start(); self.addCleanup(patcher.stop)
        resp = self.client.post(reverse('analyses:create'),
                                {'repository_url': 'https://github.com/foo/bar'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_missing_model_shows_config_error(self):
        self.login(self.org)
        patcher = mock.patch.dict('os.environ', {'GEMINI_MODEL': ''})
        patcher.start(); self.addCleanup(patcher.stop)
        resp = self.client.post(reverse('analyses:create'),
                                {'repository_url': 'https://github.com/foo/bar'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_participant_cannot_create_direct(self):
        self.login(self.participant)
        resp = self.client.post(reverse('analyses:create'),
                                {'repository_url': 'https://github.com/foo/bar'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AIReport.objects.count(), 0)


class CsrfTests(ViewsBase):
    def test_post_without_csrf_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.org)
        resp = csrf_client.post(reverse('analyses:create'),
                                {'repository_url': 'https://github.com/foo/bar'})
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
