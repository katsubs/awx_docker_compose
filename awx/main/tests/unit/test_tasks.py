# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
from pathlib import Path

import fcntl
from unittest import mock
import pytest
import yaml

from awx_plugins.interfaces._temporary_private_container_api import CONTAINER_ROOT

from django.conf import settings

from awx.main.models import (
    AdHocCommand,
    Credential,
    CredentialType,
    ExecutionEnvironment,
    Inventory,
    InventorySource,
    InventoryUpdate,
    Job,
    JobTemplate,
    Notification,
    Organization,
    Project,
    ProjectUpdate,
    UnifiedJob,
    User,
    build_safe_env,
)
from awx.main.models.credential import HIDDEN_PASSWORD, ManagedCredentialType

from awx.main.tasks import jobs, system, receptor
from awx.main.utils import encrypt_field, encrypt_value
from awx.main.utils.safe_yaml import SafeLoader

from awx.main.utils.licensing import Licenser
from awx.main.constants import JOB_VARIABLE_PREFIXES

from receptorctl.socket_interface import ReceptorControl


def to_host_path(path, private_data_dir):
    """Given a path inside of the EE container, this gives the absolute path
    on the host machine within the private_data_dir
    """
    if not os.path.isabs(private_data_dir):
        raise RuntimeError('The private_data_dir path must be absolute')
    if CONTAINER_ROOT != path and Path(CONTAINER_ROOT) not in Path(path).resolve().parents:
        raise RuntimeError(f'Cannot convert path {path} unless it is a subdir of {CONTAINER_ROOT}')
    return path.replace(CONTAINER_ROOT, private_data_dir, 1)


class TestJobExecution(object):
    EXAMPLE_PRIVATE_KEY = '-----BEGIN PRIVATE KEY-----\nxyz==\n-----END PRIVATE KEY-----'


@pytest.fixture
def private_data_dir():
    private_data = tempfile.mkdtemp(prefix='awx_')
    for subfolder in ('inventory', 'env'):
        runner_subfolder = os.path.join(private_data, subfolder)
        if not os.path.exists(runner_subfolder):
            os.mkdir(runner_subfolder)
    yield private_data
    shutil.rmtree(private_data, True)


@pytest.fixture
def patch_Job():
    with mock.patch.object(Job, 'cloud_credentials') as mock_cred:
        mock_cred.__get__ = lambda *args, **kwargs: []
        with mock.patch.object(Job, 'network_credentials') as mock_net:
            mock_net.__get__ = lambda *args, **kwargs: []
            yield


@pytest.fixture
def mock_create_partition():
    with mock.patch('awx.main.tasks.jobs.create_partition') as cp_mock:
        yield cp_mock


@pytest.fixture
def patch_Organization():
    _credentials = []
    credentials_mock = mock.Mock(
        **{
            'all': lambda: _credentials,
            'add': _credentials.append,
            'exists': lambda: len(_credentials) > 0,
            'spec_set': ['all', 'add', 'exists'],
        }
    )
    with mock.patch.object(Organization, 'galaxy_credentials', credentials_mock):
        yield


@pytest.fixture
def job():
    return Job(pk=1, id=1, project=Project(local_path='/projects/_23_foo'), inventory=Inventory(), job_template=JobTemplate(id=1, name='foo'))


@pytest.fixture
def adhoc_job():
    return AdHocCommand(pk=1, id=1, inventory=Inventory())


@pytest.fixture
def update_model_wrapper(job):
    def fn(pk, **kwargs):
        for k, v in kwargs.items():
            setattr(job, k, v)
        return job

    return fn


@pytest.fixture
def adhoc_update_model_wrapper(adhoc_job):
    def fn(pk, **kwargs):
        for k, v in kwargs.items():
            setattr(adhoc_job, k, v)
        return adhoc_job

    return fn


def test_send_notifications_not_list():
    with pytest.raises(TypeError):
        system.send_notifications(None)


def test_send_notifications_job_id(mocker):
    mocker.patch('awx.main.models.UnifiedJob.objects.get')
    system.send_notifications([], job_id=1)
    assert UnifiedJob.objects.get.called
    assert UnifiedJob.objects.get.called_with(id=1)


@mock.patch('awx.main.models.UnifiedJob.objects.get')
@mock.patch('awx.main.models.Notification.objects.filter')
def test_send_notifications_list(mock_notifications_filter, mock_job_get, mocker):
    mock_job = mocker.MagicMock(spec=UnifiedJob)
    mock_job_get.return_value = mock_job
    mock_notifications = [mocker.MagicMock(spec=Notification, subject="test", body={'hello': 'world'})]
    mock_notifications_filter.return_value = mock_notifications

    system.send_notifications([1, 2], job_id=1)
    assert Notification.objects.filter.call_count == 1
    assert mock_notifications[0].status == "successful"
    assert mock_notifications[0].save.called

    assert mock_job.notifications.add.called
    assert mock_job.notifications.add.called_with(*mock_notifications)


@pytest.mark.parametrize(
    "key,value",
    [
        ('REST_API_TOKEN', 'SECRET'),
        ('SECRET_KEY', 'SECRET'),
        ('VMWARE_PASSWORD', 'SECRET'),
        ('API_SECRET', 'SECRET'),
        ('ANSIBLE_GALAXY_SERVER_PRIMARY_GALAXY_TOKEN', 'SECRET'),
    ],
)
def test_safe_env_filtering(key, value):
    assert build_safe_env({key: value})[key] == HIDDEN_PASSWORD


def test_safe_env_returns_new_copy():
    env = {'foo': 'bar'}
    assert build_safe_env(env) is not env


@pytest.mark.parametrize("source,expected", [(None, True), (False, False), (True, True)])
def test_openstack_client_config_generation(mocker, source, expected, private_data_dir, mock_me):
    update = jobs.RunInventoryUpdate()
    credential_type = CredentialType.defaults['openstack']()
    inputs = {
        'host': 'https://keystone.openstack.example.org',
        'username': 'demo',
        'password': 'secrete',
        'project': 'demo-project',
        'domain': 'my-demo-domain',
    }
    if source is not None:
        inputs['verify_ssl'] = source
    credential = Credential(pk=1, credential_type=credential_type, inputs=inputs)

    inventory_update = mocker.Mock(
        **{
            'source': 'openstack',
            'source_vars_dict': {},
            'get_cloud_credential': mocker.Mock(return_value=credential),
            'get_extra_credentials': lambda x: [],
        }
    )
    cloud_config = update.build_private_data(inventory_update, private_data_dir)
    cloud_credential = yaml.safe_load(cloud_config.get('credentials')[credential])
    assert cloud_credential['clouds'] == {
        'devstack': {
            'auth': {
                'auth_url': 'https://keystone.openstack.example.org',
                'password': 'secrete',
                'project_name': 'demo-project',
                'username': 'demo',
                'domain_name': 'my-demo-domain',
            },
            'verify': expected,
            'private': True,
        }
    }


@pytest.mark.parametrize("source,expected", [(None, True), (False, False), (True, True)])
def test_openstack_client_config_generation_with_project_domain_name(mocker, source, expected, private_data_dir, mock_me):
    update = jobs.RunInventoryUpdate()
    credential_type = CredentialType.defaults['openstack']()
    inputs = {
        'host': 'https://keystone.openstack.example.org',
        'username': 'demo',
        'password': 'secrete',
        'project': 'demo-project',
        'domain': 'my-demo-domain',
        'project_domain_name': 'project-domain',
    }
    if source is not None:
        inputs['verify_ssl'] = source
    credential = Credential(pk=1, credential_type=credential_type, inputs=inputs)

    inventory_update = mocker.Mock(
        **{
            'source': 'openstack',
            'source_vars_dict': {},
            'get_cloud_credential': mocker.Mock(return_value=credential),
            'get_extra_credentials': lambda x: [],
        }
    )
    cloud_config = update.build_private_data(inventory_update, private_data_dir)
    cloud_credential = yaml.safe_load(cloud_config.get('credentials')[credential])
    assert cloud_credential['clouds'] == {
        'devstack': {
            'auth': {
                'auth_url': 'https://keystone.openstack.example.org',
                'password': 'secrete',
                'project_name': 'demo-project',
                'username': 'demo',
                'domain_name': 'my-demo-domain',
                'project_domain_name': 'project-domain',
            },
            'verify': expected,
            'private': True,
        }
    }


