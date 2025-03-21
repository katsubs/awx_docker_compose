# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# Python
import dateutil
import functools
import html
import itertools
import logging
import re
import requests
import socket
import sys
import time
from base64 import b64encode
from collections import OrderedDict

from urllib3.exceptions import ConnectTimeoutError

# Django
from django.conf import settings
from django.core.exceptions import FieldError, ObjectDoesNotExist
from django.db.models import Q, Sum, Count
from django.db import IntegrityError, ProgrammingError, transaction, connection
from django.db.models.fields.related import ManyToManyField, ForeignKey
from django.db.models.functions import Trunc
from django.shortcuts import get_object_or_404
from django.utils.safestring import mark_safe
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.template.loader import render_to_string
from django.http import HttpResponse, HttpResponseRedirect
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

# Django REST Framework
from rest_framework.exceptions import APIException, PermissionDenied, ParseError, NotFound
from rest_framework.parsers import FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer, StaticHTMLRenderer
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import exception_handler, get_view_name
from rest_framework import status

# Django REST Framework YAML
from rest_framework_yaml.parsers import YAMLParser
from rest_framework_yaml.renderers import YAMLRenderer

# ansi2html
from ansi2html import Ansi2HTMLConverter

import pytz
from wsgiref.util import FileWrapper

# django-ansible-base
from ansible_base.lib.utils.requests import get_remote_hosts
from ansible_base.rbac.models import RoleEvaluation, ObjectRole
from ansible_base.resource_registry.shared_types import OrganizationType, TeamType, UserType

# AWX
from awx.main.tasks.system import send_notifications, update_inventory_computed_fields
from awx.main.access import get_user_queryset
from awx.api.generics import (
    APIView,
    BaseUsersList,
    CopyAPIView,
    GenericCancelView,
    GenericAPIView,
    ListAPIView,
    ListCreateAPIView,
    ResourceAccessList,
    RetrieveAPIView,
    RetrieveDestroyAPIView,
    RetrieveUpdateAPIView,
    RetrieveUpdateDestroyAPIView,
    SimpleListAPIView,
    SubDetailAPIView,
    SubListAPIView,
    SubListAttachDetachAPIView,
    SubListCreateAPIView,
    SubListCreateAttachDetachAPIView,
    SubListDestroyAPIView,
)
from awx.api.views.labels import LabelSubListCreateAttachDetachView
from awx.api.versioning import reverse
from awx.main import models
from awx.main.models.rbac import get_role_definition
from awx.main.utils import (
    camelcase_to_underscore,
    extract_ansible_vars,
    get_object_or_400,
    getattrd,
    get_pk_from_dict,
    ScheduleWorkflowManager,
    ignore_inventory_computed_fields,
)
from awx.main.utils.encryption import encrypt_value
from awx.main.utils.filters import SmartFilter
from awx.main.utils.plugins import compute_cloud_inventory_sources
from awx.main.redact import UriCleaner
from awx.api.permissions import (
    JobTemplateCallbackPermission,
    TaskPermission,
    ProjectUpdatePermission,
    InventoryInventorySourcesUpdatePermission,
    UserPermission,
    VariableDataPermission,
    WorkflowApprovalPermission,
    IsSystemAdminOrAuditor,
)
from awx.api import renderers
from awx.api import serializers
from awx.api.metadata import RoleMetadata
from awx.main.constants import ACTIVE_STATES, SURVEY_TYPE_MAPPING
from awx.main.scheduler.dag_workflow import WorkflowDAG
from awx.api.views.mixin import (
    InstanceGroupMembershipMixin,
    OrganizationCountsMixin,
    RelatedJobsPreventDeleteMixin,
    UnifiedJobDeletionMixin,
    NoTruncateMixin,
)
from awx.api.pagination import UnifiedJobEventPagination
from awx.main.utils import set_environ

logger = logging.getLogger('awx.api.views')


def unpartitioned_event_horizon(cls):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE table_name = '_unpartitioned_{cls._meta.db_table}';")
        if not cursor.fetchone():
            return 0
    with connection.cursor() as cursor:
        try:
            cursor.execute(f'SELECT MAX(id) FROM _unpartitioned_{cls._meta.db_table}')
            return cursor.fetchone()[0] or -1
        except ProgrammingError:
            return 0


def api_exception_handler(exc, context):
    """
    Override default API exception handler to catch IntegrityError exceptions.
    """
    if isinstance(exc, IntegrityError):
        exc = ParseError(exc.args[0])
    if isinstance(exc, FieldError):
        exc = ParseError(exc.args[0])
    if isinstance(context['view'], UnifiedJobStdout):
        context['view'].renderer_classes = [renderers.BrowsableAPIRenderer, JSONRenderer]
    if isinstance(exc, APIException):
        req = context['request']._request
        if 'awx.named_url_rewritten' in req.environ and not str(getattr(exc, 'status_code', 0)).startswith('2'):
            # if the URL was rewritten, and it's not a 2xx level status code,
            # revert the request.path to its original value to avoid leaking
            # any context about the existence of resources
            req.path = req.environ['awx.named_url_rewritten']
            if exc.status_code == 403:
                exc = NotFound(detail=_('Not found.'))
    return exception_handler(exc, context)


class DashboardView(APIView):
    deprecated = True

    name = _("Dashboard")
    swagger_topic = 'Dashboard'

    def get(self, request, format=None):
        '''Show Dashboard Details'''
        data = OrderedDict()
        data['related'] = {'jobs_graph': reverse('api:dashboard_jobs_graph_view', request=request)}
        user_inventory = get_user_queryset(request.user, models.Inventory)
        inventory_with_failed_hosts = user_inventory.filter(hosts_with_active_failures__gt=0)
        user_inventory_external = user_inventory.filter(has_inventory_sources=True)
        # if there are *zero* inventories, this aggregate query will be None, fall back to 0
        failed_inventory = user_inventory.aggregate(Sum('inventory_sources_with_failures'))['inventory_sources_with_failures__sum'] or 0
        data['inventories'] = {
            'url': reverse('api:inventory_list', request=request),
            'total': user_inventory.count(),
            'total_with_inventory_source': user_inventory_external.count(),
            'job_failed': inventory_with_failed_hosts.count(),
            'inventory_failed': failed_inventory,
        }
        user_inventory_sources = get_user_queryset(request.user, models.InventorySource)
        ec2_inventory_sources = user_inventory_sources.filter(source='ec2')
        ec2_inventory_failed = ec2_inventory_sources.filter(status='failed')
        data['inventory_sources'] = {}
        data['inventory_sources']['ec2'] = {
            'url': reverse('api:inventory_source_list', request=request) + "?source=ec2",
            'failures_url': reverse('api:inventory_source_list', request=request) + "?source=ec2&status=failed",
            'label': 'Amazon EC2',
            'total': ec2_inventory_sources.count(),
            'failed': ec2_inventory_failed.count(),
        }

        user_groups = get_user_queryset(request.user, models.Group)
        groups_inventory_failed = models.Group.objects.filter(inventory_sources__last_job_failed=True).count()
        data['groups'] = {'url': reverse('api:group_list', request=request), 'total': user_groups.count(), 'inventory_failed': groups_inventory_failed}

        user_hosts = get_user_queryset(request.user, models.Host)
        user_hosts_failed = user_hosts.filter(last_job_host_summary__failed=True)
        data['hosts'] = {
            'url': reverse('api:host_list', request=request),
            'failures_url': reverse('api:host_list', request=request) + "?last_job_host_summary__failed=True",
            'total': user_hosts.count(),
            'failed': user_hosts_failed.count(),
        }

        user_projects = get_user_queryset(request.user, models.Project)
        user_projects_failed = user_projects.filter(last_job_failed=True)
        data['projects'] = {
            'url': reverse('api:project_list', request=request),
            'failures_url': reverse('api:project_list', request=request) + "?last_job_failed=True",
            'total': user_projects.count(),
            'failed': user_projects_failed.count(),
        }

        git_projects = user_projects.filter(scm_type='git')
        git_failed_projects = git_projects.filter(last_job_failed=True)
        svn_projects = user_projects.filter(scm_type='svn')
        svn_failed_projects = svn_projects.filter(last_job_failed=True)
        archive_projects = user_projects.filter(scm_type='archive')
        archive_failed_projects = archive_projects.filter(last_job_failed=True)
        data['scm_types'] = {}
        data['scm_types']['git'] = {
            'url': reverse('api:project_list', request=request) + "?scm_type=git",
            'label': 'Git',
            'failures_url': reverse('api:project_list', request=request) + "?scm_type=git&last_job_failed=True",
            'total': git_projects.count(),
            'failed': git_failed_projects.count(),
        }
        data['scm_types']['svn'] = {
            'url': reverse('api:project_list', request=request) + "?scm_type=svn",
            'label': 'Subversion',
            'failures_url': reverse('api:project_list', request=request) + "?scm_type=svn&last_job_failed=True",
            'total': svn_projects.count(),
            'failed': svn_failed_projects.count(),
        }
        data['scm_types']['archive'] = {
            'url': reverse('api:project_list', request=request) + "?scm_type=archive",
            'label': 'Remote Archive',
            'failures_url': reverse('api:project_list', request=request) + "?scm_type=archive&last_job_failed=True",
            'total': archive_projects.count(),
            'failed': archive_failed_projects.count(),
        }

        user_list = get_user_queryset(request.user, models.User)
        team_list = get_user_queryset(request.user, models.Team)
        credential_list = get_user_queryset(request.user, models.Credential)
        job_template_list = get_user_queryset(request.user, models.JobTemplate)
        organization_list = get_user_queryset(request.user, models.Organization)
        data['users'] = {'url': reverse('api:user_list', request=request), 'total': user_list.count()}
        data['organizations'] = {'url': reverse('api:organization_list', request=request), 'total': organization_list.count()}
        data['teams'] = {'url': reverse('api:team_list', request=request), 'total': team_list.count()}
        data['credentials'] = {'url': reverse('api:credential_list', request=request), 'total': credential_list.count()}
        data['job_templates'] = {'url': reverse('api:job_template_list', request=request), 'total': job_template_list.count()}
        return Response(data)


class DashboardJobsGraphView(APIView):
    name = _("Dashboard Jobs Graphs")
    swagger_topic = 'Jobs'

    def get(self, request, format=None):
        period = request.query_params.get('period', 'month')
        job_type = request.query_params.get('job_type', 'all')

        user_unified_jobs = get_user_queryset(request.user, models.UnifiedJob).exclude(launch_type='sync')

        success_query = user_unified_jobs.filter(status='successful')
        failed_query = user_unified_jobs.filter(status='failed')
        canceled_query = user_unified_jobs.filter(status='canceled')
        error_query = user_unified_jobs.filter(status='error')

        if job_type == 'inv_sync':
            success_query = success_query.filter(instance_of=models.InventoryUpdate)
            failed_query = failed_query.filter(instance_of=models.InventoryUpdate)
            canceled_query = canceled_query.filter(instance_of=models.InventoryUpdate)
            error_query = error_query.filter(instance_of=models.InventoryUpdate)
        elif job_type == 'playbook_run':
            success_query = success_query.filter(instance_of=models.Job)
            failed_query = failed_query.filter(instance_of=models.Job)
            canceled_query = canceled_query.filter(instance_of=models.Job)
            error_query = error_query.filter(instance_of=models.Job)
        elif job_type == 'scm_update':
            success_query = success_query.filter(instance_of=models.ProjectUpdate)
            failed_query = failed_query.filter(instance_of=models.ProjectUpdate)
            canceled_query = canceled_query.filter(instance_of=models.ProjectUpdate)
            error_query = error_query.filter(instance_of=models.ProjectUpdate)

        end = now()
        interval = 'day'
        if period == 'month':
            start = end - dateutil.relativedelta.relativedelta(months=1)
        elif period == 'two_weeks':
            start = end - dateutil.relativedelta.relativedelta(weeks=2)
        elif period == 'week':
            start = end - dateutil.relativedelta.relativedelta(weeks=1)
        elif period == 'day':
            start = end - dateutil.relativedelta.relativedelta(days=1)
            interval = 'hour'
        else:
            return Response({'error': _('Unknown period "%s"') % str(period)}, status=status.HTTP_400_BAD_REQUEST)

        dashboard_data = {"jobs": {"successful": [], "failed": [], "canceled": [], "error": []}}

        succ_list = dashboard_data['jobs']['successful']
        fail_list = dashboard_data['jobs']['failed']
        canceled_list = dashboard_data['jobs']['canceled']
        error_list = dashboard_data['jobs']['error']

        qs_s = (
            success_query.filter(finished__range=(start, end))
            .annotate(d=Trunc('finished', interval, tzinfo=end.tzinfo))
            .order_by()
            .values('d')
            .annotate(agg=Count('id', distinct=True))
        )
        data_s = {item['d']: item['agg'] for item in qs_s}
        qs_f = (
            failed_query.filter(finished__range=(start, end))
            .annotate(d=Trunc('finished', interval, tzinfo=end.tzinfo))
            .order_by()
            .values('d')
            .annotate(agg=Count('id', distinct=True))
        )
        data_f = {item['d']: item['agg'] for item in qs_f}
        qs_c = (
            canceled_query.filter(finished__range=(start, end))
            .annotate(d=Trunc('finished', interval, tzinfo=end.tzinfo))
            .order_by()
            .values('d')
            .annotate(agg=Count('id', distinct=True))
        )
        data_c = {item['d']: item['agg'] for item in qs_c}
        qs_e = (
            error_query.filter(finished__range=(start, end))
            .annotate(d=Trunc('finished', interval, tzinfo=end.tzinfo))
            .order_by()
            .values('d')
            .annotate(agg=Count('id', distinct=True))
        )
        data_e = {item['d']: item['agg'] for item in qs_e}

        start_date = start.replace(hour=0, minute=0, second=0, microsecond=0)
        for d in itertools.count():
            date = start_date + dateutil.relativedelta.relativedelta(days=d)
            if date > end:
                break
            succ_list.append([time.mktime(date.timetuple()), data_s.get(date, 0)])
            fail_list.append([time.mktime(date.timetuple()), data_f.get(date, 0)])
            canceled_list.append([time.mktime(date.timetuple()), data_c.get(date, 0)])
            error_list.append([time.mktime(date.timetuple()), data_e.get(date, 0)])

        return Response(dashboard_data)


class InstanceList(ListCreateAPIView):
    name = _("Instances")
    model = models.Instance
    serializer_class = serializers.InstanceSerializer
    search_fields = ('hostname',)
    ordering = ('id',)

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related('receptor_addresses')
        return qs


class InstanceDetail(RetrieveUpdateAPIView):
    name = _("Instance Detail")
    model = models.Instance
    serializer_class = serializers.InstanceSerializer

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related('receptor_addresses')
        return qs

    def update_raw_data(self, data):
        # these fields are only valid on creation of an instance, so they unwanted on detail view
        data.pop('node_type', None)
        data.pop('hostname', None)
        data.pop('ip_address', None)
        return super(InstanceDetail, self).update_raw_data(data)

    def update(self, request, *args, **kwargs):
        r = super(InstanceDetail, self).update(request, *args, **kwargs)
        if status.is_success(r.status_code):
            obj = self.get_object()
            capacity_changed = obj.set_capacity_value()
            if capacity_changed:
                obj.save(update_fields=['capacity'])
            r.data = serializers.InstanceSerializer(obj, context=self.get_serializer_context()).to_representation(obj)
        return r


class InstanceUnifiedJobsList(SubListAPIView):
    name = _("Instance Jobs")
    model = models.UnifiedJob
    serializer_class = serializers.UnifiedJobListSerializer
    parent_model = models.Instance

    def get_queryset(self):
        po = self.get_parent_object()
        qs = get_user_queryset(self.request.user, models.UnifiedJob)
        qs = qs.filter(execution_node=po.hostname)
        return qs


class InstancePeersList(SubListAPIView):
    name = _("Peers")
    model = models.ReceptorAddress
    serializer_class = serializers.ReceptorAddressSerializer
    parent_model = models.Instance
    parent_access = 'read'
    relationship = 'peers'
    search_fields = ('address',)


class InstanceReceptorAddressesList(SubListAPIView):
    name = _("Receptor Addresses")
    model = models.ReceptorAddress
    parent_key = 'instance'
    parent_model = models.Instance
    serializer_class = serializers.ReceptorAddressSerializer
    search_fields = ('address',)


class ReceptorAddressesList(ListAPIView):
    name = _("Receptor Addresses")
    model = models.ReceptorAddress
    serializer_class = serializers.ReceptorAddressSerializer
    search_fields = ('address',)


class ReceptorAddressDetail(RetrieveAPIView):
    name = _("Receptor Address Detail")
    model = models.ReceptorAddress
    serializer_class = serializers.ReceptorAddressSerializer
    parent_model = models.Instance
    relationship = 'receptor_addresses'


class InstanceInstanceGroupsList(InstanceGroupMembershipMixin, SubListCreateAttachDetachAPIView):
    name = _("Instance's Instance Groups")
    model = models.InstanceGroup
    serializer_class = serializers.InstanceGroupSerializer
    parent_model = models.Instance
    relationship = 'rampart_groups'

    def is_valid_relation(self, parent, sub, created=False):
        if parent.node_type == 'control':
            return {'msg': _(f"Cannot change instance group membership of control-only node: {parent.hostname}.")}
        if parent.node_type == 'hop':
            return {'msg': _(f"Cannot change instance group membership of hop node : {parent.hostname}.")}
        return None

    def is_valid_removal(self, parent, sub):
        res = self.is_valid_relation(parent, sub)
        if res:
            return res
        if sub.name == settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME and parent.node_type == 'hybrid':
            return {'msg': _(f"Cannot disassociate hybrid instance {parent.hostname} from {sub.name}.")}
        return None


class InstanceHealthCheck(GenericAPIView):
    name = _('Instance Health Check')
    model = models.Instance
    serializer_class = serializers.InstanceHealthCheckSerializer
    permission_classes = (IsSystemAdminOrAuditor,)

    def get_queryset(self):
        return super().get_queryset().filter(node_type='execution')
        # FIXME: For now, we don't have a good way of checking the health of a hop node.

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        data = self.get_serializer(data=request.data).to_representation(obj)
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.health_check_pending:
            return Response({'msg': f"Health check was already in progress for {obj.hostname}."}, status=status.HTTP_200_OK)

        # Note: hop nodes are already excluded by the get_queryset method
        obj.health_check_started = now()
        obj.save(update_fields=['health_check_started'])
        if obj.node_type == models.Instance.Types.EXECUTION:
            from awx.main.tasks.system import execution_node_health_check

            execution_node_health_check.apply_async([obj.hostname])
        else:
            return Response(
                {"error": f"Cannot run a health check on instances of type {obj.node_type}.  Health checks can only be run on execution nodes."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'msg': f"Health check is running for {obj.hostname}."}, status=status.HTTP_200_OK)


