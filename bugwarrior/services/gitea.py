import re
import sys
import urllib.parse

import pydantic
import requests
import typing_extensions

from bugwarrior import config
from bugwarrior.services import IssueService, Issue, ServiceClient

import logging
log = logging.getLogger(__name__)


class GiteaConfig(config.ServiceConfig, prefix='gitea'):
    password: str = 'Deprecated'

    # strictly required
    service: typing_extensions.Literal['gitea']
    host: str
    login: str
    token: str

    # conditionally required
    username: str = ''
    query: str = ''

    # optional
    include_user_repos: bool = True
    include_repos: config.ConfigList = config.ConfigList([])
    exclude_repos: config.ConfigList = config.ConfigList([])
    import_labels_as_tags: bool = False
    label_template: str = '{{label}}'
    filter_pull_requests: bool = False
    exclude_pull_requests: bool = False
    include_user_issues: bool = True
    involved_issues: bool = False
    body_length: int = sys.maxsize
    project_owner_prefix: bool = False
    issue_urls: config.ConfigList = config.ConfigList([])

    @pydantic.root_validator
    def deprecate_password(cls, values):
        if values['password'] != 'Deprecated':
            log.warning(
                'Basic auth is no longer supported. Please remove '
                'gitea.password in favor of gitea.token.')
        return values

    @pydantic.root_validator
    def require_username_or_query(cls, values):
        if not values['username'] and not values['query']:
            raise ValueError(
                'section requires one of:\ngitea.username\ngitea.query')
        return values

    @pydantic.root_validator
    def issue_urls_consistent_with_host(cls, values):
        issue_url_paths = []
        for url in values['issue_urls']:
            parsed_url = urllib.parse.urlparse(url)
            if parsed_url.netloc != values['host']:
                raise ValueError(
                    f'gitea.issue_urls: {url} inconsistent with host {values["host"]}')
            if not re.match(r'^/.*/.*/(issues|pull)/[0-9]*$', parsed_url.path):
                raise ValueError(
                    f'gitea.issue_urls: {parsed_url.path} is not a valid issue path')
            issue_url_paths.append(parsed_url.path)
        values['issue_urls'] = issue_url_paths
        return values

    @pydantic.root_validator
    def require_username_if_include_user_repos(cls, values):
        if values['include_user_repos'] and not values['username']:
            raise ValueError(
                'username required when include_user_repos is True (default)')
        return values


class GiteaClient(ServiceClient):
    def __init__(self, host, auth):
        self.host = host
        self.auth = auth
        self.session = requests.Session()
        if 'token' in self.auth:
            authorization = 'token ' + self.auth['token']
            self.session.headers['Authorization'] = authorization

        self.kwargs = {}
        if 'basic' in self.auth:
            self.kwargs['auth'] = self.auth['basic']

    def _api_url(self, path, **context):
        """ Build the full url to the API endpoint """
        baseurl = f"https://{self.host}/api/v1"
        return baseurl + path.format(**context)

    def _repo_url(self, repo_full_name):
        """ Build the full repo url """
        baseurl = f"https://{self.host}/{repo_full_name}"

    def get_repos(self, username):
        user_repos = self._getter(self._api_url("/user/repos?per_page=100"))
        public_repos = self._getter(self._api_url(
            "/users/{username}/repos?per_page=100", username=username))
        return user_repos + public_repos

    def get_query(self, query):
        """Run a generic issue/PR query"""
        url = self._api_url(
            "/repos/issues/search?q={query}&per_page=100", query=query)
        return self._getter(url, subkey='items')

    def get_issues(self, username, repo):
        url = self._api_url(
            "/repos/{username}/{repo}/issues?per_page=100",
            username=username, repo=repo)
        return self._getter(url)

    def get_directly_assigned_issues(self):
        """ Returns all issues assigned to authenticated user.

        List issues assigned to the authenticated user across all visible
        repositories including owned repositories, member repositories, and
        organization repositories.
        """
        url = self._api_url("/repos/issues/search?per_page=100")
        return self._getter(url)

    def get_issue_for_url_path(self, url_path):
        # The pull request url is '/pull/' but the api path is '/pulls/'.
        api_path = re.sub(r'pull(?=/[0-9]*$)', 'pulls', url_path)
        url = self._api_url(f'/repos{api_path}')
        return self.json_response(self._request(url))

    def get_comments(self, username, repo, number):
        url = self._api_url(
            "/repos/{username}/{repo}/issues/{number}/comments?per_page=100",
            username=username, repo=repo, number=number)
        return self._getter(url)

    def get_pulls(self, username, repo):
        url = self._api_url(
            "/repos/{username}/{repo}/pulls?per_page=100",
            username=username, repo=repo)
        return self._getter(url)

    def _getter(self, url, subkey=None):
        """ Pagination utility.  Obnoxious. """
        results = []
        link = dict(next=url)

        while 'next' in link:
            response = self._request(link['next'])
            json_res = self.json_response(response)

            if subkey is not None:
                json_res = json_res[subkey]

            results += json_res

            link = self._link_field_to_dict(response.headers.get('link', None))

        return results

    def _request(self, url):
        response = self.session.get(url, **self.kwargs)

        # Warn about the mis-leading 404 error code.  See:
        # https://gitea.com/ralphbean/bugwarrior/issues/374
        if response.status_code == 404 and 'token' in self.auth:
            log.warn("A '404' from gitea may indicate an auth "
                     "failure. Make sure both that your token is correct "
                     "and that it has 'public_repo' and not 'public "
                     "access' rights.")

        return response

    @staticmethod
    def _link_field_to_dict(field):
        """ Utility for ripping apart gitea's Link header field.
        It's kind of ugly.
        """

        if not field:
            return dict()

        return {
            part.split('; ')[1][5:-1]:
            part.split('; ')[0][1:-1]
            for part in field.split(', ')
        }