@pytest.mark.parametrize("source,expected", [(None, True), (False, False), (True, True)])
def test_openstack_client_config_generation_with_region(mocker, source, expected, private_data_dir, mock_me):
    update = jobs.RunInventoryUpdate()
    credential_type = CredentialType.defaults['openstack']()
    inputs = {
        'host': 'https://keystone.openstack.example.org',
        'username': 'demo',
        'password': 'secrete',
        'project': 'demo-project',
        'domain': 'my-demo-domain',
        'project_domain_name': 'project-domain',
        'region': 'region-name',
    }
    if source is not None:
        inputs['verify_ssl'] = source
    credential = Credential(pk=1, credential_type=credential_type, inputs=inputs)

    inventory_update = mocker.Mock(
        **{
            'source': 'openstack',
            'source_vars_dict': {},
            'get_cloud_credential': mocker.Mock(return_value=credential),
            'get_extra_credentials': lambda x: [],
        }
    )
    cloud_config = update.build_private_data(inventory_update, private_data_dir)
    cloud_credential = yaml.safe_load(cloud_config.get('credentials')[credential])
    assert cloud_credential['clouds'] == {
        'devstack': {
            'auth': {
                'auth_url': 'https://keystone.openstack.example.org',
                'password': 'secrete',
                'project_name': 'demo-project',
                'username': 'demo',
                'domain_name': 'my-demo-domain',
                'project_domain_name': 'project-domain',
            },
            'verify': expected,
            'private': True,
            'region_name': 'region-name',
        }
    }


@pytest.mark.parametrize("source,expected", [(False, False), (True, True)])
def test_openstack_client_config_generation_with_private_source_vars(mocker, source, expected, private_data_dir, mock_me):
    update = jobs.RunInventoryUpdate()
    credential_type = CredentialType.defaults['openstack']()
    inputs = {
        'host': 'https://keystone.openstack.example.org',
        'username': 'demo',
        'password': 'secrete',
        'project': 'demo-project',
        'domain': None,
        'verify_ssl': True,
    }
    credential = Credential(pk=1, credential_type=credential_type, inputs=inputs)

    inventory_update = mocker.Mock(
        **{
            'source': 'openstack',
            'source_vars_dict': {'private': source},
            'get_cloud_credential': mocker.Mock(return_value=credential),
            'get_extra_credentials': lambda x: [],
        }
    )
    cloud_config = update.build_private_data(inventory_update, private_data_dir)
    cloud_credential = yaml.load(cloud_config.get('credentials')[credential], Loader=SafeLoader)
    assert cloud_credential['clouds'] == {
        'devstack': {
            'auth': {'auth_url': 'https://keystone.openstack.example.org', 'password': 'secrete', 'project_name': 'demo-project', 'username': 'demo'},
            'verify': True,
            'private': expected,
        }
    }


def pytest_generate_tests(metafunc):
    # pytest.mark.parametrize doesn't work on unittest.TestCase methods
    # see: https://docs.pytest.org/en/latest/example/parametrize.html#parametrizing-test-methods-through-per-class-configuration
    if metafunc.cls and hasattr(metafunc.cls, 'parametrize'):
        funcarglist = metafunc.cls.parametrize.get(metafunc.function.__name__)
        if funcarglist:
            argnames = sorted(funcarglist[0])
            metafunc.parametrize(argnames, [[funcargs[name] for name in argnames] for funcargs in funcarglist])


def parse_extra_vars(args, private_data_dir):
    extra_vars = {}
    for chunk in args:
        if chunk.startswith(f'@{CONTAINER_ROOT}'):
            local_path = chunk[len('@') :].replace(CONTAINER_ROOT, private_data_dir)  # container path to host path
            with open(local_path, 'r') as f:
                extra_vars.update(yaml.load(f, Loader=SafeLoader))
    return extra_vars


class TestExtraVarSanitation(TestJobExecution):
    # By default, extra vars are marked as `!unsafe` in the generated yaml
    # _unless_ they've been specified on the JobTemplate's extra_vars (which
    # are deemed trustable, because they can only be added by users w/ enough
    # privilege to add/modify a Job Template)

    UNSAFE = "{{ lookup('pipe', 'ls -la') }}"

    def test_vars_unsafe_by_default(self, job, private_data_dir, mock_me):
        job.created_by = User(pk=123, username='angry-spud')
        job.inventory = Inventory(pk=123, name='example-inv')

        task = jobs.RunJob()
        task.build_extra_vars_file(job, private_data_dir)

        with open(os.path.join(private_data_dir, 'env', 'extravars')) as fd:
            extra_vars = yaml.load(fd, Loader=SafeLoader)

        # ensure that strings are marked as unsafe
        for name in JOB_VARIABLE_PREFIXES:
            for variable_name in ['_job_template_name', '_user_name', '_job_launch_type', '_project_revision', '_inventory_name']:
                assert hasattr(extra_vars['{}{}'.format(name, variable_name)], '__UNSAFE__')

        # ensure that non-strings are marked as safe
        for name in JOB_VARIABLE_PREFIXES:
            for variable_name in ['_job_template_id', '_job_id', '_user_id', '_inventory_id']:
                assert not hasattr(extra_vars['{}{}'.format(name, variable_name)], '__UNSAFE__')

    def test_launchtime_vars_unsafe(self, job, private_data_dir, mock_me):
        job.extra_vars = json.dumps({'msg': self.UNSAFE})
        task = jobs.RunJob()

        task.build_extra_vars_file(job, private_data_dir)

        with open(os.path.join(private_data_dir, 'env', 'extravars')) as fd:
            extra_vars = yaml.load(fd, Loader=SafeLoader)
        assert extra_vars['msg'] == self.UNSAFE
        assert hasattr(extra_vars['msg'], '__UNSAFE__')

    def test_nested_launchtime_vars_unsafe(self, job, private_data_dir, mock_me):
        job.extra_vars = json.dumps({'msg': {'a': [self.UNSAFE]}})
        task = jobs.RunJob()

        task.build_extra_vars_file(job, private_data_dir)

        with open(os.path.join(private_data_dir, 'env', 'extravars')) as fd:
            extra_vars = yaml.load(fd, Loader=SafeLoader)
        assert extra_vars['msg'] == {'a': [self.UNSAFE]}
        assert hasattr(extra_vars['msg']['a'][0], '__UNSAFE__')

    def test_allowed_jt_extra_vars(self, job, private_data_dir, mock_me):
        job.job_template.extra_vars = job.extra_vars = json.dumps({'msg': self.UNSAFE})
        task = jobs.RunJob()

        task.build_extra_vars_file(job, private_data_dir)

        with open(os.path.join(private_data_dir, 'env', 'extravars')) as fd:
            extra_vars = yaml.load(fd, Loader=SafeLoader)
        assert extra_vars['msg'] == self.UNSAFE
        assert not hasattr(extra_vars['msg'], '__UNSAFE__')

    def test_nested_allowed_vars(self, job, private_data_dir, mock_me):
        job.extra_vars = json.dumps({'msg': {'a': {'b': [self.UNSAFE]}}})
        job.job_template.extra_vars = job.extra_vars
        task = jobs.RunJob()

        task.build_extra_vars_file(job, private_data_dir)

        with open(os.path.join(private_data_dir, 'env', 'extravars')) as fd:
            extra_vars = yaml.load(fd, Loader=SafeLoader)
        assert extra_vars['msg'] == {'a': {'b': [self.UNSAFE]}}
        assert not hasattr(extra_vars['msg']['a']['b'][0], '__UNSAFE__')

    def test_sensitive_values_dont_leak(self, job, private_data_dir, mock_me):
        # JT defines `msg=SENSITIVE`, the job *should not* be able to do
        # `other_var=SENSITIVE`
        job.job_template.extra_vars = json.dumps({'msg': self.UNSAFE})
        job.extra_vars = json.dumps({'msg': 'other-value', 'other_var': self.UNSAFE})
        task = jobs.RunJob()

        task.build_extra_vars_file(job, private_data_dir)

        with open(os.path.join(private_data_dir, 'env', 'extravars')) as fd:
            extra_vars = yaml.load(fd, Loader=SafeLoader)
        assert extra_vars['msg'] == 'other-value'
        assert hasattr(extra_vars['msg'], '__UNSAFE__')

        assert extra_vars['other_var'] == self.UNSAFE
        assert hasattr(extra_vars['other_var'], '__UNSAFE__')

    def test_overwritten_jt_extra_vars(self, job, private_data_dir, mock_me):
        job.job_template.extra_vars = json.dumps({'msg': 'SAFE'})
        job.extra_vars = json.dumps({'msg': self.UNSAFE})
        task = jobs.RunJob()

        task.build_extra_vars_file(job, private_data_dir)

        with open(os.path.join(private_data_dir, 'env', 'extravars')) as fd:
            extra_vars = yaml.load(fd, Loader=SafeLoader)
        assert extra_vars['msg'] == self.UNSAFE
        assert hasattr(extra_vars['msg'], '__UNSAFE__')