class InstanceGroupList(ListCreateAPIView):
    name = _("Instance Groups")
    model = models.InstanceGroup
    serializer_class = serializers.InstanceGroupSerializer


class InstanceGroupDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    always_allow_superuser = False
    name = _("Instance Group Detail")
    model = models.InstanceGroup
    serializer_class = serializers.InstanceGroupSerializer

    def update_raw_data(self, data):
        if self.get_object().is_container_group:
            data.pop('policy_instance_percentage', None)
            data.pop('policy_instance_minimum', None)
            data.pop('policy_instance_list', None)
        return super(InstanceGroupDetail, self).update_raw_data(data)


class InstanceGroupUnifiedJobsList(SubListAPIView):
    name = _("Instance Group Running Jobs")
    model = models.UnifiedJob
    serializer_class = serializers.UnifiedJobListSerializer
    parent_model = models.InstanceGroup
    relationship = "unifiedjob_set"


class InstanceGroupAccessList(ResourceAccessList):
    model = models.User  # needs to be User for AccessLists
    parent_model = models.InstanceGroup


class InstanceGroupObjectRolesList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.InstanceGroup
    search_fields = ('role_field', 'content_type__model')

    def get_queryset(self):
        po = self.get_parent_object()
        content_type = ContentType.objects.get_for_model(self.parent_model)
        return models.Role.objects.filter(content_type=content_type, object_id=po.pk)


class InstanceGroupInstanceList(InstanceGroupMembershipMixin, SubListAttachDetachAPIView):
    name = _("Instance Group's Instances")
    model = models.Instance
    serializer_class = serializers.InstanceSerializer
    parent_model = models.InstanceGroup
    relationship = "instances"
    search_fields = ('hostname',)

    def is_valid_relation(self, parent, sub, created=False):
        if sub.node_type == 'control':
            return {'msg': _(f"Cannot change instance group membership of control-only node: {sub.hostname}.")}
        if sub.node_type == 'hop':
            return {'msg': _(f"Cannot change instance group membership of hop node : {sub.hostname}.")}
        return None

    def is_valid_removal(self, parent, sub):
        res = self.is_valid_relation(parent, sub)
        if res:
            return res
        if sub.node_type == 'hybrid' and parent.name == settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME:
            return {'msg': _(f"Cannot disassociate hybrid node {sub.hostname} from {parent.name}.")}
        return None


class ScheduleList(ListCreateAPIView):
    name = _("Schedules")
    model = models.Schedule
    serializer_class = serializers.ScheduleSerializer
    ordering = ('id',)


class ScheduleDetail(RetrieveUpdateDestroyAPIView):
    model = models.Schedule
    serializer_class = serializers.ScheduleSerializer


class SchedulePreview(GenericAPIView):
    model = models.Schedule
    name = _('Schedule Recurrence Rule Preview')
    serializer_class = serializers.SchedulePreviewSerializer
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            next_stamp = now()
            schedule = []
            gen = models.Schedule.rrulestr(serializer.validated_data['rrule']).xafter(next_stamp, count=20)

            # loop across the entire generator and grab the first 10 events
            for event in gen:
                if len(schedule) >= 10:
                    break
                if not dateutil.tz.datetime_exists(event):
                    # skip imaginary dates, like 2:30 on DST boundaries
                    continue
                schedule.append(event)

            return Response({'local': schedule, 'utc': [s.astimezone(pytz.utc) for s in schedule]})
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ScheduleZoneInfo(APIView):
    swagger_topic = 'System Configuration'

    def get(self, request):
        return Response({'zones': models.Schedule.get_zoneinfo(), 'links': models.Schedule.get_zoneinfo_links()})


class LaunchConfigCredentialsBase(SubListAttachDetachAPIView):
    model = models.Credential
    serializer_class = serializers.CredentialSerializer
    relationship = 'credentials'

    def is_valid_relation(self, parent, sub, created=False):
        if not parent.unified_job_template:
            return {"msg": _("Cannot assign credential when related template is null.")}

        ask_mapping = parent.unified_job_template.get_ask_mapping()

        if self.relationship not in ask_mapping:
            return {"msg": _("Related template cannot accept {} on launch.").format(self.relationship)}
        elif sub.passwords_needed:
            return {"msg": _("Credential that requires user input on launch cannot be used in saved launch configuration.")}

        ask_field_name = ask_mapping[self.relationship]

        if not getattr(parent.unified_job_template, ask_field_name):
            return {"msg": _("Related template is not configured to accept credentials on launch.")}
        elif sub.unique_hash() in [cred.unique_hash() for cred in parent.credentials.all()]:
            return {
                "msg": _("This launch configuration already provides a {credential_type} credential.").format(credential_type=sub.unique_hash(display=True))
            }
        elif sub.pk in parent.unified_job_template.credentials.values_list('pk', flat=True):
            return {"msg": _("Related template already uses {credential_type} credential.").format(credential_type=sub.name)}

        # None means there were no validation errors
        return None


class ScheduleCredentialsList(LaunchConfigCredentialsBase):
    parent_model = models.Schedule


class ScheduleLabelsList(LabelSubListCreateAttachDetachView):
    parent_model = models.Schedule


class ScheduleInstanceGroupList(SubListAttachDetachAPIView):
    model = models.InstanceGroup
    serializer_class = serializers.InstanceGroupSerializer
    parent_model = models.Schedule
    relationship = 'instance_groups'


class ScheduleUnifiedJobsList(SubListAPIView):
    model = models.UnifiedJob
    serializer_class = serializers.UnifiedJobListSerializer
    parent_model = models.Schedule
    relationship = 'unifiedjob_set'
    name = _('Schedule Jobs List')


def immutablesharedfields(cls):
    '''
    Class decorator to prevent modifying shared resources when ALLOW_LOCAL_RESOURCE_MANAGEMENT setting is set to False.

    Works by overriding these view methods:
    - create
    - delete
    - perform_update
    create and delete are overridden to raise a PermissionDenied exception.
    perform_update is overridden to check if any shared fields are being modified,
    and raise a PermissionDenied exception if so.
    '''
    # create instead of perform_create because some of our views
    # override create instead of perform_create
    if hasattr(cls, 'create'):
        cls.original_create = cls.create

        @functools.wraps(cls.create)
        def create_wrapper(*args, **kwargs):
            if settings.ALLOW_LOCAL_RESOURCE_MANAGEMENT:
                return cls.original_create(*args, **kwargs)
            raise PermissionDenied({'detail': _('Creation of this resource is not allowed. Create this resource via the platform ingress.')})

        cls.create = create_wrapper

    if hasattr(cls, 'delete'):
        cls.original_delete = cls.delete

        @functools.wraps(cls.delete)
        def delete_wrapper(*args, **kwargs):
            if settings.ALLOW_LOCAL_RESOURCE_MANAGEMENT:
                return cls.original_delete(*args, **kwargs)
            raise PermissionDenied({'detail': _('Deletion of this resource is not allowed. Delete this resource via the platform ingress.')})

        cls.delete = delete_wrapper

    if hasattr(cls, 'perform_update'):
        cls.original_perform_update = cls.perform_update

        @functools.wraps(cls.perform_update)
        def update_wrapper(*args, **kwargs):
            if not settings.ALLOW_LOCAL_RESOURCE_MANAGEMENT:
                view, serializer = args
                instance = view.get_object()
                if instance:
                    if isinstance(instance, models.Organization):
                        shared_fields = OrganizationType._declared_fields.keys()
                    elif isinstance(instance, models.User):
                        shared_fields = UserType._declared_fields.keys()
                    elif isinstance(instance, models.Team):
                        shared_fields = TeamType._declared_fields.keys()
                    attrs = serializer.validated_data
                    for field in shared_fields:
                        if field in attrs and getattr(instance, field) != attrs[field]:
                            raise PermissionDenied({field: _(f"Cannot change shared field '{field}'. Alter this field via the platform ingress.")})
            return cls.original_perform_update(*args, **kwargs)

        cls.perform_update = update_wrapper

    return cls


@immutablesharedfields
class TeamList(ListCreateAPIView):
    model = models.Team
    serializer_class = serializers.TeamSerializer


@immutablesharedfields
class TeamDetail(RetrieveUpdateDestroyAPIView):
    model = models.Team
    serializer_class = serializers.TeamSerializer


@immutablesharedfields
class TeamUsersList(BaseUsersList):
    model = models.User
    serializer_class = serializers.UserSerializer
    parent_model = models.Team
    relationship = 'member_role.members'
    ordering = ('username',)


class TeamRolesList(SubListAttachDetachAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializerWithParentAccess
    metadata_class = RoleMetadata
    parent_model = models.Team
    relationship = 'member_role.children'
    search_fields = ('role_field', 'content_type__model')

    def get_queryset(self):
        team = get_object_or_404(models.Team, pk=self.kwargs['pk'])
        if not self.request.user.can_access(models.Team, 'read', team):
            raise PermissionDenied()
        return models.Role.filter_visible_roles(self.request.user, team.member_role.children.all().exclude(pk=team.read_role.pk))

    def post(self, request, *args, **kwargs):
        sub_id = request.data.get('id', None)
        if not sub_id:
            return super(TeamRolesList, self).post(request)

        role = get_object_or_400(models.Role, pk=sub_id)
        org_content_type = ContentType.objects.get_for_model(models.Organization)
        if role.content_type == org_content_type and role.role_field in ['member_role', 'admin_role']:
            data = dict(msg=_("You cannot assign an Organization participation role as a child role for a Team."))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)

        if role.is_singleton():
            data = dict(msg=_("You cannot grant system-level permissions to a team."))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)

        team = get_object_or_404(models.Team, pk=self.kwargs['pk'])
        credential_content_type = ContentType.objects.get_for_model(models.Credential)
        if role.content_type == credential_content_type:
            if not role.content_object.organization or role.content_object.organization.id != team.organization.id:
                data = dict(msg=_("You cannot grant credential access to a team when the Organization field isn't set, or belongs to a different organization"))
                return Response(data, status=status.HTTP_400_BAD_REQUEST)

        return super(TeamRolesList, self).post(request, *args, **kwargs)


class TeamObjectRolesList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.Team
    search_fields = ('role_field', 'content_type__model')
    deprecated = True

    def get_queryset(self):
        po = self.get_parent_object()
        content_type = ContentType.objects.get_for_model(self.parent_model)
        return models.Role.objects.filter(content_type=content_type, object_id=po.pk)


class TeamProjectsList(SubListAPIView):
    model = models.Project
    serializer_class = serializers.ProjectSerializer
    parent_model = models.Team

    def get_queryset(self):
        team = self.get_parent_object()
        self.check_parent_access(team)
        model_ct = ContentType.objects.get_for_model(self.model)
        parent_ct = ContentType.objects.get_for_model(self.parent_model)

        rd = get_role_definition(team.member_role)
        role = ObjectRole.objects.filter(object_id=team.id, content_type=parent_ct, role_definition=rd).first()
        if role is None:
            # Team has no permissions, therefore team has no projects
            return self.model.objects.none()
        else:
            project_qs = self.model.accessible_objects(self.request.user, 'read_role')
            return project_qs.filter(id__in=RoleEvaluation.objects.filter(content_type_id=model_ct.id, role=role).values_list('object_id'))


class TeamActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.Team
    relationship = 'activitystream_set'
    search_fields = ('changes',)

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)

        qs = self.request.user.get_queryset(self.model)

        return qs.filter(
            Q(team=parent)
            | Q(
                project__in=RoleEvaluation.objects.filter(
                    role__in=parent.has_roles.all(), content_type_id=ContentType.objects.get_for_model(models.Project).id, codename='view_project'
                )
                .values_list('object_id')
                .distinct()
            )
            | Q(
                credential__in=RoleEvaluation.objects.filter(
                    role__in=parent.has_roles.all(), content_type_id=ContentType.objects.get_for_model(models.Credential).id, codename='view_credential'
                )
                .values_list('object_id')
                .distinct()
            )
        )


class TeamAccessList(ResourceAccessList):
    model = models.User  # needs to be User for AccessLists's
    parent_model = models.Team


class ExecutionEnvironmentList(ListCreateAPIView):
    always_allow_superuser = False
    model = models.ExecutionEnvironment
    serializer_class = serializers.ExecutionEnvironmentSerializer
    swagger_topic = "Execution Environments"


class ExecutionEnvironmentDetail(RetrieveUpdateDestroyAPIView):
    always_allow_superuser = False
    model = models.ExecutionEnvironment
    serializer_class = serializers.ExecutionEnvironmentSerializer
    swagger_topic = "Execution Environments"

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        fields_to_check = ['name', 'description', 'organization', 'image', 'credential']
        if instance.managed and request.user.can_access(models.ExecutionEnvironment, 'change', instance):
            for field in fields_to_check:
                if kwargs.get('partial') and field not in request.data:
                    continue
                left = getattr(instance, field, None)
                if hasattr(left, 'id'):
                    left = left.id
                right = request.data.get(field)
                if left != right:
                    raise PermissionDenied(_("Only the 'pull' field can be edited for managed execution environments."))
        return super().update(request, *args, **kwargs)


class ExecutionEnvironmentJobTemplateList(SubListAPIView):
    model = models.UnifiedJobTemplate
    serializer_class = serializers.UnifiedJobTemplateSerializer
    parent_model = models.ExecutionEnvironment
    relationship = 'unifiedjobtemplates'


class ExecutionEnvironmentCopy(CopyAPIView):
    model = models.ExecutionEnvironment
    copy_return_serializer_class = serializers.ExecutionEnvironmentSerializer


class ExecutionEnvironmentActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.ExecutionEnvironment
    relationship = 'activitystream_set'
    search_fields = ('changes',)
    filter_read_permission = False


class ProjectList(ListCreateAPIView):
    model = models.Project
    serializer_class = serializers.ProjectSerializer


class ProjectDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    model = models.Project
    serializer_class = serializers.ProjectSerializer


class ProjectPlaybooks(RetrieveAPIView):
    model = models.Project
    serializer_class = serializers.ProjectPlaybooksSerializer


class ProjectInventories(RetrieveAPIView):
    model = models.Project
    serializer_class = serializers.ProjectInventoriesSerializer


class ProjectTeamsList(ListAPIView):
    model = models.Team
    serializer_class = serializers.TeamSerializer

    def get_queryset(self):
        p = get_object_or_404(models.Project, pk=self.kwargs['pk'])
        if not self.request.user.can_access(models.Project, 'read', p):
            raise PermissionDenied()
        project_ct = ContentType.objects.get_for_model(models.Project)
        team_ct = ContentType.objects.get_for_model(self.model)
        all_roles = models.Role.objects.filter(Q(descendents__content_type=project_ct) & Q(descendents__object_id=p.pk), content_type=team_ct)
        return self.model.accessible_objects(self.request.user, 'read_role').filter(pk__in=[t.content_object.pk for t in all_roles])


class ProjectSchedulesList(SubListCreateAPIView):
    name = _("Project Schedules")

    model = models.Schedule
    serializer_class = serializers.ScheduleSerializer
    parent_model = models.Project
    relationship = 'schedules'
    parent_key = 'unified_job_template'


class ProjectScmInventorySources(SubListAPIView):
    name = _("Project SCM Inventory Sources")
    model = models.InventorySource
    serializer_class = serializers.InventorySourceSerializer
    parent_model = models.Project
    relationship = 'scm_inventory_sources'
    parent_key = 'source_project'


class ProjectActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.Project
    relationship = 'activitystream_set'
    search_fields = ('changes',)

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model)
        if parent is None:
            return qs
        elif parent.credential is None:
            return qs.filter(project=parent)
        return qs.filter(Q(project=parent) | Q(credential=parent.credential))


class ProjectNotificationTemplatesAnyList(SubListCreateAttachDetachAPIView):
    model = models.NotificationTemplate
    serializer_class = serializers.NotificationTemplateSerializer
    parent_model = models.Project


class ProjectNotificationTemplatesStartedList(ProjectNotificationTemplatesAnyList):
    relationship = 'notification_templates_started'


class ProjectNotificationTemplatesErrorList(ProjectNotificationTemplatesAnyList):
    relationship = 'notification_templates_error'


class ProjectNotificationTemplatesSuccessList(ProjectNotificationTemplatesAnyList):
    relationship = 'notification_templates_success'


class ProjectUpdatesList(SubListAPIView):
    model = models.ProjectUpdate
    serializer_class = serializers.ProjectUpdateListSerializer
    parent_model = models.Project
    relationship = 'project_updates'


class ProjectUpdateView(RetrieveAPIView):
    model = models.Project
    serializer_class = serializers.ProjectUpdateViewSerializer
    permission_classes = (ProjectUpdatePermission,)

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.can_update:
            project_update = obj.update()
            if not project_update:
                return Response({}, status=status.HTTP_400_BAD_REQUEST)
            else:
                data = OrderedDict()
                data['project_update'] = project_update.id
                data.update(serializers.ProjectUpdateSerializer(project_update, context=self.get_serializer_context()).to_representation(project_update))
                headers = {'Location': project_update.get_absolute_url(request=request)}
                return Response(data, headers=headers, status=status.HTTP_202_ACCEPTED)
        else:
            return self.http_method_not_allowed(request, *args, **kwargs)


class ProjectUpdateList(ListAPIView):
    model = models.ProjectUpdate
    serializer_class = serializers.ProjectUpdateListSerializer


class ProjectUpdateDetail(UnifiedJobDeletionMixin, RetrieveDestroyAPIView):
    model = models.ProjectUpdate
    serializer_class = serializers.ProjectUpdateDetailSerializer


class ProjectUpdateEventsList(SubListAPIView):
    model = models.ProjectUpdateEvent
    serializer_class = serializers.ProjectUpdateEventSerializer
    parent_model = models.ProjectUpdate
    relationship = 'project_update_events'
    name = _('Project Update Events List')
    search_fields = ('stdout',)
    pagination_class = UnifiedJobEventPagination

    def finalize_response(self, request, response, *args, **kwargs):
        response['X-UI-Max-Events'] = settings.MAX_UI_JOB_EVENTS
        return super(ProjectUpdateEventsList, self).finalize_response(request, response, *args, **kwargs)

    def get_queryset(self):
        pu = self.get_parent_object()
        self.check_parent_access(pu)
        return pu.get_event_queryset()


class SystemJobEventsList(SubListAPIView):
    model = models.SystemJobEvent
    serializer_class = serializers.SystemJobEventSerializer
    parent_model = models.SystemJob
    relationship = 'system_job_events'
    name = _('System Job Events List')
    search_fields = ('stdout',)
    pagination_class = UnifiedJobEventPagination

    def finalize_response(self, request, response, *args, **kwargs):
        response['X-UI-Max-Events'] = settings.MAX_UI_JOB_EVENTS
        return super(SystemJobEventsList, self).finalize_response(request, response, *args, **kwargs)

    def get_queryset(self):
        job = self.get_parent_object()
        self.check_parent_access(job)
        return job.get_event_queryset()