class GiteaIssue(Issue):
    TITLE = 'giteatitle'
    BODY = 'giteabody'
    CREATED_AT = 'giteacreatedon'
    UPDATED_AT = 'giteaupdatedat'
    CLOSED_AT = 'giteaclosedon'
    MILESTONE = 'giteamilestone'
    URL = 'giteaurl'
    REPO = 'gitearepo'
    TYPE = 'giteatype'
    NUMBER = 'giteanumber'
    USER = 'giteauser'
    NAMESPACE = 'giteanamespace'
    STATE = 'giteastate'

    UDAS = {
        TITLE: {
            'type': 'string',
            'label': 'Gitea Title',
        },
        BODY: {
            'type': 'string',
            'label': 'Gitea Body',
        },
        CREATED_AT: {
            'type': 'date',
            'label': 'Gitea Created',
        },
        UPDATED_AT: {
            'type': 'date',
            'label': 'Gitea Updated',
        },
        CLOSED_AT: {
            'type': 'date',
            'label': 'Gitea Closed',
        },
        MILESTONE: {
            'type': 'string',
            'label': 'Gitea Milestone',
        },
        REPO: {
            'type': 'string',
            'label': 'Gitea Repo Slug',
        },
        URL: {
            'type': 'string',
            'label': 'Gitea URL',
        },
        TYPE: {
            'type': 'string',
            'label': 'Gitea Type',
        },
        NUMBER: {
            'type': 'numeric',
            'label': 'Gitea Issue/PR #',
        },
        USER: {
            'type': 'string',
            'label': 'Gitea User',
        },
        NAMESPACE: {
            'type': 'string',
            'label': 'Gitea Namespace',
        },
        STATE: {
            'type': 'string',
            'label': 'Gitea State',
        }
    }
    UNIQUE_KEY = (URL, TYPE,)

    def to_taskwarrior(self):
        milestone = self.record['milestone']
        if milestone:
            milestone = milestone['title']

        created = self.parse_date(self.record.get('created_at'))
        updated = self.parse_date(self.record.get('updated_at'))
        closed = self.parse_date(self.record.get('closed_at'))

        return {
            'project': self.extra['project'],
            'priority': self.origin['default_priority'],
            'annotations': self.extra.get('annotations', []),
            'tags': self.get_tags(),
            'entry': created,
            'end': closed,

            self.URL: self.record['html_url'],
            self.REPO: self.record['repo'],
            self.TYPE: self.extra['type'],
            self.USER: self.record['user']['login'],
            self.TITLE: self.record['title'],
            self.BODY: self.extra['body'],
            self.MILESTONE: milestone,
            self.NUMBER: self.record['number'],
            self.CREATED_AT: created,
            self.UPDATED_AT: updated,
            self.CLOSED_AT: closed,
            self.NAMESPACE: self.extra['namespace'],
            self.STATE: self.record.get('state', '')
        }

    def get_tags(self):
        labels = [label['name'] for label in self.record.get('labels', [])]
        return self.get_tags_from_labels(labels)

    def get_default_description(self):
        return self.build_default_description(
            title=self.record['title'],
            url=self.get_processed_url(self.record['html_url']),
            number=self.record['number'],
            cls=self.extra['type'],
        )