class TestGenericRun:
    def test_generic_failure(self, patch_Job, execution_environment, mock_me, mock_create_partition):
        job = Job(status='running', inventory=Inventory(), project=Project(local_path='/projects/_23_foo'))
        job.websocket_emit_status = mock.Mock()
        job.execution_environment = execution_environment

        task = jobs.RunJob()
        task.instance = job
        task.update_model = mock.Mock(return_value=job)
        task.model.objects.get = mock.Mock(return_value=job)
        task.build_private_data_files = mock.Mock(side_effect=OSError())

        with mock.patch('awx.main.tasks.jobs.shutil.copytree'):
            with pytest.raises(Exception):
                task.run(1)

        update_model_call = task.update_model.call_args[1]
        assert 'OSError' in update_model_call['result_traceback']
        assert update_model_call['status'] == 'error'
        assert update_model_call['emitted_events'] == 0

    def test_cancel_flag(self, job, update_model_wrapper, execution_environment, mock_me, mock_create_partition):
        job.status = 'running'
        job.cancel_flag = True
        job.websocket_emit_status = mock.Mock()
        job.send_notification_templates = mock.Mock()
        job.execution_environment = execution_environment

        task = jobs.RunJob()
        task.instance = job
        task.update_model = mock.Mock(wraps=update_model_wrapper)
        task.model.objects.get = mock.Mock(return_value=job)
        task.build_private_data_files = mock.Mock()

        with mock.patch('awx.main.tasks.jobs.shutil.copytree'):
            with pytest.raises(Exception):
                task.run(1)

        for c in [mock.call(1, start_args='', status='canceled')]:
            assert c in task.update_model.call_args_list

    def test_event_count(self, mock_me):
        task = jobs.RunJob()
        task.runner_callback.dispatcher = mock.MagicMock()
        task.runner_callback.instance = Job()
        task.runner_callback.event_ct = 0
        event_data = {}

        [task.runner_callback.event_handler(event_data) for i in range(20)]
        assert 20 == task.runner_callback.event_ct

    def test_finished_callback_eof(self, mock_me):
        task = jobs.RunJob()
        task.runner_callback.dispatcher = mock.MagicMock()
        task.runner_callback.instance = Job(pk=1, id=1)
        task.runner_callback.event_ct = 17
        task.runner_callback.finished_callback(None)
        task.runner_callback.dispatcher.dispatch.assert_called_with({'event': 'EOF', 'final_counter': 17, 'job_id': 1, 'guid': None})

    def test_save_job_metadata(self, job, update_model_wrapper, mock_me):
        class MockMe:
            pass

        task = jobs.RunJob()
        task.runner_callback.instance = job
        task.runner_callback.safe_env = {'secret_key': 'redacted_value'}
        task.runner_callback.update_model = mock.Mock(wraps=update_model_wrapper)
        runner_config = MockMe()
        runner_config.command = {'foo': 'bar'}
        runner_config.cwd = '/foobar'
        runner_config.env = {'switch': 'blade', 'foot': 'ball', 'secret_key': 'secret_value'}
        task.runner_callback.status_handler({'status': 'starting'}, runner_config)

        task.runner_callback.update_model.assert_called_with(
            1, job_args=json.dumps({'foo': 'bar'}), job_cwd='/foobar', job_env={'switch': 'blade', 'foot': 'ball', 'secret_key': 'redacted_value'}
        )

    def test_created_by_extra_vars(self, mock_me):
        job = Job(created_by=User(pk=123, username='angry-spud'))

        task = jobs.RunJob()
        task._write_extra_vars_file = mock.Mock()
        task.build_extra_vars_file(job, None)

        call_args, _ = task._write_extra_vars_file.call_args_list[0]

        private_data_dir, extra_vars, safe_dict = call_args
        for name in JOB_VARIABLE_PREFIXES:
            assert extra_vars['{}_user_id'.format(name)] == 123
            assert extra_vars['{}_user_name'.format(name)] == "angry-spud"

    def test_survey_extra_vars(self, mock_me):
        job = Job()
        job.extra_vars = json.dumps({'super_secret': encrypt_value('CLASSIFIED', pk=None)})
        job.survey_passwords = {'super_secret': '$encrypted$'}

        task = jobs.RunJob()
        task._write_extra_vars_file = mock.Mock()
        task.build_extra_vars_file(job, None)

        call_args, _ = task._write_extra_vars_file.call_args_list[0]

        private_data_dir, extra_vars, safe_dict = call_args
        assert extra_vars['super_secret'] == "CLASSIFIED"

    def test_awx_task_env(self, patch_Job, private_data_dir, execution_environment, mock_me):
        job = Job(project=Project(), inventory=Inventory())
        job.execution_environment = execution_environment

        task = jobs.RunJob()
        task.instance = job
        task._write_extra_vars_file = mock.Mock()

        with mock.patch('awx.main.tasks.jobs.settings.AWX_TASK_ENV', {'FOO': 'BAR'}):
            env = task.build_env(job, private_data_dir)
        assert env['FOO'] == 'BAR'


@pytest.mark.django_db
class TestAdhocRun(TestJobExecution):
    def test_options_jinja_usage(self, adhoc_job, adhoc_update_model_wrapper, mock_me, mock_create_partition):
        ExecutionEnvironment.objects.create(name='Control Plane EE', managed=True)
        ExecutionEnvironment.objects.create(name='Default Job EE', managed=False)

        adhoc_job.module_args = '{{ ansible_ssh_pass }}'
        adhoc_job.websocket_emit_status = mock.Mock()
        adhoc_job.send_notification_templates = mock.Mock()

        task = jobs.RunAdHocCommand()
        task.update_model = mock.Mock(wraps=adhoc_update_model_wrapper)
        task.model.objects.get = mock.Mock(return_value=adhoc_job)
        task.build_inventory = mock.Mock()

        with pytest.raises(Exception):
            task.run(adhoc_job.pk)

        call_args, _ = task.update_model.call_args_list[0]
        update_model_call = task.update_model.call_args[1]
        assert 'Jinja variables are not allowed' in update_model_call['result_traceback']

    '''
    TODO: The jinja action is in _write_extra_vars_file. The extra vars should
    be wrapped in unsafe
    '''
    '''
    def test_extra_vars_jinja_usage(self, adhoc_job, adhoc_update_model_wrapper, mock_me):
        adhoc_job.module_args = 'ls'
        adhoc_job.extra_vars = json.dumps({
            'foo': '{{ bar }}'
        })
        #adhoc_job.websocket_emit_status = mock.Mock()

        task = jobs.RunAdHocCommand()
        #task.update_model = mock.Mock(wraps=adhoc_update_model_wrapper)
        #task.build_inventory = mock.Mock(return_value='/tmp/something.inventory')
        task._write_extra_vars_file = mock.Mock()

        task.build_extra_vars_file(adhoc_job, 'ignore')

        call_args, _ = task._write_extra_vars_file.call_args_list[0]
        private_data_dir, extra_vars = call_args
        assert extra_vars['foo'] == '{{ bar }}'
    '''

    def test_created_by_extra_vars(self, mock_me):
        adhoc_job = AdHocCommand(created_by=User(pk=123, username='angry-spud'))

        task = jobs.RunAdHocCommand()
        task._write_extra_vars_file = mock.Mock()
        task.build_extra_vars_file(adhoc_job, None)

        call_args, _ = task._write_extra_vars_file.call_args_list[0]

        private_data_dir, extra_vars = call_args
        for name in JOB_VARIABLE_PREFIXES:
            assert extra_vars['{}_user_id'.format(name)] == 123
            assert extra_vars['{}_user_name'.format(name)] == "angry-spud"