class ProjectUpdateCancel(GenericCancelView):
    model = models.ProjectUpdate
    serializer_class = serializers.ProjectUpdateCancelSerializer


class ProjectUpdateNotificationsList(SubListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    parent_model = models.ProjectUpdate
    relationship = 'notifications'
    search_fields = ('subject', 'notification_type', 'body')


class ProjectUpdateScmInventoryUpdates(SubListAPIView):
    name = _("Project Update SCM Inventory Updates")
    model = models.InventoryUpdate
    serializer_class = serializers.InventoryUpdateListSerializer
    parent_model = models.ProjectUpdate
    relationship = 'scm_inventory_updates'
    parent_key = 'source_project_update'


class ProjectAccessList(ResourceAccessList):
    model = models.User  # needs to be User for AccessLists's
    parent_model = models.Project


class ProjectObjectRolesList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.Project
    search_fields = ('role_field', 'content_type__model')
    deprecated = True

    def get_queryset(self):
        po = self.get_parent_object()
        content_type = ContentType.objects.get_for_model(self.parent_model)
        return models.Role.objects.filter(content_type=content_type, object_id=po.pk)


class ProjectCopy(CopyAPIView):
    model = models.Project
    copy_return_serializer_class = serializers.ProjectSerializer


@immutablesharedfields
class UserList(ListCreateAPIView):
    model = models.User
    serializer_class = serializers.UserSerializer
    permission_classes = (UserPermission,)
    ordering = ('username',)


class UserMeList(ListAPIView):
    model = models.User
    serializer_class = serializers.UserSerializer
    name = _('Me')
    ordering = ('username',)

    def get_queryset(self):
        return self.model.objects.filter(pk=self.request.user.pk)


class UserTeamsList(SubListAPIView):
    model = models.Team
    serializer_class = serializers.TeamSerializer
    parent_model = models.User

    def get_queryset(self):
        u = get_object_or_404(models.User, pk=self.kwargs['pk'])
        if not self.request.user.can_access(models.User, 'read', u):
            raise PermissionDenied()
        return models.Team.accessible_objects(self.request.user, 'read_role').filter(Q(member_role__members=u) | Q(admin_role__members=u)).distinct()


class UserRolesList(SubListAttachDetachAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializerWithParentAccess
    metadata_class = RoleMetadata
    parent_model = models.User
    relationship = 'roles'
    permission_classes = (IsAuthenticated,)
    search_fields = ('role_field', 'content_type__model')

    def get_queryset(self):
        u = get_object_or_404(models.User, pk=self.kwargs['pk'])
        if not self.request.user.can_access(models.User, 'read', u):
            raise PermissionDenied()
        content_type = ContentType.objects.get_for_model(models.User)

        return models.Role.filter_visible_roles(self.request.user, u.roles.all()).exclude(content_type=content_type, object_id=u.id)

    def post(self, request, *args, **kwargs):
        sub_id = request.data.get('id', None)
        if not sub_id:
            return super(UserRolesList, self).post(request)

        user = get_object_or_400(models.User, pk=self.kwargs['pk'])
        role = get_object_or_400(models.Role, pk=sub_id)

        content_types = ContentType.objects.get_for_models(models.Organization, models.Team, models.Credential)  # dict of {model: content_type}
        # Prevent user to be associated with team/org when ALLOW_LOCAL_RESOURCE_MANAGEMENT is False
        if not settings.ALLOW_LOCAL_RESOURCE_MANAGEMENT:
            for model in [models.Organization, models.Team]:
                ct = content_types[model]
                if role.content_type == ct and role.role_field in ['member_role', 'admin_role']:
                    data = dict(msg=_(f"Cannot directly modify user membership to {ct.model}. Direct shared resource management disabled"))
                    return Response(data, status=status.HTTP_403_FORBIDDEN)

        credential_content_type = content_types[models.Credential]
        if role.content_type == credential_content_type:
            if 'disassociate' not in request.data and role.content_object.organization and user not in role.content_object.organization.member_role:
                data = dict(msg=_("You cannot grant credential access to a user not in the credentials' organization"))
                return Response(data, status=status.HTTP_400_BAD_REQUEST)

            if not role.content_object.organization and not request.user.is_superuser:
                data = dict(msg=_("You cannot grant private credential access to another user"))
                return Response(data, status=status.HTTP_400_BAD_REQUEST)

        return super(UserRolesList, self).post(request, *args, **kwargs)

    def check_parent_access(self, parent=None):
        # We hide roles that shouldn't be seen in our queryset
        return True


class UserProjectsList(SubListAPIView):
    model = models.Project
    serializer_class = serializers.ProjectSerializer
    parent_model = models.User

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        my_qs = models.Project.accessible_objects(self.request.user, 'read_role')
        user_qs = models.Project.accessible_objects(parent, 'read_role')
        return my_qs & user_qs


class UserOrganizationsList(OrganizationCountsMixin, SubListAPIView):
    model = models.Organization
    serializer_class = serializers.OrganizationSerializer
    parent_model = models.User
    relationship = 'organizations'

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        my_qs = models.Organization.accessible_objects(self.request.user, 'read_role')
        user_qs = models.Organization.objects.filter(member_role__members=parent)
        return my_qs & user_qs


class UserAdminOfOrganizationsList(OrganizationCountsMixin, SubListAPIView):
    model = models.Organization
    serializer_class = serializers.OrganizationSerializer
    parent_model = models.User
    relationship = 'admin_of_organizations'

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        my_qs = models.Organization.accessible_objects(self.request.user, 'read_role')
        user_qs = models.Organization.objects.filter(admin_role__members=parent)
        return my_qs & user_qs


class UserActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.User
    relationship = 'activitystream_set'
    search_fields = ('changes',)

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model)
        return qs.filter(Q(actor=parent) | Q(user__in=[parent]))


@immutablesharedfields
class UserDetail(RetrieveUpdateDestroyAPIView):
    model = models.User
    serializer_class = serializers.UserSerializer

    def update_filter(self, request, *args, **kwargs):
        '''make sure non-read-only fields that can only be edited by admins, are only edited by admins'''
        obj = self.get_object()
        can_change = request.user.can_access(models.User, 'change', obj, request.data)
        can_admin = request.user.can_access(models.User, 'admin', obj, request.data)

        su_only_edit_fields = ('is_superuser', 'is_system_auditor')
        admin_only_edit_fields = ('username', 'is_active')

        fields_to_check = ()
        if not request.user.is_superuser:
            fields_to_check += su_only_edit_fields

        if can_change and not can_admin:
            fields_to_check += admin_only_edit_fields

        bad_changes = {}
        for field in fields_to_check:
            left = getattr(obj, field, None)
            right = request.data.get(field, None)
            if left is not None and right is not None and left != right:
                bad_changes[field] = (left, right)
        if bad_changes:
            raise PermissionDenied(_('Cannot change %s.') % ', '.join(bad_changes.keys()))

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        can_delete = request.user.can_access(models.User, 'delete', obj)
        if not can_delete:
            raise PermissionDenied(_('Cannot delete user.'))
        return super(UserDetail, self).destroy(request, *args, **kwargs)


class UserAccessList(ResourceAccessList):
    model = models.User  # needs to be User for AccessLists's
    parent_model = models.User


class CredentialTypeList(ListCreateAPIView):
    model = models.CredentialType
    serializer_class = serializers.CredentialTypeSerializer


class CredentialTypeDetail(RetrieveUpdateDestroyAPIView):
    model = models.CredentialType
    serializer_class = serializers.CredentialTypeSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.managed:
            raise PermissionDenied(detail=_("Deletion not allowed for managed credential types"))
        if instance.credentials.exists():
            raise PermissionDenied(detail=_("Credential types that are in use cannot be deleted"))
        return super(CredentialTypeDetail, self).destroy(request, *args, **kwargs)


class CredentialTypeCredentialList(SubListCreateAPIView):
    model = models.Credential
    parent_model = models.CredentialType
    relationship = 'credentials'
    serializer_class = serializers.CredentialSerializer


class CredentialTypeActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.CredentialType
    relationship = 'activitystream_set'
    search_fields = ('changes',)


class CredentialList(ListCreateAPIView):
    model = models.Credential
    serializer_class = serializers.CredentialSerializerCreate


class CredentialOwnerUsersList(SubListAPIView):
    model = models.User
    serializer_class = serializers.UserSerializer
    parent_model = models.Credential
    relationship = 'admin_role.members'
    ordering = ('username',)


class CredentialOwnerTeamsList(SubListAPIView):
    model = models.Team
    serializer_class = serializers.TeamSerializer
    parent_model = models.Credential

    def get_queryset(self):
        credential = get_object_or_404(self.parent_model, pk=self.kwargs['pk'])
        if not self.request.user.can_access(models.Credential, 'read', credential):
            raise PermissionDenied()

        content_type = ContentType.objects.get_for_model(self.model)
        teams = [c.content_object.pk for c in credential.admin_role.parents.filter(content_type=content_type)]

        return self.model.objects.filter(pk__in=teams)


class UserCredentialsList(SubListCreateAPIView):
    model = models.Credential
    serializer_class = serializers.UserCredentialSerializerCreate
    parent_model = models.User
    parent_key = 'user'

    def get_queryset(self):
        user = self.get_parent_object()
        self.check_parent_access(user)

        visible_creds = models.Credential.accessible_objects(self.request.user, 'read_role')
        user_creds = models.Credential.accessible_objects(user, 'read_role')
        return user_creds & visible_creds


class TeamCredentialsList(SubListCreateAPIView):
    model = models.Credential
    serializer_class = serializers.TeamCredentialSerializerCreate
    parent_model = models.Team
    parent_key = 'team'

    def get_queryset(self):
        team = self.get_parent_object()
        self.check_parent_access(team)

        visible_creds = models.Credential.accessible_objects(self.request.user, 'read_role')
        team_creds = models.Credential.objects.filter(Q(use_role__parents=team.member_role) | Q(admin_role__parents=team.member_role))
        return (team_creds & visible_creds).distinct()


class OrganizationCredentialList(SubListCreateAPIView):
    model = models.Credential
    serializer_class = serializers.OrganizationCredentialSerializerCreate
    parent_model = models.Organization
    parent_key = 'organization'

    def get_queryset(self):
        organization = self.get_parent_object()
        self.check_parent_access(organization)

        user_visible = models.Credential.accessible_objects(self.request.user, 'read_role').all()
        org_set = models.Credential.objects.filter(organization=organization)

        if self.request.user.is_superuser or self.request.user.is_system_auditor:
            return org_set

        return org_set & user_visible


class CredentialDetail(RetrieveUpdateDestroyAPIView):
    model = models.Credential
    serializer_class = serializers.CredentialSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.managed:
            raise PermissionDenied(detail=_("Deletion not allowed for managed credentials"))
        return super(CredentialDetail, self).destroy(request, *args, **kwargs)


class CredentialActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.Credential
    relationship = 'activitystream_set'
    search_fields = ('changes',)


class CredentialAccessList(ResourceAccessList):
    model = models.User  # needs to be User for AccessLists's
    parent_model = models.Credential


class CredentialObjectRolesList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.Credential
    search_fields = ('role_field', 'content_type__model')
    deprecated = True

    def get_queryset(self):
        po = self.get_parent_object()
        content_type = ContentType.objects.get_for_model(self.parent_model)
        return models.Role.objects.filter(content_type=content_type, object_id=po.pk)


class CredentialCopy(CopyAPIView):
    model = models.Credential
    copy_return_serializer_class = serializers.CredentialSerializer


class CredentialExternalTest(SubDetailAPIView):
    """
    Test updates to the input values and metadata of an external credential
    before saving them.
    """

    name = _('External Credential Test')

    model = models.Credential
    serializer_class = serializers.EmptySerializer
    obj_permission_type = 'use'

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        backend_kwargs = {}
        for field_name, value in obj.inputs.items():
            backend_kwargs[field_name] = obj.get_input(field_name)
        for field_name, value in request.data.get('inputs', {}).items():
            if value != '$encrypted$':
                backend_kwargs[field_name] = value
        backend_kwargs.update(request.data.get('metadata', {}))
        try:
            with set_environ(**settings.AWX_TASK_ENV):
                obj.credential_type.plugin.backend(**backend_kwargs)
                return Response({}, status=status.HTTP_202_ACCEPTED)
        except requests.exceptions.HTTPError as exc:
            message = 'HTTP {}'.format(exc.response.status_code)
            return Response({'inputs': message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            message = exc.__class__.__name__
            args = getattr(exc, 'args', [])
            for a in args:
                if isinstance(getattr(a, 'reason', None), ConnectTimeoutError):
                    message = str(a.reason)
            return Response({'inputs': message}, status=status.HTTP_400_BAD_REQUEST)


class CredentialInputSourceDetail(RetrieveUpdateDestroyAPIView):
    name = _("Credential Input Source Detail")

    model = models.CredentialInputSource
    serializer_class = serializers.CredentialInputSourceSerializer


class CredentialInputSourceList(ListCreateAPIView):
    name = _("Credential Input Sources")

    model = models.CredentialInputSource
    serializer_class = serializers.CredentialInputSourceSerializer


class CredentialInputSourceSubList(SubListCreateAPIView):
    name = _("Credential Input Sources")

    model = models.CredentialInputSource
    serializer_class = serializers.CredentialInputSourceSerializer
    parent_model = models.Credential
    relationship = 'input_sources'
    parent_key = 'target_credential'


class CredentialTypeExternalTest(SubDetailAPIView):
    """
    Test a complete set of input values for an external credential before
    saving it.
    """

    name = _('External Credential Type Test')

    model = models.CredentialType
    serializer_class = serializers.EmptySerializer

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        backend_kwargs = request.data.get('inputs', {})
        backend_kwargs.update(request.data.get('metadata', {}))
        try:
            obj.plugin.backend(**backend_kwargs)
            return Response({}, status=status.HTTP_202_ACCEPTED)
        except requests.exceptions.HTTPError as exc:
            message = 'HTTP {}'.format(exc.response.status_code)
            return Response({'inputs': message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            message = exc.__class__.__name__
            args = getattr(exc, 'args', [])
            for a in args:
                if isinstance(getattr(a, 'reason', None), ConnectTimeoutError):
                    message = str(a.reason)
            return Response({'inputs': message}, status=status.HTTP_400_BAD_REQUEST)


class HostRelatedSearchMixin(object):
    @property
    def related_search_fields(self):
        # Edge-case handle: https://github.com/ansible/ansible-tower/issues/7712
        ret = super(HostRelatedSearchMixin, self).related_search_fields
        ret.append('ansible_facts')
        return ret


class HostMetricList(ListAPIView):
    name = _("Host Metrics List")
    model = models.HostMetric
    serializer_class = serializers.HostMetricSerializer
    permission_classes = (IsSystemAdminOrAuditor,)
    search_fields = ('hostname', 'deleted')

    def get_queryset(self):
        return self.model.objects.all()


class HostMetricDetail(RetrieveDestroyAPIView):
    name = _("Host Metric Detail")
    model = models.HostMetric
    serializer_class = serializers.HostMetricSerializer
    permission_classes = (IsSystemAdminOrAuditor,)

    def delete(self, request, *args, **kwargs):
        self.get_object().soft_delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class HostMetricSummaryMonthlyList(ListAPIView):
    name = _("Host Metrics Summary Monthly")
    model = models.HostMetricSummaryMonthly
    serializer_class = serializers.HostMetricSummaryMonthlySerializer
    permission_classes = (IsSystemAdminOrAuditor,)
    search_fields = ('date',)

    def get_queryset(self):
        return self.model.objects.all()


class HostList(HostRelatedSearchMixin, ListCreateAPIView):
    always_allow_superuser = False
    model = models.Host
    serializer_class = serializers.HostSerializer

    def get_queryset(self):
        qs = super(HostList, self).get_queryset()
        filter_string = self.request.query_params.get('host_filter', None)
        if filter_string:
            filter_qs = SmartFilter.query_from_string(filter_string)
            qs &= filter_qs
        return qs.distinct()

    def list(self, *args, **kwargs):
        try:
            return super(HostList, self).list(*args, **kwargs)
        except Exception as e:
            return Response(dict(error=_(str(e))), status=status.HTTP_400_BAD_REQUEST)


class HostDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    always_allow_superuser = False
    model = models.Host
    serializer_class = serializers.HostSerializer

    def delete(self, request, *args, **kwargs):
        if self.get_object().inventory.pending_deletion:
            return Response({"error": _("The inventory for this host is already being deleted.")}, status=status.HTTP_400_BAD_REQUEST)
        if self.get_object().inventory.kind == 'constructed':
            return Response({"error": _("Delete constructed inventory hosts from input inventory.")}, status=status.HTTP_400_BAD_REQUEST)
        return super(HostDetail, self).delete(request, *args, **kwargs)


class HostAnsibleFactsDetail(RetrieveAPIView):
    model = models.Host
    serializer_class = serializers.AnsibleFactsSerializer

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.inventory.kind == 'constructed':
            # If this is a constructed inventory host, it is not the source of truth about facts
            # redirect to the original input inventory host instead
            return HttpResponseRedirect(reverse('api:host_ansible_facts_detail', kwargs={'pk': obj.instance_id}, request=self.request))
        return super().get(request, *args, **kwargs)


class InventoryHostsList(HostRelatedSearchMixin, SubListCreateAttachDetachAPIView):
    model = models.Host
    serializer_class = serializers.HostSerializer
    parent_model = models.Inventory
    relationship = 'hosts'
    parent_key = 'inventory'
    filter_read_permission = False


class HostGroupsList(SubListCreateAttachDetachAPIView):
    '''the list of groups a host is directly a member of'''

    model = models.Group
    serializer_class = serializers.GroupSerializer
    parent_model = models.Host
    relationship = 'groups'

    def update_raw_data(self, data):
        data.pop('inventory', None)
        return super(HostGroupsList, self).update_raw_data(data)

    def create(self, request, *args, **kwargs):
        # Inject parent host inventory ID into new group data.
        data = request.data
        # HACK: Make request data mutable.
        if getattr(data, '_mutable', None) is False:
            data._mutable = True
        data['inventory'] = self.get_parent_object().inventory_id
        return super(HostGroupsList, self).create(request, *args, **kwargs)


class HostAllGroupsList(SubListAPIView):
    '''the list of all groups of which the host is directly or indirectly a member'''

    model = models.Group
    serializer_class = serializers.GroupSerializer
    parent_model = models.Host
    relationship = 'groups'

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model).distinct()
        sublist_qs = parent.all_groups.distinct()
        return qs & sublist_qs


class HostInventorySourcesList(SubListAPIView):
    model = models.InventorySource
    serializer_class = serializers.InventorySourceSerializer
    parent_model = models.Host
    relationship = 'inventory_sources'


class HostSmartInventoriesList(SubListAPIView):
    model = models.Inventory
    serializer_class = serializers.InventorySerializer
    parent_model = models.Host
    relationship = 'smart_inventories'


class HostActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.Host
    relationship = 'activitystream_set'
    search_fields = ('changes',)

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model)
        return qs.filter(Q(host=parent) | Q(inventory=parent.inventory))


class BadGateway(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = ''
    default_code = 'bad_gateway'


class GatewayTimeout(APIException):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    default_detail = ''
    default_code = 'gateway_timeout'


class GroupList(ListCreateAPIView):
    model = models.Group
    serializer_class = serializers.GroupSerializer


class EnforceParentRelationshipMixin(object):
    """
    Useful when you have a self-referring ManyToManyRelationship.
    * Tower uses a shallow (2-deep only) url pattern. For example:

    When an object hangs off of a parent object you would have the url of the
    form /api/v2/parent_model/34/child_model. If you then wanted a child of the
    child model you would NOT do /api/v2/parent_model/34/child_model/87/child_child_model
    Instead, you would access the child_child_model via /api/v2/child_child_model/87/
    and you would create child_child_model's off of /api/v2/child_model/87/child_child_model_set
    Now, when creating child_child_model related to child_model you still want to
    link child_child_model to parent_model. That's what this class is for
    """

    enforce_parent_relationship = ''

    def update_raw_data(self, data):
        data.pop(self.enforce_parent_relationship, None)
        return super(EnforceParentRelationshipMixin, self).update_raw_data(data)

    def create(self, request, *args, **kwargs):
        # Inject parent group inventory ID into new group data.
        data = request.data
        # HACK: Make request data mutable.
        if getattr(data, '_mutable', None) is False:
            data._mutable = True
        data[self.enforce_parent_relationship] = getattr(self.get_parent_object(), '%s_id' % self.enforce_parent_relationship)
        return super(EnforceParentRelationshipMixin, self).create(request, *args, **kwargs)


class GroupChildrenList(EnforceParentRelationshipMixin, SubListCreateAttachDetachAPIView):
    model = models.Group
    serializer_class = serializers.GroupSerializer
    parent_model = models.Group
    relationship = 'children'
    enforce_parent_relationship = 'inventory'

    def unattach(self, request, *args, **kwargs):
        sub_id = request.data.get('id', None)
        if sub_id is not None:
            return super(GroupChildrenList, self).unattach(request, *args, **kwargs)
        parent = self.get_parent_object()
        if not request.user.can_access(self.model, 'delete', parent):
            raise PermissionDenied()
        parent.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def is_valid_relation(self, parent, sub, created=False):
        # Prevent any cyclical group associations.
        parent_pks = set(parent.all_parents.values_list('pk', flat=True))
        parent_pks.add(parent.pk)
        child_pks = set(sub.all_children.values_list('pk', flat=True))
        child_pks.add(sub.pk)
        if parent_pks & child_pks:
            return {'error': _('Cyclical Group association.')}
        return None


class GroupPotentialChildrenList(SubListAPIView):
    model = models.Group
    serializer_class = serializers.GroupSerializer
    parent_model = models.Group

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model)
        qs = qs.filter(inventory__pk=parent.inventory.pk)
        except_pks = set([parent.pk])
        except_pks.update(parent.all_parents.values_list('pk', flat=True))
        except_pks.update(parent.all_children.values_list('pk', flat=True))
        return qs.exclude(pk__in=except_pks)


class GroupHostsList(HostRelatedSearchMixin, SubListCreateAttachDetachAPIView):
    '''the list of hosts directly below a group'''

    model = models.Host
    serializer_class = serializers.HostSerializer
    parent_model = models.Group
    relationship = 'hosts'

    def update_raw_data(self, data):
        data.pop('inventory', None)
        return super(GroupHostsList, self).update_raw_data(data)

    def create(self, request, *args, **kwargs):
        parent_group = models.Group.objects.get(id=self.kwargs['pk'])
        # Inject parent group inventory ID into new host data.
        request.data['inventory'] = parent_group.inventory_id
        existing_hosts = models.Host.objects.filter(inventory=parent_group.inventory, name=request.data.get('name', ''))
        if existing_hosts.count() > 0 and (
            'variables' not in request.data or request.data['variables'] == '' or request.data['variables'] == '{}' or request.data['variables'] == '---'
        ):
            request.data['id'] = existing_hosts[0].id
            return self.attach(request, *args, **kwargs)
        return super(GroupHostsList, self).create(request, *args, **kwargs)


class GroupAllHostsList(HostRelatedSearchMixin, SubListAPIView):
    '''the list of all hosts below a group, even including subgroups'''

    model = models.Host
    serializer_class = serializers.HostSerializer
    parent_model = models.Group
    relationship = 'hosts'

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model).distinct()  # need distinct for '&' operator
        sublist_qs = parent.all_hosts.distinct()
        return qs & sublist_qs


class GroupInventorySourcesList(SubListAPIView):
    model = models.InventorySource
    serializer_class = serializers.InventorySourceSerializer
    parent_model = models.Group
    relationship = 'inventory_sources'


class GroupActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.Group
    relationship = 'activitystream_set'
    search_fields = ('changes',)

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model)
        return qs.filter(Q(group=parent) | Q(host__in=parent.hosts.all()))


class GroupDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    model = models.Group
    serializer_class = serializers.GroupSerializer

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        if not request.user.can_access(self.model, 'delete', obj):
            raise PermissionDenied()
        obj.delete_recursive()
        return Response(status=status.HTTP_204_NO_CONTENT)


class InventoryGroupsList(SubListCreateAttachDetachAPIView):
    model = models.Group
    serializer_class = serializers.GroupSerializer
    parent_model = models.Inventory
    relationship = 'groups'
    parent_key = 'inventory'


class InventoryRootGroupsList(SubListCreateAttachDetachAPIView):
    model = models.Group
    serializer_class = serializers.GroupSerializer
    parent_model = models.Inventory
    relationship = 'groups'
    parent_key = 'inventory'

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model).distinct()  # need distinct for '&' operator
        return qs & parent.root_groups