class GiteaService(IssueService):
    ISSUE_CLASS = GiteaIssue
    CONFIG_SCHEMA = GiteaConfig

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)

        auth = {'token': self.get_password('token', self.config.login)}
        self.client = GiteaClient(self.config.host, auth)

    @staticmethod
    def get_keyring_service(config):
        return f"gitea://{config.login}@{config.host}/{config.username}"

    def get_service_metadata(self):
        return {
            'import_labels_as_tags': self.config.import_labels_as_tags,
            'label_template': self.config.label_template,
        }

    def get_owned_repo_issues(self, tag):
        """ Grab all the issues """
        issues = {}
        for issue in self.client.get_issues(*tag.split('/')):
            issues[issue['url']] = (tag, issue)
        return issues

    def get_query(self, query):
        """ Grab all issues matching a gitea query """
        issues = {}
        for issue in self.client.get_query(query):
            url = issue['html_url']
            try:
                repo = self.get_repository_from_issue(issue)
            except ValueError as e:
                log.critical(e)
            else:
                issues[url] = (repo, issue)
        return issues

    def get_directly_assigned_issues(self):
        issues = {}
        for issue in self.client.get_directly_assigned_issues():
            repo = self.get_repository_from_issue(issue)
            issues[issue['url']] = (repo, issue)
        return issues

    def get_issues_by_url(self):
        issues = {}
        for url_path in self.config.issue_urls:
            issue = self.client.get_issue_for_url_path(url_path)
            repo = re.search(r'(?<=^/)(.*/.*)(?=/(issues|pull)/[0-9]*$)', url_path)[0]
            issues[url_path] = (repo, issue)
        return issues

    @classmethod
    def get_repository_from_issue(cls, issue):
        if 'repository' in issue and 'full_name' in issue['repository']:
            return issue['repository']['full_name']
        raise ValueError("Issue has no repository url" + str(issue))

    def _comments(self, tag, number):
        user, repo = tag.split('/')
        return self.client.get_comments(user, repo, number)

    def annotations(self, tag, issue, issue_obj):
        url = issue['html_url']
        annotations = []
        if self.main_config.annotation_comments:
            comments = self._comments(tag, issue['number'])
            log.debug(" got comments for %s", issue['html_url'])
            annotations = ((
                c['user']['login'],
                c['body'],
            ) for c in comments)
        return self.build_annotations(
            annotations,
            issue_obj.get_processed_url(url)
        )

    def body(self, issue):
        body = issue['body']

        if body:
            body = body.replace('\r\n', '\n')
            max_length = self.config.body_length
            body = body[:max_length]

        return body

    def _reqs(self, tag):
        """ Grab all the pull requests """
        return [
            (tag, i) for i in
            self.client.get_pulls(*tag.split('/'))
        ]

    def get_owner(self, issue):
        if issue[1]['assignee']:
            return issue[1]['assignee']['login']

    def filter_issues(self, issue):
        repo, _ = issue
        return self.filter_repo_name(repo.split('/')[-3])

    def filter_repos(self, repo):
        if repo['owner']['login'] != self.config.username:
            return False

        return self.filter_repo_name(repo['name'])

    def filter_repo_name(self, name):
        if name in self.config.exclude_repos:
            return False

        if self.config.include_repos:
            if name in self.config.include_repos:
                return True
            else:
                return False

        return True

    def include(self, issue):
        if 'pull_request' in issue[1]:
            if self.config.exclude_pull_requests:
                return False
            if not self.config.filter_pull_requests:
                return True
        return super().include(issue)

    def issues(self):
        issues = {}
        if self.config.query:
            issues.update(self.get_query(self.config.query))
        elif self.config.involved_issues:
            issues.update(self.get_query('involves:{user} state:open'.format(
                user=self.config.username)))

        if self.config.include_user_repos:
            # Only query for all repos if an explicit
            # include_repos list is not specified.
            if self.config.include_repos:
                repos = self.config.include_repos
            else:
                all_repos = self.client.get_repos(self.config.username)
                repos = filter(self.filter_repos, all_repos)
                repos = [repo['name'] for repo in repos]

            for repo in repos:
                issues.update(
                    self.get_owned_repo_issues(
                        self.config.username + "/" + repo)
                )
        if self.config.include_user_issues:
            issues.update(
                filter(self.filter_issues,
                       self.get_directly_assigned_issues().items())
            )
        if self.config.issue_urls:
            issues.update(filter(self.filter_issues, self.get_issues_by_url().items()))

        log.debug(" Found %i issues.", len(issues))
        issues = list(filter(self.include, issues.values()))
        log.debug(" Pruned down to %i issues.", len(issues))

        for tag, issue in issues:
            # Stuff this value into the upstream dict for:
            # https://github.com/ralphbean/bugwarrior/issues/159
            issue['repo'] = tag

            issue_obj = self.get_issue_for_record(issue)
            tagParts = tag.split('/')
            projectName = tagParts[1]
            if self.config.project_owner_prefix:
                projectName = tagParts[0] + "." + projectName
            extra = {
                'project': projectName,
                'type': 'pull_request' if 'pull_request' in issue else 'issue',
                'annotations': self.annotations(tag, issue, issue_obj),
                'body': self.body(issue),
                'namespace': self.config.username,
            }
            issue_obj.update_extra(extra)
            yield issue_obj