class TestJobCredentials(TestJobExecution):
    @pytest.fixture
    def job(self, execution_environment):
        job = Job(pk=1, inventory=Inventory(pk=1), project=Project(pk=1))
        job.websocket_emit_status = mock.Mock()
        job._credentials = []

        job.execution_environment = execution_environment

        def _credentials_filter(credential_type__kind=None):
            creds = job._credentials
            if credential_type__kind:
                creds = [c for c in creds if c.credential_type.kind == credential_type__kind]
            return mock.Mock(__iter__=lambda *args: iter(creds), first=lambda: creds[0] if len(creds) else None)

        credentials_mock = mock.Mock(
            **{
                'all': lambda: job._credentials,
                'add': job._credentials.append,
                'filter.side_effect': _credentials_filter,
                'prefetch_related': lambda _: credentials_mock,
                'spec_set': ['all', 'add', 'filter', 'prefetch_related'],
            }
        )

        with mock.patch.object(UnifiedJob, 'credentials', credentials_mock):
            yield job

    @pytest.fixture
    def update_model_wrapper(self, job):
        def fn(pk, **kwargs):
            for k, v in kwargs.items():
                setattr(job, k, v)
            return job

        return fn

    parametrize = {
        'test_ssh_passwords': [
            dict(field='password', password_name='ssh_password', expected_flag='--ask-pass'),
            dict(field='ssh_key_unlock', password_name='ssh_key_unlock', expected_flag=None),
            dict(field='become_password', password_name='become_password', expected_flag='--ask-become-pass'),
        ]
    }

    def test_username_jinja_usage(self, job, private_data_dir, mock_me):
        task = jobs.RunJob()
        ssh = CredentialType.defaults['ssh']()
        credential = Credential(pk=1, credential_type=ssh, inputs={'username': '{{ ansible_ssh_pass }}'})
        job.credentials.add(credential)
        with pytest.raises(ValueError) as e:
            task.build_args(job, private_data_dir, {})

        assert 'Jinja variables are not allowed' in str(e.value)

    @pytest.mark.parametrize("flag", ['become_username', 'become_method'])
    def test_become_jinja_usage(self, job, private_data_dir, flag, mock_me):
        task = jobs.RunJob()
        ssh = CredentialType.defaults['ssh']()
        credential = Credential(pk=1, credential_type=ssh, inputs={'username': 'joe', flag: '{{ ansible_ssh_pass }}'})
        job.credentials.add(credential)

        with pytest.raises(ValueError) as e:
            task.build_args(job, private_data_dir, {})

        assert 'Jinja variables are not allowed' in str(e.value)

    def test_ssh_passwords(self, job, private_data_dir, field, password_name, expected_flag, mock_me):
        task = jobs.RunJob()
        ssh = CredentialType.defaults['ssh']()
        credential = Credential(pk=1, credential_type=ssh, inputs={'username': 'bob', field: 'secret'})
        credential.inputs[field] = encrypt_field(credential, field)
        job.credentials.add(credential)

        passwords = task.build_passwords(job, {})
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)
        args = task.build_args(job, private_data_dir, passwords)

        assert 'secret' in expect_passwords.values()
        assert '-u bob' in ' '.join(args)
        if expected_flag:
            assert expected_flag in ' '.join(args)

    def test_net_ssh_key_unlock(self, job, mock_me):
        task = jobs.RunJob()
        net = CredentialType.defaults['net']()
        credential = Credential(pk=1, credential_type=net, inputs={'ssh_key_unlock': 'secret'})
        credential.inputs['ssh_key_unlock'] = encrypt_field(credential, 'ssh_key_unlock')
        job.credentials.add(credential)

        passwords = task.build_passwords(job, {})
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        assert 'secret' in expect_passwords.values()

    def test_net_first_ssh_key_unlock_wins(self, job, mock_me):
        task = jobs.RunJob()
        for i in range(3):
            net = CredentialType.defaults['net']()
            credential = Credential(pk=i, credential_type=net, inputs={'ssh_key_unlock': 'secret{}'.format(i)})
            credential.inputs['ssh_key_unlock'] = encrypt_field(credential, 'ssh_key_unlock')
            job.credentials.add(credential)

        passwords = task.build_passwords(job, {})
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        assert 'secret0' in expect_passwords.values()

    def test_prefer_ssh_over_net_ssh_key_unlock(self, job, mock_me):
        task = jobs.RunJob()
        net = CredentialType.defaults['net']()
        net_credential = Credential(pk=1, credential_type=net, inputs={'ssh_key_unlock': 'net_secret'})
        net_credential.inputs['ssh_key_unlock'] = encrypt_field(net_credential, 'ssh_key_unlock')

        ssh = CredentialType.defaults['ssh']()
        ssh_credential = Credential(pk=2, credential_type=ssh, inputs={'ssh_key_unlock': 'ssh_secret'})
        ssh_credential.inputs['ssh_key_unlock'] = encrypt_field(ssh_credential, 'ssh_key_unlock')

        job.credentials.add(net_credential)
        job.credentials.add(ssh_credential)

        passwords = task.build_passwords(job, {})
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        assert 'ssh_secret' in expect_passwords.values()

    def test_vault_password(self, private_data_dir, job, mock_me):
        task = jobs.RunJob()
        vault = CredentialType.defaults['vault']()
        credential = Credential(pk=1, credential_type=vault, inputs={'vault_password': 'vault-me'})
        credential.inputs['vault_password'] = encrypt_field(credential, 'vault_password')
        job.credentials.add(credential)

        passwords = task.build_passwords(job, {})
        args = task.build_args(job, private_data_dir, passwords)
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        assert expect_passwords[r'Vault password:\s*?$'] == 'vault-me'  # noqa
        assert '--ask-vault-pass' in ' '.join(args)

    def test_vault_password_ask(self, private_data_dir, job, mock_me):
        task = jobs.RunJob()
        vault = CredentialType.defaults['vault']()
        credential = Credential(pk=1, credential_type=vault, inputs={'vault_password': 'ASK'})
        credential.inputs['vault_password'] = encrypt_field(credential, 'vault_password')
        job.credentials.add(credential)

        passwords = task.build_passwords(job, {'vault_password': 'provided-at-launch'})
        args = task.build_args(job, private_data_dir, passwords)
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        assert expect_passwords[r'Vault password:\s*?$'] == 'provided-at-launch'  # noqa
        assert '--ask-vault-pass' in ' '.join(args)

    def test_multi_vault_password(self, private_data_dir, job, mock_me):
        task = jobs.RunJob()
        vault = CredentialType.defaults['vault']()
        for i, label in enumerate(['dev', 'prod', 'dotted.name']):
            credential = Credential(pk=i, credential_type=vault, inputs={'vault_password': 'pass@{}'.format(label), 'vault_id': label})
            credential.inputs['vault_password'] = encrypt_field(credential, 'vault_password')
            job.credentials.add(credential)

        passwords = task.build_passwords(job, {})
        args = task.build_args(job, private_data_dir, passwords)
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        vault_passwords = dict((k, v) for k, v in expect_passwords.items() if 'Vault' in k)
        assert vault_passwords[r'Vault password \(prod\):\s*?$'] == 'pass@prod'  # noqa
        assert vault_passwords[r'Vault password \(dev\):\s*?$'] == 'pass@dev'  # noqa
        assert vault_passwords[r'Vault password \(dotted.name\):\s*?$'] == 'pass@dotted.name'  # noqa
        assert vault_passwords[r'Vault password:\s*?$'] == ''  # noqa
        assert '--ask-vault-pass' not in ' '.join(args)
        assert '--vault-id dev@prompt' in ' '.join(args)
        assert '--vault-id prod@prompt' in ' '.join(args)
        assert '--vault-id dotted.name@prompt' in ' '.join(args)

    def test_multi_vault_id_conflict(self, job, mock_me):
        task = jobs.RunJob()
        vault = CredentialType.defaults['vault']()
        for i in range(2):
            credential = Credential(pk=i, credential_type=vault, inputs={'vault_password': 'some-pass', 'vault_id': 'conflict'})
            credential.inputs['vault_password'] = encrypt_field(credential, 'vault_password')
            job.credentials.add(credential)

        with pytest.raises(RuntimeError) as e:
            task.build_passwords(job, {})

        assert 'multiple vault credentials were specified with --vault-id' in str(e.value)

    def test_multi_vault_password_ask(self, private_data_dir, job, mock_me):
        task = jobs.RunJob()
        vault = CredentialType.defaults['vault']()
        for i, label in enumerate(['dev', 'prod']):
            credential = Credential(pk=i, credential_type=vault, inputs={'vault_password': 'ASK', 'vault_id': label})
            credential.inputs['vault_password'] = encrypt_field(credential, 'vault_password')
            job.credentials.add(credential)
        passwords = task.build_passwords(job, {'vault_password.dev': 'provided-at-launch@dev', 'vault_password.prod': 'provided-at-launch@prod'})
        args = task.build_args(job, private_data_dir, passwords)
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        vault_passwords = dict((k, v) for k, v in expect_passwords.items() if 'Vault' in k)
        assert vault_passwords[r'Vault password \(prod\):\s*?$'] == 'provided-at-launch@prod'  # noqa
        assert vault_passwords[r'Vault password \(dev\):\s*?$'] == 'provided-at-launch@dev'  # noqa
        assert vault_passwords[r'Vault password:\s*?$'] == ''  # noqa
        assert '--ask-vault-pass' not in ' '.join(args)
        assert '--vault-id dev@prompt' in ' '.join(args)
        assert '--vault-id prod@prompt' in ' '.join(args)

    @pytest.mark.parametrize(
        'authorize, expected_authorize',
        [
            [True, '1'],
            [False, '0'],
            [None, '0'],
        ],
    )
    def test_net_credentials(self, authorize, expected_authorize, job, private_data_dir, mock_me):
        task = jobs.RunJob()
        task.instance = job
        net = CredentialType.defaults['net']()
        inputs = {'username': 'bob', 'password': 'secret', 'ssh_key_data': self.EXAMPLE_PRIVATE_KEY, 'authorize_password': 'authorizeme'}
        if authorize is not None:
            inputs['authorize'] = authorize
        credential = Credential(pk=1, credential_type=net, inputs=inputs)
        for field in ('password', 'ssh_key_data', 'authorize_password'):
            credential.inputs[field] = encrypt_field(credential, field)
        job.credentials.add(credential)

        private_data_files, ssh_key_data = task.build_private_data_files(job, private_data_dir)
        env = task.build_env(job, private_data_dir, private_data_files=private_data_files)
        safe_env = build_safe_env(env)
        credential.credential_type.inject_credential(credential, env, safe_env, [], private_data_dir)

        assert env['ANSIBLE_NET_USERNAME'] == 'bob'
        assert env['ANSIBLE_NET_PASSWORD'] == 'secret'
        assert env['ANSIBLE_NET_AUTHORIZE'] == expected_authorize
        if authorize:
            assert env['ANSIBLE_NET_AUTH_PASS'] == 'authorizeme'
        with open(env['ANSIBLE_NET_SSH_KEYFILE'], 'r') as f:
            assert f.read() == self.EXAMPLE_PRIVATE_KEY
        assert safe_env['ANSIBLE_NET_PASSWORD'] == HIDDEN_PASSWORD

    def test_multi_cloud(self, private_data_dir, mock_me):
        gce = CredentialType.defaults['gce']()
        gce_credential = Credential(pk=1, credential_type=gce, inputs={'username': 'bob', 'project': 'some-project', 'ssh_key_data': self.EXAMPLE_PRIVATE_KEY})
        gce_credential.inputs['ssh_key_data'] = encrypt_field(gce_credential, 'ssh_key_data')

        azure_rm = CredentialType.defaults['azure_rm']()
        azure_rm_credential = Credential(pk=2, credential_type=azure_rm, inputs={'subscription': 'some-subscription', 'username': 'bob', 'password': 'secret'})
        azure_rm_credential.inputs['secret'] = ''
        azure_rm_credential.inputs['secret'] = encrypt_field(azure_rm_credential, 'secret')

        env = {}
        safe_env = {}
        for credential in [gce_credential, azure_rm_credential]:
            credential.credential_type.inject_credential(credential, env, safe_env, [], private_data_dir)

        assert env['AZURE_SUBSCRIPTION_ID'] == 'some-subscription'
        assert env['AZURE_AD_USER'] == 'bob'
        assert env['AZURE_PASSWORD'] == 'secret'

        # Because this is testing a mix of multiple cloud creds, we are not going to test the GOOGLE_APPLICATION_CREDENTIALS here
        path = to_host_path(env['GCE_CREDENTIALS_FILE_PATH'], private_data_dir)
        with open(path, 'rb') as f:
            json_data = json.load(f)
        assert json_data['type'] == 'service_account'
        assert json_data['private_key'] == self.EXAMPLE_PRIVATE_KEY
        assert json_data['client_email'] == 'bob'
        assert json_data['project_id'] == 'some-project'

        assert safe_env['AZURE_PASSWORD'] == HIDDEN_PASSWORD

    def test_awx_task_env(self, settings, private_data_dir, job, mock_me):
        settings.AWX_TASK_ENV = {'FOO': 'BAR'}
        task = jobs.RunJob()
        task.instance = job
        env = task.build_env(job, private_data_dir)

        assert env['FOO'] == 'BAR'