class BaseVariableData(RetrieveUpdateAPIView):
    parser_classes = api_settings.DEFAULT_PARSER_CLASSES + [YAMLParser]
    renderer_classes = api_settings.DEFAULT_RENDERER_CLASSES + [YAMLRenderer]
    permission_classes = (VariableDataPermission,)


class InventoryVariableData(BaseVariableData):
    model = models.Inventory
    serializer_class = serializers.InventoryVariableDataSerializer


class HostVariableData(BaseVariableData):
    model = models.Host
    serializer_class = serializers.HostVariableDataSerializer


class GroupVariableData(BaseVariableData):
    model = models.Group
    serializer_class = serializers.GroupVariableDataSerializer


class InventoryScriptView(RetrieveAPIView):
    model = models.Inventory
    serializer_class = serializers.InventoryScriptSerializer
    permission_classes = (TaskPermission,)
    filter_backends = ()

    def retrieve(self, request, *args, **kwargs):
        obj = self.get_object()
        hostname = request.query_params.get('host', '')
        hostvars = bool(request.query_params.get('hostvars', ''))
        towervars = bool(request.query_params.get('towervars', ''))
        show_all = bool(request.query_params.get('all', ''))
        subset = request.query_params.get('subset', '')
        if subset:
            if not isinstance(subset, str):
                raise ParseError(_('Inventory subset argument must be a string.'))
            if subset.startswith('slice'):
                slice_number, slice_count = models.Inventory.parse_slice_params(subset)
            else:
                raise ParseError(_('Subset does not use any supported syntax.'))
        else:
            slice_number, slice_count = 1, 1
        if hostname:
            hosts_q = dict(name=hostname)
            if not show_all:
                hosts_q['enabled'] = True
            host = get_object_or_404(obj.hosts, **hosts_q)
            return Response(host.variables_dict)
        return Response(obj.get_script_data(hostvars=hostvars, towervars=towervars, show_all=show_all, slice_number=slice_number, slice_count=slice_count))


class InventoryTreeView(RetrieveAPIView):
    model = models.Inventory
    serializer_class = serializers.GroupTreeSerializer
    filter_backends = ()

    def _populate_group_children(self, group_data, all_group_data_map, group_children_map):
        if 'children' in group_data:
            return
        group_data['children'] = []
        for child_id in group_children_map.get(group_data['id'], set()):
            group_data['children'].append(all_group_data_map[child_id])
        group_data['children'].sort(key=lambda x: x['name'])
        for child_data in group_data['children']:
            self._populate_group_children(child_data, all_group_data_map, group_children_map)

    def retrieve(self, request, *args, **kwargs):
        inventory = self.get_object()
        group_children_map = inventory.get_group_children_map()
        root_group_pks = inventory.root_groups.order_by('name').values_list('pk', flat=True)
        groups_qs = inventory.groups
        groups_qs = groups_qs.prefetch_related('inventory_sources')
        all_group_data = serializers.GroupSerializer(groups_qs, many=True).data
        all_group_data_map = dict((x['id'], x) for x in all_group_data)
        tree_data = [all_group_data_map[x] for x in root_group_pks]
        for group_data in tree_data:
            self._populate_group_children(group_data, all_group_data_map, group_children_map)
        return Response(tree_data)


class InventoryInventorySourcesList(SubListCreateAPIView):
    name = _('Inventory Source List')

    model = models.InventorySource
    serializer_class = serializers.InventorySourceSerializer
    parent_model = models.Inventory
    # Sometimes creation blocked by SCM inventory source restrictions
    always_allow_superuser = False
    relationship = 'inventory_sources'
    parent_key = 'inventory'


class InventoryInventorySourcesUpdate(RetrieveAPIView):
    name = _('Inventory Sources Update')

    model = models.Inventory
    obj_permission_type = 'start'
    serializer_class = serializers.InventorySourceUpdateSerializer
    permission_classes = (InventoryInventorySourcesUpdatePermission,)

    def retrieve(self, request, *args, **kwargs):
        inventory = self.get_object()
        update_data = []
        for inventory_source in inventory.inventory_sources.exclude(source=''):
            details = {'inventory_source': inventory_source.pk, 'can_update': inventory_source.can_update}
            update_data.append(details)
        return Response(update_data)

    def post(self, request, *args, **kwargs):
        inventory = self.get_object()
        update_data = []
        successes = 0
        failures = 0
        for inventory_source in inventory.inventory_sources.exclude(source=''):
            details = OrderedDict()
            details['inventory_source'] = inventory_source.pk
            details['status'] = None
            if inventory_source.can_update:
                update = inventory_source.update()
                details.update(serializers.InventoryUpdateDetailSerializer(update, context=self.get_serializer_context()).to_representation(update))
                details['status'] = 'started'
                details['inventory_update'] = update.id
                successes += 1
            else:
                if not details.get('status'):
                    details['status'] = _('Could not start because `can_update` returned False')
                failures += 1
            update_data.append(details)
        if failures and successes:
            status_code = status.HTTP_202_ACCEPTED
        elif failures and not successes:
            status_code = status.HTTP_400_BAD_REQUEST
        elif not failures and not successes:
            return Response({'detail': _('No inventory sources to update.')}, status=status.HTTP_400_BAD_REQUEST)
        else:
            status_code = status.HTTP_200_OK
        return Response(update_data, status=status_code)


class InventorySourceList(ListCreateAPIView):
    model = models.InventorySource
    serializer_class = serializers.InventorySourceSerializer
    always_allow_superuser = False


class InventorySourceDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    model = models.InventorySource
    serializer_class = serializers.InventorySourceSerializer


class InventorySourceSchedulesList(SubListCreateAPIView):
    name = _("Inventory Source Schedules")

    model = models.Schedule
    serializer_class = serializers.ScheduleSerializer
    parent_model = models.InventorySource
    relationship = 'schedules'
    parent_key = 'unified_job_template'


class InventorySourceActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.InventorySource
    relationship = 'activitystream_set'
    search_fields = ('changes',)