@pytest.mark.usefixtures("patch_Organization")
class TestProjectUpdateGalaxyCredentials(TestJobExecution):
    @pytest.fixture
    def project_update(self, execution_environment):
        org = Organization(pk=1)
        proj = Project(pk=1, organization=org)
        project_update = ProjectUpdate(pk=1, project=proj, scm_type='git')
        project_update.websocket_emit_status = mock.Mock()
        project_update.execution_environment = execution_environment
        return project_update

    parametrize = {
        'test_galaxy_credentials_ignore_certs': [
            dict(ignore=True),
            dict(ignore=False),
        ],
    }

    def test_galaxy_credentials_ignore_certs(self, private_data_dir, project_update, ignore, mock_me):
        settings.GALAXY_IGNORE_CERTS = ignore
        task = jobs.RunProjectUpdate()
        task.instance = project_update
        env = task.build_env(project_update, private_data_dir)
        if ignore:
            assert env['ANSIBLE_GALAXY_IGNORE'] == 'True'
        else:
            assert 'ANSIBLE_GALAXY_IGNORE' not in env

    def test_galaxy_credentials_empty(self, private_data_dir, project_update, mock_me):
        class RunProjectUpdate(jobs.RunProjectUpdate):
            __vars__ = {}

            def _write_extra_vars_file(self, private_data_dir, extra_vars, *kw):
                self.__vars__ = extra_vars

        task = RunProjectUpdate()
        task.instance = project_update
        env = task.build_env(project_update, private_data_dir)

        with mock.patch.object(Licenser, 'validate', lambda *args, **kw: {}):
            task.build_extra_vars_file(project_update, private_data_dir)

        assert task.__vars__['roles_enabled'] is False
        assert task.__vars__['collections_enabled'] is False
        for k in env:
            assert not k.startswith('ANSIBLE_GALAXY_SERVER')

    def test_single_public_galaxy(self, private_data_dir, project_update, mock_me):
        class RunProjectUpdate(jobs.RunProjectUpdate):
            __vars__ = {}

            def _write_extra_vars_file(self, private_data_dir, extra_vars, *kw):
                self.__vars__ = extra_vars

        credential_type = CredentialType.defaults['galaxy_api_token']()
        public_galaxy = Credential(
            pk=1,
            credential_type=credential_type,
            inputs={
                'url': 'https://galaxy.ansible.com/',
            },
        )
        project_update.project.organization.galaxy_credentials.add(public_galaxy)
        task = RunProjectUpdate()
        task.instance = project_update
        env = task.build_env(project_update, private_data_dir)

        with mock.patch.object(Licenser, 'validate', lambda *args, **kw: {}):
            task.build_extra_vars_file(project_update, private_data_dir)

        assert task.__vars__['roles_enabled'] is True
        assert task.__vars__['collections_enabled'] is True
        assert sorted([(k, v) for k, v in env.items() if k.startswith('ANSIBLE_GALAXY')]) == [
            ('ANSIBLE_GALAXY_SERVER_LIST', 'server0'),
            ('ANSIBLE_GALAXY_SERVER_SERVER0_URL', 'https://galaxy.ansible.com/'),
        ]

    def test_multiple_galaxy_endpoints(self, private_data_dir, project_update, mock_me):
        credential_type = CredentialType.defaults['galaxy_api_token']()
        public_galaxy = Credential(
            pk=1,
            credential_type=credential_type,
            inputs={
                'url': 'https://galaxy.ansible.com/',
            },
        )
        rh = Credential(
            pk=2,
            credential_type=credential_type,
            inputs={
                'url': 'https://cloud.redhat.com/api/automation-hub/',
                'auth_url': 'https://sso.redhat.com/example/openid-connect/token/',
                'token': 'secret123',
            },
        )
        project_update.project.organization.galaxy_credentials.add(public_galaxy)
        project_update.project.organization.galaxy_credentials.add(rh)
        task = jobs.RunProjectUpdate()
        task.instance = project_update
        env = task.build_env(project_update, private_data_dir)
        assert sorted([(k, v) for k, v in env.items() if k.startswith('ANSIBLE_GALAXY')]) == [
            ('ANSIBLE_GALAXY_SERVER_LIST', 'server0,server1'),
            ('ANSIBLE_GALAXY_SERVER_SERVER0_URL', 'https://galaxy.ansible.com/'),
            ('ANSIBLE_GALAXY_SERVER_SERVER1_AUTH_URL', 'https://sso.redhat.com/example/openid-connect/token/'),  # noqa
            ('ANSIBLE_GALAXY_SERVER_SERVER1_TOKEN', 'secret123'),
            ('ANSIBLE_GALAXY_SERVER_SERVER1_URL', 'https://cloud.redhat.com/api/automation-hub/'),
        ]


@pytest.mark.usefixtures("patch_Organization")
class TestProjectUpdateCredentials(TestJobExecution):
    @pytest.fixture
    def project_update(self):
        project_update = ProjectUpdate(
            pk=1,
            project=Project(pk=1, organization=Organization(pk=1)),
        )
        project_update.websocket_emit_status = mock.Mock()
        return project_update

    parametrize = {
        'test_username_and_password_auth': [
            dict(scm_type='git'),
            dict(scm_type='svn'),
            dict(scm_type='archive'),
        ],
        'test_ssh_key_auth': [
            dict(scm_type='git'),
            dict(scm_type='svn'),
            dict(scm_type='archive'),
        ],
        'test_awx_task_env': [
            dict(scm_type='git'),
            dict(scm_type='svn'),
            dict(scm_type='archive'),
        ],
    }

    def test_username_and_password_auth(self, project_update, scm_type, mock_me):
        task = jobs.RunProjectUpdate()
        ssh = CredentialType.defaults['ssh']()
        project_update.scm_type = scm_type
        project_update.credential = Credential(pk=1, credential_type=ssh, inputs={'username': 'bob', 'password': 'secret'})
        project_update.credential.inputs['password'] = encrypt_field(project_update.credential, 'password')

        passwords = task.build_passwords(project_update, {})
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)

        assert 'bob' in expect_passwords.values()
        assert 'secret' in expect_passwords.values()

    def test_ssh_key_auth(self, project_update, scm_type, mock_me):
        task = jobs.RunProjectUpdate()
        ssh = CredentialType.defaults['ssh']()
        project_update.scm_type = scm_type
        project_update.credential = Credential(pk=1, credential_type=ssh, inputs={'username': 'bob', 'ssh_key_data': self.EXAMPLE_PRIVATE_KEY})
        project_update.credential.inputs['ssh_key_data'] = encrypt_field(project_update.credential, 'ssh_key_data')

        passwords = task.build_passwords(project_update, {})
        password_prompts = task.get_password_prompts(passwords)
        expect_passwords = task.create_expect_passwords_data_struct(password_prompts, passwords)
        assert 'bob' in expect_passwords.values()

    def test_awx_task_env(self, project_update, settings, private_data_dir, scm_type, execution_environment, mock_me):
        project_update.execution_environment = execution_environment
        settings.AWX_TASK_ENV = {'FOO': 'BAR'}
        task = jobs.RunProjectUpdate()
        task.instance = project_update
        project_update.scm_type = scm_type

        env = task.build_env(project_update, private_data_dir)

        assert env['FOO'] == 'BAR'