class InventorySourceNotificationTemplatesAnyList(SubListCreateAttachDetachAPIView):
    model = models.NotificationTemplate
    serializer_class = serializers.NotificationTemplateSerializer
    parent_model = models.InventorySource

    def post(self, request, *args, **kwargs):
        parent = self.get_parent_object()
        if parent.source not in compute_cloud_inventory_sources():
            return Response(
                dict(msg=_("Notification Templates can only be assigned when source is one of {}.").format(compute_cloud_inventory_sources(), parent.source)),
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super(InventorySourceNotificationTemplatesAnyList, self).post(request, *args, **kwargs)


class InventorySourceNotificationTemplatesStartedList(InventorySourceNotificationTemplatesAnyList):
    relationship = 'notification_templates_started'


class InventorySourceNotificationTemplatesErrorList(InventorySourceNotificationTemplatesAnyList):
    relationship = 'notification_templates_error'


class InventorySourceNotificationTemplatesSuccessList(InventorySourceNotificationTemplatesAnyList):
    relationship = 'notification_templates_success'


class InventorySourceHostsList(HostRelatedSearchMixin, SubListDestroyAPIView):
    model = models.Host
    serializer_class = serializers.HostSerializer
    parent_model = models.InventorySource
    relationship = 'hosts'
    check_sub_obj_permission = False

    def perform_list_destroy(self, instance_list):
        inv_source = self.get_parent_object()
        with ignore_inventory_computed_fields():
            if not settings.ACTIVITY_STREAM_ENABLED_FOR_INVENTORY_SYNC:
                from awx.main.signals import disable_activity_stream

                with disable_activity_stream():
                    # job host summary deletion necessary to avoid deadlock
                    models.JobHostSummary.objects.filter(host__inventory_sources=inv_source).update(host=None)
                    models.Host.objects.filter(inventory_sources=inv_source).delete()
                    r = super(InventorySourceHostsList, self).perform_list_destroy([])
            else:
                # Advance delete of group-host memberships to prevent deadlock
                # Activity stream doesn't record disassociation here anyway
                # no signals-related reason to not bulk-delete
                models.Host.groups.through.objects.filter(host__inventory_sources=inv_source).delete()
                r = super(InventorySourceHostsList, self).perform_list_destroy(instance_list)
        update_inventory_computed_fields.delay(inv_source.inventory_id)
        return r


class InventorySourceGroupsList(SubListDestroyAPIView):
    model = models.Group
    serializer_class = serializers.GroupSerializer
    parent_model = models.InventorySource
    relationship = 'groups'
    check_sub_obj_permission = False

    def perform_list_destroy(self, instance_list):
        inv_source = self.get_parent_object()
        with ignore_inventory_computed_fields():
            if not settings.ACTIVITY_STREAM_ENABLED_FOR_INVENTORY_SYNC:
                from awx.main.signals import disable_activity_stream

                with disable_activity_stream():
                    models.Group.objects.filter(inventory_sources=inv_source).delete()
                    r = super(InventorySourceGroupsList, self).perform_list_destroy([])
            else:
                # Advance delete of group-host memberships to prevent deadlock
                # Same arguments for bulk delete as with host list
                models.Group.hosts.through.objects.filter(group__inventory_sources=inv_source).delete()
                r = super(InventorySourceGroupsList, self).perform_list_destroy(instance_list)
        update_inventory_computed_fields.delay(inv_source.inventory_id)
        return r


class InventorySourceUpdatesList(SubListAPIView):
    model = models.InventoryUpdate
    serializer_class = serializers.InventoryUpdateListSerializer
    parent_model = models.InventorySource
    relationship = 'inventory_updates'


class InventorySourceCredentialsList(SubListAttachDetachAPIView):
    parent_model = models.InventorySource
    model = models.Credential
    serializer_class = serializers.CredentialSerializer
    relationship = 'credentials'

    def is_valid_relation(self, parent, sub, created=False):
        # Inventory source credentials are exclusive with all other credentials
        # subject to change for https://github.com/ansible/awx/issues/277
        # or https://github.com/ansible/awx/issues/223
        if parent.credentials.exists():
            return {'msg': _("Source already has credential assigned.")}
        error = models.InventorySource.cloud_credential_validation(parent.source, sub)
        if error:
            return {'msg': error}
        return None


class InventorySourceUpdateView(RetrieveAPIView):
    model = models.InventorySource
    obj_permission_type = 'start'
    serializer_class = serializers.InventorySourceUpdateSerializer

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        serializer = self.get_serializer(instance=obj, data=request.data)
        serializer.is_valid(raise_exception=True)
        if obj.can_update:
            update = obj.update()
            if not update:
                return Response({}, status=status.HTTP_400_BAD_REQUEST)
            else:
                headers = {'Location': update.get_absolute_url(request=request)}
                data = OrderedDict()
                data['inventory_update'] = update.id
                data.update(serializers.InventoryUpdateDetailSerializer(update, context=self.get_serializer_context()).to_representation(update))
                return Response(data, status=status.HTTP_202_ACCEPTED, headers=headers)
        else:
            return self.http_method_not_allowed(request, *args, **kwargs)


class InventoryUpdateList(ListAPIView):
    model = models.InventoryUpdate
    serializer_class = serializers.InventoryUpdateListSerializer


class InventoryUpdateDetail(UnifiedJobDeletionMixin, RetrieveDestroyAPIView):
    model = models.InventoryUpdate
    serializer_class = serializers.InventoryUpdateDetailSerializer


class InventoryUpdateCredentialsList(SubListAPIView):
    parent_model = models.InventoryUpdate
    model = models.Credential
    serializer_class = serializers.CredentialSerializer
    relationship = 'credentials'


class InventoryUpdateCancel(GenericCancelView):
    model = models.InventoryUpdate
    serializer_class = serializers.InventoryUpdateCancelSerializer


class InventoryUpdateNotificationsList(SubListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    parent_model = models.InventoryUpdate
    relationship = 'notifications'
    search_fields = ('subject', 'notification_type', 'body')


class JobTemplateList(ListCreateAPIView):
    model = models.JobTemplate
    serializer_class = serializers.JobTemplateSerializer
    always_allow_superuser = False

    def check_permissions(self, request):
        if request.method == 'POST':
            if request.user.is_anonymous:
                self.permission_denied(request)
            else:
                can_access, messages = request.user.can_access_with_errors(self.model, 'add', request.data)
                if not can_access:
                    self.permission_denied(request, message=messages)

        super(JobTemplateList, self).check_permissions(request)


class JobTemplateDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    model = models.JobTemplate
    serializer_class = serializers.JobTemplateSerializer
    always_allow_superuser = False


class JobTemplateLaunch(RetrieveAPIView):
    model = models.JobTemplate
    obj_permission_type = 'start'
    serializer_class = serializers.JobLaunchSerializer
    always_allow_superuser = False

    def update_raw_data(self, data):
        try:
            obj = self.get_object()
        except PermissionDenied:
            return data
        extra_vars = data.pop('extra_vars', None) or {}
        if obj:
            needed_passwords = obj.passwords_needed_to_start
            if needed_passwords:
                data['credential_passwords'] = {}
                for p in needed_passwords:
                    data['credential_passwords'][p] = u''
            else:
                data.pop('credential_passwords')
            for v in obj.variables_needed_to_start:
                extra_vars.setdefault(v, u'')
            if extra_vars:
                data['extra_vars'] = extra_vars
            modified_ask_mapping = models.JobTemplate.get_ask_mapping()
            modified_ask_mapping.pop('extra_vars')
            for field, ask_field_name in modified_ask_mapping.items():
                if not getattr(obj, ask_field_name):
                    data.pop(field, None)
                elif isinstance(getattr(obj.__class__, field).field, ForeignKey):
                    data[field] = getattrd(obj, "%s.%s" % (field, 'id'), None)
                elif isinstance(getattr(obj.__class__, field).field, ManyToManyField):
                    if field == 'instance_groups':
                        data[field] = []
                        continue
                    data[field] = [item.id for item in getattr(obj, field).all()]
                else:
                    data[field] = getattr(obj, field)
        return data

    def modernize_launch_payload(self, data, obj):
        """
        Steps to do simple translations of request data to support
        old field structure to launch endpoint
        TODO: delete this method with future API version changes
        """
        modern_data = data.copy()

        if 'inventory' not in modern_data and 'inventory_id' in modern_data:
            modern_data['inventory'] = modern_data['inventory_id']

        # credential passwords were historically provided as top-level attributes
        if 'credential_passwords' not in modern_data:
            modern_data['credential_passwords'] = data.copy()

        return modern_data

    def post(self, request, *args, **kwargs):
        obj = self.get_object()

        try:
            modern_data = self.modernize_launch_payload(data=request.data, obj=obj)
        except ParseError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.serializer_class(data=modern_data, context={'template': obj})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        if not request.user.can_access(models.JobLaunchConfig, 'add', serializer.validated_data, template=obj):
            raise PermissionDenied()

        passwords = serializer.validated_data.pop('credential_passwords', {})
        new_job = obj.create_unified_job(**serializer.validated_data)
        result = new_job.signal_start(**passwords)

        if not result:
            data = dict(passwords_needed_to_start=new_job.passwords_needed_to_start)
            new_job.delete()
            return Response(data, status=status.HTTP_400_BAD_REQUEST)
        else:
            data = OrderedDict()
            if isinstance(new_job, models.WorkflowJob):
                data['workflow_job'] = new_job.id
                data['ignored_fields'] = self.sanitize_for_response(serializer._ignored_fields)
                data.update(serializers.WorkflowJobSerializer(new_job, context=self.get_serializer_context()).to_representation(new_job))
            else:
                data['job'] = new_job.id
                data['ignored_fields'] = self.sanitize_for_response(serializer._ignored_fields)
                data.update(serializers.JobSerializer(new_job, context=self.get_serializer_context()).to_representation(new_job))
            headers = {'Location': new_job.get_absolute_url(request)}
            return Response(data, status=status.HTTP_201_CREATED, headers=headers)

    def sanitize_for_response(self, data):
        """
        Model objects cannot be serialized by DRF,
        this replaces objects with their ids for inclusion in response
        """

        def display_value(val):
            if hasattr(val, 'id'):
                return val.id
            else:
                return val

        sanitized_data = {}
        for field_name, value in data.items():
            if isinstance(value, (set, list)):
                sanitized_data[field_name] = []
                for sub_value in value:
                    sanitized_data[field_name].append(display_value(sub_value))
            else:
                sanitized_data[field_name] = display_value(value)

        return sanitized_data


class JobTemplateSchedulesList(SubListCreateAPIView):
    name = _("Job Template Schedules")

    model = models.Schedule
    serializer_class = serializers.ScheduleSerializer
    parent_model = models.JobTemplate
    relationship = 'schedules'
    parent_key = 'unified_job_template'


class JobTemplateSurveySpec(GenericAPIView):
    model = models.JobTemplate
    obj_permission_type = 'admin'
    serializer_class = serializers.EmptySerializer

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        return Response(obj.display_survey_spec())

    def post(self, request, *args, **kwargs):
        obj = self.get_object()

        if not request.user.can_access(self.model, 'change', obj, None):
            raise PermissionDenied()
        response = self._validate_spec_data(request.data, obj.survey_spec)
        if response:
            return response
        obj.survey_spec = request.data
        obj.save(update_fields=['survey_spec'])
        return Response()

    @staticmethod
    def _validate_spec_data(new_spec, old_spec):
        schema_errors = {}
        for field, expect_type, type_label in [('name', str, 'string'), ('description', str, 'string'), ('spec', list, 'list of items')]:
            if field not in new_spec:
                schema_errors['error'] = _("Field '{}' is missing from survey spec.").format(field)
            elif not isinstance(new_spec[field], expect_type):
                schema_errors['error'] = _("Expected {} for field '{}', received {} type.").format(type_label, field, type(new_spec[field]).__name__)

        if isinstance(new_spec.get('spec', None), list) and len(new_spec["spec"]) < 1:
            schema_errors['error'] = _("'spec' doesn't contain any items.")

        if schema_errors:
            return Response(schema_errors, status=status.HTTP_400_BAD_REQUEST)

        variable_set = set()
        old_spec_dict = models.JobTemplate.pivot_spec(old_spec)
        for idx, survey_item in enumerate(new_spec["spec"]):
            context = dict(idx=str(idx), survey_item=survey_item)
            # General element validation
            if not isinstance(survey_item, dict):
                return Response(dict(error=_("Survey question %s is not a json object.") % str(idx)), status=status.HTTP_400_BAD_REQUEST)
            for field_name in ['type', 'question_name', 'variable', 'required']:
                if field_name not in survey_item:
                    return Response(
                        dict(error=_("'{field_name}' missing from survey question {idx}").format(field_name=field_name, **context)),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                val = survey_item[field_name]
                allow_types = str
                type_label = 'string'
                if field_name == 'required':
                    allow_types = bool
                    type_label = 'boolean'
                if not isinstance(val, allow_types):
                    return Response(
                        dict(
                            error=_("'{field_name}' in survey question {idx} expected to be {type_label}.").format(
                                field_name=field_name, type_label=type_label, **context
                            )
                        ),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            if survey_item['variable'] in variable_set:
                return Response(
                    dict(error=_("'variable' '%(item)s' duplicated in survey question %(survey)s.") % {'item': survey_item['variable'], 'survey': str(idx)}),
                    status=status.HTTP_400_BAD_REQUEST,
                )
            else:
                variable_set.add(survey_item['variable'])

            # Type-specific validation
            # validate question type <-> default type
            qtype = survey_item["type"]
            if qtype not in SURVEY_TYPE_MAPPING:
                return Response(
                    dict(
                        error=_("'{survey_item[type]}' in survey question {idx} is not one of '{allowed_types}' allowed question types.").format(
                            allowed_types=', '.join(SURVEY_TYPE_MAPPING.keys()), **context
                        )
                    ),
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if 'default' in survey_item and survey_item['default'] != '':
                if not isinstance(survey_item['default'], SURVEY_TYPE_MAPPING[qtype]):
                    type_label = 'string'
                    if qtype in ['integer', 'float']:
                        type_label = qtype
                    return Response(
                        dict(
                            error=_("Default value {survey_item[default]} in survey question {idx} expected to be {type_label}.").format(
                                type_label=type_label, **context
                            )
                        ),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            # additional type-specific properties, the UI provides these even
            # if not applicable to the question, TODO: request that they not do this
            for key in ['min', 'max']:
                if key in survey_item:
                    if survey_item[key] is not None and (not isinstance(survey_item[key], int)):
                        return Response(
                            dict(error=_("The {min_or_max} limit in survey question {idx} expected to be integer.").format(min_or_max=key, **context)),
                            status=status.HTTP_400_BAD_REQUEST,
                        )
            # if it's a multiselect or multiple choice, it must have coices listed
            # choices and defaults must come in as strings separated by /n characters.
            if qtype == 'multiselect' or qtype == 'multiplechoice':
                if 'choices' in survey_item:
                    if isinstance(survey_item['choices'], str):
                        survey_item['choices'] = '\n'.join(choice for choice in survey_item['choices'].splitlines() if choice.strip() != '')
                else:
                    return Response(
                        dict(error=_("Survey question {idx} of type {survey_item[type]} must specify choices.".format(**context))),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                # If there is a default string split it out removing extra /n characters.
                # Note: There can still be extra newline characters added in the API, these are sanitized out using .strip()
                if 'default' in survey_item:
                    if isinstance(survey_item['default'], str):
                        survey_item['default'] = '\n'.join(choice for choice in survey_item['default'].splitlines() if choice.strip() != '')
                        list_of_defaults = survey_item['default'].splitlines()
                    else:
                        list_of_defaults = survey_item['default']
                    if qtype == 'multiplechoice':
                        # Multiplechoice types should only have 1 default.
                        if len(list_of_defaults) > 1:
                            return Response(
                                dict(error=_("Multiple Choice (Single Select) can only have one default value.".format(**context))),
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                    if any(item not in survey_item['choices'] for item in list_of_defaults):
                        return Response(
                            dict(error=_("Default choice must be answered from the choices listed.".format(**context))), status=status.HTTP_400_BAD_REQUEST
                        )

            # Process encryption substitution
            if "default" in survey_item and isinstance(survey_item['default'], str) and survey_item['default'].startswith('$encrypted$'):
                # Submission expects the existence of encrypted DB value to replace given default
                if qtype != "password":
                    return Response(
                        dict(
                            error=_(
                                "$encrypted$ is a reserved keyword for password question defaults, survey question {idx} is type {survey_item[type]}."
                            ).format(**context)
                        ),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                old_element = old_spec_dict.get(survey_item['variable'], {})
                encryptedish_default_exists = False
                if 'default' in old_element:
                    old_default = old_element['default']
                    if isinstance(old_default, str):
                        if old_default.startswith('$encrypted$'):
                            encryptedish_default_exists = True
                        elif old_default == "":  # unencrypted blank string is allowed as DB value as special case
                            encryptedish_default_exists = True
                if not encryptedish_default_exists:
                    return Response(
                        dict(error=_("$encrypted$ is a reserved keyword, may not be used for new default in position {idx}.").format(**context)),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                survey_item['default'] = old_element['default']
            elif qtype == "password" and 'default' in survey_item:
                # Submission provides new encrypted default
                survey_item['default'] = encrypt_value(survey_item['default'])

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        if not request.user.can_access(self.model, 'delete', obj):
            raise PermissionDenied()
        obj.survey_spec = {}
        obj.save()
        return Response()


class WorkflowJobTemplateSurveySpec(JobTemplateSurveySpec):
    model = models.WorkflowJobTemplate


class JobTemplateActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.JobTemplate
    relationship = 'activitystream_set'
    search_fields = ('changes',)


class JobTemplateNotificationTemplatesAnyList(SubListCreateAttachDetachAPIView):
    model = models.NotificationTemplate
    serializer_class = serializers.NotificationTemplateSerializer
    parent_model = models.JobTemplate


class JobTemplateNotificationTemplatesStartedList(JobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_started'


class JobTemplateNotificationTemplatesErrorList(JobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_error'


class JobTemplateNotificationTemplatesSuccessList(JobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_success'


class JobTemplateCredentialsList(SubListCreateAttachDetachAPIView):
    model = models.Credential
    serializer_class = serializers.CredentialSerializer
    parent_model = models.JobTemplate
    relationship = 'credentials'
    filter_read_permission = False

    def is_valid_relation(self, parent, sub, created=False):
        if sub.unique_hash() in [cred.unique_hash() for cred in parent.credentials.all()]:
            return {"error": _("Cannot assign multiple {credential_type} credentials.").format(credential_type=sub.unique_hash(display=True))}
        kind = sub.credential_type.kind
        if kind not in ('ssh', 'vault', 'cloud', 'net', 'kubernetes'):
            return {'error': _('Cannot assign a Credential of kind `{}`.').format(kind)}

        return super(JobTemplateCredentialsList, self).is_valid_relation(parent, sub, created)


class JobTemplateLabelList(LabelSubListCreateAttachDetachView):
    parent_model = models.JobTemplate


class JobTemplateCallback(GenericAPIView):
    model = models.JobTemplate
    permission_classes = (JobTemplateCallbackPermission,)
    serializer_class = serializers.EmptySerializer
    parser_classes = api_settings.DEFAULT_PARSER_CLASSES + [FormParser]

    @csrf_exempt
    @transaction.non_atomic_requests
    def dispatch(self, *args, **kwargs):
        return super(JobTemplateCallback, self).dispatch(*args, **kwargs)

    def find_matching_hosts(self):
        """
        Find the host(s) in the job template's inventory that match the remote
        host for the current request.
        """
        # Find the list of remote host names/IPs to check.
        remote_hosts = set(get_remote_hosts(self.request))
        # Add the reverse lookup of IP addresses.
        for rh in list(remote_hosts):
            try:
                result = socket.gethostbyaddr(rh)
            except socket.herror:
                continue
            except socket.gaierror:
                continue
            remote_hosts.add(result[0])
            remote_hosts.update(result[1])
        # Filter out any .arpa results.
        for rh in list(remote_hosts):
            if rh.endswith('.arpa'):
                remote_hosts.remove(rh)
        if not remote_hosts:
            return set()
        # Find the host objects to search for a match.
        obj = self.get_object()
        hosts = obj.inventory.hosts.all()
        # Populate host_mappings
        host_mappings = {}
        for host in hosts:
            host_name = host.get_effective_host_name()
            host_mappings.setdefault(host_name, [])
            host_mappings[host_name].append(host)
        # Try finding direct match
        matches = set()
        for host_name in remote_hosts:
            if host_name in host_mappings:
                matches.update(host_mappings[host_name])
        if len(matches) == 1:
            return matches
        # Try to resolve forward addresses for each host to find matches.
        for host_name in host_mappings:
            try:
                result = socket.getaddrinfo(host_name, None)
                possible_ips = set(x[4][0] for x in result)
                possible_ips.discard(host_name)
                if possible_ips and possible_ips & remote_hosts:
                    matches.update(host_mappings[host_name])
            except socket.gaierror:
                pass
            except UnicodeError:
                pass
        return matches

    def get(self, request, *args, **kwargs):
        job_template = self.get_object()
        matching_hosts = self.find_matching_hosts()
        data = dict(host_config_key=job_template.host_config_key, matching_hosts=[x.name for x in matching_hosts])
        if settings.DEBUG:
            d = dict([(k, v) for k, v in request.META.items() if k.startswith('HTTP_') or k.startswith('REMOTE_')])
            data['request_meta'] = d
        return Response(data)

    def post(self, request, *args, **kwargs):
        extra_vars = None
        # Be careful here: content_type can look like '<content_type>; charset=blar'
        if request.content_type.startswith("application/json"):
            extra_vars = request.data.get("extra_vars", None)
        # Permission class should have already validated host_config_key.
        job_template = self.get_object()
        # Attempt to find matching hosts based on remote address.
        if job_template.inventory:
            matching_hosts = self.find_matching_hosts()
        else:
            return Response({"msg": _("Cannot start automatically, an inventory is required.")}, status=status.HTTP_400_BAD_REQUEST)
        # If the host is not found, update the inventory before trying to
        # match again.
        inventory_sources_already_updated = []
        if len(matching_hosts) != 1:
            inventory_sources = job_template.inventory.inventory_sources.filter(update_on_launch=True)
            inventory_update_pks = set()
            for inventory_source in inventory_sources:
                if inventory_source.needs_update_on_launch:
                    # FIXME: Doesn't check for any existing updates.
                    inventory_update = inventory_source.create_inventory_update(**{'_eager_fields': {'launch_type': 'callback'}})
                    inventory_update.signal_start()
                    inventory_update_pks.add(inventory_update.pk)
            inventory_update_qs = models.InventoryUpdate.objects.filter(pk__in=inventory_update_pks, status__in=('pending', 'waiting', 'running'))
            # Poll for the inventory updates we've started to complete.
            while inventory_update_qs.count():
                time.sleep(1.0)
                transaction.commit()
            # Ignore failed inventory updates here, only add successful ones
            # to the list to be excluded when running the job.
            for inventory_update in models.InventoryUpdate.objects.filter(pk__in=inventory_update_pks, status='successful'):
                inventory_sources_already_updated.append(inventory_update.inventory_source_id)
            matching_hosts = self.find_matching_hosts()
        # Check matching hosts.
        if not matching_hosts:
            data = dict(msg=_('No matching host could be found!'))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)
        elif len(matching_hosts) > 1:
            data = dict(msg=_('Multiple hosts matched the request!'))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)
        else:
            host = list(matching_hosts)[0]
        if not job_template.can_start_without_user_input(callback_extra_vars=extra_vars):
            data = dict(msg=_('Cannot start automatically, user input required!'))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)
        limit = host.name

        # NOTE: We limit this to one job waiting per host per callblack to keep them from stacking crazily
        if models.Job.objects.filter(status__in=['pending', 'waiting', 'running'], job_template=job_template, limit=limit).count() > 0:
            data = dict(msg=_('Host callback job already pending.'))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)

        # Everything is fine; actually create the job.
        kv = {"limit": limit}
        kv.setdefault('_eager_fields', {})['launch_type'] = 'callback'
        if extra_vars is not None and job_template.ask_variables_on_launch:
            extra_vars_redacted, removed = extract_ansible_vars(extra_vars)
            kv['extra_vars'] = extra_vars_redacted
        kv['_prevent_slicing'] = True  # will only run against 1 host, so no point
        with transaction.atomic():
            job = job_template.create_job(**kv)

        # Send a signal to signify that the job should be started.
        result = job.signal_start(inventory_sources_already_updated=inventory_sources_already_updated)
        if not result:
            data = dict(msg=_('Error starting job!'))
            job.delete()
            return Response(data, status=status.HTTP_400_BAD_REQUEST)

        # Return the location of the new job.
        headers = {'Location': job.get_absolute_url(request=request)}
        return Response(status=status.HTTP_201_CREATED, headers=headers)


class JobTemplateJobsList(SubListAPIView):
    model = models.Job
    serializer_class = serializers.JobListSerializer
    parent_model = models.JobTemplate
    relationship = 'jobs'
    parent_key = 'job_template'


class JobTemplateSliceWorkflowJobsList(SubListCreateAPIView):
    model = models.WorkflowJob
    serializer_class = serializers.WorkflowJobListSerializer
    parent_model = models.JobTemplate
    relationship = 'slice_workflow_jobs'
    parent_key = 'job_template'


class JobTemplateInstanceGroupsList(SubListAttachDetachAPIView):
    model = models.InstanceGroup
    serializer_class = serializers.InstanceGroupSerializer
    parent_model = models.JobTemplate
    relationship = 'instance_groups'
    filter_read_permission = False


class JobTemplateAccessList(ResourceAccessList):
    model = models.User  # needs to be User for AccessLists's
    parent_model = models.JobTemplate


class JobTemplateObjectRolesList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.JobTemplate
    search_fields = ('role_field', 'content_type__model')
    deprecated = True

    def get_queryset(self):
        po = self.get_parent_object()
        content_type = ContentType.objects.get_for_model(self.parent_model)
        return models.Role.objects.filter(content_type=content_type, object_id=po.pk)


class JobTemplateCopy(CopyAPIView):
    model = models.JobTemplate
    copy_return_serializer_class = serializers.JobTemplateSerializer


class WorkflowJobNodeList(ListAPIView):
    model = models.WorkflowJobNode
    serializer_class = serializers.WorkflowJobNodeListSerializer
    search_fields = ('unified_job_template__name', 'unified_job_template__description')


class WorkflowJobNodeDetail(RetrieveAPIView):
    model = models.WorkflowJobNode
    serializer_class = serializers.WorkflowJobNodeDetailSerializer


class WorkflowJobNodeCredentialsList(SubListAPIView):
    model = models.Credential
    serializer_class = serializers.CredentialSerializer
    parent_model = models.WorkflowJobNode
    relationship = 'credentials'


class WorkflowJobNodeLabelsList(SubListAPIView):
    model = models.Label
    serializer_class = serializers.LabelSerializer
    parent_model = models.WorkflowJobNode
    relationship = 'labels'


class WorkflowJobNodeInstanceGroupsList(SubListAttachDetachAPIView):
    model = models.InstanceGroup
    serializer_class = serializers.InstanceGroupSerializer
    parent_model = models.WorkflowJobNode
    relationship = 'instance_groups'


class WorkflowJobTemplateNodeList(ListCreateAPIView):
    model = models.WorkflowJobTemplateNode
    serializer_class = serializers.WorkflowJobTemplateNodeSerializer
    search_fields = ('unified_job_template__name', 'unified_job_template__description')


class WorkflowJobTemplateNodeDetail(RetrieveUpdateDestroyAPIView):
    model = models.WorkflowJobTemplateNode
    serializer_class = serializers.WorkflowJobTemplateNodeDetailSerializer


class WorkflowJobTemplateNodeCredentialsList(LaunchConfigCredentialsBase):
    parent_model = models.WorkflowJobTemplateNode


class WorkflowJobTemplateNodeLabelsList(LabelSubListCreateAttachDetachView):
    parent_model = models.WorkflowJobTemplateNode


class WorkflowJobTemplateNodeInstanceGroupsList(SubListAttachDetachAPIView):
    model = models.InstanceGroup
    serializer_class = serializers.InstanceGroupSerializer
    parent_model = models.WorkflowJobTemplateNode
    relationship = 'instance_groups'


class WorkflowJobTemplateNodeChildrenBaseList(EnforceParentRelationshipMixin, SubListCreateAttachDetachAPIView):
    model = models.WorkflowJobTemplateNode
    serializer_class = serializers.WorkflowJobTemplateNodeSerializer
    always_allow_superuser = True
    parent_model = models.WorkflowJobTemplateNode
    relationship = ''
    enforce_parent_relationship = 'workflow_job_template'
    search_fields = ('unified_job_template__name', 'unified_job_template__description')
    filter_read_permission = False

    def is_valid_relation(self, parent, sub, created=False):
        if created:
            return None

        if parent.id == sub.id:
            return {"Error": _("Cycle detected.")}

        '''
        Look for parent->child connection in all relationships except the relationship that is
        attempting to be added; because it's ok to re-add the relationship
        '''
        relationships = ['success_nodes', 'failure_nodes', 'always_nodes']
        relationships.remove(self.relationship)
        qs = functools.reduce(lambda x, y: (x | y), (Q(**{'{}__in'.format(r): [sub.id]}) for r in relationships))

        if models.WorkflowJobTemplateNode.objects.filter(Q(pk=parent.id) & qs).exists():
            return {"Error": _("Relationship not allowed.")}

        parent_node_type_relationship = getattr(parent, self.relationship)
        parent_node_type_relationship.add(sub)

        graph = WorkflowDAG(parent.workflow_job_template)
        if graph.has_cycle():
            parent_node_type_relationship.remove(sub)
            return {"Error": _("Cycle detected.")}
        parent_node_type_relationship.remove(sub)
        return None


class WorkflowJobTemplateNodeCreateApproval(RetrieveAPIView):
    model = models.WorkflowJobTemplateNode
    serializer_class = serializers.WorkflowJobTemplateNodeCreateApprovalSerializer
    permission_classes = []

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        serializer = self.get_serializer(instance=obj, data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        approval_template = obj.create_approval_template(**serializer.validated_data)
        data = serializers.WorkflowApprovalTemplateSerializer(approval_template, context=self.get_serializer_context()).data
        return Response(data, status=status.HTTP_201_CREATED)

    def check_permissions(self, request):
        if not request.user.is_authenticated:
            raise PermissionDenied()
        obj = self.get_object().workflow_job_template
        if request.method == 'POST':
            if not request.user.can_access(models.WorkflowJobTemplate, 'change', obj, request.data):
                self.permission_denied(request)
        else:
            if not request.user.can_access(models.WorkflowJobTemplate, 'read', obj):
                self.permission_denied(request)


class WorkflowJobTemplateNodeSuccessNodesList(WorkflowJobTemplateNodeChildrenBaseList):
    relationship = 'success_nodes'


class WorkflowJobTemplateNodeFailureNodesList(WorkflowJobTemplateNodeChildrenBaseList):
    relationship = 'failure_nodes'


class WorkflowJobTemplateNodeAlwaysNodesList(WorkflowJobTemplateNodeChildrenBaseList):
    relationship = 'always_nodes'


class WorkflowJobNodeChildrenBaseList(SubListAPIView):
    model = models.WorkflowJobNode
    serializer_class = serializers.WorkflowJobNodeListSerializer
    parent_model = models.WorkflowJobNode
    relationship = ''
    search_fields = ('unified_job_template__name', 'unified_job_template__description')
    filter_read_permission = False


class WorkflowJobNodeSuccessNodesList(WorkflowJobNodeChildrenBaseList):
    relationship = 'success_nodes'


class WorkflowJobNodeFailureNodesList(WorkflowJobNodeChildrenBaseList):
    relationship = 'failure_nodes'


class WorkflowJobNodeAlwaysNodesList(WorkflowJobNodeChildrenBaseList):
    relationship = 'always_nodes'


class WorkflowJobTemplateList(ListCreateAPIView):
    model = models.WorkflowJobTemplate
    serializer_class = serializers.WorkflowJobTemplateSerializer
    always_allow_superuser = False

    def check_permissions(self, request):
        if request.method == 'POST':
            if request.user.is_anonymous:
                self.permission_denied(request)
            else:
                can_access, messages = request.user.can_access_with_errors(self.model, 'add', request.data)
                if not can_access:
                    self.permission_denied(request, message=messages)

        super(WorkflowJobTemplateList, self).check_permissions(request)


class WorkflowJobTemplateDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    model = models.WorkflowJobTemplate
    serializer_class = serializers.WorkflowJobTemplateSerializer
    always_allow_superuser = False


class WorkflowJobTemplateCopy(CopyAPIView):
    model = models.WorkflowJobTemplate
    copy_return_serializer_class = serializers.WorkflowJobTemplateSerializer

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        if not request.user.can_access(obj.__class__, 'read', obj):
            raise PermissionDenied()
        can_copy, messages = request.user.can_access_with_errors(self.model, 'copy', obj)
        data = OrderedDict(
            [
                ('can_copy', can_copy),
                ('can_copy_without_user_input', can_copy),
                ('templates_unable_to_copy', [] if can_copy else ['all']),
                ('credentials_unable_to_copy', [] if can_copy else ['all']),
                ('inventories_unable_to_copy', [] if can_copy else ['all']),
            ]
        )
        if messages and can_copy:
            data['can_copy_without_user_input'] = False
            data.update(messages)
        return Response(data)

    def _build_create_dict(self, obj):
        """Special processing of fields managed by char_prompts"""
        r = super(WorkflowJobTemplateCopy, self)._build_create_dict(obj)
        field_names = set(f.name for f in obj._meta.get_fields())
        for field_name, ask_field_name in obj.get_ask_mapping().items():
            if field_name in r and field_name not in field_names:
                r.setdefault('char_prompts', {})
                r['char_prompts'][field_name] = r.pop(field_name)
        return r

    @staticmethod
    def deep_copy_permission_check_func(user, new_objs):
        for obj in new_objs:
            for field_name in obj._get_workflow_job_field_names():
                item = getattr(obj, field_name, None)
                if item is None:
                    continue
                elif field_name in ['inventory']:
                    if not user.can_access(item.__class__, 'use', item):
                        setattr(obj, field_name, None)
                elif field_name in ['unified_job_template']:
                    if not user.can_access(item.__class__, 'start', item, validate_license=False):
                        setattr(obj, field_name, None)
                elif field_name in ['credentials']:
                    for cred in item.all():
                        if not user.can_access(cred.__class__, 'use', cred):
                            logger.debug('Deep copy: removing {} from relationship due to permissions'.format(cred))
                            item.remove(cred.pk)
            obj.save()


class WorkflowJobTemplateLabelList(JobTemplateLabelList):
    parent_model = models.WorkflowJobTemplate


class WorkflowJobTemplateLaunch(RetrieveAPIView):
    model = models.WorkflowJobTemplate
    obj_permission_type = 'start'
    serializer_class = serializers.WorkflowJobLaunchSerializer
    always_allow_superuser = False

    def update_raw_data(self, data):
        try:
            obj = self.get_object()
        except PermissionDenied:
            return data
        extra_vars = data.pop('extra_vars', None) or {}
        if obj:
            for v in obj.variables_needed_to_start:
                extra_vars.setdefault(v, u'')
            if extra_vars:
                data['extra_vars'] = extra_vars
            modified_ask_mapping = models.WorkflowJobTemplate.get_ask_mapping()
            modified_ask_mapping.pop('extra_vars')

            for field, ask_field_name in modified_ask_mapping.items():
                if not getattr(obj, ask_field_name):
                    data.pop(field, None)
                elif isinstance(getattr(obj.__class__, field).field, ForeignKey):
                    data[field] = getattrd(obj, "%s.%s" % (field, 'id'), None)
                elif isinstance(getattr(obj.__class__, field).field, ManyToManyField):
                    data[field] = [item.id for item in getattr(obj, field).all()]
                else:
                    data[field] = getattr(obj, field)

        return data

    def post(self, request, *args, **kwargs):
        obj = self.get_object()

        if 'inventory_id' in request.data:
            request.data['inventory'] = request.data['inventory_id']

        serializer = self.serializer_class(instance=obj, data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        if not request.user.can_access(models.JobLaunchConfig, 'add', serializer.validated_data, template=obj):
            raise PermissionDenied()

        new_job = obj.create_unified_job(**serializer.validated_data)
        new_job.signal_start()

        data = OrderedDict()
        data['workflow_job'] = new_job.id
        data['ignored_fields'] = serializer._ignored_fields
        data.update(serializers.WorkflowJobSerializer(new_job, context=self.get_serializer_context()).to_representation(new_job))
        headers = {'Location': new_job.get_absolute_url(request)}
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class WorkflowJobRelaunch(GenericAPIView):
    model = models.WorkflowJob
    obj_permission_type = 'start'
    serializer_class = serializers.EmptySerializer

    def check_object_permissions(self, request, obj):
        if request.method == 'POST' and obj:
            relaunch_perm, messages = request.user.can_access_with_errors(self.model, 'start', obj)
            if not relaunch_perm and 'workflow_job_template' in messages:
                self.permission_denied(request, message=messages['workflow_job_template'])
        return super(WorkflowJobRelaunch, self).check_object_permissions(request, obj)

    def get(self, request, *args, **kwargs):
        return Response({})

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.is_sliced_job:
            jt = obj.job_template
            if not jt:
                raise ParseError(_('Cannot relaunch slice workflow job orphaned from job template.'))
            elif not obj.inventory or min(obj.inventory.hosts.count(), jt.job_slice_count) != obj.workflow_nodes.count():
                raise ParseError(_('Cannot relaunch sliced workflow job after slice count has changed.'))
        new_workflow_job = obj.create_relaunch_workflow_job()
        new_workflow_job.signal_start()

        data = serializers.WorkflowJobSerializer(new_workflow_job, context=self.get_serializer_context()).data
        headers = {'Location': new_workflow_job.get_absolute_url(request=request)}
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class WorkflowJobTemplateWorkflowNodesList(SubListCreateAPIView):
    model = models.WorkflowJobTemplateNode
    serializer_class = serializers.WorkflowJobTemplateNodeSerializer
    parent_model = models.WorkflowJobTemplate
    relationship = 'workflow_job_template_nodes'
    parent_key = 'workflow_job_template'
    search_fields = ('unified_job_template__name', 'unified_job_template__description')
    ordering = ('id',)  # assure ordering by id for consistency
    filter_read_permission = False


class WorkflowJobTemplateJobsList(SubListAPIView):
    model = models.WorkflowJob
    serializer_class = serializers.WorkflowJobListSerializer
    parent_model = models.WorkflowJobTemplate
    relationship = 'workflow_jobs'
    parent_key = 'workflow_job_template'


class WorkflowJobTemplateSchedulesList(SubListCreateAPIView):
    name = _("Workflow Job Template Schedules")

    model = models.Schedule
    serializer_class = serializers.ScheduleSerializer
    parent_model = models.WorkflowJobTemplate
    relationship = 'schedules'
    parent_key = 'unified_job_template'


class WorkflowJobTemplateNotificationTemplatesAnyList(SubListCreateAttachDetachAPIView):
    model = models.NotificationTemplate
    serializer_class = serializers.NotificationTemplateSerializer
    parent_model = models.WorkflowJobTemplate


class WorkflowJobTemplateNotificationTemplatesStartedList(WorkflowJobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_started'


class WorkflowJobTemplateNotificationTemplatesErrorList(WorkflowJobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_error'


class WorkflowJobTemplateNotificationTemplatesSuccessList(WorkflowJobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_success'


class WorkflowJobTemplateNotificationTemplatesApprovalList(WorkflowJobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_approvals'


class WorkflowJobTemplateAccessList(ResourceAccessList):
    model = models.User  # needs to be User for AccessLists's
    parent_model = models.WorkflowJobTemplate


class WorkflowJobTemplateObjectRolesList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.WorkflowJobTemplate
    search_fields = ('role_field', 'content_type__model')
    deprecated = True

    def get_queryset(self):
        po = self.get_parent_object()
        content_type = ContentType.objects.get_for_model(self.parent_model)
        return models.Role.objects.filter(content_type=content_type, object_id=po.pk)


class WorkflowJobTemplateActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.WorkflowJobTemplate
    relationship = 'activitystream_set'
    search_fields = ('changes',)

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model)
        return qs.filter(Q(workflow_job_template=parent) | Q(workflow_job_template_node__workflow_job_template=parent)).distinct()


class WorkflowJobList(ListAPIView):
    model = models.WorkflowJob
    serializer_class = serializers.WorkflowJobListSerializer


class WorkflowJobDetail(UnifiedJobDeletionMixin, RetrieveDestroyAPIView):
    model = models.WorkflowJob
    serializer_class = serializers.WorkflowJobSerializer


class WorkflowJobWorkflowNodesList(SubListAPIView):
    model = models.WorkflowJobNode
    serializer_class = serializers.WorkflowJobNodeListSerializer
    always_allow_superuser = True
    parent_model = models.WorkflowJob
    relationship = 'workflow_job_nodes'
    parent_key = 'workflow_job'
    search_fields = ('unified_job_template__name', 'unified_job_template__description')
    ordering = ('id',)  # assure ordering by id for consistency
    filter_read_permission = False


class WorkflowJobCancel(GenericCancelView):
    model = models.WorkflowJob
    serializer_class = serializers.WorkflowJobCancelSerializer

    def post(self, request, *args, **kwargs):
        r = super().post(request, *args, **kwargs)
        ScheduleWorkflowManager().schedule()
        return r


class WorkflowJobNotificationsList(SubListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    parent_model = models.WorkflowJob
    relationship = 'notifications'
    search_fields = ('subject', 'notification_type', 'body')

    def get_sublist_queryset(self, parent):
        return self.model.objects.filter(
            Q(unifiedjob_notifications=parent)
            | Q(unifiedjob_notifications__unified_job_node__workflow_job=parent, unifiedjob_notifications__workflowapproval__isnull=False)
        ).distinct()


class WorkflowJobActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.WorkflowJob
    relationship = 'activitystream_set'
    search_fields = ('changes',)


class SystemJobTemplateList(ListAPIView):
    model = models.SystemJobTemplate
    serializer_class = serializers.SystemJobTemplateSerializer

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser and not request.user.is_system_auditor:
            raise PermissionDenied(_("Superuser privileges needed."))
        return super(SystemJobTemplateList, self).get(request, *args, **kwargs)


class SystemJobTemplateDetail(RetrieveAPIView):
    model = models.SystemJobTemplate
    serializer_class = serializers.SystemJobTemplateSerializer


class SystemJobTemplateLaunch(GenericAPIView):
    model = models.SystemJobTemplate
    obj_permission_type = 'start'
    serializer_class = serializers.EmptySerializer

    def get(self, request, *args, **kwargs):
        return Response({})

    def post(self, request, *args, **kwargs):
        obj = self.get_object()

        new_job = obj.create_unified_job(extra_vars=request.data.get('extra_vars', {}))
        new_job.signal_start()
        data = OrderedDict()
        data['system_job'] = new_job.id
        data.update(serializers.SystemJobSerializer(new_job, context=self.get_serializer_context()).to_representation(new_job))
        headers = {'Location': new_job.get_absolute_url(request)}
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class SystemJobTemplateSchedulesList(SubListCreateAPIView):
    name = _("System Job Template Schedules")

    model = models.Schedule
    serializer_class = serializers.ScheduleSerializer
    parent_model = models.SystemJobTemplate
    relationship = 'schedules'
    parent_key = 'unified_job_template'


class SystemJobTemplateJobsList(SubListAPIView):
    model = models.SystemJob
    serializer_class = serializers.SystemJobListSerializer
    parent_model = models.SystemJobTemplate
    relationship = 'jobs'
    parent_key = 'system_job_template'


class SystemJobTemplateNotificationTemplatesAnyList(SubListCreateAttachDetachAPIView):
    model = models.NotificationTemplate
    serializer_class = serializers.NotificationTemplateSerializer
    parent_model = models.SystemJobTemplate


class SystemJobTemplateNotificationTemplatesStartedList(SystemJobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_started'


class SystemJobTemplateNotificationTemplatesErrorList(SystemJobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_error'


class SystemJobTemplateNotificationTemplatesSuccessList(SystemJobTemplateNotificationTemplatesAnyList):
    relationship = 'notification_templates_success'


class JobList(ListAPIView):
    model = models.Job
    serializer_class = serializers.JobListSerializer


class JobDetail(UnifiedJobDeletionMixin, RetrieveDestroyAPIView):
    model = models.Job
    serializer_class = serializers.JobDetailSerializer

    def update(self, request, *args, **kwargs):
        obj = self.get_object()
        # Only allow changes (PUT/PATCH) when job status is "new".
        if obj.status != 'new':
            return self.http_method_not_allowed(request, *args, **kwargs)
        return super(JobDetail, self).update(request, *args, **kwargs)


class JobCredentialsList(SubListAPIView):
    model = models.Credential
    serializer_class = serializers.CredentialSerializer
    parent_model = models.Job
    relationship = 'credentials'


class JobLabelList(SubListAPIView):
    model = models.Label
    serializer_class = serializers.LabelSerializer
    parent_model = models.Job
    relationship = 'labels'


class WorkflowJobLabelList(JobLabelList):
    parent_model = models.WorkflowJob


class JobActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.Job
    relationship = 'activitystream_set'
    search_fields = ('changes',)


class JobCancel(GenericCancelView):
    model = models.Job
    serializer_class = serializers.JobCancelSerializer


class JobRelaunch(RetrieveAPIView):
    model = models.Job
    obj_permission_type = 'start'
    serializer_class = serializers.JobRelaunchSerializer

    def update_raw_data(self, data):
        data = super(JobRelaunch, self).update_raw_data(data)
        try:
            obj = self.get_object()
        except PermissionDenied:
            return data
        if obj:
            needed_passwords = obj.passwords_needed_to_start
            if needed_passwords:
                data['credential_passwords'] = {}
                for p in needed_passwords:
                    data['credential_passwords'][p] = u''
            else:
                data.pop('credential_passwords', None)
        return data

    @transaction.non_atomic_requests
    def dispatch(self, *args, **kwargs):
        return super(JobRelaunch, self).dispatch(*args, **kwargs)

    def check_object_permissions(self, request, obj):
        if request.method == 'POST' and obj:
            relaunch_perm, messages = request.user.can_access_with_errors(self.model, 'start', obj)
            if not relaunch_perm and 'detail' in messages:
                self.permission_denied(request, message=messages['detail'])
        return super(JobRelaunch, self).check_object_permissions(request, obj)

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        context = self.get_serializer_context()

        modified_data = request.data.copy()
        modified_data.setdefault('credential_passwords', {})
        for password in obj.passwords_needed_to_start:
            if password in modified_data:
                modified_data['credential_passwords'][password] = modified_data[password]

        # Note: is_valid() may modify request.data
        # It will remove any key/value pair who's key is not in the 'passwords_needed_to_start' list
        serializer = self.serializer_class(data=modified_data, context=context, instance=obj)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        copy_kwargs = {}
        retry_hosts = serializer.validated_data.get('hosts', None)
        job_type = serializer.validated_data.get('job_type', None)
        if retry_hosts and retry_hosts != 'all':
            if obj.status in ACTIVE_STATES:
                return Response(
                    {'hosts': _('Wait until job finishes before retrying on {status_value} hosts.').format(status_value=retry_hosts)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            host_qs = obj.retry_qs(retry_hosts)
            if not obj.get_event_queryset().filter(event='playbook_on_stats').exists():
                return Response(
                    {'hosts': _('Cannot retry on {status_value} hosts, playbook stats not available.').format(status_value=retry_hosts)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            retry_host_list = host_qs.values_list('name', flat=True)
            if len(retry_host_list) == 0:
                return Response(
                    {'hosts': _('Cannot relaunch because previous job had 0 {status_value} hosts.').format(status_value=retry_hosts)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            copy_kwargs['limit'] = ','.join(retry_host_list)

        if job_type:
            copy_kwargs['job_type'] = job_type
        new_job = obj.copy_unified_job(**copy_kwargs)
        result = new_job.signal_start(**serializer.validated_data['credential_passwords'])
        if not result:
            data = dict(msg=_('Error starting job!'))
            new_job.delete()
            return Response(data, status=status.HTTP_400_BAD_REQUEST)
        else:
            data = serializers.JobSerializer(new_job, context=context).data
            # Add job key to match what old relaunch returned.
            data['job'] = new_job.id
            headers = {'Location': new_job.get_absolute_url(request=request)}
            return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class JobCreateSchedule(RetrieveAPIView):
    model = models.Job
    obj_permission_type = 'start'
    serializer_class = serializers.JobCreateScheduleSerializer

    def post(self, request, *args, **kwargs):
        obj = self.get_object()

        if not obj.can_schedule:
            if getattr(obj, 'passwords_needed_to_start', None):
                return Response({"error": _('Cannot create schedule because job requires credential passwords.')}, status=status.HTTP_400_BAD_REQUEST)
            try:
                obj.launch_config
            except ObjectDoesNotExist:
                return Response({"error": _('Cannot create schedule because job was launched by legacy method.')}, status=status.HTTP_400_BAD_REQUEST)
            return Response({"error": _('Cannot create schedule because a related resource is missing.')}, status=status.HTTP_400_BAD_REQUEST)

        config = obj.launch_config

        # Make up a name for the schedule, guarantee that it is unique
        name = 'Auto-generated schedule from job {}'.format(obj.id)
        existing_names = models.Schedule.objects.filter(name__startswith=name).values_list('name', flat=True)
        if name in existing_names:
            idx = 1
            alt_name = '{} - number {}'.format(name, idx)
            while alt_name in existing_names:
                idx += 1
                alt_name = '{} - number {}'.format(name, idx)
            name = alt_name

        schedule_data = dict(
            name=name,
            unified_job_template=obj.unified_job_template,
            enabled=False,
            rrule='{}Z RRULE:FREQ=MONTHLY;INTERVAL=1'.format(now().strftime('DTSTART:%Y%m%dT%H%M%S')),
            extra_data=config.extra_data,
            survey_passwords=config.survey_passwords,
            inventory=config.inventory,
            execution_environment=config.execution_environment,
            char_prompts=config.char_prompts,
            credentials=set(config.credentials.all()),
            labels=set(config.labels.all()),
            instance_groups=list(config.instance_groups.all()),
        )
        if not request.user.can_access(models.Schedule, 'add', schedule_data):
            raise PermissionDenied()

        related_fields = ('credentials', 'labels', 'instance_groups')
        related = [schedule_data.pop(relationship) for relationship in related_fields]
        schedule = models.Schedule.objects.create(**schedule_data)
        for relationship, items in zip(related_fields, related):
            for item in items:
                getattr(schedule, relationship).add(item)

        data = serializers.ScheduleSerializer(schedule, context=self.get_serializer_context()).data
        data.serializer.instance = None  # hack to avoid permissions.py assuming this is Job model
        headers = {'Location': schedule.get_absolute_url(request=request)}
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class JobNotificationsList(SubListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    parent_model = models.Job
    relationship = 'notifications'
    search_fields = ('subject', 'notification_type', 'body')


class BaseJobHostSummariesList(SubListAPIView):
    model = models.JobHostSummary
    serializer_class = serializers.JobHostSummarySerializer
    parent_model = None  # Subclasses must define this attribute.
    relationship = 'job_host_summaries'
    name = _('Job Host Summaries List')
    search_fields = ('host_name',)
    filter_read_permission = False


class HostJobHostSummariesList(BaseJobHostSummariesList):
    parent_model = models.Host


class GroupJobHostSummariesList(BaseJobHostSummariesList):
    parent_model = models.Group


class JobJobHostSummariesList(BaseJobHostSummariesList):
    parent_model = models.Job


class JobHostSummaryDetail(RetrieveAPIView):
    model = models.JobHostSummary
    serializer_class = serializers.JobHostSummarySerializer


class JobEventDetail(RetrieveAPIView):
    serializer_class = serializers.JobEventSerializer

    @property
    def is_partitioned(self):
        if 'pk' not in self.kwargs:
            return True
        return int(self.kwargs['pk']) > unpartitioned_event_horizon(models.JobEvent)

    @property
    def model(self):
        if self.is_partitioned:
            return models.JobEvent
        return models.UnpartitionedJobEvent

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update(no_truncate=True)
        return context


class JobEventChildrenList(NoTruncateMixin, SubListAPIView):
    serializer_class = serializers.JobEventSerializer
    relationship = 'children'
    name = _('Job Event Children List')
    search_fields = ('stdout',)

    @property
    def is_partitioned(self):
        if 'pk' not in self.kwargs:
            return True
        return int(self.kwargs['pk']) > unpartitioned_event_horizon(models.JobEvent)

    @property
    def model(self):
        if self.is_partitioned:
            return models.JobEvent
        return models.UnpartitionedJobEvent

    @property
    def parent_model(self):
        return self.model

    def get_queryset(self):
        parent_event = self.get_parent_object()
        self.check_parent_access(parent_event)
        return parent_event.job.get_event_queryset().filter(parent_uuid=parent_event.uuid)


class BaseJobEventsList(NoTruncateMixin, SubListAPIView):
    model = models.JobEvent
    serializer_class = serializers.JobEventSerializer
    parent_model = None  # Subclasses must define this attribute.
    relationship = 'job_events'
    name = _('Job Events List')
    search_fields = ('stdout',)

    def finalize_response(self, request, response, *args, **kwargs):
        response['X-UI-Max-Events'] = settings.MAX_UI_JOB_EVENTS
        return super(BaseJobEventsList, self).finalize_response(request, response, *args, **kwargs)


class HostJobEventsList(BaseJobEventsList):
    parent_model = models.Host

    def get_queryset(self):
        parent_obj = self.get_parent_object()
        self.check_parent_access(parent_obj)
        qs = self.request.user.get_queryset(self.model).filter(host=parent_obj)
        return qs


class GroupJobEventsList(BaseJobEventsList):
    parent_model = models.Group


class JobJobEventsList(BaseJobEventsList):
    parent_model = models.Job
    pagination_class = UnifiedJobEventPagination

    def get_queryset(self):
        job = self.get_parent_object()
        self.check_parent_access(job)
        return job.get_event_queryset().prefetch_related('job__job_template', 'host').order_by('start_line')


class JobJobEventsChildrenSummary(APIView):
    renderer_classes = [JSONRenderer]
    meta_events = ('debug', 'verbose', 'warning', 'error', 'system_warning', 'deprecated')

    def get(self, request, **kwargs):
        resp = dict(children_summary={}, meta_event_nested_uuid={}, event_processing_finished=False, is_tree=True)
        job = get_object_or_404(models.Job, pk=kwargs['pk'])
        if not job.event_processing_finished:
            return Response(resp)
        else:
            resp["event_processing_finished"] = True

        events = list(job.get_event_queryset().values('counter', 'uuid', 'parent_uuid', 'event').order_by('counter'))
        if len(events) == 0:
            return Response(resp)

        # key is counter, value is number of total children (including children of children, etc.)
        map_counter_children_tally = {i['counter']: {"rowNumber": 0, "numChildren": 0} for i in events}
        # key is uuid, value is counter
        map_uuid_counter = {i['uuid']: i['counter'] for i in events}
        # key is uuid, value is parent uuid. Used as a quick lookup
        map_uuid_puuid = {i['uuid']: i['parent_uuid'] for i in events}
        # key is counter of meta events (i.e. verbose), value is uuid of the assigned parent
        map_meta_counter_nested_uuid = {}

        # collapsible tree view in the UI only makes sense for tree-like
        # hierarchy. If ansible is ran with a strategy like free or host_pinned, then
        # events can be out of sequential order, and no longer follow a tree structure
        # E1
        #  E2
        # E3
        #  E4  <- parent is E3
        #  E5  <- parent is E1
        # in the above, there is no clear way to collapse E1, because E5 comes after
        # E3, which occurs after E1. Thus the tree view should be disabled.

        # mark the last seen uuid at a given level (0-3)
        # if a parent uuid is not in this list, then we know the events are not tree-like
        # and return a response with is_tree: False
        level_current_uuid = [None, None, None, None]

        prev_non_meta_event = events[0]
        for i, e in enumerate(events):
            if not e['event'] in JobJobEventsChildrenSummary.meta_events:
                prev_non_meta_event = e
            if not e['uuid']:
                continue

            if not e['event'] in JobJobEventsChildrenSummary.meta_events:
                level = models.JobEvent.LEVEL_FOR_EVENT[e['event']]
                level_current_uuid[level] = e['uuid']
                # if setting level 1, for example, set levels 2 and 3 back to None
                for u in range(level + 1, len(level_current_uuid)):
                    level_current_uuid[u] = None

            puuid = e['parent_uuid']
            if puuid and puuid not in level_current_uuid:
                # improper tree detected, so bail out early
                resp['is_tree'] = False
                return Response(resp)

            # if event is verbose (or debug, etc), we need to "assign" it a
            # parent. This code looks at the event level of the previous
            # non-verbose event, and the level of the next (by looking ahead)
            # non-verbose event. The verbose event is assigned the same parent
            # uuid of the higher level event.
            # e.g.
            # E1
            #  E2
            # verbose
            # verbose <- we are on this event currently
            #    E4
            # We'll compare E2 and E4, and the verbose event
            # will be assigned the parent uuid of E4 (higher event level)
            if e['event'] in JobJobEventsChildrenSummary.meta_events:
                event_level_before = models.JobEvent.LEVEL_FOR_EVENT[prev_non_meta_event['event']]
                # find next non meta event
                z = i
                next_non_meta_event = events[-1]
                while z < len(events):
                    if events[z]['event'] not in JobJobEventsChildrenSummary.meta_events:
                        next_non_meta_event = events[z]
                        break
                    z += 1
                event_level_after = models.JobEvent.LEVEL_FOR_EVENT[next_non_meta_event['event']]
                if event_level_after and event_level_after > event_level_before:
                    puuid = next_non_meta_event['parent_uuid']
                else:
                    puuid = prev_non_meta_event['parent_uuid']
                if puuid:
                    map_meta_counter_nested_uuid[e['counter']] = puuid
            map_counter_children_tally[e['counter']]['rowNumber'] = i
            if not puuid:
                continue
            # now traverse up the parent, grandparent, etc. events and tally those
            while puuid:
                map_counter_children_tally[map_uuid_counter[puuid]]['numChildren'] += 1
                puuid = map_uuid_puuid.get(puuid, None)

        # create new dictionary, dropping events with 0 children
        resp["children_summary"] = {k: v for k, v in map_counter_children_tally.items() if v['numChildren'] != 0}
        resp["meta_event_nested_uuid"] = map_meta_counter_nested_uuid
        return Response(resp)


class AdHocCommandList(ListCreateAPIView):
    model = models.AdHocCommand
    serializer_class = serializers.AdHocCommandListSerializer
    always_allow_superuser = False

    @transaction.non_atomic_requests
    def dispatch(self, *args, **kwargs):
        return super(AdHocCommandList, self).dispatch(*args, **kwargs)

    def update_raw_data(self, data):
        # Hide inventory and limit fields from raw data, since they will be set
        # automatically by sub list create view.
        parent_model = getattr(self, 'parent_model', None)
        if parent_model in (models.Host, models.Group):
            data.pop('inventory', None)
            data.pop('limit', None)
        return super(AdHocCommandList, self).update_raw_data(data)

    def create(self, request, *args, **kwargs):
        # Inject inventory ID and limit if parent objects is a host/group.
        if hasattr(self, 'get_parent_object') and not getattr(self, 'parent_key', None):
            data = request.data
            # HACK: Make request data mutable.
            if getattr(data, '_mutable', None) is False:
                data._mutable = True
            parent_obj = self.get_parent_object()
            if isinstance(parent_obj, (models.Host, models.Group)):
                data['inventory'] = parent_obj.inventory_id
                data['limit'] = parent_obj.name

        # Check for passwords needed before creating ad hoc command.
        credential_pk = get_pk_from_dict(request.data, 'credential')
        if credential_pk:
            credential = get_object_or_400(models.Credential, pk=credential_pk)
            needed = credential.passwords_needed
            provided = dict([(field, request.data.get(field, '')) for field in needed])
            if not all(provided.values()):
                data = dict(passwords_needed_to_start=needed)
                return Response(data, status=status.HTTP_400_BAD_REQUEST)

        response = super(AdHocCommandList, self).create(request, *args, **kwargs)
        if response.status_code != status.HTTP_201_CREATED:
            return response

        # Start ad hoc command running when created.
        ad_hoc_command = get_object_or_400(self.model, pk=response.data['id'])
        result = ad_hoc_command.signal_start(**request.data)
        if not result:
            data = dict(passwords_needed_to_start=ad_hoc_command.passwords_needed_to_start)
            ad_hoc_command.delete()
            return Response(data, status=status.HTTP_400_BAD_REQUEST)
        return response


class InventoryAdHocCommandsList(AdHocCommandList, SubListCreateAPIView):
    parent_model = models.Inventory
    relationship = 'ad_hoc_commands'
    parent_key = 'inventory'


class GroupAdHocCommandsList(AdHocCommandList, SubListCreateAPIView):
    parent_model = models.Group
    relationship = 'ad_hoc_commands'


class HostAdHocCommandsList(AdHocCommandList, SubListCreateAPIView):
    parent_model = models.Host
    relationship = 'ad_hoc_commands'


class AdHocCommandDetail(UnifiedJobDeletionMixin, RetrieveDestroyAPIView):
    model = models.AdHocCommand
    serializer_class = serializers.AdHocCommandDetailSerializer


class AdHocCommandCancel(GenericCancelView):
    model = models.AdHocCommand
    serializer_class = serializers.AdHocCommandCancelSerializer


class AdHocCommandRelaunch(GenericAPIView):
    model = models.AdHocCommand
    obj_permission_type = 'start'
    serializer_class = serializers.AdHocCommandRelaunchSerializer

    # FIXME: Figure out why OPTIONS request still shows all fields.

    @transaction.non_atomic_requests
    def dispatch(self, *args, **kwargs):
        return super(AdHocCommandRelaunch, self).dispatch(*args, **kwargs)

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        data = dict(passwords_needed_to_start=obj.passwords_needed_to_start)
        return Response(data)

    def post(self, request, *args, **kwargs):
        obj = self.get_object()

        # Re-validate ad hoc command against serializer to check if module is
        # still allowed.
        data = {}
        for field in ('job_type', 'inventory_id', 'limit', 'credential_id', 'module_name', 'module_args', 'forks', 'verbosity', 'extra_vars', 'become_enabled'):
            if field.endswith('_id'):
                data[field[:-3]] = getattr(obj, field)
            else:
                data[field] = getattr(obj, field)
        serializer = serializers.AdHocCommandSerializer(data=data, context=self.get_serializer_context())
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Check for passwords needed before copying ad hoc command.
        needed = obj.passwords_needed_to_start
        provided = dict([(field, request.data.get(field, '')) for field in needed])
        if not all(provided.values()):
            data = dict(passwords_needed_to_start=needed)
            return Response(data, status=status.HTTP_400_BAD_REQUEST)

        # Copy and start the new ad hoc command.
        new_ad_hoc_command = obj.copy()
        result = new_ad_hoc_command.signal_start(**request.data)
        if not result:
            data = dict(passwords_needed_to_start=new_ad_hoc_command.passwords_needed_to_start)
            new_ad_hoc_command.delete()
            return Response(data, status=status.HTTP_400_BAD_REQUEST)
        else:
            data = serializers.AdHocCommandSerializer(new_ad_hoc_command, context=self.get_serializer_context()).data
            # Add ad_hoc_command key to match what was previously returned.
            data['ad_hoc_command'] = new_ad_hoc_command.id
            headers = {'Location': new_ad_hoc_command.get_absolute_url(request=request)}
            return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class AdHocCommandEventDetail(RetrieveAPIView):
    model = models.AdHocCommandEvent
    serializer_class = serializers.AdHocCommandEventSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update(no_truncate=True)
        return context


class BaseAdHocCommandEventsList(NoTruncateMixin, SubListAPIView):
    model = models.AdHocCommandEvent
    serializer_class = serializers.AdHocCommandEventSerializer
    parent_model = None  # Subclasses must define this attribute.
    relationship = 'ad_hoc_command_events'
    name = _('Ad Hoc Command Events List')
    search_fields = ('stdout',)
    pagination_class = UnifiedJobEventPagination

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        return parent.get_event_queryset()


class HostAdHocCommandEventsList(BaseAdHocCommandEventsList):
    parent_model = models.Host

    def get_queryset(self):
        return super(BaseAdHocCommandEventsList, self).get_queryset()


# class GroupJobEventsList(BaseJobEventsList):
#    parent_model = Group


class AdHocCommandAdHocCommandEventsList(BaseAdHocCommandEventsList):
    parent_model = models.AdHocCommand


class AdHocCommandActivityStreamList(SubListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    parent_model = models.AdHocCommand
    relationship = 'activitystream_set'
    search_fields = ('changes',)


class AdHocCommandNotificationsList(SubListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    parent_model = models.AdHocCommand
    relationship = 'notifications'
    search_fields = ('subject', 'notification_type', 'body')


class SystemJobList(ListAPIView):
    model = models.SystemJob
    serializer_class = serializers.SystemJobListSerializer

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser and not request.user.is_system_auditor:
            raise PermissionDenied(_("Superuser privileges needed."))
        return super(SystemJobList, self).get(request, *args, **kwargs)


class SystemJobDetail(UnifiedJobDeletionMixin, RetrieveDestroyAPIView):
    model = models.SystemJob
    serializer_class = serializers.SystemJobSerializer


class SystemJobCancel(GenericCancelView):
    model = models.SystemJob
    serializer_class = serializers.SystemJobCancelSerializer


class SystemJobNotificationsList(SubListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    parent_model = models.SystemJob
    relationship = 'notifications'
    search_fields = ('subject', 'notification_type', 'body')


class UnifiedJobTemplateList(ListAPIView):
    model = models.UnifiedJobTemplate
    serializer_class = serializers.UnifiedJobTemplateSerializer
    search_fields = ('description', 'name', 'jobtemplate__playbook')


class UnifiedJobList(ListAPIView):
    model = models.UnifiedJob
    serializer_class = serializers.UnifiedJobListSerializer
    search_fields = ('description', 'name', 'job__playbook')


def redact_ansi(line):
    # Remove ANSI escape sequences used to embed event data.
    line = re.sub(r'\x1b\[K(?:[A-Za-z0-9+/=]+\x1b\[\d+D)+\x1b\[K', '', line)
    # Remove ANSI color escape sequences.
    return re.sub(r'\x1b[^m]*m', '', line)


class StdoutFilter(object):
    def __init__(self, fileobj):
        self._functions = []
        self.fileobj = fileobj
        self.extra_data = ''
        if hasattr(fileobj, 'close'):
            self.close = fileobj.close

    def read(self, size=-1):
        data = self.extra_data
        while size > 0 and len(data) < size:
            line = self.fileobj.readline(size)
            if not line:
                break
            line = self.process_line(line)
            data += line
        if size > 0 and len(data) > size:
            self.extra_data = data[size:]
            data = data[:size]
        else:
            self.extra_data = ''
        return data

    def register(self, func):
        self._functions.append(func)

    def process_line(self, line):
        for func in self._functions:
            line = func(line)
        return line


class UnifiedJobStdout(RetrieveAPIView):
    authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
    serializer_class = serializers.UnifiedJobStdoutSerializer
    renderer_classes = [
        renderers.BrowsableAPIRenderer,
        StaticHTMLRenderer,
        renderers.PlainTextRenderer,
        renderers.AnsiTextRenderer,
        JSONRenderer,
        renderers.DownloadTextRenderer,
        renderers.AnsiDownloadRenderer,
    ]
    filter_backends = ()

    def retrieve(self, request, *args, **kwargs):
        unified_job = self.get_object()
        try:
            target_format = request.accepted_renderer.format
            if target_format in ('html', 'api', 'json'):
                content_encoding = request.query_params.get('content_encoding', None)
                start_line = request.query_params.get('start_line', 0)
                end_line = request.query_params.get('end_line', None)
                dark_val = request.query_params.get('dark', '')
                dark = bool(dark_val and dark_val[0].lower() in ('1', 't', 'y'))
                content_only = bool(target_format in ('api', 'json'))
                dark_bg = (content_only and dark) or (not content_only and (dark or not dark_val))
                content, start, end, absolute_end = unified_job.result_stdout_raw_limited(start_line, end_line)

                # Remove any ANSI escape sequences containing job event data.
                content = re.sub(r'\x1b\[K(?:[A-Za-z0-9+/=]+\x1b\[\d+D)+\x1b\[K', '', content)

                conv = Ansi2HTMLConverter()
                body = conv.convert(html.escape(content))

                context = {'title': get_view_name(self.__class__), 'body': mark_safe(body), 'dark': dark_bg, 'content_only': content_only}
                data = render_to_string('api/stdout.html', context).strip()

                if target_format == 'api':
                    return Response(mark_safe(data))
                if target_format == 'json':
                    content = content.encode('utf-8')
                    if content_encoding == 'base64':
                        content = b64encode(content)
                    return Response({'range': {'start': start, 'end': end, 'absolute_end': absolute_end}, 'content': content})
                return Response(data)
            elif target_format == 'txt':
                return Response(unified_job.result_stdout)
            elif target_format == 'ansi':
                return Response(unified_job.result_stdout_raw)
            elif target_format in {'txt_download', 'ansi_download'}:
                filename = '{type}_{pk}{suffix}.txt'.format(
                    type=camelcase_to_underscore(unified_job.__class__.__name__), pk=unified_job.id, suffix='.ansi' if target_format == 'ansi_download' else ''
                )
                content_fd = unified_job.result_stdout_raw_handle(enforce_max_bytes=False)
                redactor = StdoutFilter(content_fd)
                if target_format == 'txt_download':
                    redactor.register(redact_ansi)
                if type(unified_job) == models.ProjectUpdate:
                    redactor.register(UriCleaner.remove_sensitive)
                response = HttpResponse(FileWrapper(redactor), content_type='text/plain')
                response["Content-Disposition"] = 'attachment; filename="{}"'.format(filename)
                return response
            else:
                return super(UnifiedJobStdout, self).retrieve(request, *args, **kwargs)
        except models.StdoutMaxBytesExceeded as e:
            response_message = _(
                "Standard Output too large to display ({text_size} bytes), only download supported for sizes over {supported_size} bytes."
            ).format(text_size=e.total, supported_size=e.supported)
            if request.accepted_renderer.format == 'json':
                return Response({'range': {'start': 0, 'end': 1, 'absolute_end': 1}, 'content': response_message})
            else:
                return Response(response_message)


class ProjectUpdateStdout(UnifiedJobStdout):
    model = models.ProjectUpdate


class InventoryUpdateStdout(UnifiedJobStdout):
    model = models.InventoryUpdate


class JobStdout(UnifiedJobStdout):
    model = models.Job


class AdHocCommandStdout(UnifiedJobStdout):
    model = models.AdHocCommand


class NotificationTemplateList(ListCreateAPIView):
    model = models.NotificationTemplate
    serializer_class = serializers.NotificationTemplateSerializer


class NotificationTemplateDetail(RetrieveUpdateDestroyAPIView):
    model = models.NotificationTemplate
    serializer_class = serializers.NotificationTemplateSerializer

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        if not request.user.can_access(self.model, 'delete', obj):
            return Response(status=status.HTTP_404_NOT_FOUND)

        hours_old = now() - dateutil.relativedelta.relativedelta(hours=8)
        if obj.notifications.filter(status='pending', created__gt=hours_old).exists():
            return Response({"error": _("Delete not allowed while there are pending notifications")}, status=status.HTTP_405_METHOD_NOT_ALLOWED)
        return super(NotificationTemplateDetail, self).delete(request, *args, **kwargs)


class NotificationTemplateTest(GenericAPIView):
    '''Test a Notification Template'''

    name = _('Notification Template Test')
    model = models.NotificationTemplate
    obj_permission_type = 'start'
    serializer_class = serializers.EmptySerializer

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        msg = "Notification Test {} {}".format(obj.id, settings.TOWER_URL_BASE)
        if obj.notification_type in ('email', 'pagerduty'):
            body = "Test Notification {} {}".format(obj.id, settings.TOWER_URL_BASE)
        elif obj.notification_type in ('webhook', 'grafana'):
            body = '{{"body": "Test Notification {} {}"}}'.format(obj.id, settings.TOWER_URL_BASE)
        else:
            body = {"body": "Test Notification {} {}".format(obj.id, settings.TOWER_URL_BASE)}
        notification = obj.generate_notification(msg, body)

        if not notification:
            return Response({}, status=status.HTTP_400_BAD_REQUEST)
        else:
            connection.on_commit(lambda: send_notifications.delay([notification.id]))
            data = OrderedDict()
            data['notification'] = notification.id
            data.update(serializers.NotificationSerializer(notification, context=self.get_serializer_context()).to_representation(notification))
            headers = {'Location': notification.get_absolute_url(request=request)}
            return Response(data, headers=headers, status=status.HTTP_202_ACCEPTED)


class NotificationTemplateNotificationList(SubListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    parent_model = models.NotificationTemplate
    relationship = 'notifications'
    parent_key = 'notification_template'
    search_fields = ('subject', 'notification_type', 'body')


class NotificationTemplateCopy(CopyAPIView):
    model = models.NotificationTemplate
    copy_return_serializer_class = serializers.NotificationTemplateSerializer


class NotificationList(ListAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer
    search_fields = ('subject', 'notification_type', 'body')


class NotificationDetail(RetrieveAPIView):
    model = models.Notification
    serializer_class = serializers.NotificationSerializer


class ActivityStreamList(SimpleListAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer
    search_fields = ('changes',)


class ActivityStreamDetail(RetrieveAPIView):
    model = models.ActivityStream
    serializer_class = serializers.ActivityStreamSerializer


class RoleList(ListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    permission_classes = (IsAuthenticated,)
    search_fields = ('role_field', 'content_type__model')


class RoleDetail(RetrieveAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer


class RoleUsersList(SubListAttachDetachAPIView):
    deprecated = True
    model = models.User
    serializer_class = serializers.UserSerializer
    parent_model = models.Role
    relationship = 'members'
    ordering = ('username',)

    def get_queryset(self):
        role = self.get_parent_object()
        self.check_parent_access(role)
        return role.members.all()

    def post(self, request, *args, **kwargs):
        # Forbid implicit user creation here
        sub_id = request.data.get('id', None)
        if not sub_id:
            return super(RoleUsersList, self).post(request)

        user = get_object_or_400(models.User, pk=sub_id)
        role = self.get_parent_object()

        content_types = ContentType.objects.get_for_models(models.Organization, models.Team, models.Credential)  # dict of {model: content_type}
        if not settings.ALLOW_LOCAL_RESOURCE_MANAGEMENT:
            for model in [models.Organization, models.Team]:
                ct = content_types[model]
                if role.content_type == ct and role.role_field in ['member_role', 'admin_role']:
                    data = dict(msg=_(f"Cannot directly modify user membership to {ct.model}. Direct shared resource management disabled"))
                    return Response(data, status=status.HTTP_403_FORBIDDEN)

        credential_content_type = content_types[models.Credential]
        if role.content_type == credential_content_type:
            if 'disassociate' not in request.data and role.content_object.organization and user not in role.content_object.organization.member_role:
                data = dict(msg=_("You cannot grant credential access to a user not in the credentials' organization"))
                return Response(data, status=status.HTTP_400_BAD_REQUEST)

            if not role.content_object.organization and not request.user.is_superuser:
                data = dict(msg=_("You cannot grant private credential access to another user"))
                return Response(data, status=status.HTTP_400_BAD_REQUEST)

        return super(RoleUsersList, self).post(request, *args, **kwargs)


class RoleTeamsList(SubListAttachDetachAPIView):
    deprecated = True
    model = models.Team
    serializer_class = serializers.TeamSerializer
    parent_model = models.Role
    relationship = 'member_role.parents'
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        role = self.get_parent_object()
        self.check_parent_access(role)
        return models.Team.objects.filter(member_role__children=role)

    def post(self, request, pk, *args, **kwargs):
        sub_id = request.data.get('id', None)
        if not sub_id:
            return super(RoleTeamsList, self).post(request)

        team = get_object_or_400(models.Team, pk=sub_id)
        role = models.Role.objects.get(pk=self.kwargs['pk'])

        organization_content_type = ContentType.objects.get_for_model(models.Organization)
        if role.content_type == organization_content_type and role.role_field in ['member_role', 'admin_role']:
            data = dict(msg=_("You cannot assign an Organization participation role as a child role for a Team."))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)

        credential_content_type = ContentType.objects.get_for_model(models.Credential)
        if role.content_type == credential_content_type:
            if not role.content_object.organization or role.content_object.organization.id != team.organization.id:
                data = dict(msg=_("You cannot grant credential access to a team when the Organization field isn't set, or belongs to a different organization"))
                return Response(data, status=status.HTTP_400_BAD_REQUEST)

        action = 'attach'
        if request.data.get('disassociate', None):
            action = 'unattach'

        if role.is_singleton() and action == 'attach':
            data = dict(msg=_("You cannot grant system-level permissions to a team."))
            return Response(data, status=status.HTTP_400_BAD_REQUEST)

        if not request.user.can_access(self.parent_model, action, role, team, self.relationship, request.data, skip_sub_obj_read_check=False):
            raise PermissionDenied()
        if request.data.get('disassociate', None):
            team.member_role.children.remove(role)
        else:
            team.member_role.children.add(role)

        return Response(status=status.HTTP_204_NO_CONTENT)


class RoleParentsList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.Role
    relationship = 'parents'
    permission_classes = (IsAuthenticated,)
    search_fields = ('role_field', 'content_type__model')

    def get_queryset(self):
        role = models.Role.objects.get(pk=self.kwargs['pk'])
        return models.Role.filter_visible_roles(self.request.user, role.parents.all())


class RoleChildrenList(SubListAPIView):
    deprecated = True
    model = models.Role
    serializer_class = serializers.RoleSerializer
    parent_model = models.Role
    relationship = 'children'
    permission_classes = (IsAuthenticated,)
    search_fields = ('role_field', 'content_type__model')

    def get_queryset(self):
        role = models.Role.objects.get(pk=self.kwargs['pk'])
        return models.Role.filter_visible_roles(self.request.user, role.children.all())


# Create view functions for all of the class-based views to simplify inclusion
# in URL patterns and reverse URL lookups, converting CamelCase names to
# lowercase_with_underscore (e.g. MyView.as_view() becomes my_view).
this_module = sys.modules[__name__]
for attr, value in list(locals().items()):
    if isinstance(value, type) and issubclass(value, APIView):
        name = camelcase_to_underscore(attr)
        view = value.as_view()
        setattr(this_module, name, view)


class WorkflowApprovalTemplateDetail(RelatedJobsPreventDeleteMixin, RetrieveUpdateDestroyAPIView):
    model = models.WorkflowApprovalTemplate
    serializer_class = serializers.WorkflowApprovalTemplateSerializer


class WorkflowApprovalTemplateJobsList(SubListAPIView):
    model = models.WorkflowApproval
    serializer_class = serializers.WorkflowApprovalListSerializer
    parent_model = models.WorkflowApprovalTemplate
    relationship = 'approvals'
    parent_key = 'workflow_approval_template'


class WorkflowApprovalList(ListAPIView):
    model = models.WorkflowApproval
    serializer_class = serializers.WorkflowApprovalListSerializer

    def get(self, request, *args, **kwargs):
        return super(WorkflowApprovalList, self).get(request, *args, **kwargs)


class WorkflowApprovalDetail(UnifiedJobDeletionMixin, RetrieveDestroyAPIView):
    model = models.WorkflowApproval
    serializer_class = serializers.WorkflowApprovalSerializer


class WorkflowApprovalApprove(RetrieveAPIView):
    model = models.WorkflowApproval
    serializer_class = serializers.WorkflowApprovalViewSerializer
    permission_classes = (WorkflowApprovalPermission,)

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        if not request.user.can_access(models.WorkflowApproval, 'approve_or_deny', obj):
            raise PermissionDenied(detail=_("User does not have permission to approve or deny this workflow."))
        if obj.status != 'pending':
            return Response({"error": _("This workflow step has already been approved or denied.")}, status=status.HTTP_400_BAD_REQUEST)
        obj.approve(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkflowApprovalDeny(RetrieveAPIView):
    model = models.WorkflowApproval
    serializer_class = serializers.WorkflowApprovalViewSerializer
    permission_classes = (WorkflowApprovalPermission,)

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        if not request.user.can_access(models.WorkflowApproval, 'approve_or_deny', obj):
            raise PermissionDenied(detail=_("User does not have permission to approve or deny this workflow."))
        if obj.status != 'pending':
            return Response({"error": _("This workflow step has already been approved or denied.")}, status=status.HTTP_400_BAD_REQUEST)
        obj.deny(request)
        return Response(status=status.HTTP_204_NO_CONTENT)