class TestInventoryUpdateCredentials(TestJobExecution):
    @pytest.fixture
    def inventory_update(self, execution_environment):
        return InventoryUpdate(pk=1, execution_environment=execution_environment, inventory_source=InventorySource(pk=1, inventory=Inventory(pk=1)))

    def test_source_without_credential(self, mocker, inventory_update, private_data_dir, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        inventory_update.source = 'ec2'
        inventory_update.get_cloud_credential = mocker.Mock(return_value=None)
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)

        assert 'AWS_ACCESS_KEY_ID' not in env
        assert 'AWS_SECRET_ACCESS_KEY' not in env

    def test_ec2_source(self, private_data_dir, inventory_update, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        aws = CredentialType.defaults['aws']()
        inventory_update.source = 'ec2'

        def get_cred():
            cred = Credential(pk=1, credential_type=aws, inputs={'username': 'bob', 'password': 'secret'})
            cred.inputs['password'] = encrypt_field(cred, 'password')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)

        safe_env = build_safe_env(env)

        assert env['AWS_ACCESS_KEY_ID'] == 'bob'
        assert env['AWS_SECRET_ACCESS_KEY'] == 'secret'

        assert safe_env['AWS_SECRET_ACCESS_KEY'] == HIDDEN_PASSWORD

    def test_vmware_source(self, inventory_update, private_data_dir, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        vmware = CredentialType.defaults['vmware']()
        inventory_update.source = 'vmware'

        def get_cred():
            cred = Credential(pk=1, credential_type=vmware, inputs={'username': 'bob', 'password': 'secret', 'host': 'https://example.org'})
            cred.inputs['password'] = encrypt_field(cred, 'password')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)

        safe_env = {}
        credentials = task.build_credentials_list(inventory_update)
        for credential in credentials:
            if credential:
                credential.credential_type.inject_credential(credential, env, safe_env, [], private_data_dir)

        env["VMWARE_USER"] == "bob",
        env["VMWARE_PASSWORD"] == "secret",
        env["VMWARE_HOST"] == "https://example.org",
        env["VMWARE_VALIDATE_CERTS"] == "False",

    def test_azure_rm_source_with_tenant(self, private_data_dir, inventory_update, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        azure_rm = CredentialType.defaults['azure_rm']()
        inventory_update.source = 'azure_rm'

        def get_cred():
            cred = Credential(
                pk=1,
                credential_type=azure_rm,
                inputs={
                    'client': 'some-client',
                    'secret': 'some-secret',
                    'tenant': 'some-tenant',
                    'subscription': 'some-subscription',
                    'cloud_environment': 'foobar',
                },
            )
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)

        safe_env = build_safe_env(env)

        assert env['AZURE_CLIENT_ID'] == 'some-client'
        assert env['AZURE_SECRET'] == 'some-secret'
        assert env['AZURE_TENANT'] == 'some-tenant'
        assert env['AZURE_SUBSCRIPTION_ID'] == 'some-subscription'
        assert env['AZURE_CLOUD_ENVIRONMENT'] == 'foobar'

        assert safe_env['AZURE_SECRET'] == HIDDEN_PASSWORD

    def test_azure_rm_source_with_password(self, private_data_dir, inventory_update, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        azure_rm = CredentialType.defaults['azure_rm']()
        inventory_update.source = 'azure_rm'

        def get_cred():
            cred = Credential(
                pk=1,
                credential_type=azure_rm,
                inputs={'subscription': 'some-subscription', 'username': 'bob', 'password': 'secret', 'cloud_environment': 'foobar'},
            )
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)

        safe_env = build_safe_env(env)

        assert env['AZURE_SUBSCRIPTION_ID'] == 'some-subscription'
        assert env['AZURE_AD_USER'] == 'bob'
        assert env['AZURE_PASSWORD'] == 'secret'
        assert env['AZURE_CLOUD_ENVIRONMENT'] == 'foobar'

        assert safe_env['AZURE_PASSWORD'] == HIDDEN_PASSWORD

    @pytest.mark.parametrize("cred_env_var", ['GCE_CREDENTIALS_FILE_PATH', 'GOOGLE_APPLICATION_CREDENTIALS'])
    def test_gce_source(self, cred_env_var, inventory_update, private_data_dir, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        gce = CredentialType.defaults['gce']()
        inventory_update.source = 'gce'

        def get_cred():
            cred = Credential(pk=1, credential_type=gce, inputs={'username': 'bob', 'project': 'some-project', 'ssh_key_data': self.EXAMPLE_PRIVATE_KEY})
            cred.inputs['ssh_key_data'] = encrypt_field(cred, 'ssh_key_data')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        def run(expected_gce_zone):
            private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
            env = task.build_env(inventory_update, private_data_dir, private_data_files)
            safe_env = {}
            credentials = task.build_credentials_list(inventory_update)
            for credential in credentials:
                if credential:
                    credential.credential_type.inject_credential(credential, env, safe_env, [], private_data_dir)

            assert env['GCE_ZONE'] == expected_gce_zone
            with open(env[cred_env_var], 'rb') as f:
                json_data = json.load(f)
            assert json_data['type'] == 'service_account'
            assert json_data['private_key'] == self.EXAMPLE_PRIVATE_KEY
            assert json_data['client_email'] == 'bob'
            assert json_data['project_id'] == 'some-project'

    def test_openstack_source(self, inventory_update, private_data_dir, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        openstack = CredentialType.defaults['openstack']()
        inventory_update.source = 'openstack'

        def get_cred():
            cred = Credential(
                pk=1,
                credential_type=openstack,
                inputs={'username': 'bob', 'password': 'secret', 'project': 'tenant-name', 'host': 'https://keystone.example.org'},
            )
            cred.inputs['ssh_key_data'] = ''
            cred.inputs['ssh_key_data'] = encrypt_field(cred, 'ssh_key_data')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)

        path = to_host_path(env['OS_CLIENT_CONFIG_FILE'], private_data_dir)
        with open(path, 'r') as f:
            shade_config = f.read()
        assert (
            '\n'.join(
                [
                    'clouds:',
                    '  devstack:',
                    '    auth:',
                    '      auth_url: https://keystone.example.org',
                    '      password: secret',
                    '      project_name: tenant-name',
                    '      username: bob',
                    '',
                ]
            )
            in shade_config
        )

    def test_satellite6_source(self, inventory_update, private_data_dir, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        satellite6 = CredentialType.defaults['satellite6']()
        inventory_update.source = 'satellite6'

        def get_cred():
            cred = Credential(pk=1, credential_type=satellite6, inputs={'username': 'bob', 'password': 'secret', 'host': 'https://example.org'})
            cred.inputs['password'] = encrypt_field(cred, 'password')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)
        safe_env = build_safe_env(env)

        assert env["FOREMAN_SERVER"] == "https://example.org"
        assert env["FOREMAN_USER"] == "bob"
        assert env["FOREMAN_PASSWORD"] == "secret"
        assert safe_env["FOREMAN_PASSWORD"] == HIDDEN_PASSWORD

    def test_insights_source(self, inventory_update, private_data_dir, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        insights = CredentialType.defaults['insights']()
        inventory_update.source = 'insights'

        def get_cred():
            cred = Credential(
                pk=1,
                credential_type=insights,
                inputs={
                    'username': 'bob',
                    'password': 'secret',
                },
            )
            cred.inputs['password'] = encrypt_field(cred, 'password')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        env = task.build_env(inventory_update, private_data_dir, False)
        safe_env = build_safe_env(env)

        assert env["INSIGHTS_USER"] == "bob"
        assert env["INSIGHTS_PASSWORD"] == "secret"
        assert safe_env['INSIGHTS_PASSWORD'] == HIDDEN_PASSWORD

    @pytest.mark.parametrize('verify', [True, False])
    def test_tower_source(self, verify, inventory_update, private_data_dir, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        tower = CredentialType.defaults['controller']()
        inventory_update.source = 'controller'
        inputs = {'host': 'https://tower.example.org', 'username': 'bob', 'password': 'secret', 'verify_ssl': verify}

        def get_cred():
            cred = Credential(pk=1, credential_type=tower, inputs=inputs)
            cred.inputs['password'] = encrypt_field(cred, 'password')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        env = task.build_env(inventory_update, private_data_dir)

        safe_env = build_safe_env(env)

        assert env['CONTROLLER_HOST'] == 'https://tower.example.org'
        assert env['CONTROLLER_USERNAME'] == 'bob'
        assert env['CONTROLLER_PASSWORD'] == 'secret'
        if verify:
            assert env['CONTROLLER_VERIFY_SSL'] == 'True'
        else:
            assert env['CONTROLLER_VERIFY_SSL'] == 'False'
        assert safe_env['CONTROLLER_PASSWORD'] == HIDDEN_PASSWORD

    def test_tower_source_ssl_verify_empty(self, inventory_update, private_data_dir, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        tower = CredentialType.defaults['controller']()
        inventory_update.source = 'controller'
        inputs = {
            'host': 'https://tower.example.org',
            'username': 'bob',
            'password': 'secret',
        }

        def get_cred():
            cred = Credential(pk=1, credential_type=tower, inputs=inputs)
            cred.inputs['password'] = encrypt_field(cred, 'password')
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])

        env = task.build_env(inventory_update, private_data_dir)
        safe_env = {}
        credentials = task.build_credentials_list(inventory_update)
        for credential in credentials:
            if credential:
                credential.credential_type.inject_credential(credential, env, safe_env, [], private_data_dir)

        assert env['TOWER_VERIFY_SSL'] == 'False'

    def test_awx_task_env(self, inventory_update, private_data_dir, settings, mocker, mock_me):
        task = jobs.RunInventoryUpdate()
        task.instance = inventory_update
        gce = CredentialType.defaults['gce']()
        inventory_update.source = 'gce'

        def get_cred():
            cred = Credential(
                pk=1,
                credential_type=gce,
                inputs={
                    'username': 'bob',
                    'project': 'some-project',
                },
            )
            return cred

        inventory_update.get_cloud_credential = get_cred
        inventory_update.get_extra_credentials = mocker.Mock(return_value=[])
        settings.AWX_TASK_ENV = {'FOO': 'BAR'}

        private_data_files, ssh_key_data = task.build_private_data_files(inventory_update, private_data_dir)
        env = task.build_env(inventory_update, private_data_dir, private_data_files)

        assert env['FOO'] == 'BAR'


def test_os_open_oserror():
    with pytest.raises(OSError):
        os.open('this_file_does_not_exist', os.O_RDONLY)


def test_fcntl_ioerror():
    with pytest.raises(OSError):
        fcntl.lockf(99999, fcntl.LOCK_EX)


@mock.patch('os.open')
@mock.patch('logging.getLogger')
def test_acquire_lock_open_fail_logged(logging_getLogger, os_open, mock_me):
    err = OSError()
    err.errno = 3
    err.strerror = 'dummy message'

    instance = mock.Mock()
    instance.get_lock_file.return_value = 'this_file_does_not_exist'

    os_open.side_effect = err

    logger = mock.Mock()
    logging_getLogger.return_value = logger

    ProjectUpdate = jobs.RunProjectUpdate()

    with pytest.raises(OSError):
        ProjectUpdate.acquire_lock(instance)
    assert logger.err.called_with("I/O error({0}) while trying to open lock file [{1}]: {2}".format(3, 'this_file_does_not_exist', 'dummy message'))


@mock.patch('os.open')
@mock.patch('os.close')
@mock.patch('logging.getLogger')
@mock.patch('fcntl.lockf')
def test_acquire_lock_acquisition_fail_logged(fcntl_lockf, logging_getLogger, os_close, os_open, mock_me):
    err = IOError()
    err.errno = 3
    err.strerror = 'dummy message'

    instance = mock.Mock()
    instance.get_lock_file.return_value = 'this_file_does_not_exist'
    instance.cancel_flag = False

    os_open.return_value = 3

    logger = mock.Mock()
    logging_getLogger.return_value = logger

    fcntl_lockf.side_effect = err

    ProjectUpdate = jobs.RunProjectUpdate()
    with pytest.raises(IOError):
        ProjectUpdate.acquire_lock(instance)
    os_close.assert_called_with(3)
    assert logger.err.called_with("I/O error({0}) while trying to acquire lock on file [{1}]: {2}".format(3, 'this_file_does_not_exist', 'dummy message'))


@pytest.mark.parametrize('injector_cls', [cls for cls in ManagedCredentialType.registry.values() if cls.injectors])
def test_managed_injector_redaction(injector_cls):
    """See awx.main.models.inventory.PluginFileInjector._get_shared_env
    The ordering within awx.main.tasks.jobs.BaseTask and contract with build_env
    requires that all managed injectors are safely redacted by the
    static method build_safe_env without having to employ the safe namespace
    as in inject_credential

    This test enforces that condition uniformly to prevent password leakages
    """
    secrets = set()
    for element in injector_cls.inputs.get('fields', []):
        if element.get('secret', False):
            secrets.add(element['id'])
    env = {}
    for env_name, template in injector_cls.injectors.get('env', {}).items():
        for secret_field_name in secrets:
            if secret_field_name in template:
                env[env_name] = 'very_secret_value'
    assert 'very_secret_value' not in str(build_safe_env(env))


def test_job_run_no_ee(mock_me, mock_create_partition):
    org = Organization(pk=1)
    proj = Project(pk=1, organization=org)
    job = Job(project=proj, organization=org, inventory=Inventory(pk=1))
    job.execution_environment = None
    task = jobs.RunJob()
    task.instance = job
    task.update_model = mock.Mock(return_value=job)
    task.model.objects.get = mock.Mock(return_value=job)

    with mock.patch('awx.main.tasks.jobs.shutil.copytree'):
        with pytest.raises(RuntimeError) as e:
            task.pre_run_hook(job, private_data_dir)

    update_model_call = task.update_model.call_args[1]
    assert update_model_call['status'] == 'error'
    assert 'Job could not start because no Execution Environment could be found' in str(e.value)


def test_project_update_no_ee(mock_me):
    org = Organization(pk=1)
    proj = Project(pk=1, organization=org)
    project_update = ProjectUpdate(pk=1, project=proj, scm_type='git')
    project_update.execution_environment = None
    task = jobs.RunProjectUpdate()
    task.instance = project_update

    with pytest.raises(RuntimeError) as e:
        task.build_env(job, {})

    assert 'The ProjectUpdate could not run because there is no Execution Environment' in str(e.value)


@pytest.mark.parametrize(
    'work_unit_data, expected_function_call',
    [
        [
            # if (extra_data is None): continue
            {
                'zpdFi4BX': {
                    'ExtraData': None,
                }
            },
            False,
        ],
        [
            # Extra data is a string and StateName is None
            {
                "y4NgMKKW": {
                    "ExtraData": "Unknown WorkType",
                }
            },
            False,
        ],
        [
            # Extra data is a string and StateName in RECEPTOR_ACTIVE_STATES
            {
                "y4NgMKKW": {
                    "ExtraData": "Unknown WorkType",
                    "StateName": "Running",
                }
            },
            False,
        ],
        [
            # Extra data is a string and StateName not in RECEPTOR_ACTIVE_STATES
            {
                "y4NgMKKW": {
                    "ExtraData": "Unknown WorkType",
                    "StateName": "Succeeded",
                }
            },
            True,
        ],
        [
            # Extra data is a dict but RemoteWorkType is not ansible-runner
            {
                "y4NgMKKW": {
                    'ExtraData': {
                        'RemoteWorkType': 'not-ansible-runner',
                    },
                }
            },
            False,
        ],
        [
            # Extra data is a dict and its an ansible-runner but we have no params
            {
                'zpdFi4BX': {
                    'ExtraData': {
                        'RemoteWorkType': 'ansible-runner',
                    },
                }
            },
            False,
        ],
        [
            # Extra data is a dict and its an ansible-runner but params is not --worker-info
            {
                'zpdFi4BX': {
                    'ExtraData': {'RemoteWorkType': 'ansible-runner', 'RemoteParams': {'params': '--not-worker-info'}},
                }
            },
            False,
        ],
        [
            # Extra data is a dict and its an ansible-runner but params starts without cleanup
            {
                'zpdFi4BX': {
                    'ExtraData': {'RemoteWorkType': 'ansible-runner', 'RemoteParams': {'params': 'not cleanup stuff'}},
                }
            },
            False,
        ],
        [
            # Extra data is a dict and its an ansible-runner w/ params but still running
            {
                'zpdFi4BX': {
                    'ExtraData': {'RemoteWorkType': 'ansible-runner', 'RemoteParams': {'params': '--worker-info'}},
                    "StateName": "Running",
                }
            },
            False,
        ],
        [
            # Extra data is a dict and its an ansible-runner w/ params and completed
            {
                'zpdFi4BX': {
                    'ExtraData': {'RemoteWorkType': 'ansible-runner', 'RemoteParams': {'params': '--worker-info'}},
                    "StateName": "Succeeded",
                }
            },
            True,
        ],
    ],
)
def test_administrative_workunit_reaper(work_unit_data, expected_function_call):
    # Mock the get_receptor_ctl call and let it return a dummy object
    # It does not matter what file name we return as the socket because we won't actually call receptor (unless something is broken)
    with mock.patch('awx.main.tasks.receptor.get_receptor_ctl') as mock_get_receptor_ctl:
        mock_get_receptor_ctl.return_value = ReceptorControl('/var/run/awx-receptor/receptor.sock')
        with mock.patch('receptorctl.socket_interface.ReceptorControl.simple_command') as simple_command:
            receptor.administrative_workunit_reaper(work_list=work_unit_data)

    if expected_function_call:
        simple_command.assert_called()
    else:
        simple_command.assert_not_called()
