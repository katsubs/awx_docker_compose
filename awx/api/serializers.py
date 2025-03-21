# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# Python
import copy
import json
import logging
import re
from collections import Counter, OrderedDict
from datetime import timedelta
from uuid import uuid4

# Jinja
from jinja2 import sandbox, StrictUndefined
from jinja2.exceptions import TemplateSyntaxError, UndefinedError, SecurityError

# Django
from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.encoding import force_str
from django.utils.text import capfirst
from django.utils.timezone import now
from django.core.validators import RegexValidator, MaxLengthValidator

# Django REST Framework
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.relations import ManyRelatedField
from rest_framework import fields
from rest_framework import serializers
from rest_framework import validators
from rest_framework.utils.serializer_helpers import ReturnList

# Django-Polymorphic
from polymorphic.models import PolymorphicModel

# django-ansible-base
from ansible_base.lib.utils.models import get_type_for_model
from ansible_base.rbac.models import RoleEvaluation, ObjectRole
from ansible_base.rbac import permission_registry

# AWX
from awx.main.access import get_user_capabilities
from awx.main.constants import ACTIVE_STATES, org_role_to_permission
from awx.main.models import (
    ActivityStream,
    AdHocCommand,
    AdHocCommandEvent,
    Credential,
    CredentialInputSource,
    CredentialType,
    ExecutionEnvironment,
    Group,
    Host,
    HostMetric,
    HostMetricSummaryMonthly,
    Instance,
    InstanceGroup,
    InstanceLink,
    Inventory,
    InventorySource,
    InventoryUpdate,
    InventoryUpdateEvent,
    Job,
    JobEvent,
    JobHostSummary,
    JobLaunchConfig,
    JobNotificationMixin,
    JobTemplate,
    Label,
    Notification,
    NotificationTemplate,
    Organization,
    Project,
    ProjectUpdate,
    ProjectUpdateEvent,
    ReceptorAddress,
    Role,
    Schedule,
    SystemJob,
    SystemJobEvent,
    SystemJobTemplate,
    Team,
    UnifiedJob,
    UnifiedJobTemplate,
    WorkflowApproval,
    WorkflowApprovalTemplate,
    WorkflowJob,
    WorkflowJobNode,
    WorkflowJobTemplate,
    WorkflowJobTemplateNode,
    StdoutMaxBytesExceeded,
)
from awx.main.models.base import VERBOSITY_CHOICES, NEW_JOB_TYPE_CHOICES
from awx.main.models.rbac import role_summary_fields_generator, give_creator_permissions, get_role_codenames, to_permissions, get_role_from_object_role
from awx.main.fields import ImplicitRoleField
from awx.main.utils import (
    get_model_for_type,
    camelcase_to_underscore,
    getattrd,
    parse_yaml_or_json,
    has_model_field_prefetched,
    extract_ansible_vars,
    encrypt_dict,
    prefetch_page_capabilities,
    truncate_stdout,
    get_licenser,
)

from awx.main.utils.filters import SmartFilter
from awx.main.utils.plugins import load_combined_inventory_source_options
from awx.main.utils.named_url_graph import reset_counters
from awx.main.scheduler.task_manager_models import TaskManagerModels
from awx.main.redact import UriCleaner, REPLACE_STR
from awx.main.signals import update_inventory_computed_fields


from awx.main.validators import vars_validate_or_raise

from awx.api.versioning import reverse
from awx.api.fields import BooleanNullField, CharNullField, ChoiceNullField, VerbatimField, DeprecatedCredentialField

# AWX Utils
from awx.api.validators import HostnameRegexValidator

logger = logging.getLogger('awx.api.serializers')

# Fields that should be summarized regardless of object type.
DEFAULT_SUMMARY_FIELDS = ('id', 'name', 'description')  # , 'created_by', 'modified_by')#, 'type')

# Keys are fields (foreign keys) where, if found on an instance, summary info
# should be added to the serialized data.  Values are a tuple of field names on
# the related object to include in the summary data (if the field is present on
# the related object).
SUMMARIZABLE_FK_FIELDS = {
    'organization': DEFAULT_SUMMARY_FIELDS,
    'user': ('id', 'username', 'first_name', 'last_name'),
    'application': ('id', 'name'),
    'team': DEFAULT_SUMMARY_FIELDS,
    'inventory': DEFAULT_SUMMARY_FIELDS
    + (
        'has_active_failures',
        'total_hosts',
        'hosts_with_active_failures',
        'total_groups',
        'has_inventory_sources',
        'total_inventory_sources',
        'inventory_sources_with_failures',
        'organization_id',
        'kind',
    ),
    'host': DEFAULT_SUMMARY_FIELDS,
    'constructed_host': DEFAULT_SUMMARY_FIELDS,
    'group': DEFAULT_SUMMARY_FIELDS,
    'default_environment': DEFAULT_SUMMARY_FIELDS + ('image',),
    'execution_environment': DEFAULT_SUMMARY_FIELDS + ('image',),
    'project': DEFAULT_SUMMARY_FIELDS + ('status', 'scm_type', 'allow_override'),
    'source_project': DEFAULT_SUMMARY_FIELDS + ('status', 'scm_type', 'allow_override'),
    'project_update': DEFAULT_SUMMARY_FIELDS + ('status', 'failed'),
    'credential': DEFAULT_SUMMARY_FIELDS + ('kind', 'cloud', 'kubernetes', 'credential_type_id'),
    'signature_validation_credential': DEFAULT_SUMMARY_FIELDS + ('kind', 'credential_type_id'),
    'job': DEFAULT_SUMMARY_FIELDS + ('status', 'failed', 'elapsed', 'type', 'canceled_on'),
    'job_template': DEFAULT_SUMMARY_FIELDS,
    'workflow_job_template': DEFAULT_SUMMARY_FIELDS,
    'workflow_job': DEFAULT_SUMMARY_FIELDS,
    'workflow_approval_template': DEFAULT_SUMMARY_FIELDS + ('timeout',),
    'workflow_approval': DEFAULT_SUMMARY_FIELDS + ('timeout',),
    'schedule': DEFAULT_SUMMARY_FIELDS + ('next_run',),
    'unified_job_template': DEFAULT_SUMMARY_FIELDS + ('unified_job_type',),
    'last_job': DEFAULT_SUMMARY_FIELDS + ('finished', 'status', 'failed', 'license_error', 'canceled_on'),
    'last_job_host_summary': DEFAULT_SUMMARY_FIELDS + ('failed',),
    'last_update': DEFAULT_SUMMARY_FIELDS + ('status', 'failed', 'license_error'),
    'current_update': DEFAULT_SUMMARY_FIELDS + ('status', 'failed', 'license_error'),
    'current_job': DEFAULT_SUMMARY_FIELDS + ('status', 'failed', 'license_error'),
    'inventory_source': ('id', 'name', 'source', 'last_updated', 'status'),
    'role': ('id', 'role_field'),
    'notification_template': DEFAULT_SUMMARY_FIELDS,
    'instance_group': ('id', 'name', 'is_container_group'),
    'source_credential': DEFAULT_SUMMARY_FIELDS + ('kind', 'cloud', 'credential_type_id'),
    'target_credential': DEFAULT_SUMMARY_FIELDS + ('kind', 'cloud', 'credential_type_id'),
    'webhook_credential': DEFAULT_SUMMARY_FIELDS + ('kind', 'cloud', 'credential_type_id'),
    'approved_or_denied_by': ('id', 'username', 'first_name', 'last_name'),
    'credential_type': DEFAULT_SUMMARY_FIELDS,
    'resource': ('ansible_id', 'resource_type'),
}


# These fields can be edited on a constructed inventory's generated source (possibly by using the constructed
# inventory's special API endpoint, but also by using the inventory sources endpoint).
CONSTRUCTED_INVENTORY_SOURCE_EDITABLE_FIELDS = ('source_vars', 'update_cache_timeout', 'limit', 'verbosity')


def reverse_gfk(content_object, request):
    """
    Computes a reverse for a GenericForeignKey field.

    Returns a dictionary of the form
        { '<type>': reverse(<type detail>) }
    for example
        { 'organization': '/api/v2/organizations/1/' }
    """
    if content_object is None or not hasattr(content_object, 'get_absolute_url'):
        return {}

    return {camelcase_to_underscore(content_object.__class__.__name__): content_object.get_absolute_url(request=request)}


class CopySerializer(serializers.Serializer):
    name = serializers.CharField()

    def validate(self, attrs):
        name = attrs.get('name')
        view = self.context.get('view', None)
        obj = view.get_object()
        if name == obj.name:
            raise serializers.ValidationError(_('The original object is already named {}, a copy from it cannot have the same name.'.format(name)))
        return attrs


class BaseSerializerMetaclass(serializers.SerializerMetaclass):
    """
    Custom metaclass to enable attribute inheritance from Meta objects on
    serializer base classes.

    Also allows for inheriting or updating field lists from base class(es):

        class Meta:

            # Inherit all fields from base class.
            fields = ('*',)

            # Inherit all fields from base class and add 'foo'.
            fields = ('*', 'foo')

            # Inherit all fields from base class except 'bar'.
            fields = ('*', '-bar')

            # Define fields as 'foo' and 'bar'; ignore base class fields.
            fields = ('foo', 'bar')

            # Extra field kwargs dicts are also merged from base classes.
            extra_kwargs = {
                'foo': {'required': True},
                'bar': {'read_only': True},
            }

            # If a subclass were to define extra_kwargs as:
            extra_kwargs = {
                'foo': {'required': False, 'default': ''},
                'bar': {'label': 'New Label for Bar'},
            }

            # The resulting value of extra_kwargs would be:
            extra_kwargs = {
                'foo': {'required': False, 'default': ''},
                'bar': {'read_only': True, 'label': 'New Label for Bar'},
            }

            # Extra field kwargs cannot be removed in subclasses, only replaced.

    """

    @staticmethod
    def _is_list_of_strings(x):
        return isinstance(x, (list, tuple)) and all([isinstance(y, str) for y in x])

    @staticmethod
    def _is_extra_kwargs(x):
        return isinstance(x, dict) and all([isinstance(k, str) and isinstance(v, dict) for k, v in x.items()])

    @classmethod
    def _update_meta(cls, base, meta, other=None):
        for attr in dir(other):
            if attr.startswith('_'):
                continue
            val = getattr(other, attr)
            meta_val = getattr(meta, attr, None)
            # Special handling for lists/tuples of strings (field names).
            if cls._is_list_of_strings(val) and cls._is_list_of_strings(meta_val or []):
                meta_val = meta_val or []
                new_vals = []
                except_vals = []
                if base:  # Merge values from all bases.
                    new_vals.extend([x for x in meta_val])
                for v in val:
                    if not base and v == '*':  # Inherit all values from previous base(es).
                        new_vals.extend([x for x in meta_val])
                    elif not base and v.startswith('-'):  # Except these values.
                        except_vals.append(v[1:])
                    else:
                        new_vals.append(v)
                val = []
                for v in new_vals:
                    if v not in except_vals and v not in val:
                        val.append(v)
                val = tuple(val)
            # Merge extra_kwargs dicts from base classes.
            elif cls._is_extra_kwargs(val) and cls._is_extra_kwargs(meta_val or {}):
                meta_val = meta_val or {}
                new_val = {}
                if base:
                    for k, v in meta_val.items():
                        new_val[k] = copy.deepcopy(v)
                for k, v in val.items():
                    new_val.setdefault(k, {}).update(copy.deepcopy(v))
                val = new_val
            # Any other values are copied in case they are mutable objects.
            else:
                val = copy.deepcopy(val)
            setattr(meta, attr, val)

    def __new__(cls, name, bases, attrs):
        meta = type('Meta', (object,), {})
        for base in bases[::-1]:
            cls._update_meta(base, meta, getattr(base, 'Meta', None))
        cls._update_meta(None, meta, attrs.get('Meta', meta))
        attrs['Meta'] = meta
        return super(BaseSerializerMetaclass, cls).__new__(cls, name, bases, attrs)


class BaseSerializer(serializers.ModelSerializer, metaclass=BaseSerializerMetaclass):
    class Meta:
        fields = ('id', 'type', 'url', 'related', 'summary_fields', 'created', 'modified', 'name', 'description')
        summary_fields = ()
        summarizable_fields = ()

    # add the URL and related resources
    type = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()
    related = serializers.SerializerMethodField('_get_related')
    summary_fields = serializers.SerializerMethodField('_get_summary_fields')

    # make certain fields read only
    created = serializers.SerializerMethodField()
    modified = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super(BaseSerializer, self).__init__(*args, **kwargs)
        # The following lines fix the problem of being able to pass JSON dict into PrimaryKeyRelatedField.
        data = kwargs.get('data', False)
        if data:
            for field_name, field_instance in self.fields.items():
                if isinstance(field_instance, ManyRelatedField) and not field_instance.read_only:
                    if isinstance(data.get(field_name, False), dict):
                        raise serializers.ValidationError(_('Cannot use dictionary for %s' % field_name))

    @property
    def version(self):
        return 2

    def get_type(self, obj):
        return get_type_for_model(self.Meta.model)

    def get_types(self):
        return [self.get_type(None)]

    def get_type_choices(self):
        type_name_map = {
            'job': _('Playbook Run'),
            'ad_hoc_command': _('Command'),
            'project_update': _('SCM Update'),
            'inventory_update': _('Inventory Sync'),
            'system_job': _('Management Job'),
            'workflow_job': _('Workflow Job'),
            'workflow_job_template': _('Workflow Template'),
            'job_template': _('Job Template'),
        }
        choices = []
        for t in self.get_types():
            name = _(type_name_map.get(t, force_str(get_model_for_type(t)._meta.verbose_name).title()))
            choices.append((t, name))
        return choices

    def get_url(self, obj):
        if obj is None or not hasattr(obj, 'get_absolute_url'):
            return ''
        elif isinstance(obj, User):
            return self.reverse('api:user_detail', kwargs={'pk': obj.pk})
        else:
            return obj.get_absolute_url(request=self.context.get('request'))

    def filter_field_metadata(self, fields, method):
        """
        Filter field metadata based on the request method.
        This it intended to be extended by subclasses.
        """
        return fields

    def _get_related(self, obj):
        return {} if obj is None else self.get_related(obj)

    def _generate_friendly_id(self, obj, node):
        reset_counters()
        return node.generate_named_url(obj)

    def get_related(self, obj):
        res = OrderedDict()
        view = self.context.get('view', None)
        if view and (hasattr(view, 'retrieve') or view.request.method == 'POST') and type(obj) in settings.NAMED_URL_GRAPH:
            original_path = self.get_url(obj)
            path_components = original_path.lstrip('/').rstrip('/').split('/')

            friendly_id = self._generate_friendly_id(obj, settings.NAMED_URL_GRAPH[type(obj)])
            path_components[-1] = friendly_id

            new_path = '/' + '/'.join(path_components) + '/'
            res['named_url'] = new_path
        if getattr(obj, 'created_by', None):
            res['created_by'] = self.reverse('api:user_detail', kwargs={'pk': obj.created_by.pk})
        if getattr(obj, 'modified_by', None):
            res['modified_by'] = self.reverse('api:user_detail', kwargs={'pk': obj.modified_by.pk})
        return res

    def _get_summary_fields(self, obj):
        return {} if obj is None else self.get_summary_fields(obj)

    def get_summary_fields(self, obj):
        # Return values for certain fields on related objects, to simplify
        # displaying lists of items without additional API requests.
        summary_fields = OrderedDict()
        for fk, related_fields in SUMMARIZABLE_FK_FIELDS.items():
            try:
                # A few special cases where we don't want to access the field
                # because it results in additional queries.
                if fk == 'job' and isinstance(obj, UnifiedJob):
                    continue
                if fk == 'project' and (isinstance(obj, InventorySource) or isinstance(obj, Project)):
                    continue

                try:
                    fkval = getattr(obj, fk, None)
                except ObjectDoesNotExist:
                    continue
                if fkval is None:
                    continue
                if fkval == obj:
                    continue
                summary_fields[fk] = OrderedDict()
                for field in related_fields:
                    fval = getattr(fkval, field, None)

                    if fval is None and field == 'type':
                        if isinstance(fkval, PolymorphicModel):
                            fkval = fkval.get_real_instance()
                        fval = get_type_for_model(fkval)
                    elif fval is None and field == 'unified_job_type' and isinstance(fkval, UnifiedJobTemplate):
                        fkval = fkval.get_real_instance()
                        fval = get_type_for_model(fkval._get_unified_job_class())
                    if fval is not None:
                        summary_fields[fk][field] = fval
            # Can be raised by the reverse accessor for a OneToOneField.
            except ObjectDoesNotExist:
                pass
        if getattr(obj, 'created_by', None):
            summary_fields['created_by'] = OrderedDict()
            for field in SUMMARIZABLE_FK_FIELDS['user']:
                summary_fields['created_by'][field] = getattr(obj.created_by, field)
        if getattr(obj, 'modified_by', None):
            summary_fields['modified_by'] = OrderedDict()
            for field in SUMMARIZABLE_FK_FIELDS['user']:
                summary_fields['modified_by'][field] = getattr(obj.modified_by, field)

        # RBAC summary fields
        roles = {}
        for field in obj._meta.get_fields():
            if type(field) is ImplicitRoleField:
                roles[field.name] = role_summary_fields_generator(obj, field.name)
        if len(roles) > 0:
            summary_fields['object_roles'] = roles

        # Advance display of RBAC capabilities
        if hasattr(self, 'show_capabilities'):
            user_capabilities = self._obj_capability_dict(obj)
            if user_capabilities:
                summary_fields['user_capabilities'] = user_capabilities

        return summary_fields

    def _obj_capability_dict(self, obj):
        """
        Returns the user_capabilities dictionary for a single item
        If inside of a list view, it runs the prefetching algorithm for
        the entire current page, saves it into context
        """
        view = self.context.get('view', None)
        parent_obj = None
        if view and hasattr(view, 'parent_model') and hasattr(view, 'get_parent_object'):
            parent_obj = view.get_parent_object()
        if view and view.request and view.request.user:
            capabilities_cache = {}
            # if serializer has parent, it is ListView, apply page capabilities prefetch
            if self.parent and hasattr(self, 'capabilities_prefetch') and self.capabilities_prefetch:
                qs = self.parent.instance
                if 'capability_map' not in self.context:
                    if hasattr(self, 'polymorphic_base'):
                        model = self.polymorphic_base.Meta.model
                        prefetch_list = self.polymorphic_base.capabilities_prefetch
                    else:
                        model = self.Meta.model
                        prefetch_list = self.capabilities_prefetch
                    self.context['capability_map'] = prefetch_page_capabilities(model, qs, prefetch_list, view.request.user)
                if obj.id in self.context['capability_map']:
                    capabilities_cache = self.context['capability_map'][obj.id]
            return get_user_capabilities(
                view.request.user, obj, method_list=self.show_capabilities, parent_obj=parent_obj, capabilities_cache=capabilities_cache
            )
        else:
            # Contextual information to produce user_capabilities doesn't exist
            return {}

    def get_created(self, obj):
        if obj is None:
            return None
        elif isinstance(obj, User):
            return obj.date_joined
        elif hasattr(obj, 'created'):
            return obj.created
        return None

    def get_modified(self, obj):
        if obj is None:
            return None
        elif isinstance(obj, User):
            return obj.last_login  # Not actually exposed for User.
        elif hasattr(obj, 'modified'):
            return obj.modified
        return None

    def get_extra_kwargs(self):
        extra_kwargs = super(BaseSerializer, self).get_extra_kwargs()
        if self.instance:
            read_only_on_update_fields = getattr(self.Meta, 'read_only_on_update_fields', tuple())
            for field_name in read_only_on_update_fields:
                kwargs = extra_kwargs.get(field_name, {})
                kwargs['read_only'] = True
                extra_kwargs[field_name] = kwargs
        return extra_kwargs

    def build_standard_field(self, field_name, model_field):
        # DRF 3.3 serializers.py::build_standard_field() -> utils/field_mapping.py::get_field_kwargs() short circuits
        # when a Model's editable field is set to False. The short circuit skips choice rendering.
        #
        # This logic is to force rendering choice's on an uneditable field.
        # Note: Consider expanding this rendering for more than just choices fields
        # Note: This logic works in conjunction with
        if hasattr(model_field, 'choices') and model_field.choices:
            was_editable = model_field.editable
            model_field.editable = True

        field_class, field_kwargs = super(BaseSerializer, self).build_standard_field(field_name, model_field)
        if hasattr(model_field, 'choices') and model_field.choices:
            model_field.editable = was_editable
            if was_editable is False:
                field_kwargs['read_only'] = True

        # Pass model field default onto the serializer field if field is not read-only.
        if model_field.has_default() and not field_kwargs.get('read_only', False):
            field_kwargs['default'] = field_kwargs['initial'] = model_field.get_default()

        # Enforce minimum value of 0 for PositiveIntegerFields.
        if isinstance(model_field, (models.PositiveIntegerField, models.PositiveSmallIntegerField)) and 'choices' not in field_kwargs:
            field_kwargs['min_value'] = 0

        # Use custom boolean field that allows null and empty string as False values.
        if isinstance(model_field, models.BooleanField) and not field_kwargs.get('read_only', False):
            field_class = BooleanNullField

        # Use custom char or choice field that coerces null to an empty string.
        if isinstance(model_field, (models.CharField, models.TextField)) and not field_kwargs.get('read_only', False):
            if 'choices' in field_kwargs:
                field_class = ChoiceNullField
            else:
                field_class = CharNullField

        # Update the message used for the unique validator to use capitalized
        # verbose name; keeps unique message the same as with DRF 2.x.
        opts = self.Meta.model._meta.concrete_model._meta
        for validator in field_kwargs.get('validators', []):
            if isinstance(validator, validators.UniqueValidator):
                unique_error_message = model_field.error_messages.get('unique', None)
                if unique_error_message:
                    unique_error_message = unique_error_message % {'model_name': capfirst(opts.verbose_name), 'field_label': capfirst(model_field.verbose_name)}
                    validator.message = unique_error_message

        return field_class, field_kwargs

    def build_relational_field(self, field_name, relation_info):
        field_class, field_kwargs = super(BaseSerializer, self).build_relational_field(field_name, relation_info)
        # Don't include choices for foreign key fields.
        field_kwargs.pop('choices', None)
        return field_class, field_kwargs

    def get_unique_together_validators(self):
        # Allow the model's full_clean method to handle the unique together validation.
        return []

    def run_validation(self, data=fields.empty):
        try:
            return super(BaseSerializer, self).run_validation(data)
        except ValidationError as exc:
            # Avoid bug? in DRF if exc.detail happens to be a list instead of a dict.
            raise ValidationError(detail=serializers.as_serializer_error(exc))

    def get_validation_exclusions(self, obj=None):
        # Borrowed from DRF 2.x - return model fields that should be excluded
        # from model validation.
        cls = self.Meta.model
        opts = cls._meta.concrete_model._meta
        exclusions = [field.name for field in opts.fields]
        for field_name, field in self.fields.items():
            field_name = field.source or field_name
            if field_name not in exclusions:
                continue
            if field.read_only:
                continue
            if isinstance(field, serializers.Serializer):
                continue
            exclusions.remove(field_name)
        # The clean_ methods cannot be ran on many-to-many models
        exclusions.extend([field.name for field in opts.many_to_many])
        return exclusions

    def validate(self, attrs):
        attrs = super(BaseSerializer, self).validate(attrs)
        try:
            # Create/update a model instance and run its full_clean() method to
            # do any validation implemented on the model class.
            exclusions = self.get_validation_exclusions(self.instance)
            obj = self.instance or self.Meta.model()
            for k, v in attrs.items():
                if k not in exclusions and k != 'canonical_address_port':
                    setattr(obj, k, v)
            obj.full_clean(exclude=exclusions)
            # full_clean may modify values on the instance; copy those changes
            # back to attrs so they are saved.
            for k in attrs.keys():
                if k not in exclusions:
                    attrs[k] = getattr(obj, k)
        except DjangoValidationError as exc:
            # DjangoValidationError may contain a list or dict; normalize into a
            # dict where the keys are the field name and the values are a list
            # of error messages, then raise as a DRF ValidationError.  DRF would
            # normally convert any DjangoValidationError to a non-field specific
            # error message; here we preserve field-specific errors raised from
            # the model's full_clean method.
            d = exc.update_error_dict({})
            for k, v in d.items():
                v = v if isinstance(v, list) else [v]
                v2 = []
                for e in v:
                    if isinstance(e, DjangoValidationError):
                        v2.extend(list(e))
                    elif isinstance(e, list):
                        v2.extend(e)
                    else:
                        v2.append(e)
                d[k] = list(map(force_str, v2))
            raise ValidationError(d)
        return attrs

    def reverse(self, *args, **kwargs):
        kwargs['request'] = self.context.get('request')
        return reverse(*args, **kwargs)

    @property
    def is_detail_view(self):
        if 'view' in self.context:
            if 'pk' in self.context['view'].kwargs:
                return True
        return False


class EmptySerializer(serializers.Serializer):
    pass


class UnifiedJobTemplateSerializer(BaseSerializer):
    # As a base serializer, the capabilities prefetch is not used directly,
    # instead they are derived from the Workflow Job Template Serializer and the Job Template Serializer, respectively.
    capabilities_prefetch = []

    class Meta:
        model = UnifiedJobTemplate
        fields = ('*', 'last_job_run', 'last_job_failed', 'next_job_run', 'status', 'execution_environment')

    def get_related(self, obj):
        res = super(UnifiedJobTemplateSerializer, self).get_related(obj)
        if obj.current_job:
            res['current_job'] = obj.current_job.get_absolute_url(request=self.context.get('request'))
        if obj.last_job:
            res['last_job'] = obj.last_job.get_absolute_url(request=self.context.get('request'))
        if obj.next_schedule:
            res['next_schedule'] = obj.next_schedule.get_absolute_url(request=self.context.get('request'))
        if obj.execution_environment_id:
            res['execution_environment'] = self.reverse('api:execution_environment_detail', kwargs={'pk': obj.execution_environment_id})
        return res

    def get_types(self):
        if type(self) is UnifiedJobTemplateSerializer:
            return ['project', 'inventory_source', 'job_template', 'system_job_template', 'workflow_job_template']
        else:
            return super(UnifiedJobTemplateSerializer, self).get_types()

    def get_sub_serializer(self, obj):
        serializer_class = None
        if type(self) is UnifiedJobTemplateSerializer:
            if isinstance(obj, Project):
                serializer_class = ProjectSerializer
            elif isinstance(obj, InventorySource):
                serializer_class = InventorySourceSerializer
            elif isinstance(obj, JobTemplate):
                serializer_class = JobTemplateSerializer
            elif isinstance(obj, SystemJobTemplate):
                serializer_class = SystemJobTemplateSerializer
            elif isinstance(obj, WorkflowJobTemplate):
                serializer_class = WorkflowJobTemplateSerializer
            elif isinstance(obj, WorkflowApprovalTemplate):
                serializer_class = WorkflowApprovalTemplateSerializer
        return serializer_class

    def to_representation(self, obj):
        serializer_class = self.get_sub_serializer(obj)
        if serializer_class:
            serializer = serializer_class(instance=obj, context=self.context)
            # preserve links for list view
            if self.parent:
                serializer.parent = self.parent
                serializer.polymorphic_base = self
                # capabilities prefetch is only valid for these models
                if isinstance(obj, (JobTemplate, WorkflowJobTemplate)):
                    serializer.capabilities_prefetch = serializer_class.capabilities_prefetch
                else:
                    serializer.capabilities_prefetch = None
            return serializer.to_representation(obj)
        else:
            return super(UnifiedJobTemplateSerializer, self).to_representation(obj)

    def get_summary_fields(self, obj):
        summary_fields = super().get_summary_fields(obj)

        if self.is_detail_view:
            resolved_ee = obj.resolve_execution_environment()
            if resolved_ee is not None:
                summary_fields['resolved_environment'] = {
                    field: getattr(resolved_ee, field, None)
                    for field in SUMMARIZABLE_FK_FIELDS['execution_environment']
                    if getattr(resolved_ee, field, None) is not None
                }

        return summary_fields


class UnifiedJobSerializer(BaseSerializer):
    show_capabilities = ['start', 'delete']
    event_processing_finished = serializers.BooleanField(
        help_text=_('Indicates whether all of the events generated by this unified job have been saved to the database.'), read_only=True
    )

    class Meta:
        model = UnifiedJob

        fields = (
            '*',
            'unified_job_template',
            'launch_type',
            'status',
            'execution_environment',
            'failed',
            'started',
            'finished',
            'canceled_on',
            'elapsed',
            'job_args',
            'job_cwd',
            'job_env',
            'job_explanation',
            'execution_node',
            'controller_node',
            'result_traceback',
            'event_processing_finished',
            'launched_by',
            'work_unit_id',
        )

        extra_kwargs = {
            'unified_job_template': {'source': 'unified_job_template_id', 'label': 'unified job template'},
            'job_env': {'read_only': True, 'label': 'job_env'},
        }

    def get_types(self):
        if type(self) is UnifiedJobSerializer:
            return ['project_update', 'inventory_update', 'job', 'ad_hoc_command', 'system_job', 'workflow_job']
        else:
            return super(UnifiedJobSerializer, self).get_types()

    def get_related(self, obj):
        res = super(UnifiedJobSerializer, self).get_related(obj)
        if obj.unified_job_template:
            res['unified_job_template'] = obj.unified_job_template.get_absolute_url(request=self.context.get('request'))
        if obj.schedule:
            res['schedule'] = obj.schedule.get_absolute_url(request=self.context.get('request'))
        if isinstance(obj, ProjectUpdate):
            res['stdout'] = self.reverse('api:project_update_stdout', kwargs={'pk': obj.pk})
        elif isinstance(obj, InventoryUpdate):
            res['stdout'] = self.reverse('api:inventory_update_stdout', kwargs={'pk': obj.pk})
        elif isinstance(obj, Job):
            res['stdout'] = self.reverse('api:job_stdout', kwargs={'pk': obj.pk})
        elif isinstance(obj, AdHocCommand):
            res['stdout'] = self.reverse('api:ad_hoc_command_stdout', kwargs={'pk': obj.pk})
        if obj.workflow_job_id:
            res['source_workflow_job'] = self.reverse('api:workflow_job_detail', kwargs={'pk': obj.workflow_job_id})
        if obj.execution_environment_id:
            res['execution_environment'] = self.reverse('api:execution_environment_detail', kwargs={'pk': obj.execution_environment_id})
        return res

    def get_summary_fields(self, obj):
        summary_fields = super(UnifiedJobSerializer, self).get_summary_fields(obj)
        if obj.spawned_by_workflow:
            summary_fields['source_workflow_job'] = {}
            try:
                summary_obj = obj.unified_job_node.workflow_job
            except UnifiedJob.unified_job_node.RelatedObjectDoesNotExist:
                return summary_fields

            for field in SUMMARIZABLE_FK_FIELDS['job']:
                val = getattr(summary_obj, field, None)
                if val is not None:
                    summary_fields['source_workflow_job'][field] = val

        if self.is_detail_view:
            ancestor = obj.ancestor_job
            if ancestor != obj:
                summary_fields['ancestor_job'] = {
                    'id': ancestor.id,
                    'name': ancestor.name,
                    'type': get_type_for_model(ancestor),
                    'url': ancestor.get_absolute_url(),
                }

        return summary_fields

    def get_sub_serializer(self, obj):
        serializer_class = None
        if type(self) is UnifiedJobSerializer:
            if isinstance(obj, ProjectUpdate):
                serializer_class = ProjectUpdateSerializer
            elif isinstance(obj, InventoryUpdate):
                serializer_class = InventoryUpdateSerializer
            elif isinstance(obj, Job):
                serializer_class = JobSerializer
            elif isinstance(obj, AdHocCommand):
                serializer_class = AdHocCommandSerializer
            elif isinstance(obj, SystemJob):
                serializer_class = SystemJobSerializer
            elif isinstance(obj, WorkflowJob):
                serializer_class = WorkflowJobSerializer
            elif isinstance(obj, WorkflowApproval):
                serializer_class = WorkflowApprovalSerializer
        return serializer_class

    def to_representation(self, obj):
        serializer_class = self.get_sub_serializer(obj)
        if serializer_class:
            serializer = serializer_class(instance=obj, context=self.context)
            # preserve links for list view
            if self.parent:
                serializer.parent = self.parent
                serializer.polymorphic_base = self
                # TODO: restrict models for capabilities prefetch, when it is added
            ret = serializer.to_representation(obj)
        else:
            ret = super(UnifiedJobSerializer, self).to_representation(obj)

        if 'elapsed' in ret:
            if obj and obj.pk and obj.started and not obj.finished:
                td = now() - obj.started
                ret['elapsed'] = (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / (10**6 * 1.0)
            ret['elapsed'] = float(ret['elapsed'])
        # Because this string is saved in the db in the source language,
        # it must be marked for translation after it is pulled from the db, not when set
        ret['job_explanation'] = _(obj.job_explanation)
        return ret

    def get_launched_by(self, obj):
        if obj is not None:
            return obj.launched_by


class UnifiedJobListSerializer(UnifiedJobSerializer):
    class Meta:
        fields = ('*', '-job_args', '-job_cwd', '-job_env', '-result_traceback', '-event_processing_finished')

    def get_field_names(self, declared_fields, info):
        field_names = super(UnifiedJobListSerializer, self).get_field_names(declared_fields, info)
        # Meta multiple inheritance and -field_name options don't seem to be
        # taking effect above, so remove the undesired fields here.
        return tuple(x for x in field_names if x not in ('job_args', 'job_cwd', 'job_env', 'result_traceback', 'event_processing_finished'))

    def get_types(self):
        if type(self) is UnifiedJobListSerializer:
            return ['project_update', 'inventory_update', 'job', 'ad_hoc_command', 'system_job', 'workflow_job']
        else:
            return super(UnifiedJobListSerializer, self).get_types()

    def get_sub_serializer(self, obj):
        serializer_class = None
        if type(self) is UnifiedJobListSerializer:
            if isinstance(obj, ProjectUpdate):
                serializer_class = ProjectUpdateListSerializer
            elif isinstance(obj, InventoryUpdate):
                serializer_class = InventoryUpdateListSerializer
            elif isinstance(obj, Job):
                serializer_class = JobListSerializer
            elif isinstance(obj, AdHocCommand):
                serializer_class = AdHocCommandListSerializer
            elif isinstance(obj, SystemJob):
                serializer_class = SystemJobListSerializer
            elif isinstance(obj, WorkflowJob):
                serializer_class = WorkflowJobListSerializer
            elif isinstance(obj, WorkflowApproval):
                serializer_class = WorkflowApprovalListSerializer
        return serializer_class

    def to_representation(self, obj):
        serializer_class = self.get_sub_serializer(obj)
        if serializer_class:
            serializer = serializer_class(instance=obj, context=self.context)
            ret = serializer.to_representation(obj)
        else:
            ret = super(UnifiedJobListSerializer, self).to_representation(obj)
        if 'elapsed' in ret:
            ret['elapsed'] = float(ret['elapsed'])
        return ret


class UnifiedJobStdoutSerializer(UnifiedJobSerializer):
    result_stdout = serializers.SerializerMethodField()

    class Meta:
        fields = ('result_stdout',)

    def get_types(self):
        if type(self) is UnifiedJobStdoutSerializer:
            return ['project_update', 'inventory_update', 'job', 'ad_hoc_command', 'system_job']
        else:
            return super(UnifiedJobStdoutSerializer, self).get_types()


class UserSerializer(BaseSerializer):
    password = serializers.CharField(required=False, default='', help_text=_('Field used to change the password.'))
    is_system_auditor = serializers.BooleanField(default=False)
    show_capabilities = ['edit', 'delete']

    class Meta:
        model = User
        fields = (
            '*',
            '-name',
            '-description',
            'username',
            'first_name',
            'last_name',
            'email',
            'is_superuser',
            'is_system_auditor',
            'password',
            'last_login',
        )
        extra_kwargs = {'last_login': {'read_only': True}}

    def to_representation(self, obj):
        ret = super(UserSerializer, self).to_representation(obj)
        ret['password'] = '$encrypted$'
        return ret

    def get_validation_exclusions(self, obj=None):
        ret = super(UserSerializer, self).get_validation_exclusions(obj)
        ret.extend(['password', 'is_system_auditor'])
        return ret

    def validate_password(self, value):
        django_validate_password(value)
        if not self.instance and value in (None, ''):
            raise serializers.ValidationError(_('Password required for new User.'))

        # Check if a password is too long
        password_max_length = User._meta.get_field('password').max_length
        if len(value) > password_max_length:
            raise serializers.ValidationError(_('Password max length is {}'.format(password_max_length)))
        if getattr(settings, 'LOCAL_PASSWORD_MIN_LENGTH', 0) and len(value) < getattr(settings, 'LOCAL_PASSWORD_MIN_LENGTH'):
            raise serializers.ValidationError(_('Password must be at least {} characters long.'.format(getattr(settings, 'LOCAL_PASSWORD_MIN_LENGTH'))))
        if getattr(settings, 'LOCAL_PASSWORD_MIN_DIGITS', 0) and sum(c.isdigit() for c in value) < getattr(settings, 'LOCAL_PASSWORD_MIN_DIGITS'):
            raise serializers.ValidationError(_('Password must contain at least {} digits.'.format(getattr(settings, 'LOCAL_PASSWORD_MIN_DIGITS'))))
        if getattr(settings, 'LOCAL_PASSWORD_MIN_UPPER', 0) and sum(c.isupper() for c in value) < getattr(settings, 'LOCAL_PASSWORD_MIN_UPPER'):
            raise serializers.ValidationError(
                _('Password must contain at least {} uppercase characters.'.format(getattr(settings, 'LOCAL_PASSWORD_MIN_UPPER')))
            )
        if getattr(settings, 'LOCAL_PASSWORD_MIN_SPECIAL', 0) and sum(not c.isalnum() for c in value) < getattr(settings, 'LOCAL_PASSWORD_MIN_SPECIAL'):
            raise serializers.ValidationError(
                _('Password must contain at least {} special characters.'.format(getattr(settings, 'LOCAL_PASSWORD_MIN_SPECIAL')))
            )

        return value

    def _update_password(self, obj, new_password):
        if new_password and new_password != '$encrypted$':
            obj.set_password(new_password)
            obj.save(update_fields=['password'])

            # Cycle the session key, but if the requesting user is the same
            # as the modified user then inject a session key derived from
            # the updated user to prevent logout. This is the logic used by
            # the Django admin's own user_change_password view.
            if self.instance and self.context['request'].user.username == obj.username:
                update_session_auth_hash(self.context['request'], obj)

        elif not obj.password:
            obj.set_unusable_password()
            obj.save(update_fields=['password'])

    def create(self, validated_data):
        new_password = validated_data.pop('password', None)
        is_system_auditor = validated_data.pop('is_system_auditor', None)
        obj = super(UserSerializer, self).create(validated_data)
        self._update_password(obj, new_password)
        if is_system_auditor is not None:
            obj.is_system_auditor = is_system_auditor
        return obj

    def update(self, obj, validated_data):
        new_password = validated_data.pop('password', None)
        is_system_auditor = validated_data.pop('is_system_auditor', None)
        obj = super(UserSerializer, self).update(obj, validated_data)
        self._update_password(obj, new_password)
        if is_system_auditor is not None:
            obj.is_system_auditor = is_system_auditor
        return obj

    def get_related(self, obj):
        res = super(UserSerializer, self).get_related(obj)
        res.update(
            dict(
                teams=self.reverse('api:user_teams_list', kwargs={'pk': obj.pk}),
                organizations=self.reverse('api:user_organizations_list', kwargs={'pk': obj.pk}),
                admin_of_organizations=self.reverse('api:user_admin_of_organizations_list', kwargs={'pk': obj.pk}),
                projects=self.reverse('api:user_projects_list', kwargs={'pk': obj.pk}),
                credentials=self.reverse('api:user_credentials_list', kwargs={'pk': obj.pk}),
                roles=self.reverse('api:user_roles_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:user_activity_stream_list', kwargs={'pk': obj.pk}),
                access_list=self.reverse('api:user_access_list', kwargs={'pk': obj.pk}),
            )
        )
        return res


class UserActivityStreamSerializer(UserSerializer):
    """Changes to system auditor status are shown as separate entries,
    so by excluding it from fields here we avoid duplication, which
    would carry some unintended consequences.
    """

    class Meta:
        model = User
        fields = ('*', '-is_system_auditor')


class OrganizationSerializer(BaseSerializer):
    show_capabilities = ['edit', 'delete']

    class Meta:
        model = Organization
        fields = ('*', 'max_hosts', 'custom_virtualenv', 'default_environment')
        read_only_fields = ('*', 'custom_virtualenv')

    def get_related(self, obj):
        res = super(OrganizationSerializer, self).get_related(obj)
        res.update(
            execution_environments=self.reverse('api:organization_execution_environments_list', kwargs={'pk': obj.pk}),
            projects=self.reverse('api:organization_projects_list', kwargs={'pk': obj.pk}),
            inventories=self.reverse('api:organization_inventories_list', kwargs={'pk': obj.pk}),
            job_templates=self.reverse('api:organization_job_templates_list', kwargs={'pk': obj.pk}),
            workflow_job_templates=self.reverse('api:organization_workflow_job_templates_list', kwargs={'pk': obj.pk}),
            users=self.reverse('api:organization_users_list', kwargs={'pk': obj.pk}),
            admins=self.reverse('api:organization_admins_list', kwargs={'pk': obj.pk}),
            teams=self.reverse('api:organization_teams_list', kwargs={'pk': obj.pk}),
            credentials=self.reverse('api:organization_credential_list', kwargs={'pk': obj.pk}),
            activity_stream=self.reverse('api:organization_activity_stream_list', kwargs={'pk': obj.pk}),
            notification_templates=self.reverse('api:organization_notification_templates_list', kwargs={'pk': obj.pk}),
            notification_templates_started=self.reverse('api:organization_notification_templates_started_list', kwargs={'pk': obj.pk}),
            notification_templates_success=self.reverse('api:organization_notification_templates_success_list', kwargs={'pk': obj.pk}),
            notification_templates_error=self.reverse('api:organization_notification_templates_error_list', kwargs={'pk': obj.pk}),
            notification_templates_approvals=self.reverse('api:organization_notification_templates_approvals_list', kwargs={'pk': obj.pk}),
            object_roles=self.reverse('api:organization_object_roles_list', kwargs={'pk': obj.pk}),
            access_list=self.reverse('api:organization_access_list', kwargs={'pk': obj.pk}),
            instance_groups=self.reverse('api:organization_instance_groups_list', kwargs={'pk': obj.pk}),
            galaxy_credentials=self.reverse('api:organization_galaxy_credentials_list', kwargs={'pk': obj.pk}),
        )
        if obj.default_environment:
            res['default_environment'] = self.reverse('api:execution_environment_detail', kwargs={'pk': obj.default_environment_id})
        return res

    def get_summary_fields(self, obj):
        summary_dict = super(OrganizationSerializer, self).get_summary_fields(obj)
        counts_dict = self.context.get('related_field_counts', None)
        if counts_dict is not None and summary_dict is not None:
            if obj.id not in counts_dict:
                summary_dict['related_field_counts'] = {'inventories': 0, 'teams': 0, 'users': 0, 'job_templates': 0, 'admins': 0, 'projects': 0}
            else:
                summary_dict['related_field_counts'] = counts_dict[obj.id]

        # Organization participation roles (admin, member) can't be assigned
        # to a team. This provides a hint to the ui so it can know to not
        # display these roles for team role selection.
        for key in ('admin_role', 'member_role'):
            if key in summary_dict.get('object_roles', {}):
                summary_dict['object_roles'][key]['user_only'] = True

        return summary_dict

    def validate(self, attrs):
        obj = self.instance
        view = self.context['view']

        obj_limit = getattr(obj, 'max_hosts', None)
        api_limit = attrs.get('max_hosts')

        if not view.request.user.is_superuser:
            if api_limit is not None and api_limit != obj_limit:
                # Only allow superusers to edit the max_hosts field
                raise serializers.ValidationError(_('Cannot change max_hosts.'))

        return super(OrganizationSerializer, self).validate(attrs)


class ProjectOptionsSerializer(BaseSerializer):
    class Meta:
        fields = (
            '*',
            'local_path',
            'scm_type',
            'scm_url',
            'scm_branch',
            'scm_refspec',
            'scm_clean',
            'scm_track_submodules',
            'scm_delete_on_update',
            'credential',
            'timeout',
            'scm_revision',
        )

    def get_related(self, obj):
        res = super(ProjectOptionsSerializer, self).get_related(obj)
        if obj.credential:
            res['credential'] = self.reverse('api:credential_detail', kwargs={'pk': obj.credential.pk})
        return res

    def validate(self, attrs):
        errors = {}

        # Don't allow assigning a local_path used by another project.
        # Don't allow assigning a local_path when scm_type is set.
        valid_local_paths = Project.get_local_path_choices()
        if self.instance:
            scm_type = attrs.get('scm_type', self.instance.scm_type) or u''
        else:
            scm_type = attrs.get('scm_type', u'') or u''
        if self.instance and not scm_type:
            valid_local_paths.append(self.instance.local_path)
        if self.instance and scm_type and "local_path" in attrs and self.instance.local_path != attrs['local_path']:
            errors['local_path'] = _(f'Cannot change local_path for {scm_type}-based projects')
        if scm_type:
            attrs.pop('local_path', None)
        if 'local_path' in attrs and attrs['local_path'] not in valid_local_paths:
            errors['local_path'] = _('This path is already being used by another manual project.')
        if attrs.get('scm_branch') and scm_type == 'archive':
            errors['scm_branch'] = _('SCM branch cannot be used with archive projects.')
        if attrs.get('scm_refspec') and scm_type != 'git':
            errors['scm_refspec'] = _('SCM refspec can only be used with git projects.')
        if attrs.get('scm_track_submodules') and scm_type != 'git':
            errors['scm_track_submodules'] = _('SCM track_submodules can only be used with git projects.')

        if errors:
            raise serializers.ValidationError(errors)

        return super(ProjectOptionsSerializer, self).validate(attrs)


class ExecutionEnvironmentSerializer(BaseSerializer):
    show_capabilities = ['edit', 'delete', 'copy']
    managed = serializers.ReadOnlyField()

    class Meta:
        model = ExecutionEnvironment
        fields = ('*', 'organization', 'image', 'managed', 'credential', 'pull')

    def get_related(self, obj):
        res = super(ExecutionEnvironmentSerializer, self).get_related(obj)
        res.update(
            activity_stream=self.reverse('api:execution_environment_activity_stream_list', kwargs={'pk': obj.pk}),
            unified_job_templates=self.reverse('api:execution_environment_job_template_list', kwargs={'pk': obj.pk}),
            copy=self.reverse('api:execution_environment_copy', kwargs={'pk': obj.pk}),
        )
        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})
        if obj.credential:
            res['credential'] = self.reverse('api:credential_detail', kwargs={'pk': obj.credential.pk})
        return res

    def validate_credential(self, value):
        if value and value.kind != 'registry':
            raise serializers.ValidationError(_('Only Container Registry credentials can be associated with an Execution Environment'))
        return value

    def validate(self, attrs):
        # prevent changing organization of ee. Unsetting (change to null) is allowed
        if self.instance:
            org = attrs.get('organization', None)
            if org and org.pk != self.instance.organization_id:
                raise serializers.ValidationError({"organization": _("Cannot change the organization of an execution environment")})
        return super(ExecutionEnvironmentSerializer, self).validate(attrs)


class ProjectSerializer(UnifiedJobTemplateSerializer, ProjectOptionsSerializer):
    status = serializers.ChoiceField(choices=Project.PROJECT_STATUS_CHOICES, read_only=True)
    last_update_failed = serializers.BooleanField(read_only=True)
    last_updated = serializers.DateTimeField(read_only=True)
    show_capabilities = ['start', 'schedule', 'edit', 'delete', 'copy']
    capabilities_prefetch = ['admin', 'update', {'copy': 'organization.project_admin'}]

    class Meta:
        model = Project
        fields = (
            '*',
            '-execution_environment',
            'organization',
            'scm_update_on_launch',
            'scm_update_cache_timeout',
            'allow_override',
            'custom_virtualenv',
            'default_environment',
            'signature_validation_credential',
        ) + (
            'last_update_failed',
            'last_updated',
        )  # Backwards compatibility
        read_only_fields = ('*', 'custom_virtualenv')

    def get_related(self, obj):
        res = super(ProjectSerializer, self).get_related(obj)
        res.update(
            dict(
                teams=self.reverse('api:project_teams_list', kwargs={'pk': obj.pk}),
                playbooks=self.reverse('api:project_playbooks', kwargs={'pk': obj.pk}),
                inventory_files=self.reverse('api:project_inventories', kwargs={'pk': obj.pk}),
                update=self.reverse('api:project_update_view', kwargs={'pk': obj.pk}),
                project_updates=self.reverse('api:project_updates_list', kwargs={'pk': obj.pk}),
                scm_inventory_sources=self.reverse('api:project_scm_inventory_sources', kwargs={'pk': obj.pk}),
                schedules=self.reverse('api:project_schedules_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:project_activity_stream_list', kwargs={'pk': obj.pk}),
                notification_templates_started=self.reverse('api:project_notification_templates_started_list', kwargs={'pk': obj.pk}),
                notification_templates_success=self.reverse('api:project_notification_templates_success_list', kwargs={'pk': obj.pk}),
                notification_templates_error=self.reverse('api:project_notification_templates_error_list', kwargs={'pk': obj.pk}),
                access_list=self.reverse('api:project_access_list', kwargs={'pk': obj.pk}),
                object_roles=self.reverse('api:project_object_roles_list', kwargs={'pk': obj.pk}),
                copy=self.reverse('api:project_copy', kwargs={'pk': obj.pk}),
            )
        )
        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})
        if obj.default_environment:
            res['default_environment'] = self.reverse('api:execution_environment_detail', kwargs={'pk': obj.default_environment_id})
        # Backwards compatibility.
        if obj.current_update:
            res['current_update'] = self.reverse('api:project_update_detail', kwargs={'pk': obj.current_update.pk})
        if obj.last_update:
            res['last_update'] = self.reverse('api:project_update_detail', kwargs={'pk': obj.last_update.pk})
        return res

    def to_representation(self, obj):
        ret = super(ProjectSerializer, self).to_representation(obj)
        if 'scm_revision' in ret and obj.scm_type == '':
            ret['scm_revision'] = ''
        return ret

    def validate(self, attrs):
        def get_field_from_model_or_attrs(fd):
            return attrs.get(fd, self.instance and getattr(self.instance, fd) or None)

        if 'allow_override' in attrs and self.instance:
            # case where user is turning off this project setting
            if self.instance.allow_override and not attrs['allow_override']:
                used_by = set(
                    JobTemplate.objects.filter(models.Q(project=self.instance), models.Q(ask_scm_branch_on_launch=True) | ~models.Q(scm_branch="")).values_list(
                        'pk', flat=True
                    )
                )
                if used_by:
                    raise serializers.ValidationError(
                        {
                            'allow_override': _('One or more job templates depend on branch override behavior for this project (ids: {}).').format(
                                ' '.join([str(pk) for pk in used_by])
                            )
                        }
                    )

        if get_field_from_model_or_attrs('scm_type') == '':
            for fd in ('scm_update_on_launch', 'scm_delete_on_update', 'scm_track_submodules', 'scm_clean'):
                if get_field_from_model_or_attrs(fd):
                    raise serializers.ValidationError({fd: _('Update options must be set to false for manual projects.')})
        return super(ProjectSerializer, self).validate(attrs)


class ProjectPlaybooksSerializer(ProjectSerializer):
    playbooks = serializers.SerializerMethodField(help_text=_('Array of playbooks available within this project.'))

    class Meta:
        model = Project
        fields = ('playbooks',)

    def get_playbooks(self, obj):
        return obj.playbook_files if obj.scm_type else obj.playbooks

    @property
    def data(self):
        ret = super(ProjectPlaybooksSerializer, self).data
        ret = ret.get('playbooks', [])
        return ReturnList(ret, serializer=self)


class ProjectInventoriesSerializer(ProjectSerializer):
    inventory_files = serializers.ReadOnlyField(help_text=_('Array of inventory files and directories available within this project, not comprehensive.'))

    class Meta:
        model = Project
        fields = ('inventory_files',)

    @property
    def data(self):
        ret = super(ProjectInventoriesSerializer, self).data
        ret = ret.get('inventory_files', [])
        return ReturnList(ret, serializer=self)


class ProjectUpdateViewSerializer(ProjectSerializer):
    can_update = serializers.BooleanField(read_only=True)

    class Meta:
        fields = ('can_update',)


class ProjectUpdateSerializer(UnifiedJobSerializer, ProjectOptionsSerializer):
    class Meta:
        model = ProjectUpdate
        fields = ('*', 'project', 'job_type', 'job_tags', '-controller_node')

    def get_related(self, obj):
        res = super(ProjectUpdateSerializer, self).get_related(obj)
        try:
            res.update(dict(project=self.reverse('api:project_detail', kwargs={'pk': obj.project.pk})))
        except ObjectDoesNotExist:
            pass
        res.update(
            dict(
                cancel=self.reverse('api:project_update_cancel', kwargs={'pk': obj.pk}),
                scm_inventory_updates=self.reverse('api:project_update_scm_inventory_updates', kwargs={'pk': obj.pk}),
                notifications=self.reverse('api:project_update_notifications_list', kwargs={'pk': obj.pk}),
                events=self.reverse('api:project_update_events_list', kwargs={'pk': obj.pk}),
            )
        )
        return res


class ProjectUpdateDetailSerializer(ProjectUpdateSerializer):
    playbook_counts = serializers.SerializerMethodField(help_text=_('A count of all plays and tasks for the job run.'))

    class Meta:
        model = ProjectUpdate
        fields = ('*', 'host_status_counts', 'playbook_counts')

    def get_playbook_counts(self, obj):
        task_count = obj.get_event_queryset().filter(event='playbook_on_task_start').count()
        play_count = obj.get_event_queryset().filter(event='playbook_on_play_start').count()

        data = {'play_count': play_count, 'task_count': task_count}

        return data


class ProjectUpdateListSerializer(ProjectUpdateSerializer, UnifiedJobListSerializer):
    class Meta:
        model = ProjectUpdate
        fields = ('*', '-controller_node')  # field removal undone by UJ serializer


class ProjectUpdateCancelSerializer(ProjectUpdateSerializer):
    can_cancel = serializers.BooleanField(read_only=True)

    class Meta:
        fields = ('can_cancel',)


class BaseSerializerWithVariables(BaseSerializer):
    def validate_variables(self, value):
        return vars_validate_or_raise(value)


class LabelsListMixin(object):
    def _summary_field_labels(self, obj):
        label_list = [{'id': x.id, 'name': x.name} for x in obj.labels.all()[:10]]
        if has_model_field_prefetched(obj, 'labels'):
            label_ct = len(obj.labels.all())
        else:
            if len(label_list) < 10:
                label_ct = len(label_list)
            else:
                label_ct = obj.labels.count()
        return {'count': label_ct, 'results': label_list}

    def get_summary_fields(self, obj):
        res = super(LabelsListMixin, self).get_summary_fields(obj)
        res['labels'] = self._summary_field_labels(obj)
        return res


class InventorySerializer(LabelsListMixin, BaseSerializerWithVariables):
    show_capabilities = ['edit', 'delete', 'adhoc', 'copy']
    capabilities_prefetch = ['admin', 'adhoc', {'copy': 'organization.inventory_admin'}]

    class Meta:
        model = Inventory
        fields = (
            '*',
            'organization',
            'kind',
            'host_filter',
            'variables',
            'has_active_failures',
            'total_hosts',
            'hosts_with_active_failures',
            'total_groups',
            'has_inventory_sources',
            'total_inventory_sources',
            'inventory_sources_with_failures',
            'pending_deletion',
            'prevent_instance_group_fallback',
        )

    def get_related(self, obj):
        res = super(InventorySerializer, self).get_related(obj)
        res.update(
            dict(
                hosts=self.reverse('api:inventory_hosts_list', kwargs={'pk': obj.pk}),
                variable_data=self.reverse('api:inventory_variable_data', kwargs={'pk': obj.pk}),
                script=self.reverse('api:inventory_script_view', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:inventory_activity_stream_list', kwargs={'pk': obj.pk}),
                job_templates=self.reverse('api:inventory_job_template_list', kwargs={'pk': obj.pk}),
                ad_hoc_commands=self.reverse('api:inventory_ad_hoc_commands_list', kwargs={'pk': obj.pk}),
                access_list=self.reverse('api:inventory_access_list', kwargs={'pk': obj.pk}),
                object_roles=self.reverse('api:inventory_object_roles_list', kwargs={'pk': obj.pk}),
                instance_groups=self.reverse('api:inventory_instance_groups_list', kwargs={'pk': obj.pk}),
                copy=self.reverse('api:inventory_copy', kwargs={'pk': obj.pk}),
                labels=self.reverse('api:inventory_label_list', kwargs={'pk': obj.pk}),
            )
        )
        if obj.kind in ('', 'constructed'):
            # links not relevant for the "old" smart inventory
            res['groups'] = self.reverse('api:inventory_groups_list', kwargs={'pk': obj.pk})
            res['root_groups'] = self.reverse('api:inventory_root_groups_list', kwargs={'pk': obj.pk})
            res['update_inventory_sources'] = self.reverse('api:inventory_inventory_sources_update', kwargs={'pk': obj.pk})
            res['inventory_sources'] = self.reverse('api:inventory_inventory_sources_list', kwargs={'pk': obj.pk})
            res['tree'] = self.reverse('api:inventory_tree_view', kwargs={'pk': obj.pk})
        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})
        if obj.kind == 'constructed':
            res['input_inventories'] = self.reverse('api:inventory_input_inventories', kwargs={'pk': obj.pk})
            res['constructed_url'] = self.reverse('api:constructed_inventory_detail', kwargs={'pk': obj.pk})
        return res

    def to_representation(self, obj):
        ret = super(InventorySerializer, self).to_representation(obj)
        if obj is not None and 'organization' in ret and not obj.organization:
            ret['organization'] = None
        return ret

    def validate_host_filter(self, host_filter):
        if host_filter:
            try:
                for match in models.JSONField.get_lookups().keys():
                    if match == 'exact':
                        # __exact is allowed
                        continue
                    match = '__{}'.format(match)
                    if re.match('ansible_facts[^=]+{}='.format(match), host_filter):
                        raise models.base.ValidationError({'host_filter': 'ansible_facts does not support searching with {}'.format(match)})
                SmartFilter().query_from_string(host_filter)
            except RuntimeError as e:
                raise models.base.ValidationError(str(e))
        return host_filter

    def validate(self, attrs):
        kind = None
        if 'kind' in attrs:
            kind = attrs['kind']
        elif self.instance:
            kind = self.instance.kind

        host_filter = None
        if 'host_filter' in attrs:
            host_filter = attrs['host_filter']
        elif self.instance:
            host_filter = self.instance.host_filter

        if kind == 'smart' and not host_filter:
            raise serializers.ValidationError({'host_filter': _('Smart inventories must specify host_filter')})
        return super(InventorySerializer, self).validate(attrs)


class ConstructedFieldMixin(serializers.Field):
    def get_attribute(self, instance):
        if not hasattr(instance, '_constructed_inv_src'):
            instance._constructed_inv_src = instance.inventory_sources.first()
        inv_src = instance._constructed_inv_src
        return super().get_attribute(inv_src)  # yoink


class ConstructedCharField(ConstructedFieldMixin, serializers.CharField):
    pass


class ConstructedIntegerField(ConstructedFieldMixin, serializers.IntegerField):
    pass


class ConstructedInventorySerializer(InventorySerializer):
    source_vars = ConstructedCharField(
        required=False,
        default=None,
        allow_blank=True,
        help_text=_('The source_vars for the related auto-created inventory source, special to constructed inventory.'),
    )
    update_cache_timeout = ConstructedIntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        default=None,
        help_text=_('The cache timeout for the related auto-created inventory source, special to constructed inventory'),
    )
    limit = ConstructedCharField(
        required=False,
        default=None,
        allow_blank=True,
        help_text=_('The limit to restrict the returned hosts for the related auto-created inventory source, special to constructed inventory.'),
    )
    verbosity = ConstructedIntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=5,
        default=None,
        help_text=_('The verbosity level for the related auto-created inventory source, special to constructed inventory'),
    )

    class Meta:
        model = Inventory
        fields = ('*', '-host_filter') + CONSTRUCTED_INVENTORY_SOURCE_EDITABLE_FIELDS
        read_only_fields = ('*', 'kind')

    def pop_inv_src_data(self, data):
        inv_src_data = {}
        for field in CONSTRUCTED_INVENTORY_SOURCE_EDITABLE_FIELDS:
            if field in data:
                # values always need to be removed, as they are not valid for Inventory model
                value = data.pop(field)
                # null is not valid for any of those fields, taken as not-provided
                if value is not None:
                    inv_src_data[field] = value
        return inv_src_data

    def apply_inv_src_data(self, inventory, inv_src_data):
        if inv_src_data:
            update_fields = []
            inv_src = inventory.inventory_sources.first()
            for field, value in inv_src_data.items():
                setattr(inv_src, field, value)
                update_fields.append(field)
            if update_fields:
                inv_src.save(update_fields=update_fields)

    def create(self, validated_data):
        validated_data['kind'] = 'constructed'
        inv_src_data = self.pop_inv_src_data(validated_data)
        inventory = super().create(validated_data)
        self.apply_inv_src_data(inventory, inv_src_data)
        return inventory

    def update(self, obj, validated_data):
        inv_src_data = self.pop_inv_src_data(validated_data)
        obj = super().update(obj, validated_data)
        self.apply_inv_src_data(obj, inv_src_data)
        return obj


class InventoryScriptSerializer(InventorySerializer):
    class Meta:
        fields = ()


class HostSerializer(BaseSerializerWithVariables):
    show_capabilities = ['edit', 'delete']
    capabilities_prefetch = ['inventory.admin']

    has_active_failures = serializers.SerializerMethodField()
    has_inventory_sources = serializers.SerializerMethodField()

    class Meta:
        model = Host
        fields = (
            '*',
            'inventory',
            'enabled',
            'instance_id',
            'variables',
            'has_active_failures',
            'has_inventory_sources',
            'last_job',
            'last_job_host_summary',
            'ansible_facts_modified',
        )
        read_only_fields = ('last_job', 'last_job_host_summary', 'ansible_facts_modified')

    def build_relational_field(self, field_name, relation_info):
        field_class, field_kwargs = super(HostSerializer, self).build_relational_field(field_name, relation_info)
        # Inventory is read-only unless creating a new host.
        if self.instance and field_name == 'inventory':
            field_kwargs['read_only'] = True
            field_kwargs.pop('queryset', None)
        return field_class, field_kwargs

    def get_related(self, obj):
        res = super(HostSerializer, self).get_related(obj)
        res.update(
            dict(
                variable_data=self.reverse('api:host_variable_data', kwargs={'pk': obj.pk}),
                groups=self.reverse('api:host_groups_list', kwargs={'pk': obj.pk}),
                all_groups=self.reverse('api:host_all_groups_list', kwargs={'pk': obj.pk}),
                job_events=self.reverse('api:host_job_events_list', kwargs={'pk': obj.pk}),
                job_host_summaries=self.reverse('api:host_job_host_summaries_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:host_activity_stream_list', kwargs={'pk': obj.pk}),
                inventory_sources=self.reverse('api:host_inventory_sources_list', kwargs={'pk': obj.pk}),
                smart_inventories=self.reverse('api:host_smart_inventories_list', kwargs={'pk': obj.pk}),
                ad_hoc_commands=self.reverse('api:host_ad_hoc_commands_list', kwargs={'pk': obj.pk}),
                ad_hoc_command_events=self.reverse('api:host_ad_hoc_command_events_list', kwargs={'pk': obj.pk}),
                ansible_facts=self.reverse('api:host_ansible_facts_detail', kwargs={'pk': obj.pk}),
            )
        )
        if obj.inventory.kind == 'constructed':
            res['original_host'] = self.reverse('api:host_detail', kwargs={'pk': obj.instance_id})
            res['ansible_facts'] = self.reverse('api:host_ansible_facts_detail', kwargs={'pk': obj.instance_id})
        if obj.inventory:
            res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory.pk})
        if obj.last_job:
            res['last_job'] = self.reverse('api:job_detail', kwargs={'pk': obj.last_job.pk})
        if obj.last_job_host_summary:
            res['last_job_host_summary'] = self.reverse('api:job_host_summary_detail', kwargs={'pk': obj.last_job_host_summary.pk})
        return res

    def get_summary_fields(self, obj):
        d = super(HostSerializer, self).get_summary_fields(obj)
        try:
            d['last_job']['job_template_id'] = obj.last_job.job_template.id
            d['last_job']['job_template_name'] = obj.last_job.job_template.name
        except (KeyError, AttributeError):
            pass
        if has_model_field_prefetched(obj, 'groups'):
            group_list = sorted([{'id': g.id, 'name': g.name} for g in obj.groups.all()], key=lambda x: x['id'])[:5]
        else:
            group_list = [{'id': g.id, 'name': g.name} for g in obj.groups.all().order_by('id')[:5]]
        group_cnt = obj.groups.count()
        d.setdefault('groups', {'count': group_cnt, 'results': group_list})
        if obj.inventory.kind == 'constructed':
            summaries_qs = obj.constructed_host_summaries
        else:
            summaries_qs = obj.job_host_summaries
        d.setdefault(
            'recent_jobs',
            [
                {
                    'id': j.job.id,
                    'name': j.job.job_template.name if j.job.job_template is not None else "",
                    'type': j.job.job_type_name,
                    'status': j.job.status,
                    'finished': j.job.finished,
                }
                for j in summaries_qs.select_related('job__job_template').order_by('-created').defer('job__extra_vars', 'job__artifacts')[:5]
            ],
        )
        return d

    def _get_host_port_from_name(self, name):
        # Allow hostname (except IPv6 for now) to specify the port # inline.
        port = None
        if name.count(':') == 1:
            name, port = name.split(':')
            try:
                port = int(port)
                if port < 1 or port > 65535:
                    raise ValueError
            except ValueError:
                raise serializers.ValidationError(_(u'Invalid port specification: %s') % force_str(port))
        return name, port

    def validate_name(self, value):
        name = force_str(value or '')
        # Validate here only, update in main validate method.
        host, port = self._get_host_port_from_name(name)
        return value

    def validate_inventory(self, value):
        if value.kind in ('constructed', 'smart'):
            raise serializers.ValidationError({"detail": _("Cannot create Host for Smart or Constructed Inventories")})
        return value

    def validate_variables(self, value):
        return vars_validate_or_raise(value)

    def validate(self, attrs):
        name = force_str(attrs.get('name', self.instance and self.instance.name or ''))
        inventory = attrs.get('inventory', self.instance and self.instance.inventory or '')
        host, port = self._get_host_port_from_name(name)

        if port:
            attrs['name'] = host
            variables = force_str(attrs.get('variables', self.instance and self.instance.variables or ''))
            vars_dict = parse_yaml_or_json(variables)
            vars_dict['ansible_ssh_port'] = port
            attrs['variables'] = json.dumps(vars_dict)
        if inventory and Group.objects.filter(name=name, inventory=inventory).exists():
            raise serializers.ValidationError(_('A Group with that name already exists.'))

        return super(HostSerializer, self).validate(attrs)

    def to_representation(self, obj):
        ret = super(HostSerializer, self).to_representation(obj)
        if not obj:
            return ret
        if 'inventory' in ret and not obj.inventory:
            ret['inventory'] = None
        if 'last_job' in ret and not obj.last_job:
            ret['last_job'] = None
        if 'last_job_host_summary' in ret and not obj.last_job_host_summary:
            ret['last_job_host_summary'] = None
        return ret

    def get_has_active_failures(self, obj):
        return bool(obj.last_job_host_summary and obj.last_job_host_summary.failed)

    def get_has_inventory_sources(self, obj):
        return obj.inventory_sources.exists()


class AnsibleFactsSerializer(BaseSerializer):
    class Meta:
        model = Host

    def to_representation(self, obj):
        return obj.ansible_facts


class GroupSerializer(BaseSerializerWithVariables):
    show_capabilities = ['copy', 'edit', 'delete']
    capabilities_prefetch = ['inventory.admin', 'inventory.adhoc']

    class Meta:
        model = Group
        fields = ('*', 'inventory', 'variables')

    def build_relational_field(self, field_name, relation_info):
        field_class, field_kwargs = super(GroupSerializer, self).build_relational_field(field_name, relation_info)
        # Inventory is read-only unless creating a new group.
        if self.instance and field_name == 'inventory':
            field_kwargs['read_only'] = True
            field_kwargs.pop('queryset', None)
        return field_class, field_kwargs

    def get_related(self, obj):
        res = super(GroupSerializer, self).get_related(obj)
        res.update(
            dict(
                variable_data=self.reverse('api:group_variable_data', kwargs={'pk': obj.pk}),
                hosts=self.reverse('api:group_hosts_list', kwargs={'pk': obj.pk}),
                potential_children=self.reverse('api:group_potential_children_list', kwargs={'pk': obj.pk}),
                children=self.reverse('api:group_children_list', kwargs={'pk': obj.pk}),
                all_hosts=self.reverse('api:group_all_hosts_list', kwargs={'pk': obj.pk}),
                job_events=self.reverse('api:group_job_events_list', kwargs={'pk': obj.pk}),
                job_host_summaries=self.reverse('api:group_job_host_summaries_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:group_activity_stream_list', kwargs={'pk': obj.pk}),
                inventory_sources=self.reverse('api:group_inventory_sources_list', kwargs={'pk': obj.pk}),
                ad_hoc_commands=self.reverse('api:group_ad_hoc_commands_list', kwargs={'pk': obj.pk}),
            )
        )
        if obj.inventory:
            res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory.pk})
        return res

    def validate(self, attrs):
        name = force_str(attrs.get('name', self.instance and self.instance.name or ''))
        inventory = attrs.get('inventory', self.instance and self.instance.inventory or '')
        if Host.objects.filter(name=name, inventory=inventory).exists():
            raise serializers.ValidationError(_('A Host with that name already exists.'))
        return super(GroupSerializer, self).validate(attrs)

    def validate_name(self, value):
        if value in ('all', '_meta'):
            raise serializers.ValidationError(_('Invalid group name.'))
        return value

    def validate_inventory(self, value):
        if value.kind in ('constructed', 'smart'):
            raise serializers.ValidationError({"detail": _("Cannot create Group for Smart or Constructed Inventories")})
        return value

    def to_representation(self, obj):
        ret = super(GroupSerializer, self).to_representation(obj)
        if obj is not None and 'inventory' in ret and not obj.inventory:
            ret['inventory'] = None
        return ret


class BulkHostSerializer(HostSerializer):
    class Meta:
        model = Host
        fields = (
            'name',
            'enabled',
            'instance_id',
            'description',
            'variables',
        )


class BulkHostCreateSerializer(serializers.Serializer):
    inventory = serializers.PrimaryKeyRelatedField(
        queryset=Inventory.objects.all(), required=True, write_only=True, help_text=_('Primary Key ID of inventory to add hosts to.')
    )
    hosts = serializers.ListField(
        child=BulkHostSerializer(),
        allow_empty=False,
        max_length=100000,
        write_only=True,
        help_text=_('List of hosts to be created, JSON. e.g. [{"name": "example.com"}, {"name": "127.0.0.1"}]'),
    )

    class Meta:
        model = Inventory
        fields = ('inventory', 'hosts')
        read_only_fields = ()

    def raise_if_host_counts_violated(self, attrs):
        validation_info = get_licenser().validate()

        org = attrs['inventory'].organization

        if org:
            org_active_count = Host.objects.org_active_count(org.id)
            new_hosts = [h['name'] for h in attrs['hosts']]
            org_net_new_host_count = len(new_hosts) - Host.objects.filter(inventory__organization=1, name__in=new_hosts).values('name').distinct().count()
            if org.max_hosts > 0 and org_active_count + org_net_new_host_count > org.max_hosts:
                raise PermissionDenied(
                    _(
                        "You have already reached the maximum number of %s hosts"
                        " allowed for your organization. Contact your System Administrator"
                        " for assistance." % org.max_hosts
                    )
                )

            # Don't check license if it is open license
        if validation_info.get('license_type', 'UNLICENSED') == 'open':
            return

        sys_free_instances = validation_info.get('free_instances', 0)
        system_net_new_host_count = Host.objects.exclude(name__in=new_hosts).count()

        if system_net_new_host_count > sys_free_instances:
            hard_error = validation_info.get('trial', False) is True or validation_info['instance_count'] == 10
            if hard_error:
                # Only raise permission error for trial, otherwise just log a warning as we do in other inventory import situations
                raise PermissionDenied(_("Host count exceeds available instances."))
            logger.warning(_("Number of hosts allowed by license has been exceeded."))

    def validate(self, attrs):
        request = self.context.get('request', None)
        inv = attrs['inventory']
        if inv.kind != '':
            raise serializers.ValidationError(_('Hosts can only be created in manual inventories (not smart or constructed types).'))
        if len(attrs['hosts']) > settings.BULK_HOST_MAX_CREATE:
            raise serializers.ValidationError(_('Number of hosts exceeds system setting BULK_HOST_MAX_CREATE'))
        if request and not request.user.is_superuser:
            if request.user not in inv.admin_role:
                raise serializers.ValidationError(_(f'Inventory with id {inv.id} not found or lack permissions to add hosts.'))
        current_hostnames = set(inv.hosts.values_list('name', flat=True))
        new_names = [host['name'] for host in attrs['hosts']]
        duplicate_new_names = [n for n in new_names if n in current_hostnames or new_names.count(n) > 1]
        if duplicate_new_names:
            raise serializers.ValidationError(_(f'Hostnames must be unique in an inventory. Duplicates found: {duplicate_new_names}'))

        self.raise_if_host_counts_violated(attrs)

        _now = now()
        for host in attrs['hosts']:
            host['created'] = _now
            host['modified'] = _now
            host['inventory'] = inv
        return attrs

    def create(self, validated_data):
        # This assumes total_hosts is up to date, and it can get out of date if the inventory computed fields have not been updated lately.
        # If we wanted to side step this we could query Hosts.objects.filter(inventory...)
        old_total_hosts = validated_data['inventory'].total_hosts
        result = [Host(**attrs) for attrs in validated_data['hosts']]
        try:
            Host.objects.bulk_create(result)
        except Exception as e:
            raise serializers.ValidationError({"detail": _(f"cannot create host, host creation error {e}")})
        new_total_hosts = old_total_hosts + len(result)
        request = self.context.get('request', None)
        changes = {'total_hosts': [old_total_hosts, new_total_hosts]}
        activity_entry = ActivityStream.objects.create(
            operation='update',
            object1='inventory',
            changes=json.dumps(changes),
            actor=request.user,
        )
        activity_entry.inventory.add(validated_data['inventory'])

        # This actually updates the cached "total_hosts" field on the inventory
        update_inventory_computed_fields.delay(validated_data['inventory'].id)
        return_keys = [k for k in BulkHostSerializer().fields.keys()] + ['id']
        return_data = {}
        host_data = []
        for r in result:
            item = {k: getattr(r, k) for k in return_keys}
            if settings.DATABASES and ('sqlite3' not in settings.DATABASES.get('default', {}).get('ENGINE')):
                # sqlite acts different with bulk_create -- it doesn't return the id of the objects
                # to get it, you have to do an additional query, which is not useful for our tests
                item['url'] = reverse('api:host_detail', kwargs={'pk': r.id})
            item['inventory'] = reverse('api:inventory_detail', kwargs={'pk': validated_data['inventory'].id})
            host_data.append(item)
        return_data['url'] = reverse('api:inventory_detail', kwargs={'pk': validated_data['inventory'].id})
        return_data['hosts'] = host_data
        return return_data


class BulkHostDeleteSerializer(serializers.Serializer):
    hosts = serializers.ListField(
        allow_empty=False,
        max_length=100000,
        write_only=True,
        help_text=_('List of hosts ids to be deleted, e.g. [105, 130, 131, 200]'),
    )

    class Meta:
        model = Host
        fields = ('hosts',)

    def validate(self, attrs):
        request = self.context.get('request', None)
        max_hosts = settings.BULK_HOST_MAX_DELETE
        # Validating the number of hosts to be deleted
        if len(attrs['hosts']) > max_hosts:
            raise serializers.ValidationError(
                {
                    "ERROR": 'Number of hosts exceeds system setting BULK_HOST_MAX_DELETE',
                    "BULK_HOST_MAX_DELETE": max_hosts,
                    "Hosts_count": len(attrs['hosts']),
                }
            )

        # Getting list of all host objects, filtered by the list of the hosts to delete
        attrs['host_qs'] = Host.objects.get_queryset().filter(pk__in=attrs['hosts']).only('id', 'inventory_id', 'name')

        # Converting the queryset data in a dict. to reduce the number of queries when
        # manipulating the data
        attrs['hosts_data'] = attrs['host_qs'].values()

        if len(attrs['host_qs']) == 0:
            error_hosts = {host: "Hosts do not exist or you lack permission to delete it" for host in attrs['hosts']}
            raise serializers.ValidationError({'hosts': error_hosts})

        if len(attrs['host_qs']) < len(attrs['hosts']):
            hosts_exists = [host['id'] for host in attrs['hosts_data']]
            failed_hosts = list(set(attrs['hosts']).difference(hosts_exists))
            error_hosts = {host: "Hosts do not exist or you lack permission to delete it" for host in failed_hosts}
            raise serializers.ValidationError({'hosts': error_hosts})

        # Getting all inventories that the hosts can be in
        inv_list = list(set([host['inventory_id'] for host in attrs['hosts_data']]))

        # Checking that the user have permission to all inventories
        errors = dict()
        for inv in Inventory.objects.get_queryset().filter(pk__in=inv_list):
            if request and not request.user.is_superuser:
                if request.user not in inv.admin_role:
                    errors[inv.name] = "Lack permissions to delete hosts from this inventory."
        if errors != {}:
            raise PermissionDenied({"inventories": errors})

        # check the inventory type only if the user have permission to it.
        errors = dict()
        for inv in Inventory.objects.get_queryset().filter(pk__in=inv_list):
            if inv.kind != '':
                errors[inv.name] = "Hosts can only be deleted from manual inventories."
        if errors != {}:
            raise serializers.ValidationError({"inventories": errors})
        attrs['inventories'] = inv_list
        return attrs

    def delete(self, validated_data):
        result = {"hosts": dict()}
        changes = {'deleted_hosts': dict()}
        for inventory in validated_data['inventories']:
            changes['deleted_hosts'][inventory] = list()

        for host in validated_data['hosts_data']:
            result["hosts"][host["id"]] = f"The host {host['name']} was deleted"
            changes['deleted_hosts'][host["inventory_id"]].append({"host_id": host["id"], "host_name": host["name"]})

        try:
            validated_data['host_qs'].delete()
        except Exception as e:
            raise serializers.ValidationError({"detail": _(f"cannot delete hosts, host deletion error {e}")})

        request = self.context.get('request', None)

        for inventory in validated_data['inventories']:
            activity_entry = ActivityStream.objects.create(
                operation='update',
                object1='inventory',
                changes=json.dumps(changes['deleted_hosts'][inventory]),
                actor=request.user,
            )
            activity_entry.inventory.add(inventory)

        return result


class GroupTreeSerializer(GroupSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = ('*', 'children')

    def get_children(self, obj):
        if obj is None:
            return {}
        children_qs = obj.children
        children_qs = children_qs.select_related('inventory')
        children_qs = children_qs.prefetch_related('inventory_source')
        return GroupTreeSerializer(children_qs, many=True).data


class BaseVariableDataSerializer(BaseSerializer):
    class Meta:
        fields = ('variables',)

    def to_representation(self, obj):
        if obj is None:
            return {}
        ret = super(BaseVariableDataSerializer, self).to_representation(obj)
        return parse_yaml_or_json(ret.get('variables', '') or '{}')

    def to_internal_value(self, data):
        data = {'variables': json.dumps(data)}
        return super(BaseVariableDataSerializer, self).to_internal_value(data)


class InventoryVariableDataSerializer(BaseVariableDataSerializer):
    class Meta:
        model = Inventory


class HostVariableDataSerializer(BaseVariableDataSerializer):
    class Meta:
        model = Host


class GroupVariableDataSerializer(BaseVariableDataSerializer):
    class Meta:
        model = Group


class InventorySourceOptionsSerializer(BaseSerializer):
    credential = DeprecatedCredentialField(help_text=_('Cloud credential to use for inventory updates.'))
    source = serializers.ChoiceField(choices=[])

    class Meta:
        fields = (
            '*',
            'source',
            'source_path',
            'source_vars',
            'scm_branch',
            'credential',
            'enabled_var',
            'enabled_value',
            'host_filter',
            'overwrite',
            'overwrite_vars',
            'custom_virtualenv',
            'timeout',
            'verbosity',
            'limit',
        )
        read_only_fields = ('*', 'custom_virtualenv')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if 'source' in self.fields:
            source_options = load_combined_inventory_source_options()

            self.fields['source'].choices = [(plugin, description) for plugin, description in source_options.items()]

    def get_related(self, obj):
        res = super(InventorySourceOptionsSerializer, self).get_related(obj)
        if obj.credential:  # TODO: remove when 'credential' field is removed
            res['credential'] = self.reverse('api:credential_detail', kwargs={'pk': obj.credential})
        return res

    def validate_source_vars(self, value):
        ret = vars_validate_or_raise(value)
        for env_k in parse_yaml_or_json(value):
            if env_k in settings.INV_ENV_VARIABLE_BLOCKED:
                raise serializers.ValidationError(_("`{}` is a prohibited environment variable".format(env_k)))
        return ret

    # TODO: remove when old 'credential' fields are removed
    def get_summary_fields(self, obj):
        summary_fields = super(InventorySourceOptionsSerializer, self).get_summary_fields(obj)
        all_creds = []
        if 'credential' in summary_fields:
            cred = obj.get_cloud_credential()
            if cred:
                summarized_cred = {'id': cred.id, 'name': cred.name, 'description': cred.description, 'kind': cred.kind, 'cloud': True}
                summary_fields['credential'] = summarized_cred
                all_creds.append(summarized_cred)
                summary_fields['credential']['credential_type_id'] = cred.credential_type_id
            else:
                summary_fields.pop('credential')
        summary_fields['credentials'] = all_creds
        return summary_fields


class InventorySourceSerializer(UnifiedJobTemplateSerializer, InventorySourceOptionsSerializer):
    status = serializers.ChoiceField(choices=InventorySource.INVENTORY_SOURCE_STATUS_CHOICES, read_only=True)
    last_update_failed = serializers.BooleanField(read_only=True)
    last_updated = serializers.DateTimeField(read_only=True)
    show_capabilities = ['start', 'schedule', 'edit', 'delete']
    capabilities_prefetch = [{'admin': 'inventory.admin'}, {'start': 'inventory.update'}]

    class Meta:
        model = InventorySource
        fields = ('*', 'name', 'inventory', 'update_on_launch', 'update_cache_timeout', 'source_project') + (
            'last_update_failed',
            'last_updated',
        )  # Backwards compatibility.
        extra_kwargs = {'inventory': {'required': True}}

    def get_related(self, obj):
        res = super(InventorySourceSerializer, self).get_related(obj)
        res.update(
            dict(
                update=self.reverse('api:inventory_source_update_view', kwargs={'pk': obj.pk}),
                inventory_updates=self.reverse('api:inventory_source_updates_list', kwargs={'pk': obj.pk}),
                schedules=self.reverse('api:inventory_source_schedules_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:inventory_source_activity_stream_list', kwargs={'pk': obj.pk}),
                hosts=self.reverse('api:inventory_source_hosts_list', kwargs={'pk': obj.pk}),
                groups=self.reverse('api:inventory_source_groups_list', kwargs={'pk': obj.pk}),
                notification_templates_started=self.reverse('api:inventory_source_notification_templates_started_list', kwargs={'pk': obj.pk}),
                notification_templates_success=self.reverse('api:inventory_source_notification_templates_success_list', kwargs={'pk': obj.pk}),
                notification_templates_error=self.reverse('api:inventory_source_notification_templates_error_list', kwargs={'pk': obj.pk}),
            )
        )
        if obj.inventory:
            res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory.pk})
        if obj.source_project_id is not None:
            res['source_project'] = self.reverse('api:project_detail', kwargs={'pk': obj.source_project.pk})
        # Backwards compatibility.
        if obj.current_update:
            res['current_update'] = self.reverse('api:inventory_update_detail', kwargs={'pk': obj.current_update.pk})
        if obj.last_update:
            res['last_update'] = self.reverse('api:inventory_update_detail', kwargs={'pk': obj.last_update.pk})
        else:
            res['credentials'] = self.reverse('api:inventory_source_credentials_list', kwargs={'pk': obj.pk})
        return res

    def build_relational_field(self, field_name, relation_info):
        field_class, field_kwargs = super(InventorySourceSerializer, self).build_relational_field(field_name, relation_info)
        # SCM Project and inventory are read-only unless creating a new inventory.
        if self.instance and field_name == 'inventory':
            field_kwargs['read_only'] = True
            field_kwargs.pop('queryset', None)
        return field_class, field_kwargs

    # TODO: remove when old 'credential' fields are removed
    def build_field(self, field_name, info, model_class, nested_depth):
        # have to special-case the field so that DRF will not automagically make it
        # read-only because it's a property on the model.
        if field_name == 'credential':
            return self.build_standard_field(field_name, self.credential)
        return super(InventorySourceOptionsSerializer, self).build_field(field_name, info, model_class, nested_depth)

    def to_representation(self, obj):
        ret = super(InventorySourceSerializer, self).to_representation(obj)
        if obj is None:
            return ret
        if 'inventory' in ret and not obj.inventory:
            ret['inventory'] = None
        return ret

    def validate_source_project(self, value):
        if value and value.scm_type == '':
            raise serializers.ValidationError(_("Cannot use manual project for SCM-based inventory."))
        return value

    def validate_inventory(self, value):
        if value and value.kind in ('constructed', 'smart'):
            raise serializers.ValidationError({"detail": _("Cannot create Inventory Source for Smart or Constructed Inventories")})
        return value

    # TODO: remove when old 'credential' fields are removed
    def create(self, validated_data):
        deprecated_fields = {}
        if 'credential' in validated_data:
            deprecated_fields['credential'] = validated_data.pop('credential')
        obj = super(InventorySourceSerializer, self).create(validated_data)
        if deprecated_fields:
            self._update_deprecated_fields(deprecated_fields, obj)
        return obj

    # TODO: remove when old 'credential' fields are removed
    def update(self, obj, validated_data):
        deprecated_fields = {}
        if 'credential' in validated_data:
            deprecated_fields['credential'] = validated_data.pop('credential')
        obj = super(InventorySourceSerializer, self).update(obj, validated_data)
        if deprecated_fields:
            self._update_deprecated_fields(deprecated_fields, obj)
        return obj

    # TODO: remove when old 'credential' fields are removed
    def _update_deprecated_fields(self, fields, obj):
        if 'credential' in fields:
            new_cred = fields['credential']
            existing = obj.credentials.all()
            if new_cred not in existing:
                for cred in existing:
                    # Remove all other cloud credentials
                    obj.credentials.remove(cred)
                if new_cred:
                    # Add new credential
                    obj.credentials.add(new_cred)

    def validate(self, attrs):
        deprecated_fields = {}
        if 'credential' in attrs:  # TODO: remove when 'credential' field removed
            deprecated_fields['credential'] = attrs.pop('credential')

        def get_field_from_model_or_attrs(fd):
            return attrs.get(fd, self.instance and getattr(self.instance, fd) or None)

        if self.instance and self.instance.source == 'constructed':
            allowed_fields = CONSTRUCTED_INVENTORY_SOURCE_EDITABLE_FIELDS
            for field in attrs:
                if attrs[field] != getattr(self.instance, field) and field not in allowed_fields:
                    raise serializers.ValidationError({"error": _("Cannot change field '{}' on a constructed inventory source.").format(field)})
        elif get_field_from_model_or_attrs('source') == 'scm':
            if ('source' in attrs or 'source_project' in attrs) and get_field_from_model_or_attrs('source_project') is None:
                raise serializers.ValidationError({"source_project": _("Project required for scm type sources.")})
        elif get_field_from_model_or_attrs('source') == 'constructed':
            raise serializers.ValidationError({"error": _('constructed not a valid source for inventory')})
        else:
            redundant_scm_fields = list(filter(lambda x: attrs.get(x, None), ['source_project', 'source_path', 'scm_branch']))
            if redundant_scm_fields:
                raise serializers.ValidationError({"detail": _("Cannot set %s if not SCM type." % ' '.join(redundant_scm_fields))})

        project = get_field_from_model_or_attrs('source_project')
        if get_field_from_model_or_attrs('scm_branch') and not project.allow_override:
            raise serializers.ValidationError({'scm_branch': _('Project does not allow overriding branch.')})

        attrs = super(InventorySourceSerializer, self).validate(attrs)

        # Check type consistency of source and cloud credential, if provided
        if 'credential' in deprecated_fields:  # TODO: remove when v2 API is deprecated
            cred = deprecated_fields['credential']
            attrs['credential'] = cred
            if cred is not None:
                cred = Credential.objects.get(pk=cred)
                view = self.context.get('view', None)
                if (not view) or (not view.request) or (view.request.user not in cred.use_role):
                    raise PermissionDenied()
            cred_error = InventorySource.cloud_credential_validation(get_field_from_model_or_attrs('source'), cred)
            if cred_error:
                raise serializers.ValidationError({"credential": cred_error})

        return attrs


class InventorySourceUpdateSerializer(InventorySourceSerializer):
    can_update = serializers.BooleanField(read_only=True)

    class Meta:
        fields = ('can_update',)

    def validate(self, attrs):
        project = self.instance.source_project
        if project:
            failed_reason = project.get_reason_if_failed()
            if failed_reason:
                raise serializers.ValidationError(failed_reason)

        return super(InventorySourceUpdateSerializer, self).validate(attrs)


class InventoryUpdateSerializer(UnifiedJobSerializer, InventorySourceOptionsSerializer):
    custom_virtualenv = serializers.ReadOnlyField()

    class Meta:
        model = InventoryUpdate
        fields = (
            '*',
            'inventory',
            'inventory_source',
            'license_error',
            'org_host_limit_error',
            'source_project_update',
            'custom_virtualenv',
            'instance_group',
            'scm_revision',
        )

    def get_related(self, obj):
        res = super(InventoryUpdateSerializer, self).get_related(obj)
        try:
            res.update(dict(inventory_source=self.reverse('api:inventory_source_detail', kwargs={'pk': obj.inventory_source.pk})))
        except ObjectDoesNotExist:
            pass
        res.update(
            dict(
                cancel=self.reverse('api:inventory_update_cancel', kwargs={'pk': obj.pk}),
                notifications=self.reverse('api:inventory_update_notifications_list', kwargs={'pk': obj.pk}),
                events=self.reverse('api:inventory_update_events_list', kwargs={'pk': obj.pk}),
            )
        )
        if obj.source_project_update_id:
            res['source_project_update'] = self.reverse('api:project_update_detail', kwargs={'pk': obj.source_project_update.pk})
        if obj.inventory:
            res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory.pk})

        res['credentials'] = self.reverse('api:inventory_update_credentials_list', kwargs={'pk': obj.pk})

        return res


class InventoryUpdateDetailSerializer(InventoryUpdateSerializer):
    source_project = serializers.SerializerMethodField(help_text=_('The project used for this job.'), method_name='get_source_project_id')

    class Meta:
        model = InventoryUpdate
        fields = ('*', 'source_project')

    def get_source_project(self, obj):
        return getattrd(obj, 'source_project_update.unified_job_template', None)

    def get_source_project_id(self, obj):
        return getattrd(obj, 'source_project_update.unified_job_template.id', None)

    def get_related(self, obj):
        res = super(InventoryUpdateDetailSerializer, self).get_related(obj)
        source_project_id = self.get_source_project_id(obj)

        if source_project_id:
            res['source_project'] = self.reverse('api:project_detail', kwargs={'pk': source_project_id})
        return res

    def get_summary_fields(self, obj):
        summary_fields = super(InventoryUpdateDetailSerializer, self).get_summary_fields(obj)

        source_project = self.get_source_project(obj)
        if source_project:
            summary_fields['source_project'] = {}
            for field in SUMMARIZABLE_FK_FIELDS['project']:
                value = getattr(source_project, field, None)
                if value is not None:
                    summary_fields['source_project'][field] = value

        cred = obj.credentials.first()
        if cred:
            summary_fields['credential'] = {
                'id': cred.pk,
                'name': cred.name,
                'description': cred.description,
                'kind': cred.kind,
                'cloud': cred.credential_type.kind == 'cloud',
            }

        return summary_fields


class InventoryUpdateListSerializer(InventoryUpdateSerializer, UnifiedJobListSerializer):
    class Meta:
        model = InventoryUpdate


class InventoryUpdateCancelSerializer(InventoryUpdateSerializer):
    can_cancel = serializers.BooleanField(read_only=True)

    class Meta:
        fields = ('can_cancel',)


class TeamSerializer(BaseSerializer):
    show_capabilities = ['edit', 'delete']

    class Meta:
        model = Team
        fields = ('*', 'organization')

    def get_related(self, obj):
        res = super(TeamSerializer, self).get_related(obj)
        res.update(
            dict(
                projects=self.reverse('api:team_projects_list', kwargs={'pk': obj.pk}),
                users=self.reverse('api:team_users_list', kwargs={'pk': obj.pk}),
                credentials=self.reverse('api:team_credentials_list', kwargs={'pk': obj.pk}),
                roles=self.reverse('api:team_roles_list', kwargs={'pk': obj.pk}),
                object_roles=self.reverse('api:team_object_roles_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:team_activity_stream_list', kwargs={'pk': obj.pk}),
                access_list=self.reverse('api:team_access_list', kwargs={'pk': obj.pk}),
            )
        )
        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})
        return res

    def to_representation(self, obj):
        ret = super(TeamSerializer, self).to_representation(obj)
        if obj is not None and 'organization' in ret and not obj.organization:
            ret['organization'] = None
        return ret


class RoleSerializer(BaseSerializer):
    class Meta:
        model = Role
        fields = ('*', '-created', '-modified')
        read_only_fields = ('id', 'role_field', 'description', 'name')

    def to_representation(self, obj):
        ret = super(RoleSerializer, self).to_representation(obj)

        if obj.object_id:
            content_object = obj.content_object
            if hasattr(content_object, 'username'):
                ret['summary_fields']['resource_name'] = obj.content_object.username
            if hasattr(content_object, 'name'):
                ret['summary_fields']['resource_name'] = obj.content_object.name
            content_model = obj.content_type.model_class()
            ret['summary_fields']['resource_type'] = get_type_for_model(content_model)
            ret['summary_fields']['resource_type_display_name'] = content_model._meta.verbose_name.title()
            ret['summary_fields']['resource_id'] = obj.object_id

        return ret

    def get_related(self, obj):
        ret = super(RoleSerializer, self).get_related(obj)
        ret['users'] = self.reverse('api:role_users_list', kwargs={'pk': obj.pk})
        ret['teams'] = self.reverse('api:role_teams_list', kwargs={'pk': obj.pk})
        try:
            if obj.content_object:
                ret.update(reverse_gfk(obj.content_object, self.context.get('request')))
        except AttributeError:
            # AttributeError's happen if our content_object is pointing at
            # a model that no longer exists. This is dirty data and ideally
            # doesn't exist, but in case it does, let's not puke.
            pass
        return ret


class RoleSerializerWithParentAccess(RoleSerializer):
    show_capabilities = ['unattach']


class ResourceAccessListElementSerializer(UserSerializer):
    show_capabilities = []  # Clear fields from UserSerializer parent class

    def to_representation(self, user):
        """
        With this method we derive "direct" and "indirect" access lists. Contained
        in the direct access list are all the roles the user is a member of, and
        all of the roles that are directly granted to any teams that the user is a
        member of.

        The indirect access list is a list of all of the roles that the user is
        a member of that are ancestors of any roles that grant permissions to
        the resource.
        """
        ret = super(ResourceAccessListElementSerializer, self).to_representation(user)
        obj = self.context['view'].get_parent_object()
        if self.context['view'].request is not None:
            requesting_user = self.context['view'].request.user
        else:
            requesting_user = None

        if 'summary_fields' not in ret:
            ret['summary_fields'] = {}

        team_content_type = ContentType.objects.get_for_model(Team)
        content_type = ContentType.objects.get_for_model(obj)

        reversed_org_map = {}
        for k, v in org_role_to_permission.items():
            reversed_org_map[v] = k
        reversed_role_map = {}
        for k, v in to_permissions.items():
            reversed_role_map[v] = k

        def get_roles_from_perms(perm_list):
            """given a list of permission codenames return a list of role names"""
            role_names = set()
            for codename in perm_list:
                action = codename.split('_', 1)[0]
                if action in reversed_role_map:
                    role_names.add(reversed_role_map[action])
                elif codename in reversed_org_map:
                    if isinstance(obj, Organization):
                        role_names.add(reversed_org_map[codename])
                        if 'view_organization' not in role_names:
                            role_names.add('read_role')
            return list(role_names)

        def format_role_perm(role):
            role_dict = {'id': role.id, 'name': role.name, 'description': role.description}
            try:
                role_dict['resource_name'] = role.content_object.name
                role_dict['resource_type'] = get_type_for_model(role.content_type.model_class())
                role_dict['related'] = reverse_gfk(role.content_object, self.context.get('request'))
            except AttributeError:
                pass
            if role.content_type is not None:
                role_dict['user_capabilities'] = {
                    'unattach': requesting_user.can_access(Role, 'unattach', role, user, 'members', data={}, skip_sub_obj_read_check=False)
                }
            else:
                # Singleton roles should not be managed from this view, as per copy/edit rework spec
                role_dict['user_capabilities'] = {'unattach': False}

            model_name = content_type.model
            if isinstance(obj, Organization):
                descendant_perms = [codename for codename in get_role_codenames(role) if codename.endswith(model_name) or codename.startswith('add_')]
            else:
                descendant_perms = [codename for codename in get_role_codenames(role) if codename.endswith(model_name)]

            return {'role': role_dict, 'descendant_roles': get_roles_from_perms(descendant_perms)}

        def format_team_role_perm(naive_team_role, permissive_role_ids):
            ret = []
            team = naive_team_role.content_object
            team_role = naive_team_role
            if naive_team_role.role_field == 'admin_role':
                team_role = team.member_role
            for role in team_role.children.filter(id__in=permissive_role_ids).all():
                role_dict = {
                    'id': role.id,
                    'name': role.name,
                    'description': role.description,
                    'team_id': team_role.object_id,
                    'team_name': team_role.content_object.name,
                    'team_organization_name': team_role.content_object.organization.name,
                }
                if role.content_type is not None:
                    role_dict['resource_name'] = role.content_object.name
                    role_dict['resource_type'] = get_type_for_model(role.content_type.model_class())
                    role_dict['related'] = reverse_gfk(role.content_object, self.context.get('request'))
                    role_dict['user_capabilities'] = {
                        'unattach': requesting_user.can_access(Role, 'unattach', role, team_role, 'parents', data={}, skip_sub_obj_read_check=False)
                    }
                else:
                    # Singleton roles should not be managed from this view, as per copy/edit rework spec
                    role_dict['user_capabilities'] = {'unattach': False}

                descendant_perms = list(
                    RoleEvaluation.objects.filter(role__in=team.has_roles.all(), object_id=obj.id, content_type_id=content_type.id)
                    .values_list('codename', flat=True)
                    .distinct()
                )

                ret.append({'role': role_dict, 'descendant_roles': get_roles_from_perms(descendant_perms)})
            return ret

        gfk_kwargs = dict(content_type_id=content_type.id, object_id=obj.id)
        direct_permissive_role_ids = Role.objects.filter(**gfk_kwargs).values_list('id', flat=True)

        if settings.ANSIBLE_BASE_ROLE_SYSTEM_ACTIVATED:
            ret['summary_fields']['direct_access'] = []
            ret['summary_fields']['indirect_access'] = []

            new_roles_seen = set()
            all_team_roles = set()
            all_permissive_role_ids = set()
            for evaluation in RoleEvaluation.objects.filter(role__in=user.has_roles.all(), **gfk_kwargs).prefetch_related('role'):
                new_role = evaluation.role
                if new_role.id in new_roles_seen:
                    continue
                new_roles_seen.add(new_role.id)
                old_role = get_role_from_object_role(new_role)
                all_permissive_role_ids.add(old_role.id)

                if int(new_role.object_id) == obj.id and new_role.content_type_id == content_type.id:
                    ret['summary_fields']['direct_access'].append(format_role_perm(old_role))
                elif new_role.content_type_id == team_content_type.id:
                    all_team_roles.add(old_role)
                else:
                    ret['summary_fields']['indirect_access'].append(format_role_perm(old_role))

            # Lazy role creation gives us a big problem, where some intermediate roles are not easy to find
            # like when a team has indirect permission, so here we get all roles the users teams have
            # these contribute to all potential permission-granting roles of the object
            user_teams_qs = permission_registry.team_model.objects.filter(member_roles__in=ObjectRole.objects.filter(users=user))
            team_obj_roles = ObjectRole.objects.filter(teams__in=user_teams_qs)
            for evaluation in RoleEvaluation.objects.filter(role__in=team_obj_roles, **gfk_kwargs).prefetch_related('role'):
                new_role = evaluation.role
                if new_role.id in new_roles_seen:
                    continue
                new_roles_seen.add(new_role.id)
                old_role = get_role_from_object_role(new_role)
                all_permissive_role_ids.add(old_role.id)

            # In DAB RBAC, superuser is strictly a user flag, and global roles are not in the RoleEvaluation table
            if user.is_superuser:
                ret['summary_fields'].setdefault('indirect_access', [])
                all_role_names = [field.name for field in obj._meta.get_fields() if isinstance(field, ImplicitRoleField)]
                ret['summary_fields']['indirect_access'].append(
                    {
                        "role": {
                            "id": None,
                            "name": _("System Administrator"),
                            "description": _("Can manage all aspects of the system"),
                            "user_capabilities": {"unattach": False},
                        },
                        "descendant_roles": all_role_names,
                    }
                )
            elif user.is_system_auditor:
                ret['summary_fields'].setdefault('indirect_access', [])
                ret['summary_fields']['indirect_access'].append(
                    {
                        "role": {
                            "id": None,
                            "name": _("Controller System Auditor"),
                            "description": _("Can view all aspects of the system"),
                            "user_capabilities": {"unattach": False},
                        },
                        "descendant_roles": ["read_role"],
                    }
                )

            ret['summary_fields']['direct_access'].extend([y for x in (format_team_role_perm(r, all_permissive_role_ids) for r in all_team_roles) for y in x])

            return ret

        all_permissive_role_ids = Role.objects.filter(content_type=content_type, object_id=obj.id).values_list('ancestors__id', flat=True)

        direct_access_roles = user.roles.filter(id__in=direct_permissive_role_ids).all()

        direct_team_roles = Role.objects.filter(content_type=team_content_type, members=user, children__in=direct_permissive_role_ids)
        if content_type == team_content_type:
            # When looking at the access list for a team, exclude the entries
            # for that team. This exists primarily so we don't list the read role
            # as a direct role when a user is a member or admin of a team
            direct_team_roles = direct_team_roles.exclude(children__content_type=team_content_type, children__object_id=obj.id)

        indirect_team_roles = Role.objects.filter(content_type=team_content_type, members=user, children__in=all_permissive_role_ids).exclude(
            id__in=direct_team_roles
        )

        indirect_access_roles = (
            user.roles.filter(id__in=all_permissive_role_ids)
            .exclude(id__in=direct_permissive_role_ids)
            .exclude(id__in=direct_team_roles)
            .exclude(id__in=indirect_team_roles)
        )

        ret['summary_fields']['direct_access'] = (
            [format_role_perm(r) for r in direct_access_roles.distinct()]
            + [y for x in (format_team_role_perm(r, direct_permissive_role_ids) for r in direct_team_roles.distinct()) for y in x]
            + [y for x in (format_team_role_perm(r, all_permissive_role_ids) for r in indirect_team_roles.distinct()) for y in x]
        )

        ret['summary_fields']['indirect_access'] = [format_role_perm(r) for r in indirect_access_roles.distinct()]

        return ret


class CredentialTypeSerializer(BaseSerializer):
    show_capabilities = ['edit', 'delete']
    managed = serializers.ReadOnlyField()

    class Meta:
        model = CredentialType
        fields = ('*', 'kind', 'namespace', 'name', 'managed', 'inputs', 'injectors')

    def validate(self, attrs):
        if self.instance and self.instance.managed:
            raise PermissionDenied(detail=_("Modifications not allowed for managed credential types"))

        old_inputs = {}
        if self.instance:
            old_inputs = copy.deepcopy(self.instance.inputs)

        ret = super(CredentialTypeSerializer, self).validate(attrs)

        if self.instance and self.instance.credentials.exists():
            if 'inputs' in attrs and old_inputs != self.instance.inputs:
                raise PermissionDenied(detail=_("Modifications to inputs are not allowed for credential types that are in use"))

        if 'kind' in attrs and attrs['kind'] not in ('cloud', 'net'):
            raise serializers.ValidationError({"kind": _("Must be 'cloud' or 'net', not %s") % attrs['kind']})

        fields = attrs.get('inputs', {}).get('fields', [])
        for field in fields:
            if field.get('ask_at_runtime', False):
                raise serializers.ValidationError({"inputs": _("'ask_at_runtime' is not supported for custom credentials.")})

        return ret

    def get_related(self, obj):
        res = super(CredentialTypeSerializer, self).get_related(obj)
        res['credentials'] = self.reverse('api:credential_type_credential_list', kwargs={'pk': obj.pk})
        res['activity_stream'] = self.reverse('api:credential_type_activity_stream_list', kwargs={'pk': obj.pk})
        return res

    def to_representation(self, data):
        value = super(CredentialTypeSerializer, self).to_representation(data)

        # translate labels and help_text for credential fields "managed"
        if value.get('managed'):
            value['name'] = _(value['name'])
            for field in value.get('inputs', {}).get('fields', []):
                field['label'] = _(field['label'])
                if 'help_text' in field:
                    field['help_text'] = _(field['help_text'])
        return value

    def filter_field_metadata(self, fields, method):
        # API-created/modified CredentialType kinds are limited to
        # `cloud` and `net`
        if method in ('PUT', 'POST'):
            fields['kind']['choices'] = list(filter(lambda choice: choice[0] in ('cloud', 'net'), fields['kind']['choices']))
        return fields


class CredentialSerializer(BaseSerializer):
    show_capabilities = ['edit', 'delete', 'copy', 'use']
    capabilities_prefetch = ['admin', 'use']
    managed = serializers.ReadOnlyField()

    class Meta:
        model = Credential
        fields = ('*', 'organization', 'credential_type', 'managed', 'inputs', 'kind', 'cloud', 'kubernetes')
        extra_kwargs = {'credential_type': {'label': _('Credential Type')}}

    def to_representation(self, data):
        value = super(CredentialSerializer, self).to_representation(data)

        if 'inputs' in value:
            value['inputs'] = data.display_inputs()
        return value

    def get_related(self, obj):
        res = super(CredentialSerializer, self).get_related(obj)

        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})

        res.update(
            dict(
                activity_stream=self.reverse('api:credential_activity_stream_list', kwargs={'pk': obj.pk}),
                access_list=self.reverse('api:credential_access_list', kwargs={'pk': obj.pk}),
                object_roles=self.reverse('api:credential_object_roles_list', kwargs={'pk': obj.pk}),
                owner_users=self.reverse('api:credential_owner_users_list', kwargs={'pk': obj.pk}),
                owner_teams=self.reverse('api:credential_owner_teams_list', kwargs={'pk': obj.pk}),
                copy=self.reverse('api:credential_copy', kwargs={'pk': obj.pk}),
                input_sources=self.reverse('api:credential_input_source_sublist', kwargs={'pk': obj.pk}),
                credential_type=self.reverse('api:credential_type_detail', kwargs={'pk': obj.credential_type.pk}),
            )
        )

        parents = [role for role in obj.admin_role.parents.all() if role.object_id is not None]
        if parents:
            res.update({parents[0].content_type.name: parents[0].content_object.get_absolute_url(self.context.get('request'))})
        elif len(obj.admin_role.members.all()) > 0:
            user = obj.admin_role.members.all()[0]
            res.update({'user': self.reverse('api:user_detail', kwargs={'pk': user.pk})})

        return res

    def get_summary_fields(self, obj):
        summary_dict = super(CredentialSerializer, self).get_summary_fields(obj)
        summary_dict['owners'] = []

        for user in obj.admin_role.members.all():
            summary_dict['owners'].append(
                {
                    'id': user.pk,
                    'type': 'user',
                    'name': user.username,
                    'description': ' '.join([user.first_name, user.last_name]),
                    'url': self.reverse('api:user_detail', kwargs={'pk': user.pk}),
                }
            )

        for parent in [role for role in obj.admin_role.parents.all() if role.object_id is not None]:
            summary_dict['owners'].append(
                {
                    'id': parent.content_object.pk,
                    'type': camelcase_to_underscore(parent.content_object.__class__.__name__),
                    'name': parent.content_object.name,
                    'description': parent.content_object.description,
                    'url': parent.content_object.get_absolute_url(self.context.get('request')),
                }
            )

        return summary_dict

    def validate(self, attrs):
        if self.instance and self.instance.managed:
            raise PermissionDenied(detail=_("Modifications not allowed for managed credentials"))
        return super(CredentialSerializer, self).validate(attrs)

    def get_validation_exclusions(self, obj=None):
        ret = super(CredentialSerializer, self).get_validation_exclusions(obj)
        for field in ('credential_type', 'inputs'):
            if field in ret:
                ret.remove(field)
        return ret

    def validate_organization(self, org):
        if self.instance and (not self.instance.managed) and self.instance.credential_type.kind == 'galaxy' and org is None:
            raise serializers.ValidationError(_("Galaxy credentials must be owned by an Organization."))
        return org

    def validate_credential_type(self, credential_type):
        if self.instance and credential_type.pk != self.instance.credential_type.pk:
            for related_objects in (
                'ad_hoc_commands',
                'unifiedjobs',
                'unifiedjobtemplates',
                'projects',
                'projectupdates',
                'workflowjobnodes',
            ):
                if getattr(self.instance, related_objects).count() > 0:
                    raise ValidationError(
                        _('You cannot change the credential type of the credential, as it may break the functionality of the resources using it.')
                    )

        return credential_type

    def validate_inputs(self, inputs):
        if self.instance and self.instance.credential_type.kind == "vault":
            if 'vault_id' in inputs and inputs['vault_id'] != self.instance.inputs['vault_id']:
                raise ValidationError(_('Vault IDs cannot be changed once they have been created.'))

        return inputs


class CredentialSerializerCreate(CredentialSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        required=False,
        default=None,
        write_only=True,
        allow_null=True,
        help_text=_('Write-only field used to add user to owner role. If provided, do not give either team or organization. Only valid for creation.'),
    )
    team = serializers.PrimaryKeyRelatedField(
        queryset=Team.objects.all(),
        required=False,
        default=None,
        write_only=True,
        allow_null=True,
        help_text=_('Write-only field used to add team to owner role. If provided, do not give either user or organization. Only valid for creation.'),
    )
    organization = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(),
        required=False,
        default=None,
        allow_null=True,
        help_text=_('Inherit permissions from organization roles. If provided on creation, do not give either user or team.'),
    )

    class Meta:
        model = Credential
        fields = ('*', 'user', 'team')

    def validate(self, attrs):
        owner_fields = set()
        for field in ('user', 'team', 'organization'):
            if field in attrs:
                if attrs[field]:
                    owner_fields.add(field)
                else:
                    attrs.pop(field)

        if not owner_fields:
            raise serializers.ValidationError({"detail": _("Missing 'user', 'team', or 'organization'.")})

        if len(owner_fields) > 1:
            received = ", ".join(sorted(owner_fields))
            raise serializers.ValidationError(
                {"detail": _("Only one of 'user', 'team', or 'organization' should be provided, received {} fields.".format(received))}
            )

        if attrs.get('team'):
            attrs['organization'] = attrs['team'].organization

        if 'credential_type' in attrs and attrs['credential_type'].kind == 'galaxy' and list(owner_fields) != ['organization']:
            raise serializers.ValidationError({"organization": _("Galaxy credentials must be owned by an Organization.")})

        return super(CredentialSerializerCreate, self).validate(attrs)

    def create(self, validated_data):
        user = validated_data.pop('user', None)
        team = validated_data.pop('team', None)

        credential = super(CredentialSerializerCreate, self).create(validated_data)

        if user:
            give_creator_permissions(user, credential)
        if team:
            if not credential.organization or team.organization.id != credential.organization.id:
                raise serializers.ValidationError({"detail": _("Credential organization must be set and match before assigning to a team")})
            credential.admin_role.parents.add(team.admin_role)
            credential.use_role.parents.add(team.member_role)
        return credential


class CredentialInputSourceSerializer(BaseSerializer):
    show_capabilities = ['delete']

    class Meta:
        model = CredentialInputSource
        fields = ('*', 'input_field_name', 'metadata', 'target_credential', 'source_credential', '-name')
        extra_kwargs = {'input_field_name': {'required': True}, 'target_credential': {'required': True}, 'source_credential': {'required': True}}

    def get_related(self, obj):
        res = super(CredentialInputSourceSerializer, self).get_related(obj)
        res['source_credential'] = obj.source_credential.get_absolute_url(request=self.context.get('request'))
        res['target_credential'] = obj.target_credential.get_absolute_url(request=self.context.get('request'))
        return res


class UserCredentialSerializerCreate(CredentialSerializerCreate):
    class Meta:
        model = Credential
        fields = ('*', '-team', '-organization')


class TeamCredentialSerializerCreate(CredentialSerializerCreate):
    class Meta:
        model = Credential
        fields = ('*', '-user', '-organization')


class OrganizationCredentialSerializerCreate(CredentialSerializerCreate):
    class Meta:
        model = Credential
        fields = ('*', '-user', '-team')


class JobOptionsSerializer(LabelsListMixin, BaseSerializer):
    class Meta:
        fields = (
            '*',
            'job_type',
            'inventory',
            'project',
            'playbook',
            'scm_branch',
            'forks',
            'limit',
            'verbosity',
            'extra_vars',
            'job_tags',
            'force_handlers',
            'skip_tags',
            'start_at_task',
            'timeout',
            'use_fact_cache',
            'organization',
        )
        read_only_fields = ('organization',)

    def get_related(self, obj):
        res = super(JobOptionsSerializer, self).get_related(obj)
        res['labels'] = self.reverse('api:job_template_label_list', kwargs={'pk': obj.pk})
        try:
            if obj.inventory:
                res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory.pk})
        except ObjectDoesNotExist:
            setattr(obj, 'inventory', None)
        try:
            if obj.project:
                res['project'] = self.reverse('api:project_detail', kwargs={'pk': obj.project.pk})
        except ObjectDoesNotExist:
            setattr(obj, 'project', None)
        if obj.organization_id:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization_id})
        if isinstance(obj, UnifiedJobTemplate):
            res['credentials'] = self.reverse('api:job_template_credentials_list', kwargs={'pk': obj.pk})
        elif isinstance(obj, UnifiedJob):
            res['credentials'] = self.reverse('api:job_credentials_list', kwargs={'pk': obj.pk})

        return res

    def to_representation(self, obj):
        ret = super(JobOptionsSerializer, self).to_representation(obj)
        if obj is None:
            return ret
        if 'inventory' in ret and not obj.inventory:
            ret['inventory'] = None
        if 'project' in ret and not obj.project:
            ret['project'] = None
            if 'playbook' in ret:
                ret['playbook'] = ''
        return ret

    def validate(self, attrs):
        if 'project' in self.fields and 'playbook' in self.fields:
            project = attrs.get('project', self.instance.project if self.instance else None)
            playbook = attrs.get('playbook', self.instance and self.instance.playbook or '')
            scm_branch = attrs.get('scm_branch', self.instance.scm_branch if self.instance else None)
            ask_scm_branch_on_launch = attrs.get('ask_scm_branch_on_launch', self.instance.ask_scm_branch_on_launch if self.instance else None)
            if not project:
                raise serializers.ValidationError({'project': _('This field is required.')})
            playbook_not_found = bool(
                (project and project.scm_type and (not project.allow_override) and playbook and force_str(playbook) not in project.playbook_files)
                or (project and not project.scm_type and playbook and force_str(playbook) not in project.playbooks)  # manual
            )
            if playbook_not_found:
                raise serializers.ValidationError({'playbook': _('Playbook not found for project.')})
            if project and not playbook:
                raise serializers.ValidationError({'playbook': _('Must select playbook for project.')})
            if scm_branch and not project.allow_override:
                raise serializers.ValidationError({'scm_branch': _('Project does not allow overriding branch.')})
            if ask_scm_branch_on_launch and not project.allow_override:
                raise serializers.ValidationError({'ask_scm_branch_on_launch': _('Project does not allow overriding branch.')})

        ret = super(JobOptionsSerializer, self).validate(attrs)
        return ret


class JobTemplateMixin(object):
    """
    Provide recent jobs and survey details in summary_fields
    """

    def _recent_jobs(self, obj):
        # Exclude "joblets", jobs that ran as part of a sliced workflow job
        uj_qs = obj.unifiedjob_unified_jobs.exclude(job__job_slice_count__gt=1).order_by('-created')
        # Would like to apply an .only, but does not play well with non_polymorphic
        # .only('id', 'status', 'finished', 'polymorphic_ctype_id')
        optimized_qs = uj_qs.non_polymorphic()
        return [
            {
                'id': x.id,
                'status': x.status,
                'finished': x.finished,
                'canceled_on': x.canceled_on,
                # Make type consistent with API top-level key, for instance workflow_job
                'type': x.job_type_name,
            }
            for x in optimized_qs[:10]
        ]

    def get_summary_fields(self, obj):
        d = super(JobTemplateMixin, self).get_summary_fields(obj)
        if obj.survey_spec is not None and ('name' in obj.survey_spec and 'description' in obj.survey_spec):
            d['survey'] = dict(title=obj.survey_spec['name'], description=obj.survey_spec['description'])
        d['recent_jobs'] = self._recent_jobs(obj)
        return d

    def validate(self, attrs):
        webhook_service = attrs.get('webhook_service', getattr(self.instance, 'webhook_service', None))
        webhook_credential = attrs.get('webhook_credential', getattr(self.instance, 'webhook_credential', None))

        if webhook_credential:
            if webhook_credential.credential_type.kind != 'token':
                raise serializers.ValidationError({'webhook_credential': _("Must be a Personal Access Token.")})

            msg = {'webhook_credential': _("Must match the selected webhook service.")}
            if webhook_service:
                if webhook_credential.credential_type.namespace != '{}_token'.format(webhook_service):
                    raise serializers.ValidationError(msg)
            else:
                raise serializers.ValidationError(msg)

        return super().validate(attrs)


class JobTemplateSerializer(JobTemplateMixin, UnifiedJobTemplateSerializer, JobOptionsSerializer):
    show_capabilities = ['start', 'schedule', 'copy', 'edit', 'delete']
    capabilities_prefetch = ['admin', 'execute', {'copy': ['project.use', 'inventory.use']}]

    status = serializers.ChoiceField(choices=JobTemplate.JOB_TEMPLATE_STATUS_CHOICES, read_only=True, required=False)

    class Meta:
        model = JobTemplate
        fields = (
            '*',
            'host_config_key',
            'ask_scm_branch_on_launch',
            'ask_diff_mode_on_launch',
            'ask_variables_on_launch',
            'ask_limit_on_launch',
            'ask_tags_on_launch',
            'ask_skip_tags_on_launch',
            'ask_job_type_on_launch',
            'ask_verbosity_on_launch',
            'ask_inventory_on_launch',
            'ask_credential_on_launch',
            'ask_execution_environment_on_launch',
            'ask_labels_on_launch',
            'ask_forks_on_launch',
            'ask_job_slice_count_on_launch',
            'ask_timeout_on_launch',
            'ask_instance_groups_on_launch',
            'survey_enabled',
            'become_enabled',
            'diff_mode',
            'allow_simultaneous',
            'custom_virtualenv',
            'job_slice_count',
            'webhook_service',
            'webhook_credential',
            'prevent_instance_group_fallback',
        )
        read_only_fields = ('*', 'custom_virtualenv')

    def get_related(self, obj):
        res = super(JobTemplateSerializer, self).get_related(obj)
        res.update(
            jobs=self.reverse('api:job_template_jobs_list', kwargs={'pk': obj.pk}),
            schedules=self.reverse('api:job_template_schedules_list', kwargs={'pk': obj.pk}),
            activity_stream=self.reverse('api:job_template_activity_stream_list', kwargs={'pk': obj.pk}),
            launch=self.reverse('api:job_template_launch', kwargs={'pk': obj.pk}),
            webhook_key=self.reverse('api:webhook_key', kwargs={'model_kwarg': 'job_templates', 'pk': obj.pk}),
            webhook_receiver=(
                self.reverse('api:webhook_receiver_{}'.format(obj.webhook_service), kwargs={'model_kwarg': 'job_templates', 'pk': obj.pk})
                if obj.webhook_service
                else ''
            ),
            notification_templates_started=self.reverse('api:job_template_notification_templates_started_list', kwargs={'pk': obj.pk}),
            notification_templates_success=self.reverse('api:job_template_notification_templates_success_list', kwargs={'pk': obj.pk}),
            notification_templates_error=self.reverse('api:job_template_notification_templates_error_list', kwargs={'pk': obj.pk}),
            access_list=self.reverse('api:job_template_access_list', kwargs={'pk': obj.pk}),
            survey_spec=self.reverse('api:job_template_survey_spec', kwargs={'pk': obj.pk}),
            labels=self.reverse('api:job_template_label_list', kwargs={'pk': obj.pk}),
            object_roles=self.reverse('api:job_template_object_roles_list', kwargs={'pk': obj.pk}),
            instance_groups=self.reverse('api:job_template_instance_groups_list', kwargs={'pk': obj.pk}),
            slice_workflow_jobs=self.reverse('api:job_template_slice_workflow_jobs_list', kwargs={'pk': obj.pk}),
            copy=self.reverse('api:job_template_copy', kwargs={'pk': obj.pk}),
        )
        if obj.host_config_key:
            res['callback'] = self.reverse('api:job_template_callback', kwargs={'pk': obj.pk})
        if obj.organization_id:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization_id})
        if obj.webhook_credential_id:
            res['webhook_credential'] = self.reverse('api:credential_detail', kwargs={'pk': obj.webhook_credential_id})
        return res

    def validate(self, attrs):
        def get_field_from_model_or_attrs(fd):
            return attrs.get(fd, self.instance and getattr(self.instance, fd) or None)

        inventory = get_field_from_model_or_attrs('inventory')
        project = get_field_from_model_or_attrs('project')

        if get_field_from_model_or_attrs('host_config_key') and not inventory:
            raise serializers.ValidationError({'host_config_key': _("Cannot enable provisioning callback without an inventory set.")})

        prompting_error_message = _("You must either set a default value or ask to prompt on launch.")
        if project is None:
            raise serializers.ValidationError({'project': _("Job Templates must have a project assigned.")})
        elif inventory is None and not get_field_from_model_or_attrs('ask_inventory_on_launch'):
            raise serializers.ValidationError({'inventory': prompting_error_message})

        return super(JobTemplateSerializer, self).validate(attrs)

    def validate_extra_vars(self, value):
        return vars_validate_or_raise(value)

    def get_summary_fields(self, obj):
        summary_fields = super(JobTemplateSerializer, self).get_summary_fields(obj)
        all_creds = []
        # Organize credential data into multitude of deprecated fields
        if obj.pk:
            for cred in obj.credentials.all():
                summarized_cred = {
                    'id': cred.pk,
                    'name': cred.name,
                    'description': cred.description,
                    'kind': cred.kind,
                    'cloud': cred.credential_type.kind == 'cloud',
                }
                all_creds.append(summarized_cred)
        summary_fields['credentials'] = all_creds
        return summary_fields


class JobTemplateWithSpecSerializer(JobTemplateSerializer):
    """
    Used for activity stream entries.
    """

    class Meta:
        model = JobTemplate
        fields = ('*', 'survey_spec')


class JobSerializer(UnifiedJobSerializer, JobOptionsSerializer):
    passwords_needed_to_start = serializers.ReadOnlyField()
    artifacts = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = (
            '*',
            'job_template',
            'passwords_needed_to_start',
            'allow_simultaneous',
            'artifacts',
            'scm_revision',
            'instance_group',
            'diff_mode',
            'job_slice_number',
            'job_slice_count',
            'webhook_service',
            'webhook_credential',
            'webhook_guid',
        )

    def get_related(self, obj):
        res = super(JobSerializer, self).get_related(obj)
        res.update(
            dict(
                job_events=self.reverse('api:job_job_events_list', kwargs={'pk': obj.pk}),  # TODO: consider adding job_created
                job_host_summaries=self.reverse('api:job_job_host_summaries_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:job_activity_stream_list', kwargs={'pk': obj.pk}),
                notifications=self.reverse('api:job_notifications_list', kwargs={'pk': obj.pk}),
                labels=self.reverse('api:job_label_list', kwargs={'pk': obj.pk}),
                create_schedule=self.reverse('api:job_create_schedule', kwargs={'pk': obj.pk}),
            )
        )
        try:
            if obj.job_template:
                res['job_template'] = self.reverse('api:job_template_detail', kwargs={'pk': obj.job_template.pk})
        except ObjectDoesNotExist:
            setattr(obj, 'job_template', None)
        if obj.can_cancel or True:
            res['cancel'] = self.reverse('api:job_cancel', kwargs={'pk': obj.pk})
        try:
            if obj.project_update:
                res['project_update'] = self.reverse('api:project_update_detail', kwargs={'pk': obj.project_update.pk})
        except ObjectDoesNotExist:
            pass
        res['relaunch'] = self.reverse('api:job_relaunch', kwargs={'pk': obj.pk})
        return res

    def get_artifacts(self, obj):
        if obj:
            return obj.display_artifacts()
        return {}

    def to_representation(self, obj):
        ret = super(JobSerializer, self).to_representation(obj)
        if obj is None:
            return ret
        if 'job_template' in ret and not obj.job_template:
            ret['job_template'] = None
        if 'extra_vars' in ret:
            ret['extra_vars'] = obj.display_extra_vars()
        return ret

    def get_summary_fields(self, obj):
        summary_fields = super(JobSerializer, self).get_summary_fields(obj)
        all_creds = []
        # Organize credential data into multitude of deprecated fields
        if obj.pk:
            for cred in obj.credentials.all():
                summarized_cred = {
                    'id': cred.pk,
                    'name': cred.name,
                    'description': cred.description,
                    'kind': cred.kind,
                    'cloud': cred.credential_type.kind == 'cloud',
                }
                all_creds.append(summarized_cred)
        summary_fields['credentials'] = all_creds
        return summary_fields


class JobDetailSerializer(JobSerializer):
    playbook_counts = serializers.SerializerMethodField(help_text=_('A count of all plays and tasks for the job run.'))
    custom_virtualenv = serializers.ReadOnlyField()

    class Meta:
        model = Job
        fields = ('*', 'host_status_counts', 'playbook_counts', 'custom_virtualenv')

    def get_playbook_counts(self, obj):
        task_count = obj.get_event_queryset().filter(event='playbook_on_task_start').count()
        play_count = obj.get_event_queryset().filter(event='playbook_on_play_start').count()

        data = {'play_count': play_count, 'task_count': task_count}

        return data


class JobCancelSerializer(BaseSerializer):
    can_cancel = serializers.BooleanField(read_only=True)

    class Meta:
        model = Job
        fields = ('can_cancel',)


class JobRelaunchSerializer(BaseSerializer):
    passwords_needed_to_start = serializers.SerializerMethodField()
    retry_counts = serializers.SerializerMethodField()
    hosts = serializers.ChoiceField(
        required=False,
        allow_null=True,
        default='all',
        choices=[('all', _('No change to job limit')), ('failed', _('All failed and unreachable hosts'))],
        write_only=True,
    )
    job_type = serializers.ChoiceField(
        required=False,
        allow_null=True,
        choices=NEW_JOB_TYPE_CHOICES,
        write_only=True,
    )
    credential_passwords = VerbatimField(required=True, write_only=True)

    class Meta:
        model = Job
        fields = ('passwords_needed_to_start', 'retry_counts', 'hosts', 'job_type', 'credential_passwords')

    def validate_credential_passwords(self, value):
        pnts = self.instance.passwords_needed_to_start
        missing = set(pnts) - set(key for key in value if value[key])
        if missing:
            raise serializers.ValidationError(_('Missing passwords needed to start: {}'.format(', '.join(missing))))
        return value

    def to_representation(self, obj):
        res = super(JobRelaunchSerializer, self).to_representation(obj)
        view = self.context.get('view', None)
        if hasattr(view, '_raw_data_form_marker'):
            password_keys = dict([(p, u'') for p in self.get_passwords_needed_to_start(obj)])
            res.update(password_keys)
        return res

    def get_passwords_needed_to_start(self, obj):
        if obj:
            return obj.passwords_needed_to_start
        return ''

    def get_retry_counts(self, obj):
        if obj.status in ACTIVE_STATES:
            return _('Relaunch by host status not available until job finishes running.')
        data = OrderedDict([])
        for status in self.fields['hosts'].choices.keys():
            data[status] = obj.retry_qs(status).count()
        return data

    def get_validation_exclusions(self, *args, **kwargs):
        r = super(JobRelaunchSerializer, self).get_validation_exclusions(*args, **kwargs)
        r.append('credential_passwords')
        return r

    def validate(self, attrs):
        obj = self.instance
        if obj.project is None:
            raise serializers.ValidationError(dict(errors=[_("Job Template Project is missing or undefined.")]))
        if obj.inventory is None or obj.inventory.pending_deletion:
            raise serializers.ValidationError(dict(errors=[_("Job Template Inventory is missing or undefined.")]))
        attrs = super(JobRelaunchSerializer, self).validate(attrs)
        return attrs


class JobCreateScheduleSerializer(LabelsListMixin, BaseSerializer):
    can_schedule = serializers.SerializerMethodField()
    prompts = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = ('can_schedule', 'prompts')

    def get_can_schedule(self, obj):
        """
        Need both a job template and job prompts to schedule
        """
        return obj.can_schedule

    @staticmethod
    def _summarize(res_name, obj):
        summary = {}
        for field in SUMMARIZABLE_FK_FIELDS[res_name]:
            summary[field] = getattr(obj, field, None)
        return summary

    def get_prompts(self, obj):
        try:
            config = obj.launch_config
            ret = config.prompts_dict(display=True)
            for field_name in ('inventory', 'execution_environment'):
                if field_name in ret:
                    ret[field_name] = self._summarize(field_name, ret[field_name])
            for field_name, singular in (('credentials', 'credential'), ('instance_groups', 'instance_group')):
                if field_name in ret:
                    ret[field_name] = [self._summarize(singular, obj) for obj in ret[field_name]]
            if 'labels' in ret:
                ret['labels'] = self._summary_field_labels(config)
            return ret
        except JobLaunchConfig.DoesNotExist:
            return {'all': _('Unknown, job may have been run before launch configurations were saved.')}


class AdHocCommandSerializer(UnifiedJobSerializer):
    class Meta:
        model = AdHocCommand
        fields = (
            '*',
            'job_type',
            'inventory',
            'limit',
            'credential',
            'module_name',
            'module_args',
            'forks',
            'verbosity',
            'extra_vars',
            'become_enabled',
            'diff_mode',
            '-unified_job_template',
            '-description',
        )
        extra_kwargs = {'name': {'read_only': True}}

    def get_field_names(self, declared_fields, info):
        field_names = super(AdHocCommandSerializer, self).get_field_names(declared_fields, info)
        # Meta multiple inheritance and -field_name options don't seem to be
        # taking effect above, so remove the undesired fields here.
        return tuple(x for x in field_names if x not in ('unified_job_template', 'description'))

    def build_standard_field(self, field_name, model_field):
        field_class, field_kwargs = super(AdHocCommandSerializer, self).build_standard_field(field_name, model_field)
        # Load module name choices dynamically from DB settings.
        if field_name == 'module_name':
            field_class = serializers.ChoiceField
            module_name_choices = [(x, x) for x in settings.AD_HOC_COMMANDS]
            module_name_default = 'command' if 'command' in [x[0] for x in module_name_choices] else ''
            field_kwargs['choices'] = module_name_choices
            field_kwargs['required'] = bool(not module_name_default)
            field_kwargs['default'] = module_name_default or serializers.empty
            field_kwargs['allow_blank'] = False
            field_kwargs.pop('max_length', None)
        return field_class, field_kwargs

    def get_related(self, obj):
        res = super(AdHocCommandSerializer, self).get_related(obj)
        if obj.inventory_id:
            res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory_id})
        if obj.credential_id:
            res['credential'] = self.reverse('api:credential_detail', kwargs={'pk': obj.credential_id})
        res.update(
            dict(
                events=self.reverse('api:ad_hoc_command_ad_hoc_command_events_list', kwargs={'pk': obj.pk}),
                activity_stream=self.reverse('api:ad_hoc_command_activity_stream_list', kwargs={'pk': obj.pk}),
                notifications=self.reverse('api:ad_hoc_command_notifications_list', kwargs={'pk': obj.pk}),
            )
        )
        res['cancel'] = self.reverse('api:ad_hoc_command_cancel', kwargs={'pk': obj.pk})
        res['relaunch'] = self.reverse('api:ad_hoc_command_relaunch', kwargs={'pk': obj.pk})
        return res

    def to_representation(self, obj):
        ret = super(AdHocCommandSerializer, self).to_representation(obj)
        if 'inventory' in ret and not obj.inventory_id:
            ret['inventory'] = None
        if 'credential' in ret and not obj.credential_id:
            ret['credential'] = None
        # For the UI, only module_name is returned for name, instead of the
        # longer module name + module_args format.
        if 'name' in ret:
            ret['name'] = obj.module_name
        return ret

    def validate(self, attrs):
        ret = super(AdHocCommandSerializer, self).validate(attrs)
        return ret

    def validate_extra_vars(self, value):
        redacted_extra_vars, removed_vars = extract_ansible_vars(value)
        if removed_vars:
            raise serializers.ValidationError(_("{} are prohibited from use in ad hoc commands.").format(", ".join(sorted(removed_vars, reverse=True))))
        return vars_validate_or_raise(value)


class AdHocCommandDetailSerializer(AdHocCommandSerializer):
    class Meta:
        model = AdHocCommand
        fields = ('*', 'host_status_counts')


class AdHocCommandCancelSerializer(AdHocCommandSerializer):
    can_cancel = serializers.BooleanField(read_only=True)

    class Meta:
        fields = ('can_cancel',)


class AdHocCommandRelaunchSerializer(AdHocCommandSerializer):
    class Meta:
        fields = ()

    def to_representation(self, obj):
        if obj:
            return dict([(p, u'') for p in obj.passwords_needed_to_start])
        else:
            return {}


class SystemJobTemplateSerializer(UnifiedJobTemplateSerializer):
    class Meta:
        model = SystemJobTemplate
        fields = ('*', 'job_type')

    def get_related(self, obj):
        res = super(SystemJobTemplateSerializer, self).get_related(obj)
        res.update(
            dict(
                jobs=self.reverse('api:system_job_template_jobs_list', kwargs={'pk': obj.pk}),
                schedules=self.reverse('api:system_job_template_schedules_list', kwargs={'pk': obj.pk}),
                launch=self.reverse('api:system_job_template_launch', kwargs={'pk': obj.pk}),
                notification_templates_started=self.reverse('api:system_job_template_notification_templates_started_list', kwargs={'pk': obj.pk}),
                notification_templates_success=self.reverse('api:system_job_template_notification_templates_success_list', kwargs={'pk': obj.pk}),
                notification_templates_error=self.reverse('api:system_job_template_notification_templates_error_list', kwargs={'pk': obj.pk}),
            )
        )
        return res


class SystemJobSerializer(UnifiedJobSerializer):
    result_stdout = serializers.SerializerMethodField()

    class Meta:
        model = SystemJob
        fields = ('*', 'system_job_template', 'job_type', 'extra_vars', 'result_stdout', '-controller_node')

    def get_related(self, obj):
        res = super(SystemJobSerializer, self).get_related(obj)
        if obj.system_job_template:
            res['system_job_template'] = self.reverse('api:system_job_template_detail', kwargs={'pk': obj.system_job_template.pk})
            res['notifications'] = self.reverse('api:system_job_notifications_list', kwargs={'pk': obj.pk})
        if obj.can_cancel or True:
            res['cancel'] = self.reverse('api:system_job_cancel', kwargs={'pk': obj.pk})
        res['events'] = self.reverse('api:system_job_events_list', kwargs={'pk': obj.pk})
        return res

    def get_result_stdout(self, obj):
        try:
            return obj.result_stdout
        except StdoutMaxBytesExceeded as e:
            return _("Standard Output too large to display ({text_size} bytes), only download supported for sizes over {supported_size} bytes.").format(
                text_size=e.total, supported_size=e.supported
            )


class SystemJobCancelSerializer(SystemJobSerializer):
    can_cancel = serializers.BooleanField(read_only=True)

    class Meta:
        fields = ('can_cancel',)


class WorkflowJobTemplateSerializer(JobTemplateMixin, LabelsListMixin, UnifiedJobTemplateSerializer):
    show_capabilities = ['start', 'schedule', 'edit', 'copy', 'delete']
    capabilities_prefetch = ['admin', 'execute', {'copy': 'organization.workflow_admin'}]
    limit = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    scm_branch = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)

    skip_tags = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    job_tags = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)

    class Meta:
        model = WorkflowJobTemplate
        fields = (
            '*',
            'extra_vars',
            'organization',
            'survey_enabled',
            'allow_simultaneous',
            'ask_variables_on_launch',
            'inventory',
            'limit',
            'scm_branch',
            'ask_inventory_on_launch',
            'ask_scm_branch_on_launch',
            'ask_limit_on_launch',
            'webhook_service',
            'webhook_credential',
            '-execution_environment',
            'ask_labels_on_launch',
            'ask_skip_tags_on_launch',
            'ask_tags_on_launch',
            'skip_tags',
            'job_tags',
        )

    def get_related(self, obj):
        res = super(WorkflowJobTemplateSerializer, self).get_related(obj)
        res.update(
            workflow_jobs=self.reverse('api:workflow_job_template_jobs_list', kwargs={'pk': obj.pk}),
            schedules=self.reverse('api:workflow_job_template_schedules_list', kwargs={'pk': obj.pk}),
            launch=self.reverse('api:workflow_job_template_launch', kwargs={'pk': obj.pk}),
            webhook_key=self.reverse('api:webhook_key', kwargs={'model_kwarg': 'workflow_job_templates', 'pk': obj.pk}),
            webhook_receiver=(
                self.reverse('api:webhook_receiver_{}'.format(obj.webhook_service), kwargs={'model_kwarg': 'workflow_job_templates', 'pk': obj.pk})
                if obj.webhook_service
                else ''
            ),
            workflow_nodes=self.reverse('api:workflow_job_template_workflow_nodes_list', kwargs={'pk': obj.pk}),
            labels=self.reverse('api:workflow_job_template_label_list', kwargs={'pk': obj.pk}),
            activity_stream=self.reverse('api:workflow_job_template_activity_stream_list', kwargs={'pk': obj.pk}),
            notification_templates_started=self.reverse('api:workflow_job_template_notification_templates_started_list', kwargs={'pk': obj.pk}),
            notification_templates_success=self.reverse('api:workflow_job_template_notification_templates_success_list', kwargs={'pk': obj.pk}),
            notification_templates_error=self.reverse('api:workflow_job_template_notification_templates_error_list', kwargs={'pk': obj.pk}),
            notification_templates_approvals=self.reverse('api:workflow_job_template_notification_templates_approvals_list', kwargs={'pk': obj.pk}),
            access_list=self.reverse('api:workflow_job_template_access_list', kwargs={'pk': obj.pk}),
            object_roles=self.reverse('api:workflow_job_template_object_roles_list', kwargs={'pk': obj.pk}),
            survey_spec=self.reverse('api:workflow_job_template_survey_spec', kwargs={'pk': obj.pk}),
            copy=self.reverse('api:workflow_job_template_copy', kwargs={'pk': obj.pk}),
        )
        res.pop('execution_environment', None)  # EEs aren't meaningful for workflows
        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})
        if obj.webhook_credential_id:
            res['webhook_credential'] = self.reverse('api:credential_detail', kwargs={'pk': obj.webhook_credential_id})
        if obj.inventory_id:
            res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory_id})
        return res

    def validate_extra_vars(self, value):
        return vars_validate_or_raise(value)

    def validate(self, attrs):
        attrs = super(WorkflowJobTemplateSerializer, self).validate(attrs)

        # process char_prompts, these are not direct fields on the model
        mock_obj = self.Meta.model()
        for field_name in ('scm_branch', 'limit', 'skip_tags', 'job_tags'):
            if field_name in attrs:
                setattr(mock_obj, field_name, attrs[field_name])
                attrs.pop(field_name)

        # Model `.save` needs the container dict, not the pseudo fields
        if mock_obj.char_prompts:
            attrs['char_prompts'] = mock_obj.char_prompts

        return attrs


class WorkflowJobTemplateWithSpecSerializer(WorkflowJobTemplateSerializer):
    """
    Used for activity stream entries.
    """

    class Meta:
        model = WorkflowJobTemplate
        fields = ('*', 'survey_spec')


class WorkflowJobSerializer(LabelsListMixin, UnifiedJobSerializer):
    limit = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    scm_branch = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)

    skip_tags = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    job_tags = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)

    class Meta:
        model = WorkflowJob
        fields = (
            '*',
            'workflow_job_template',
            'extra_vars',
            'allow_simultaneous',
            'job_template',
            'is_sliced_job',
            '-execution_environment',
            '-execution_node',
            '-event_processing_finished',
            '-controller_node',
            'inventory',
            'limit',
            'scm_branch',
            'webhook_service',
            'webhook_credential',
            'webhook_guid',
            'skip_tags',
            'job_tags',
        )

    def get_related(self, obj):
        res = super(WorkflowJobSerializer, self).get_related(obj)
        res.pop('execution_environment', None)  # EEs aren't meaningful for workflows
        if obj.workflow_job_template:
            res['workflow_job_template'] = self.reverse('api:workflow_job_template_detail', kwargs={'pk': obj.workflow_job_template.pk})
            res['notifications'] = self.reverse('api:workflow_job_notifications_list', kwargs={'pk': obj.pk})
        if obj.job_template_id:
            res['job_template'] = self.reverse('api:job_template_detail', kwargs={'pk': obj.job_template_id})
        res['workflow_nodes'] = self.reverse('api:workflow_job_workflow_nodes_list', kwargs={'pk': obj.pk})
        res['labels'] = self.reverse('api:workflow_job_label_list', kwargs={'pk': obj.pk})
        res['activity_stream'] = self.reverse('api:workflow_job_activity_stream_list', kwargs={'pk': obj.pk})
        res['relaunch'] = self.reverse('api:workflow_job_relaunch', kwargs={'pk': obj.pk})
        if obj.can_cancel or True:
            res['cancel'] = self.reverse('api:workflow_job_cancel', kwargs={'pk': obj.pk})
        return res

    def to_representation(self, obj):
        ret = super(WorkflowJobSerializer, self).to_representation(obj)
        if obj is None:
            return ret
        if 'extra_vars' in ret:
            ret['extra_vars'] = obj.display_extra_vars()
        return ret


class WorkflowJobListSerializer(WorkflowJobSerializer, UnifiedJobListSerializer):
    class Meta:
        fields = ('*', '-execution_environment', '-execution_node', '-controller_node')


class WorkflowJobCancelSerializer(WorkflowJobSerializer):
    can_cancel = serializers.BooleanField(read_only=True)

    class Meta:
        fields = ('can_cancel',)


class WorkflowApprovalViewSerializer(UnifiedJobSerializer):
    class Meta:
        model = WorkflowApproval
        fields = []


class WorkflowApprovalSerializer(UnifiedJobSerializer):
    can_approve_or_deny = serializers.SerializerMethodField()
    approval_expiration = serializers.SerializerMethodField()
    timed_out = serializers.ReadOnlyField()

    class Meta:
        model = WorkflowApproval
        fields = ('*', '-controller_node', '-execution_node', 'can_approve_or_deny', 'approval_expiration', 'timed_out')

    def get_approval_expiration(self, obj):
        if obj.status != 'pending' or obj.timeout == 0:
            return None
        return obj.created + timedelta(seconds=obj.timeout)

    def get_can_approve_or_deny(self, obj):
        request = self.context.get('request', None)
        allowed = request.user.can_access(WorkflowApproval, 'approve_or_deny', obj)
        return allowed is True and obj.status == 'pending'

    def get_related(self, obj):
        res = super(WorkflowApprovalSerializer, self).get_related(obj)

        if obj.workflow_approval_template:
            res['workflow_approval_template'] = self.reverse('api:workflow_approval_template_detail', kwargs={'pk': obj.workflow_approval_template.pk})
        res['approve'] = self.reverse('api:workflow_approval_approve', kwargs={'pk': obj.pk})
        res['deny'] = self.reverse('api:workflow_approval_deny', kwargs={'pk': obj.pk})
        if obj.approved_or_denied_by:
            res['approved_or_denied_by'] = self.reverse('api:user_detail', kwargs={'pk': obj.approved_or_denied_by.pk})
        return res


class WorkflowApprovalActivityStreamSerializer(WorkflowApprovalSerializer):
    """
    timed_out and status are usually read-only fields
    However, when we generate an activity stream record, we *want* to record
    these types of changes.  This serializer allows us to do so.
    """

    status = serializers.ChoiceField(choices=JobTemplate.JOB_TEMPLATE_STATUS_CHOICES)
    timed_out = serializers.BooleanField()


class WorkflowApprovalListSerializer(WorkflowApprovalSerializer, UnifiedJobListSerializer):
    class Meta:
        fields = ('*', '-controller_node', '-execution_node', 'can_approve_or_deny', 'approval_expiration', 'timed_out')


class WorkflowApprovalTemplateSerializer(UnifiedJobTemplateSerializer):
    class Meta:
        model = WorkflowApprovalTemplate
        fields = ('*', 'timeout', 'name')

    def get_related(self, obj):
        res = super(WorkflowApprovalTemplateSerializer, self).get_related(obj)
        if 'last_job' in res:
            del res['last_job']

        res.update(jobs=self.reverse('api:workflow_approval_template_jobs_list', kwargs={'pk': obj.pk}))
        return res


class LaunchConfigurationBaseSerializer(BaseSerializer):
    scm_branch = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    job_type = serializers.ChoiceField(allow_blank=True, allow_null=True, required=False, default=None, choices=NEW_JOB_TYPE_CHOICES)
    job_tags = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    limit = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    skip_tags = serializers.CharField(allow_blank=True, allow_null=True, required=False, default=None)
    diff_mode = serializers.BooleanField(required=False, allow_null=True, default=None)
    verbosity = serializers.ChoiceField(allow_null=True, required=False, default=None, choices=VERBOSITY_CHOICES)
    forks = serializers.IntegerField(required=False, allow_null=True, min_value=0, default=None)
    job_slice_count = serializers.IntegerField(required=False, allow_null=True, min_value=0, default=None)
    timeout = serializers.IntegerField(required=False, allow_null=True, default=None)
    exclude_errors = ()

    class Meta:
        fields = (
            '*',
            'extra_data',
            'inventory',  # Saved launch-time config fields
            'scm_branch',
            'job_type',
            'job_tags',
            'skip_tags',
            'limit',
            'skip_tags',
            'diff_mode',
            'verbosity',
            'execution_environment',
            'forks',
            'job_slice_count',
            'timeout',
        )

    def get_related(self, obj):
        res = super(LaunchConfigurationBaseSerializer, self).get_related(obj)
        if obj.inventory_id:
            res['inventory'] = self.reverse('api:inventory_detail', kwargs={'pk': obj.inventory_id})
        if obj.execution_environment_id:
            res['execution_environment'] = self.reverse('api:execution_environment_detail', kwargs={'pk': obj.execution_environment_id})
        res['labels'] = self.reverse('api:{}_labels_list'.format(get_type_for_model(self.Meta.model)), kwargs={'pk': obj.pk})
        res['credentials'] = self.reverse('api:{}_credentials_list'.format(get_type_for_model(self.Meta.model)), kwargs={'pk': obj.pk})
        res['instance_groups'] = self.reverse('api:{}_instance_groups_list'.format(get_type_for_model(self.Meta.model)), kwargs={'pk': obj.pk})
        return res

    def _build_mock_obj(self, attrs):
        mock_obj = self.Meta.model()
        if self.instance:
            for field in self.instance._meta.fields:
                setattr(mock_obj, field.name, getattr(self.instance, field.name))
        field_names = set(field.name for field in self.Meta.model._meta.fields)
        for field_name, value in list(attrs.items()):
            setattr(mock_obj, field_name, value)
            if field_name not in field_names:
                attrs.pop(field_name)
        return mock_obj

    def to_representation(self, obj):
        ret = super(LaunchConfigurationBaseSerializer, self).to_representation(obj)
        if obj is None:
            return ret
        if 'extra_data' in ret and obj.survey_passwords:
            ret['extra_data'] = obj.display_extra_vars()
        return ret

    def validate(self, attrs):
        db_extra_data = {}
        if self.instance:
            db_extra_data = parse_yaml_or_json(self.instance.extra_data)

        attrs = super(LaunchConfigurationBaseSerializer, self).validate(attrs)

        ujt = None
        if 'unified_job_template' in attrs:
            ujt = attrs['unified_job_template']
        elif self.instance:
            ujt = self.instance.unified_job_template
        if ujt is None:
            ret = {}
            for fd in ('workflow_job_template', 'identifier', 'all_parents_must_converge'):
                if fd in attrs:
                    ret[fd] = attrs[fd]
            return ret

        # build additional field survey_passwords to track redacted variables
        password_dict = {}
        extra_data = parse_yaml_or_json(attrs.get('extra_data', {}))
        if hasattr(ujt, 'survey_password_variables'):
            # Prepare additional field survey_passwords for save
            for key in ujt.survey_password_variables():
                if key in extra_data:
                    password_dict[key] = REPLACE_STR

        # Replace $encrypted$ submissions with db value if exists
        if 'extra_data' in attrs:
            if password_dict:
                if not self.instance or password_dict != self.instance.survey_passwords:
                    attrs['survey_passwords'] = password_dict.copy()
                # Force dict type (cannot preserve YAML formatting if passwords are involved)
                # Encrypt the extra_data for save, only current password vars in JT survey
                # but first, make a copy or else this is referenced by request.data, and
                # user could get encrypted string in form data in API browser
                attrs['extra_data'] = extra_data.copy()
                encrypt_dict(attrs['extra_data'], password_dict.keys())
                # For any raw $encrypted$ string, either
                # - replace with existing DB value
                # - raise a validation error
                # - ignore, if default present
                for key in password_dict.keys():
                    if attrs['extra_data'].get(key, None) == REPLACE_STR:
                        if key not in db_extra_data:
                            element = ujt.pivot_spec(ujt.survey_spec)[key]
                            # NOTE: validation _of_ the default values of password type
                            # questions not done here or on launch, but doing so could
                            # leak info about values, so it should not be added
                            if not ('default' in element and element['default']):
                                raise serializers.ValidationError({"extra_data": _('Provided variable {} has no database value to replace with.').format(key)})
                        else:
                            attrs['extra_data'][key] = db_extra_data[key]

        # Build unsaved version of this config, use it to detect prompts errors
        mock_obj = self._build_mock_obj(attrs)
        if set(list(ujt.get_ask_mapping().keys()) + ['extra_data']) & set(attrs.keys()):
            accepted, rejected, errors = ujt._accept_or_ignore_job_kwargs(_exclude_errors=self.exclude_errors, **mock_obj.prompts_dict())
        else:
            # Only perform validation of prompts if prompts fields are provided
            errors = {}

        # Remove all unprocessed $encrypted$ strings, indicating default usage
        if 'extra_data' in attrs and password_dict:
            for key, value in attrs['extra_data'].copy().items():
                if value == REPLACE_STR:
                    if key in password_dict:
                        attrs['extra_data'].pop(key)
                        attrs.get('survey_passwords', {}).pop(key, None)
                    else:
                        errors.setdefault('extra_vars', []).append(_('"$encrypted$ is a reserved keyword, may not be used for {}."'.format(key)))

        # Launch configs call extra_vars extra_data for historical reasons
        if 'extra_vars' in errors:
            errors['extra_data'] = errors.pop('extra_vars')
        if errors:
            raise serializers.ValidationError(errors)

        # Model `.save` needs the container dict, not the pseudo fields
        if mock_obj.char_prompts:
            attrs['char_prompts'] = mock_obj.char_prompts

        return attrs


class WorkflowJobTemplateNodeSerializer(LaunchConfigurationBaseSerializer):
    success_nodes = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    failure_nodes = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    always_nodes = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    exclude_errors = ('required',)  # required variables may be provided by WFJT or on launch

    class Meta:
        model = WorkflowJobTemplateNode
        fields = (
            '*',
            'workflow_job_template',
            '-name',
            '-description',
            'id',
            'url',
            'related',
            'unified_job_template',
            'success_nodes',
            'failure_nodes',
            'always_nodes',
            'all_parents_must_converge',
            'identifier',
        )

    def get_related(self, obj):
        res = super(WorkflowJobTemplateNodeSerializer, self).get_related(obj)
        res['create_approval_template'] = self.reverse('api:workflow_job_template_node_create_approval', kwargs={'pk': obj.pk})
        res['success_nodes'] = self.reverse('api:workflow_job_template_node_success_nodes_list', kwargs={'pk': obj.pk})
        res['failure_nodes'] = self.reverse('api:workflow_job_template_node_failure_nodes_list', kwargs={'pk': obj.pk})
        res['always_nodes'] = self.reverse('api:workflow_job_template_node_always_nodes_list', kwargs={'pk': obj.pk})
        if obj.unified_job_template:
            res['unified_job_template'] = obj.unified_job_template.get_absolute_url(self.context.get('request'))
        try:
            res['workflow_job_template'] = self.reverse('api:workflow_job_template_detail', kwargs={'pk': obj.workflow_job_template.pk})
        except WorkflowJobTemplate.DoesNotExist:
            pass
        return res

    def build_relational_field(self, field_name, relation_info):
        field_class, field_kwargs = super(WorkflowJobTemplateNodeSerializer, self).build_relational_field(field_name, relation_info)
        # workflow_job_template is read-only unless creating a new node.
        if self.instance and field_name == 'workflow_job_template':
            field_kwargs['read_only'] = True
            field_kwargs.pop('queryset', None)
        return field_class, field_kwargs

    def get_summary_fields(self, obj):
        summary_fields = super(WorkflowJobTemplateNodeSerializer, self).get_summary_fields(obj)
        if isinstance(obj.unified_job_template, WorkflowApprovalTemplate):
            summary_fields['unified_job_template']['timeout'] = obj.unified_job_template.timeout
        return summary_fields


class WorkflowJobNodeSerializer(LaunchConfigurationBaseSerializer):
    success_nodes = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    failure_nodes = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    always_nodes = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = WorkflowJobNode
        fields = (
            '*',
            'job',
            'workflow_job',
            '-name',
            '-description',
            'id',
            'url',
            'related',
            'unified_job_template',
            'success_nodes',
            'failure_nodes',
            'always_nodes',
            'all_parents_must_converge',
            'do_not_run',
            'identifier',
        )

    def get_related(self, obj):
        res = super(WorkflowJobNodeSerializer, self).get_related(obj)
        res['success_nodes'] = self.reverse('api:workflow_job_node_success_nodes_list', kwargs={'pk': obj.pk})
        res['failure_nodes'] = self.reverse('api:workflow_job_node_failure_nodes_list', kwargs={'pk': obj.pk})
        res['always_nodes'] = self.reverse('api:workflow_job_node_always_nodes_list', kwargs={'pk': obj.pk})
        if obj.unified_job_template:
            res['unified_job_template'] = obj.unified_job_template.get_absolute_url(self.context.get('request'))
        if obj.job:
            res['job'] = obj.job.get_absolute_url(self.context.get('request'))
        if obj.workflow_job:
            res['workflow_job'] = self.reverse('api:workflow_job_detail', kwargs={'pk': obj.workflow_job.pk})
        return res

    def get_summary_fields(self, obj):
        summary_fields = super(WorkflowJobNodeSerializer, self).get_summary_fields(obj)
        if isinstance(obj.job, WorkflowApproval):
            summary_fields['job']['timed_out'] = obj.job.timed_out
        return summary_fields


class WorkflowJobNodeListSerializer(WorkflowJobNodeSerializer):
    pass


class WorkflowJobNodeDetailSerializer(WorkflowJobNodeSerializer):
    pass


class WorkflowJobTemplateNodeDetailSerializer(WorkflowJobTemplateNodeSerializer):
    """
    Influence the api browser sample data to not include workflow_job_template
    when editing a WorkflowNode.

    Note: I was not able to accomplish this through the use of extra_kwargs.
    Maybe something to do with workflow_job_template being a relational field?
    """

    def build_relational_field(self, field_name, relation_info):
        field_class, field_kwargs = super(WorkflowJobTemplateNodeDetailSerializer, self).build_relational_field(field_name, relation_info)
        if self.instance and field_name == 'workflow_job_template':
            field_kwargs['read_only'] = True
            field_kwargs.pop('queryset', None)
        return field_class, field_kwargs


class WorkflowJobTemplateNodeCreateApprovalSerializer(BaseSerializer):
    class Meta:
        model = WorkflowApprovalTemplate
        fields = ('timeout', 'name', 'description')

    def to_representation(self, obj):
        return {}


class JobListSerializer(JobSerializer, UnifiedJobListSerializer):
    pass


class AdHocCommandListSerializer(AdHocCommandSerializer, UnifiedJobListSerializer):
    pass


class SystemJobListSerializer(SystemJobSerializer, UnifiedJobListSerializer):
    class Meta:
        model = SystemJob
        fields = ('*', '-controller_node')  # field removal undone by UJ serializer


class JobHostSummarySerializer(BaseSerializer):
    class Meta:
        model = JobHostSummary
        fields = (
            '*',
            '-name',
            '-description',
            'job',
            'host',
            'constructed_host',
            'host_name',
            'changed',
            'dark',
            'failures',
            'ok',
            'processed',
            'skipped',
            'failed',
            'ignored',
            'rescued',
        )

    def get_related(self, obj):
        res = super(JobHostSummarySerializer, self).get_related(obj)
        res.update(dict(job=self.reverse('api:job_detail', kwargs={'pk': obj.job.pk})))
        if obj.host is not None:
            res.update(dict(host=self.reverse('api:host_detail', kwargs={'pk': obj.host.pk})))
        return res

    def get_summary_fields(self, obj):
        d = super(JobHostSummarySerializer, self).get_summary_fields(obj)
        try:
            d['job']['job_template_id'] = obj.job.job_template.id
            d['job']['job_template_name'] = obj.job.job_template.name
        except (KeyError, AttributeError):
            pass
        return d


class JobEventSerializer(BaseSerializer):
    event_display = serializers.CharField(source='get_event_display2', read_only=True)
    event_level = serializers.IntegerField(read_only=True)

    class Meta:
        model = JobEvent
        fields = (
            '*',
            '-name',
            '-description',
            'job',
            'event',
            'counter',
            'event_display',
            'event_data',
            'event_level',
            'failed',
            'changed',
            'uuid',
            'parent_uuid',
            'host',
            'host_name',
            'playbook',
            'play',
            'task',
            'role',
            'stdout',
            'start_line',
            'end_line',
            'verbosity',
        )

    def get_related(self, obj):
        res = super(JobEventSerializer, self).get_related(obj)
        res.update(dict(job=self.reverse('api:job_detail', kwargs={'pk': obj.job_id})))
        res['children'] = self.reverse('api:job_event_children_list', kwargs={'pk': obj.pk})
        if obj.host_id:
            res['host'] = self.reverse('api:host_detail', kwargs={'pk': obj.host_id})
        return res

    def get_summary_fields(self, obj):
        d = super(JobEventSerializer, self).get_summary_fields(obj)
        try:
            d['job']['job_template_id'] = obj.job.job_template.id
            d['job']['job_template_name'] = obj.job.job_template.name
        except (KeyError, AttributeError):
            pass
        return d

    def to_representation(self, obj):
        data = super(JobEventSerializer, self).to_representation(obj)
        # Show full stdout for playbook_on_* events.
        if obj and obj.event.startswith('playbook_on'):
            return data
        # If the view logic says to not truncate (request was to the detail view or a param was used)
        if self.context.get('no_truncate', False):
            return data
        max_bytes = settings.EVENT_STDOUT_MAX_BYTES_DISPLAY
        if 'stdout' in data:
            data['stdout'] = truncate_stdout(data['stdout'], max_bytes)
        return data


class ProjectUpdateEventSerializer(JobEventSerializer):
    stdout = serializers.SerializerMethodField()
    event_data = serializers.SerializerMethodField()

    class Meta:
        model = ProjectUpdateEvent
        fields = ('*', '-name', '-description', '-job', '-job_id', '-parent_uuid', '-parent', '-host', 'project_update')

    def get_related(self, obj):
        res = super(JobEventSerializer, self).get_related(obj)
        res['project_update'] = self.reverse('api:project_update_detail', kwargs={'pk': obj.project_update_id})
        return res

    def get_stdout(self, obj):
        return UriCleaner.remove_sensitive(obj.stdout)

    def get_event_data(self, obj):
        # the project update playbook uses the git or svn modules
        # to clone repositories, and those modules are prone to printing
        # raw SCM URLs in their stdout (which *could* contain passwords)
        # attempt to detect and filter HTTP basic auth passwords in the stdout
        # of these types of events
        if obj.event_data.get('task_action') in ('git', 'svn', 'ansible.builtin.git', 'ansible.builtin.svn'):
            try:
                return json.loads(UriCleaner.remove_sensitive(json.dumps(obj.event_data)))
            except Exception:
                logger.exception("Failed to sanitize event_data")
                return {}
        else:
            return obj.event_data


class AdHocCommandEventSerializer(BaseSerializer):
    event_display = serializers.CharField(source='get_event_display', read_only=True)

    class Meta:
        model = AdHocCommandEvent
        fields = (
            '*',
            '-name',
            '-description',
            'ad_hoc_command',
            'event',
            'counter',
            'event_display',
            'event_data',
            'failed',
            'changed',
            'uuid',
            'host',
            'host_name',
            'stdout',
            'start_line',
            'end_line',
            'verbosity',
        )

    def get_related(self, obj):
        res = super(AdHocCommandEventSerializer, self).get_related(obj)
        res.update(dict(ad_hoc_command=self.reverse('api:ad_hoc_command_detail', kwargs={'pk': obj.ad_hoc_command_id})))
        if obj.host:
            res['host'] = self.reverse('api:host_detail', kwargs={'pk': obj.host.pk})
        return res

    def to_representation(self, obj):
        data = super(AdHocCommandEventSerializer, self).to_representation(obj)
        # If the view logic says to not truncate (request was to the detail view or a param was used)
        if self.context.get('no_truncate', False):
            return data
        max_bytes = settings.EVENT_STDOUT_MAX_BYTES_DISPLAY
        if 'stdout' in data:
            data['stdout'] = truncate_stdout(data['stdout'], max_bytes)
        return data


class InventoryUpdateEventSerializer(AdHocCommandEventSerializer):
    class Meta:
        model = InventoryUpdateEvent
        fields = ('*', '-name', '-description', '-ad_hoc_command', '-host', '-host_name', 'inventory_update')

    def get_related(self, obj):
        res = super(AdHocCommandEventSerializer, self).get_related(obj)
        res['inventory_update'] = self.reverse('api:inventory_update_detail', kwargs={'pk': obj.inventory_update_id})
        return res


class SystemJobEventSerializer(AdHocCommandEventSerializer):
    class Meta:
        model = SystemJobEvent
        fields = ('*', '-name', '-description', '-ad_hoc_command', '-host', '-host_name', 'system_job')

    def get_related(self, obj):
        res = super(AdHocCommandEventSerializer, self).get_related(obj)
        res['system_job'] = self.reverse('api:system_job_detail', kwargs={'pk': obj.system_job_id})
        return res


class JobLaunchSerializer(BaseSerializer):
    # Representational fields
    passwords_needed_to_start = serializers.ReadOnlyField()
    can_start_without_user_input = serializers.BooleanField(read_only=True)
    variables_needed_to_start = serializers.ReadOnlyField()
    credential_needed_to_start = serializers.SerializerMethodField()
    inventory_needed_to_start = serializers.SerializerMethodField()
    survey_enabled = serializers.SerializerMethodField()
    job_template_data = serializers.SerializerMethodField()
    defaults = serializers.SerializerMethodField()

    # Accepted on launch fields
    extra_vars = serializers.JSONField(required=False, write_only=True)
    inventory = serializers.PrimaryKeyRelatedField(queryset=Inventory.objects.all(), required=False, write_only=True)
    credentials = serializers.PrimaryKeyRelatedField(many=True, queryset=Credential.objects.all(), required=False, write_only=True)
    credential_passwords = VerbatimField(required=False, write_only=True)
    scm_branch = serializers.CharField(required=False, write_only=True, allow_blank=True)
    diff_mode = serializers.BooleanField(required=False, write_only=True)
    job_tags = serializers.CharField(required=False, write_only=True, allow_blank=True)
    job_type = serializers.ChoiceField(required=False, choices=NEW_JOB_TYPE_CHOICES, write_only=True)
    skip_tags = serializers.CharField(required=False, write_only=True, allow_blank=True)
    limit = serializers.CharField(required=False, write_only=True, allow_blank=True)
    verbosity = serializers.ChoiceField(required=False, choices=VERBOSITY_CHOICES, write_only=True)
    execution_environment = serializers.PrimaryKeyRelatedField(queryset=ExecutionEnvironment.objects.all(), required=False, write_only=True)
    labels = serializers.PrimaryKeyRelatedField(many=True, queryset=Label.objects.all(), required=False, write_only=True)
    forks = serializers.IntegerField(required=False, write_only=True, min_value=0)
    job_slice_count = serializers.IntegerField(required=False, write_only=True, min_value=0)
    timeout = serializers.IntegerField(required=False, write_only=True)
    instance_groups = serializers.PrimaryKeyRelatedField(many=True, queryset=InstanceGroup.objects.all(), required=False, write_only=True)

    class Meta:
        model = JobTemplate
        fields = (
            'can_start_without_user_input',
            'passwords_needed_to_start',
            'extra_vars',
            'inventory',
            'scm_branch',
            'limit',
            'job_tags',
            'skip_tags',
            'job_type',
            'verbosity',
            'diff_mode',
            'credentials',
            'credential_passwords',
            'ask_scm_branch_on_launch',
            'ask_variables_on_launch',
            'ask_tags_on_launch',
            'ask_diff_mode_on_launch',
            'ask_skip_tags_on_launch',
            'ask_job_type_on_launch',
            'ask_limit_on_launch',
            'ask_verbosity_on_launch',
            'ask_inventory_on_launch',
            'ask_credential_on_launch',
            'ask_execution_environment_on_launch',
            'ask_labels_on_launch',
            'ask_forks_on_launch',
            'ask_job_slice_count_on_launch',
            'ask_timeout_on_launch',
            'ask_instance_groups_on_launch',
            'survey_enabled',
            'variables_needed_to_start',
            'credential_needed_to_start',
            'inventory_needed_to_start',
            'job_template_data',
            'defaults',
            'verbosity',
            'execution_environment',
            'labels',
            'forks',
            'job_slice_count',
            'timeout',
            'instance_groups',
        )
        read_only_fields = (
            'ask_scm_branch_on_launch',
            'ask_diff_mode_on_launch',
            'ask_variables_on_launch',
            'ask_limit_on_launch',
            'ask_tags_on_launch',
            'ask_skip_tags_on_launch',
            'ask_job_type_on_launch',
            'ask_verbosity_on_launch',
            'ask_inventory_on_launch',
            'ask_credential_on_launch',
            'ask_execution_environment_on_launch',
            'ask_labels_on_launch',
            'ask_forks_on_launch',
            'ask_job_slice_count_on_launch',
            'ask_timeout_on_launch',
            'ask_instance_groups_on_launch',
        )

    def get_credential_needed_to_start(self, obj):
        return False

    def get_inventory_needed_to_start(self, obj):
        return not (obj and obj.inventory)

    def get_survey_enabled(self, obj):
        if obj:
            return obj.survey_enabled and 'spec' in obj.survey_spec
        return False

    def get_defaults(self, obj):
        defaults_dict = {}
        for field_name in JobTemplate.get_ask_mapping().keys():
            if field_name == 'inventory':
                defaults_dict[field_name] = dict(name=getattrd(obj, '%s.name' % field_name, None), id=getattrd(obj, '%s.pk' % field_name, None))
            elif field_name == 'credentials':
                for cred in obj.credentials.all():
                    cred_dict = dict(id=cred.id, name=cred.name, credential_type=cred.credential_type.pk, passwords_needed=cred.passwords_needed)
                    if cred.credential_type.managed and 'vault_id' in cred.credential_type.defined_fields:
                        cred_dict['vault_id'] = cred.get_input('vault_id', default=None)
                    defaults_dict.setdefault(field_name, []).append(cred_dict)
            elif field_name == 'execution_environment':
                if obj.execution_environment_id:
                    defaults_dict[field_name] = {'id': obj.execution_environment.id, 'name': obj.execution_environment.name}
                else:
                    defaults_dict[field_name] = {}
            elif field_name == 'labels':
                for label in obj.labels.all():
                    label_dict = {'id': label.id, 'name': label.name}
                    defaults_dict.setdefault(field_name, []).append(label_dict)
            elif field_name == 'instance_groups':
                defaults_dict[field_name] = []
            else:
                defaults_dict[field_name] = getattr(obj, field_name)
        return defaults_dict

    def get_job_template_data(self, obj):
        return dict(name=obj.name, id=obj.id, description=obj.description)

    def validate_extra_vars(self, value):
        return vars_validate_or_raise(value)

    def validate(self, attrs):
        template = self.context.get('template')

        accepted, rejected, errors = template._accept_or_ignore_job_kwargs(_exclude_errors=['prompts'], **attrs)  # make several error types non-blocking
        self._ignored_fields = rejected

        # Basic validation - cannot run a playbook without a playbook
        if not template.project:
            errors['project'] = _("A project is required to run a job.")
        else:
            failure_reason = template.project.get_reason_if_failed()
            if failure_reason:
                errors['playbook'] = failure_reason

        # cannot run a playbook without an inventory
        if template.inventory and template.inventory.pending_deletion is True:
            errors['inventory'] = _("The inventory associated with this Job Template is being deleted.")
        elif 'inventory' in accepted and accepted['inventory'].pending_deletion:
            errors['inventory'] = _("The provided inventory is being deleted.")

        # Prohibit providing multiple credentials of the same CredentialType.kind
        # or multiples of same vault id
        distinct_cred_kinds = []
        for cred in accepted.get('credentials', []):
            if cred.unique_hash() in distinct_cred_kinds:
                errors.setdefault('credentials', []).append(_('Cannot assign multiple {} credentials.').format(cred.unique_hash(display=True)))
            if cred.credential_type.kind not in ('ssh', 'vault', 'cloud', 'net', 'kubernetes'):
                errors.setdefault('credentials', []).append(_('Cannot assign a Credential of kind `{}`').format(cred.credential_type.kind))
            distinct_cred_kinds.append(cred.unique_hash())

        # Prohibit removing credentials from the JT list (unsupported for now)
        template_credentials = template.credentials.all()
        if 'credentials' in attrs:
            removed_creds = set(template_credentials) - set(attrs['credentials'])
            provided_mapping = Credential.unique_dict(attrs['credentials'])
            for cred in removed_creds:
                if cred.unique_hash() in provided_mapping.keys():
                    continue  # User replaced credential with new of same type
                errors.setdefault('credentials', []).append(
                    _('Removing {} credential at launch time without replacement is not supported. Provided list lacked credential(s): {}.').format(
                        cred.unique_hash(display=True), ', '.join([str(c) for c in removed_creds])
                    )
                )

        # verify that credentials (either provided or existing) don't
        # require launch-time passwords that have not been provided
        if 'credentials' in accepted:
            launch_credentials = Credential.unique_dict(list(template_credentials.all()) + list(accepted['credentials'])).values()
        else:
            launch_credentials = template_credentials
        passwords = attrs.get('credential_passwords', {})  # get from original attrs
        passwords_lacking = []
        for cred in launch_credentials:
            for p in cred.passwords_needed:
                if p not in passwords:
                    passwords_lacking.append(p)
                else:
                    accepted.setdefault('credential_passwords', {})
                    accepted['credential_passwords'][p] = passwords[p]
        if len(passwords_lacking):
            errors['passwords_needed_to_start'] = passwords_lacking

        if errors:
            raise serializers.ValidationError(errors)

        if 'extra_vars' in accepted:
            extra_vars_save = accepted['extra_vars']
        else:
            extra_vars_save = None
        # Validate job against JobTemplate clean_ methods
        accepted = super(JobLaunchSerializer, self).validate(accepted)
        # Preserve extra_vars as dictionary internally
        if extra_vars_save:
            accepted['extra_vars'] = extra_vars_save

        return accepted


class WorkflowJobLaunchSerializer(BaseSerializer):
    can_start_without_user_input = serializers.BooleanField(read_only=True)
    defaults = serializers.SerializerMethodField()
    variables_needed_to_start = serializers.ReadOnlyField()
    survey_enabled = serializers.SerializerMethodField()
    extra_vars = VerbatimField(required=False, write_only=True)
    inventory = serializers.PrimaryKeyRelatedField(queryset=Inventory.objects.all(), required=False, write_only=True)
    limit = serializers.CharField(required=False, write_only=True, allow_blank=True)
    scm_branch = serializers.CharField(required=False, write_only=True, allow_blank=True)
    workflow_job_template_data = serializers.SerializerMethodField()

    labels = serializers.PrimaryKeyRelatedField(many=True, queryset=Label.objects.all(), required=False, write_only=True)
    skip_tags = serializers.CharField(required=False, write_only=True, allow_blank=True)
    job_tags = serializers.CharField(required=False, write_only=True, allow_blank=True)

    class Meta:
        model = WorkflowJobTemplate
        fields = (
            'ask_inventory_on_launch',
            'ask_limit_on_launch',
            'ask_scm_branch_on_launch',
            'can_start_without_user_input',
            'defaults',
            'extra_vars',
            'inventory',
            'limit',
            'scm_branch',
            'survey_enabled',
            'variables_needed_to_start',
            'node_templates_missing',
            'node_prompts_rejected',
            'workflow_job_template_data',
            'survey_enabled',
            'ask_variables_on_launch',
            'ask_labels_on_launch',
            'labels',
            'ask_skip_tags_on_launch',
            'ask_tags_on_launch',
            'skip_tags',
            'job_tags',
        )
        read_only_fields = (
            'ask_inventory_on_launch',
            'ask_variables_on_launch',
            'ask_skip_tags_on_launch',
            'ask_labels_on_launch',
            'ask_limit_on_launch',
            'ask_scm_branch_on_launch',
            'ask_tags_on_launch',
        )

    def get_survey_enabled(self, obj):
        if obj:
            return obj.survey_enabled and 'spec' in obj.survey_spec
        return False

    def get_defaults(self, obj):
        defaults_dict = {}
        for field_name in WorkflowJobTemplate.get_ask_mapping().keys():
            if field_name == 'inventory':
                defaults_dict[field_name] = dict(name=getattrd(obj, '%s.name' % field_name, None), id=getattrd(obj, '%s.pk' % field_name, None))
            elif field_name == 'labels':
                for label in obj.labels.all():
                    label_dict = {"id": label.id, "name": label.name}
                    defaults_dict.setdefault(field_name, []).append(label_dict)
            else:
                defaults_dict[field_name] = getattr(obj, field_name)
        return defaults_dict

    def get_workflow_job_template_data(self, obj):
        return dict(name=obj.name, id=obj.id, description=obj.description)

    def validate(self, attrs):
        template = self.instance

        accepted, rejected, errors = template._accept_or_ignore_job_kwargs(**attrs)
        self._ignored_fields = rejected

        if template.inventory and template.inventory.pending_deletion is True:
            errors['inventory'] = _("The inventory associated with this Workflow is being deleted.")
        elif 'inventory' in accepted and accepted['inventory'].pending_deletion:
            errors['inventory'] = _("The provided inventory is being deleted.")

        if errors:
            raise serializers.ValidationError(errors)

        WFJT_extra_vars = template.extra_vars
        WFJT_inventory = template.inventory
        WFJT_limit = template.limit
        WFJT_scm_branch = template.scm_branch

        super(WorkflowJobLaunchSerializer, self).validate(attrs)
        template.extra_vars = WFJT_extra_vars
        template.inventory = WFJT_inventory
        template.limit = WFJT_limit
        template.scm_branch = WFJT_scm_branch

        return accepted


class BulkJobNodeSerializer(WorkflowJobNodeSerializer):
    # We don't do a PrimaryKeyRelatedField for unified_job_template and others, because that increases the number
    # of database queries, rather we take them as integer and later convert them to objects in get_objectified_jobs
    unified_job_template = serializers.IntegerField(
        required=True, min_value=1, help_text=_('Primary key of the template for this job, can be a job template or inventory source.')
    )
    inventory = serializers.IntegerField(required=False, min_value=1)
    execution_environment = serializers.IntegerField(required=False, min_value=1)
    # many-to-many fields
    credentials = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)
    labels = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)
    instance_groups = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)

    class Meta:
        model = WorkflowJobNode
        fields = ('*', 'credentials', 'labels', 'instance_groups')  # m2m fields are not canonical for WJ nodes

    def validate(self, attrs):
        return super(LaunchConfigurationBaseSerializer, self).validate(attrs)

    def get_validation_exclusions(self, obj=None):
        ret = super().get_validation_exclusions(obj)
        ret.extend(['unified_job_template', 'inventory', 'execution_environment'])
        return ret


class BulkJobLaunchSerializer(serializers.Serializer):
    name = serializers.CharField(default='Bulk Job Launch', max_length=512, write_only=True, required=False, allow_blank=True)  # limited by max name of jobs
    jobs = BulkJobNodeSerializer(
        many=True,
        allow_empty=False,
        write_only=True,
        max_length=100000,
        help_text=_('List of jobs to be launched, JSON. e.g. [{"unified_job_template": 7}, {"unified_job_template": 10}]'),
    )
    description = serializers.CharField(write_only=True, required=False, allow_blank=False)
    extra_vars = serializers.JSONField(write_only=True, required=False)
    organization = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(),
        required=False,
        default=None,
        allow_null=True,
        write_only=True,
        help_text=_('Inherit permissions from this organization. If not provided, a organization the user is a member of will be selected automatically.'),
    )
    inventory = serializers.PrimaryKeyRelatedField(queryset=Inventory.objects.all(), required=False, write_only=True)
    limit = serializers.CharField(write_only=True, required=False, allow_blank=False)
    scm_branch = serializers.CharField(write_only=True, required=False, allow_blank=False)
    skip_tags = serializers.CharField(write_only=True, required=False, allow_blank=False)
    job_tags = serializers.CharField(write_only=True, required=False, allow_blank=False)

    class Meta:
        model = WorkflowJob
        fields = ('name', 'jobs', 'description', 'extra_vars', 'organization', 'inventory', 'limit', 'scm_branch', 'skip_tags', 'job_tags')
        read_only_fields = ()

    def validate(self, attrs):
        request = self.context.get('request', None)
        identifiers = set()
        if len(attrs['jobs']) > settings.BULK_JOB_MAX_LAUNCH:
            raise serializers.ValidationError(_('Number of requested jobs exceeds system setting BULK_JOB_MAX_LAUNCH'))

        for node in attrs['jobs']:
            if 'identifier' in node:
                if node['identifier'] in identifiers:
                    raise serializers.ValidationError(_(f"Identifier {node['identifier']} not unique"))
                identifiers.add(node['identifier'])
            else:
                node['identifier'] = str(uuid4())

        requested_ujts = {j['unified_job_template'] for j in attrs['jobs']}
        requested_use_inventories = {job['inventory'] for job in attrs['jobs'] if 'inventory' in job}
        requested_use_execution_environments = {job['execution_environment'] for job in attrs['jobs'] if 'execution_environment' in job}
        requested_use_credentials = set()
        requested_use_labels = set()
        requested_use_instance_groups = set()
        for job in attrs['jobs']:
            for cred in job.get('credentials', []):
                requested_use_credentials.add(cred)
            for label in job.get('labels', []):
                requested_use_labels.add(label)
            for instance_group in job.get('instance_groups', []):
                requested_use_instance_groups.add(instance_group)

        key_to_obj_map = {
            "unified_job_template": {obj.id: obj for obj in UnifiedJobTemplate.objects.filter(id__in=requested_ujts)},
            "inventory": {obj.id: obj for obj in Inventory.objects.filter(id__in=requested_use_inventories)},
            "credentials": {obj.id: obj for obj in Credential.objects.filter(id__in=requested_use_credentials)},
            "labels": {obj.id: obj for obj in Label.objects.filter(id__in=requested_use_labels)},
            "instance_groups": {obj.id: obj for obj in InstanceGroup.objects.filter(id__in=requested_use_instance_groups)},
            "execution_environment": {obj.id: obj for obj in ExecutionEnvironment.objects.filter(id__in=requested_use_execution_environments)},
        }

        ujts = {}
        for ujt in key_to_obj_map['unified_job_template'].values():
            ujts.setdefault(type(ujt), [])
            ujts[type(ujt)].append(ujt)

        unallowed_types = set(ujts.keys()) - set([JobTemplate, Project, InventorySource, WorkflowJobTemplate])
        if unallowed_types:
            type_names = ' '.join([cls._meta.verbose_name.title() for cls in unallowed_types])
            raise serializers.ValidationError(_("Template types {type_names} not allowed in bulk jobs").format(type_names=type_names))

        for model, obj_list in ujts.items():
            role_field = 'execute_role' if issubclass(model, (JobTemplate, WorkflowJobTemplate)) else 'update_role'
            self.check_list_permission(model, set([obj.id for obj in obj_list]), role_field)

        self.check_organization_permission(attrs, request)

        if 'inventory' in attrs:
            requested_use_inventories.add(attrs['inventory'].id)

        self.check_list_permission(Inventory, requested_use_inventories, 'use_role')

        self.check_list_permission(Credential, requested_use_credentials, 'use_role')
        self.check_list_permission(Label, requested_use_labels)
        self.check_list_permission(InstanceGroup, requested_use_instance_groups)  # TODO: change to use_role for conflict
        self.check_list_permission(ExecutionEnvironment, requested_use_execution_environments)  # TODO: change if roles introduced

        jobs_object = self.get_objectified_jobs(attrs, key_to_obj_map)

        attrs['jobs'] = jobs_object
        if 'extra_vars' in attrs:
            extra_vars_dict = parse_yaml_or_json(attrs['extra_vars'])
            attrs['extra_vars'] = json.dumps(extra_vars_dict)
        attrs = super().validate(attrs)
        return attrs

    def check_list_permission(self, model, id_list, role_field=None):
        if not id_list:
            return
        user = self.context['request'].user
        if role_field is None:  # implies "read" level permission is required
            access_qs = user.get_queryset(model)
        else:
            access_qs = model.accessible_objects(user, role_field)

        not_allowed = set(id_list) - set(access_qs.filter(id__in=id_list).values_list('id', flat=True))
        if not_allowed:
            raise serializers.ValidationError(
                _("{model_name} {not_allowed} not found or you don't have permissions to access it").format(
                    model_name=model._meta.verbose_name_plural.title(), not_allowed=not_allowed
                )
            )

    def create(self, validated_data):
        request = self.context.get('request', None)
        launch_user = request.user if request else None
        job_node_data = validated_data.pop('jobs')
        wfj_deferred_attr_names = ('skip_tags', 'limit', 'job_tags')
        wfj_deferred_vals = {}
        for item in wfj_deferred_attr_names:
            wfj_deferred_vals[item] = validated_data.pop(item, None)

        wfj = WorkflowJob.objects.create(**validated_data, is_bulk_job=True, launch_type='manual', created_by=launch_user)
        for key, val in wfj_deferred_vals.items():
            if val:
                setattr(wfj, key, val)
        nodes = []
        node_m2m_objects = {}
        node_m2m_object_types_to_through_model = {
            'credentials': WorkflowJobNode.credentials.through,
            'labels': WorkflowJobNode.labels.through,
            'instance_groups': WorkflowJobNode.instance_groups.through,
        }
        node_deferred_attr_names = (
            'limit',
            'scm_branch',
            'verbosity',
            'forks',
            'diff_mode',
            'job_tags',
            'job_type',
            'skip_tags',
            'job_slice_count',
            'timeout',
        )
        node_deferred_attrs = {}
        for node_attrs in job_node_data:
            # we need to add any m2m objects after creation via the through model
            node_m2m_objects[node_attrs['identifier']] = {}
            node_deferred_attrs[node_attrs['identifier']] = {}
            for item in node_m2m_object_types_to_through_model.keys():
                if item in node_attrs:
                    node_m2m_objects[node_attrs['identifier']][item] = node_attrs.pop(item)

            # Some attributes are not accepted by WorkflowJobNode __init__, we have to set them after
            for item in node_deferred_attr_names:
                if item in node_attrs:
                    node_deferred_attrs[node_attrs['identifier']][item] = node_attrs.pop(item)

            # Create the node objects
            node_obj = WorkflowJobNode(workflow_job=wfj, created=wfj.created, modified=wfj.modified, **node_attrs)

            # we can set the deferred attrs now
            for item, value in node_deferred_attrs[node_attrs['identifier']].items():
                setattr(node_obj, item, value)

            # the node is now ready to be bulk created
            nodes.append(node_obj)

            # we'll need this later when we do the m2m through model bulk create
            node_m2m_objects[node_attrs['identifier']]['node'] = node_obj

        WorkflowJobNode.objects.bulk_create(nodes)

        # Deal with the m2m objects we have to create once the node exists
        for field_name, through_model in node_m2m_object_types_to_through_model.items():
            through_model_objects = []
            for node_identifier in node_m2m_objects.keys():
                if field_name in node_m2m_objects[node_identifier] and field_name == 'credentials':
                    for cred in node_m2m_objects[node_identifier][field_name]:
                        through_model_objects.append(through_model(credential=cred, workflowjobnode=node_m2m_objects[node_identifier]['node']))
                if field_name in node_m2m_objects[node_identifier] and field_name == 'labels':
                    for label in node_m2m_objects[node_identifier][field_name]:
                        through_model_objects.append(through_model(label=label, workflowjobnode=node_m2m_objects[node_identifier]['node']))
                if field_name in node_m2m_objects[node_identifier] and field_name == 'instance_groups':
                    for instance_group in node_m2m_objects[node_identifier][field_name]:
                        through_model_objects.append(through_model(instancegroup=instance_group, workflowjobnode=node_m2m_objects[node_identifier]['node']))
            if through_model_objects:
                through_model.objects.bulk_create(through_model_objects)

        wfj.save()
        wfj.signal_start()

        return WorkflowJobSerializer().to_representation(wfj)

    def check_organization_permission(self, attrs, request):
        # validate Organization
        # - If the orgs is not set, set it to the org of the launching user
        # - If the user is part of multiple orgs, throw a validation error saying user is part of multiple orgs, please provide one
        if not request.user.is_superuser:
            read_org_qs = Organization.accessible_objects(request.user, 'member_role')
            if 'organization' not in attrs or attrs['organization'] == None or attrs['organization'] == '':
                read_org_ct = read_org_qs.count()
                if read_org_ct == 1:
                    attrs['organization'] = read_org_qs.first()
                elif read_org_ct > 1:
                    raise serializers.ValidationError("User has permission to multiple Organizations, please set one of them in the request")
                else:
                    raise serializers.ValidationError("User not part of any organization, please assign an organization to assign to the bulk job")
            else:
                allowed_orgs = set(read_org_qs.values_list('id', flat=True))
                requested_org = attrs['organization']
                if requested_org.id not in allowed_orgs:
                    raise ValidationError(_(f"Organization {requested_org.id} not found or you don't have permissions to access it"))

    def get_objectified_jobs(self, attrs, key_to_obj_map):
        objectified_jobs = []
        # This loop is generalized so we should only have to add related items to the key_to_obj_map
        for job in attrs['jobs']:
            objectified_job = {}
            for key, value in job.items():
                if key in key_to_obj_map:
                    if isinstance(value, int):
                        objectified_job[key] = key_to_obj_map[key][value]
                    elif isinstance(value, list):
                        objectified_job[key] = [key_to_obj_map[key][item] for item in value]
                else:
                    objectified_job[key] = value
            objectified_jobs.append(objectified_job)
        return objectified_jobs


class NotificationTemplateSerializer(BaseSerializer):
    show_capabilities = ['edit', 'delete', 'copy']
    capabilities_prefetch = [{'copy': 'organization.admin'}]

    class Meta:
        model = NotificationTemplate
        fields = ('*', 'organization', 'notification_type', 'notification_configuration', 'messages')

    type_map = {"string": (str,), "int": (int,), "bool": (bool,), "list": (list,), "password": (str,), "object": (dict, OrderedDict)}

    def to_representation(self, obj):
        ret = super(NotificationTemplateSerializer, self).to_representation(obj)
        if 'notification_configuration' in ret:
            ret['notification_configuration'] = obj.display_notification_configuration()
        return ret

    def get_related(self, obj):
        res = super(NotificationTemplateSerializer, self).get_related(obj)
        res.update(
            dict(
                test=self.reverse('api:notification_template_test', kwargs={'pk': obj.pk}),
                notifications=self.reverse('api:notification_template_notification_list', kwargs={'pk': obj.pk}),
                copy=self.reverse('api:notification_template_copy', kwargs={'pk': obj.pk}),
            )
        )
        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})
        return res

    def _recent_notifications(self, obj):
        return [{'id': x.id, 'status': x.status, 'created': x.created, 'error': x.error} for x in obj.notifications.all().order_by('-created')[:5]]

    def get_summary_fields(self, obj):
        d = super(NotificationTemplateSerializer, self).get_summary_fields(obj)
        d['recent_notifications'] = self._recent_notifications(obj)
        return d

    def validate_messages(self, messages):
        if messages is None:
            return None

        error_list = []
        collected_messages = []

        def check_messages(messages):
            for message_type in messages:
                if message_type not in ('message', 'body'):
                    error_list.append(_("Message type '{}' invalid, must be either 'message' or 'body'").format(message_type))
                    continue
                message = messages[message_type]
                if message is None:
                    continue
                if not isinstance(message, str):
                    error_list.append(_("Expected string for '{}', found {}, ").format(message_type, type(message)))
                    continue
                if message_type == 'message':
                    if '\n' in message:
                        error_list.append(_("Messages cannot contain newlines (found newline in {} event)".format(event)))
                        continue
                collected_messages.append(message)

        # Validate structure / content types
        if not isinstance(messages, dict):
            error_list.append(_("Expected dict for 'messages' field, found {}".format(type(messages))))
        else:
            for event in messages:
                if event not in ('started', 'success', 'error', 'workflow_approval'):
                    error_list.append(_("Event '{}' invalid, must be one of 'started', 'success', 'error', or 'workflow_approval'").format(event))
                    continue
                event_messages = messages[event]
                if event_messages is None:
                    continue
                if not isinstance(event_messages, dict):
                    error_list.append(_("Expected dict for event '{}', found {}").format(event, type(event_messages)))
                    continue
                if event == 'workflow_approval':
                    for subevent in event_messages:
                        if subevent not in ('running', 'approved', 'timed_out', 'denied'):
                            error_list.append(
                                _("Workflow Approval event '{}' invalid, must be one of 'running', 'approved', 'timed_out', or 'denied'").format(subevent)
                            )
                            continue
                        subevent_messages = event_messages[subevent]
                        if subevent_messages is None:
                            continue
                        if not isinstance(subevent_messages, dict):
                            error_list.append(_("Expected dict for workflow approval event '{}', found {}").format(subevent, type(subevent_messages)))
                            continue
                        check_messages(subevent_messages)
                else:
                    check_messages(event_messages)

        # Subclass to return name of undefined field
        class DescriptiveUndefined(StrictUndefined):
            # The parent class prevents _accessing attributes_ of an object
            # but will render undefined objects with 'Undefined'. This
            # prevents their use entirely.
            __repr__ = __str__ = StrictUndefined._fail_with_undefined_error

            def __init__(self, *args, **kwargs):
                super(DescriptiveUndefined, self).__init__(*args, **kwargs)
                # When an undefined field is encountered, return the name
                # of the undefined field in the exception message
                # (StrictUndefined refers to the explicitly set exception
                # message as the 'hint')
                self._undefined_hint = self._undefined_name

        # Ensure messages can be rendered
        for msg in collected_messages:
            env = sandbox.ImmutableSandboxedEnvironment(undefined=DescriptiveUndefined)
            try:
                env.from_string(msg).render(JobNotificationMixin.context_stub())
            except TemplateSyntaxError as exc:
                error_list.append(_("Unable to render message '{}': {}".format(msg, exc.message)))
            except UndefinedError as exc:
                error_list.append(_("Field '{}' unavailable".format(exc.message)))
            except SecurityError as exc:
                error_list.append(_("Security error due to field '{}'".format(exc.message)))

        # Ensure that if a webhook body was provided, that it can be rendered as a dictionary
        notification_type = ''
        if self.instance:
            notification_type = getattr(self.instance, 'notification_type', '')
        else:
            notification_type = self.initial_data.get('notification_type', '')

        if notification_type == 'webhook':
            for event in messages:
                if not messages[event]:
                    continue
                if not isinstance(messages[event], dict):
                    continue
                body = messages[event].get('body', {})
                if body:
                    try:
                        sandbox.ImmutableSandboxedEnvironment(undefined=DescriptiveUndefined).from_string(body).render(JobNotificationMixin.context_stub())

                        # https://github.com/ansible/awx/issues/14410

                        # When rendering something such as "{{ job.id }}"
                        # the return type is not a dict, unlike "{{ job_metadata }}" which is a dict

                        # potential_body = json.loads(rendered_body)

                        # if not isinstance(potential_body, dict):
                        #     error_list.append(
                        #         _("Webhook body for '{}' should be a json dictionary. Found type '{}'.".format(event, type(potential_body).__name__))
                        #     )
                    except Exception as exc:
                        error_list.append(_("Webhook body for '{}' is not valid. The following gave an error ({}).".format(event, exc)))

        if error_list:
            raise serializers.ValidationError(error_list)

        return messages

    def validate(self, attrs):
        from awx.api.views import NotificationTemplateDetail

        notification_type = None
        if 'notification_type' in attrs:
            notification_type = attrs['notification_type']
        elif self.instance:
            notification_type = self.instance.notification_type
        else:
            notification_type = None
        if not notification_type:
            raise serializers.ValidationError(_('Missing required fields for Notification Configuration: notification_type'))

        notification_class = NotificationTemplate.CLASS_FOR_NOTIFICATION_TYPE[notification_type]
        missing_fields = []
        incorrect_type_fields = []
        password_fields_to_forward = []
        error_list = []
        if 'notification_configuration' not in attrs:
            return attrs
        if self.context['view'].kwargs and isinstance(self.context['view'], NotificationTemplateDetail):
            object_actual = self.context['view'].get_object()
        else:
            object_actual = None
        for field, params in notification_class.init_parameters.items():
            if field not in attrs['notification_configuration']:
                if 'default' in params:
                    attrs['notification_configuration'][field] = params['default']
                else:
                    missing_fields.append(field)
                    continue
            field_val = attrs['notification_configuration'][field]
            field_type = params['type']
            expected_types = self.type_map[field_type]
            if not type(field_val) in expected_types:
                incorrect_type_fields.append((field, field_type))
                continue
            if field_type == "list" and len(field_val) < 1:
                error_list.append(_("No values specified for field '{}'").format(field))
                continue
            if field_type == "password" and field_val == "$encrypted$" and object_actual is not None:
                password_fields_to_forward.append(field)
            if field == "http_method" and field_val.lower() not in ['put', 'post']:
                error_list.append(_("HTTP method must be either 'POST' or 'PUT'."))
        if missing_fields:
            error_list.append(_("Missing required fields for Notification Configuration: {}.").format(missing_fields))
        if incorrect_type_fields:
            for type_field_error in incorrect_type_fields:
                error_list.append(_("Configuration field '{}' incorrect type, expected {}.").format(type_field_error[0], type_field_error[1]))
        if error_list:
            raise serializers.ValidationError(error_list)

        # Only pull the existing encrypted passwords from the existing objects
        # to assign to the attribute and forward on the call stack IF AND ONLY IF
        # we know an error will not be raised in the validation phase.
        # Otherwise, the encrypted password will be exposed.
        for field in password_fields_to_forward:
            attrs['notification_configuration'][field] = object_actual.notification_configuration[field]
        return super(NotificationTemplateSerializer, self).validate(attrs)


class NotificationSerializer(BaseSerializer):
    body = serializers.SerializerMethodField(help_text=_('Notification body'))

    class Meta:
        model = Notification
        fields = (
            '*',
            '-name',
            '-description',
            'notification_template',
            'error',
            'status',
            'notifications_sent',
            'notification_type',
            'recipients',
            'subject',
            'body',
        )

    def get_body(self, obj):
        if obj.notification_type in ('webhook', 'pagerduty', 'awssns'):
            if isinstance(obj.body, dict):
                if 'body' in obj.body:
                    return obj.body['body']
            elif isinstance(obj.body, str):
                # attempt to load json string
                try:
                    potential_body = json.loads(obj.body)
                    if isinstance(potential_body, dict):
                        return potential_body
                except json.JSONDecodeError:
                    pass
        return obj.body

    def get_related(self, obj):
        res = super(NotificationSerializer, self).get_related(obj)
        res.update(dict(notification_template=self.reverse('api:notification_template_detail', kwargs={'pk': obj.notification_template.pk})))
        return res

    def to_representation(self, obj):
        ret = super(NotificationSerializer, self).to_representation(obj)

        if obj.notification_type in ('webhook', 'awssns'):
            ret.pop('subject')
        if obj.notification_type not in ('email', 'webhook', 'pagerduty', 'awssns'):
            ret.pop('body')
        return ret


class LabelSerializer(BaseSerializer):
    class Meta:
        model = Label
        fields = ('*', '-description', 'organization')

    def get_related(self, obj):
        res = super(LabelSerializer, self).get_related(obj)
        if obj.organization:
            res['organization'] = self.reverse('api:organization_detail', kwargs={'pk': obj.organization.pk})
        return res


class SchedulePreviewSerializer(BaseSerializer):
    class Meta:
        model = Schedule
        fields = ('rrule',)

    # We reject rrules if:
    # - DTSTART is not include
    # - Multiple DTSTART
    # - At least one of RRULE is not included
    # - EXDATE or RDATE is included
    # For any rule in the ruleset:
    #   - INTERVAL is not included
    #   - SECONDLY is used
    #   - BYDAY prefixed with a number (MO is good but not 20MO)
    #   - Can't contain both COUNT and UNTIL
    #   - COUNT > 999
    def validate_rrule(self, value):
        rrule_value = value
        by_day_with_numeric_prefix = r".*?BYDAY[\:\=][0-9]+[a-zA-Z]{2}"
        match_multiple_dtstart = re.findall(r".*?(DTSTART(;[^:]+)?\:[0-9]+T[0-9]+Z?)", rrule_value)
        match_native_dtstart = re.findall(r".*?(DTSTART:[0-9]+T[0-9]+) ", rrule_value)
        match_multiple_rrule = re.findall(r".*?(RULE\:[^\s]*)", rrule_value)
        errors = []
        if not len(match_multiple_dtstart):
            errors.append(_('Valid DTSTART required in rrule. Value should start with: DTSTART:YYYYMMDDTHHMMSSZ'))
        if len(match_native_dtstart):
            errors.append(_('DTSTART cannot be a naive datetime.  Specify ;TZINFO= or YYYYMMDDTHHMMSSZZ.'))
        if len(match_multiple_dtstart) > 1:
            errors.append(_('Multiple DTSTART is not supported.'))
        if "rrule:" not in rrule_value.lower():
            errors.append(_('One or more rule required in rrule.'))
        if "exdate:" in rrule_value.lower():
            raise serializers.ValidationError(_('EXDATE not allowed in rrule.'))
        if "rdate:" in rrule_value.lower():
            raise serializers.ValidationError(_('RDATE not allowed in rrule.'))
        for a_rule in match_multiple_rrule:
            if 'interval' not in a_rule.lower():
                errors.append("{0}: {1}".format(_('INTERVAL required in rrule'), a_rule))
            elif 'secondly' in a_rule.lower():
                errors.append("{0}: {1}".format(_('SECONDLY is not supported'), a_rule))
            if re.match(by_day_with_numeric_prefix, a_rule):
                errors.append("{0}: {1}".format(_("BYDAY with numeric prefix not supported"), a_rule))
            if 'COUNT' in a_rule and 'UNTIL' in a_rule:
                errors.append("{0}: {1}".format(_("RRULE may not contain both COUNT and UNTIL"), a_rule))
            match_count = re.match(r".*?(COUNT\=[0-9]+)", a_rule)
            if match_count:
                count_val = match_count.groups()[0].strip().split("=")
                if int(count_val[1]) > 999:
                    errors.append("{0}: {1}".format(_("COUNT > 999 is unsupported"), a_rule))

        try:
            Schedule.rrulestr(rrule_value)
        except Exception as e:
            import traceback

            logger.error(traceback.format_exc())
            errors.append(_("rrule parsing failed validation: {}").format(e))

        if errors:
            raise serializers.ValidationError(errors)

        return value


class ScheduleSerializer(LaunchConfigurationBaseSerializer, SchedulePreviewSerializer):
    show_capabilities = ['edit', 'delete']

    timezone = serializers.SerializerMethodField(
        help_text=_(
            'The timezone this schedule runs in. This field is extracted from the RRULE. If the timezone in the RRULE is a link to another timezone, the link will be reflected in this field.'
        ),
    )
    until = serializers.SerializerMethodField(
        help_text=_('The date this schedule will end. This field is computed from the RRULE. If the schedule does not end an empty string will be returned'),
    )

    class Meta:
        model = Schedule
        fields = ('*', 'unified_job_template', 'enabled', 'dtstart', 'dtend', 'rrule', 'next_run', 'timezone', 'until')

    def get_timezone(self, obj):
        return obj.timezone

    def get_until(self, obj):
        return obj.until

    def get_related(self, obj):
        res = super(ScheduleSerializer, self).get_related(obj)
        res.update(dict(unified_jobs=self.reverse('api:schedule_unified_jobs_list', kwargs={'pk': obj.pk})))
        if obj.unified_job_template:
            res['unified_job_template'] = obj.unified_job_template.get_absolute_url(self.context.get('request'))
            try:
                if obj.unified_job_template.project:
                    res['project'] = obj.unified_job_template.project.get_absolute_url(self.context.get('request'))
            except ObjectDoesNotExist:
                pass
        if obj.inventory:
            res['inventory'] = obj.inventory.get_absolute_url(self.context.get('request'))
        elif obj.unified_job_template and getattr(obj.unified_job_template, 'inventory', None):
            res['inventory'] = obj.unified_job_template.inventory.get_absolute_url(self.context.get('request'))
        return res

    def get_summary_fields(self, obj):
        summary_fields = super(ScheduleSerializer, self).get_summary_fields(obj)

        if isinstance(obj.unified_job_template, SystemJobTemplate):
            summary_fields['unified_job_template']['job_type'] = obj.unified_job_template.job_type

        # We are not showing instance groups on summary fields because JTs don't either

        if 'inventory' in summary_fields:
            return summary_fields

        inventory = None
        if obj.unified_job_template and getattr(obj.unified_job_template, 'inventory', None):
            inventory = obj.unified_job_template.inventory
        else:
            return summary_fields

        summary_fields['inventory'] = dict()
        for field in SUMMARIZABLE_FK_FIELDS['inventory']:
            summary_fields['inventory'][field] = getattr(inventory, field, None)

        return summary_fields

    def validate_unified_job_template(self, value):
        if type(value) == InventorySource and value.source not in load_combined_inventory_source_options():
            raise serializers.ValidationError(_('Inventory Source must be a cloud resource.'))
        elif type(value) == Project and value.scm_type == '':
            raise serializers.ValidationError(_('Manual Project cannot have a schedule set.'))
        return value

    def validate(self, attrs):
        # if the schedule is being disabled, there's no need
        # validate the related UnifiedJobTemplate
        # see: https://github.com/ansible/awx/issues/8641
        if self.context['request'].method == 'PATCH' and attrs == {'enabled': False}:
            return attrs
        return super(ScheduleSerializer, self).validate(attrs)


class InstanceLinkSerializer(BaseSerializer):
    class Meta:
        model = InstanceLink
        fields = ('id', 'related', 'source', 'target', 'target_full_address', 'link_state')

    source = serializers.SlugRelatedField(slug_field="hostname", queryset=Instance.objects.all())

    target = serializers.SerializerMethodField()
    target_full_address = serializers.SerializerMethodField()

    def get_related(self, obj):
        res = super(InstanceLinkSerializer, self).get_related(obj)
        res['source_instance'] = self.reverse('api:instance_detail', kwargs={'pk': obj.source.id})
        res['target_address'] = self.reverse('api:receptor_address_detail', kwargs={'pk': obj.target.id})
        return res

    def get_target(self, obj):
        return obj.target.instance.hostname

    def get_target_full_address(self, obj):
        return obj.target.get_full_address()


class InstanceNodeSerializer(BaseSerializer):
    class Meta:
        model = Instance
        fields = ('id', 'hostname', 'node_type', 'node_state', 'enabled')


class ReceptorAddressSerializer(BaseSerializer):
    full_address = serializers.SerializerMethodField()

    class Meta:
        model = ReceptorAddress
        fields = (
            'id',
            'url',
            'address',
            'port',
            'protocol',
            'websocket_path',
            'is_internal',
            'canonical',
            'instance',
            'peers_from_control_nodes',
            'full_address',
        )

    def get_full_address(self, obj):
        return obj.get_full_address()


class InstanceSerializer(BaseSerializer):
    show_capabilities = ['edit']

    consumed_capacity = serializers.SerializerMethodField()
    percent_capacity_remaining = serializers.SerializerMethodField()
    jobs_running = serializers.IntegerField(help_text=_('Count of jobs in the running or waiting state that are targeted for this instance'), read_only=True)
    jobs_total = serializers.IntegerField(help_text=_('Count of all jobs that target this instance'), read_only=True)
    health_check_pending = serializers.SerializerMethodField()
    peers = serializers.PrimaryKeyRelatedField(
        help_text=_('Primary keys of receptor addresses to peer to.'), many=True, required=False, queryset=ReceptorAddress.objects.all()
    )
    reverse_peers = serializers.SerializerMethodField()
    listener_port = serializers.IntegerField(source='canonical_address_port', required=False, allow_null=True)
    peers_from_control_nodes = serializers.BooleanField(source='canonical_address_peers_from_control_nodes', required=False)
    protocol = serializers.SerializerMethodField()

    class Meta:
        model = Instance
        read_only_fields = ('ip_address', 'uuid', 'version', 'managed', 'reverse_peers')
        fields = (
            'id',
            'hostname',
            'type',
            'url',
            'related',
            'summary_fields',
            'uuid',
            'created',
            'modified',
            'last_seen',
            'health_check_started',
            'health_check_pending',
            'last_health_check',
            'errors',
            'capacity_adjustment',
            'version',
            'capacity',
            'consumed_capacity',
            'percent_capacity_remaining',
            'jobs_running',
            'jobs_total',
            'cpu',
            'memory',
            'cpu_capacity',
            'mem_capacity',
            'enabled',
            'managed_by_policy',
            'node_type',
            'node_state',
            'managed',
            'ip_address',
            'peers',
            'reverse_peers',
            'listener_port',
            'peers_from_control_nodes',
            'protocol',
        )
        extra_kwargs = {
            'node_type': {'initial': Instance.Types.EXECUTION, 'default': Instance.Types.EXECUTION},
            'node_state': {'initial': Instance.States.INSTALLED, 'default': Instance.States.INSTALLED},
            'hostname': {
                'validators': [
                    MaxLengthValidator(limit_value=250),
                    validators.UniqueValidator(queryset=Instance.objects.all()),
                    RegexValidator(
                        regex=r'^localhost$|^127(?:\.[0-9]+){0,2}\.[0-9]+$|^(?:0*\:)*?:?0*1$',
                        flags=re.IGNORECASE,
                        inverse_match=True,
                        message="hostname cannot be localhost or 127.0.0.1",
                    ),
                    HostnameRegexValidator(),
                ],
            },
        }

    def get_related(self, obj):
        res = super(InstanceSerializer, self).get_related(obj)
        res['receptor_addresses'] = self.reverse('api:instance_receptor_addresses_list', kwargs={'pk': obj.pk})
        res['jobs'] = self.reverse('api:instance_unified_jobs_list', kwargs={'pk': obj.pk})
        res['peers'] = self.reverse('api:instance_peers_list', kwargs={"pk": obj.pk})
        res['instance_groups'] = self.reverse('api:instance_instance_groups_list', kwargs={'pk': obj.pk})
        if obj.node_type in [Instance.Types.EXECUTION, Instance.Types.HOP] and not obj.managed:
            res['install_bundle'] = self.reverse('api:instance_install_bundle', kwargs={'pk': obj.pk})
        if self.context['request'].user.is_superuser or self.context['request'].user.is_system_auditor:
            if obj.node_type == 'execution':
                res['health_check'] = self.reverse('api:instance_health_check', kwargs={'pk': obj.pk})
        return res

    def create_or_update(self, validated_data, obj=None, create=True):
        # create a managed receptor address if listener port is defined
        port = validated_data.pop('listener_port', -1)
        peers_from_control_nodes = validated_data.pop('peers_from_control_nodes', -1)

        # delete the receptor address if the port is explicitly set to None
        if obj and port == None:
            obj.receptor_addresses.filter(address=obj.hostname).delete()

        if create:
            instance = super(InstanceSerializer, self).create(validated_data)
        else:
            instance = super(InstanceSerializer, self).update(obj, validated_data)
            instance.refresh_from_db()  # instance canonical address lookup is deferred, so needs to be reloaded

        # only create or update if port is defined in validated_data or already exists in the
        # canonical address
        # this prevents creating a receptor address if peers_from_control_nodes is in
        # validated_data but a port is not set
        if (port != None and port != -1) or instance.canonical_address_port:
            kwargs = {}
            if port != -1:
                kwargs['port'] = port
            if peers_from_control_nodes != -1:
                kwargs['peers_from_control_nodes'] = peers_from_control_nodes
            if kwargs:
                kwargs['canonical'] = True
                instance.receptor_addresses.update_or_create(address=instance.hostname, defaults=kwargs)

        return instance

    def create(self, validated_data):
        return self.create_or_update(validated_data, create=True)

    def update(self, obj, validated_data):
        return self.create_or_update(validated_data, obj, create=False)

    def get_summary_fields(self, obj):
        summary = super().get_summary_fields(obj)

        # use this handle to distinguish between a listView and a detailView
        if self.is_detail_view:
            summary['links'] = InstanceLinkSerializer(InstanceLink.objects.select_related('target', 'source').filter(source=obj), many=True).data

        return summary

    def get_reverse_peers(self, obj):
        return Instance.objects.prefetch_related('peers').filter(peers__in=obj.receptor_addresses.all()).values_list('id', flat=True)

    def get_protocol(self, obj):
        # note: don't create a different query for receptor addresses, as this is prefetched on the View for optimization
        for addr in obj.receptor_addresses.all():
            if addr.canonical:
                return addr.protocol
        return ""

    def get_consumed_capacity(self, obj):
        return obj.consumed_capacity

    def get_percent_capacity_remaining(self, obj):
        if not obj.capacity or obj.consumed_capacity >= obj.capacity:
            return 0.0
        else:
            return float("{0:.2f}".format(((float(obj.capacity) - float(obj.consumed_capacity)) / (float(obj.capacity))) * 100))

    def get_health_check_pending(self, obj):
        return obj.health_check_pending

    def validate(self, attrs):
        # Oddly, using 'source' on a DRF field populates attrs with the source name, so we should rename it back
        if 'canonical_address_port' in attrs:
            attrs['listener_port'] = attrs.pop('canonical_address_port')
        if 'canonical_address_peers_from_control_nodes' in attrs:
            attrs['peers_from_control_nodes'] = attrs.pop('canonical_address_peers_from_control_nodes')

        if not self.instance and not settings.IS_K8S:
            raise serializers.ValidationError(_("Can only create instances on Kubernetes or OpenShift."))

        # cannot enable peers_from_control_nodes if listener_port is not set
        if attrs.get('peers_from_control_nodes'):
            port = attrs.get('listener_port', -1)  # -1 denotes missing, None denotes explicit null
            if (port is None) or (port == -1 and self.instance and self.instance.canonical_address is None):
                raise serializers.ValidationError(_("Cannot enable peers_from_control_nodes if listener_port is not set."))

        return super().validate(attrs)

    def validate_node_type(self, value):
        if not self.instance and value not in [Instance.Types.HOP, Instance.Types.EXECUTION]:
            raise serializers.ValidationError(_("Can only create execution or hop nodes."))

        if self.instance and self.instance.node_type != value:
            raise serializers.ValidationError(_("Cannot change node type."))

        return value

    def validate_node_state(self, value):
        if self.instance:
            if value != self.instance.node_state:
                if not settings.IS_K8S:
                    raise serializers.ValidationError(_("Can only change the state on Kubernetes or OpenShift."))
                if value != Instance.States.DEPROVISIONING:
                    raise serializers.ValidationError(_("Can only change instances to the 'deprovisioning' state."))
                if self.instance.managed:
                    raise serializers.ValidationError(_("Cannot deprovision managed nodes."))
        else:
            if value and value != Instance.States.INSTALLED:
                raise serializers.ValidationError(_("Can only create instances in the 'installed' state."))

        return value

    def validate_hostname(self, value):
        """
        Cannot change the hostname
        """
        if self.instance and self.instance.hostname != value:
            raise serializers.ValidationError(_("Cannot change hostname."))

        return value

    def validate_listener_port(self, value):
        """
        Cannot change listener port, unless going from none to integer, and vice versa
        If instance is managed, cannot change listener port at all
        """
        if self.instance:
            canonical_address_port = self.instance.canonical_address_port
            if value and canonical_address_port and canonical_address_port != value:
                raise serializers.ValidationError(_("Cannot change listener port."))
            if self.instance.managed and value != canonical_address_port:
                raise serializers.ValidationError(_("Cannot change listener port for managed nodes."))
        return value

    def validate_peers(self, value):
        # cannot peer to an instance more than once
        peers_instances = Counter(p.instance_id for p in value)
        if any(count > 1 for count in peers_instances.values()):
            raise serializers.ValidationError(_("Cannot peer to the same instance more than once."))

        if self.instance:
            instance_addresses = set(self.instance.receptor_addresses.all())
            setting_peers = set(value)
            peers_changed = set(self.instance.peers.all()) != setting_peers

            if not settings.IS_K8S and peers_changed:
                raise serializers.ValidationError(_("Cannot change peers."))

            if self.instance.managed and peers_changed:
                raise serializers.ValidationError(_("Setting peers manually for managed nodes is not allowed."))

            # cannot peer to self
            if instance_addresses & setting_peers:
                raise serializers.ValidationError(_("Instance cannot peer to its own address."))

            # cannot peer to an instance that is already peered to this instance
            if instance_addresses:
                for p in setting_peers:
                    if set(p.instance.peers.all()) & instance_addresses:
                        raise serializers.ValidationError(_(f"Instance {p.instance.hostname} is already peered to this instance."))

        return value

    def validate_peers_from_control_nodes(self, value):
        if self.instance and self.instance.managed and self.instance.canonical_address_peers_from_control_nodes != value:
            raise serializers.ValidationError(_("Cannot change peers_from_control_nodes for managed nodes."))

        return value


class InstanceHealthCheckSerializer(BaseSerializer):
    class Meta:
        model = Instance
        read_only_fields = (
            'uuid',
            'hostname',
            'ip_address',
            'version',
            'last_health_check',
            'errors',
            'cpu',
            'memory',
            'cpu_capacity',
            'mem_capacity',
            'capacity',
        )
        fields = read_only_fields


class HostMetricSerializer(BaseSerializer):
    show_capabilities = ['delete']

    class Meta:
        model = HostMetric
        fields = (
            "id",
            "hostname",
            "url",
            "first_automation",
            "last_automation",
            "last_deleted",
            "automated_counter",
            "deleted_counter",
            "deleted",
            "used_in_inventories",
        )


class HostMetricSummaryMonthlySerializer(BaseSerializer):
    class Meta:
        model = HostMetricSummaryMonthly
        read_only_fields = ("id", "date", "license_consumed", "license_capacity", "hosts_added", "hosts_deleted", "indirectly_managed_hosts")
        fields = read_only_fields


class InstanceGroupSerializer(BaseSerializer):
    show_capabilities = ['edit', 'delete']
    capacity = serializers.SerializerMethodField()
    consumed_capacity = serializers.SerializerMethodField()
    percent_capacity_remaining = serializers.SerializerMethodField()
    jobs_running = serializers.SerializerMethodField()
    jobs_total = serializers.IntegerField(help_text=_('Count of all jobs that target this instance group'), read_only=True)
    instances = serializers.SerializerMethodField()
    is_container_group = serializers.BooleanField(
        required=False,
        help_text=_('Indicates whether instances in this group are containerized.Containerized groups have a designated Openshift or Kubernetes cluster.'),
    )
    # NOTE: help_text is duplicated from field definitions, no obvious way of
    # both defining field details here and also getting the field's help_text
    policy_instance_percentage = serializers.IntegerField(
        default=0,
        min_value=0,
        max_value=100,
        required=False,
        initial=0,
        label=_('Policy Instance Percentage'),
        help_text=_("Minimum percentage of all instances that will be automatically assigned to this group when new instances come online."),
    )
    policy_instance_minimum = serializers.IntegerField(
        default=0,
        min_value=0,
        required=False,
        initial=0,
        label=_('Policy Instance Minimum'),
        help_text=_("Static minimum number of Instances that will be automatically assign to this group when new instances come online."),
    )
    max_concurrent_jobs = serializers.IntegerField(
        default=0,
        min_value=0,
        required=False,
        initial=0,
        label=_('Max Concurrent Jobs'),
        help_text=_("Maximum number of concurrent jobs to run on a group. When set to zero, no maximum is enforced."),
    )
    max_forks = serializers.IntegerField(
        default=0,
        min_value=0,
        required=False,
        initial=0,
        label=_('Max Forks'),
        help_text=_("Maximum number of forks to execute concurrently on a group. When set to zero, no maximum is enforced."),
    )
    policy_instance_list = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        label=_('Policy Instance List'),
        help_text=_("List of exact-match Instances that will be assigned to this group"),
    )

    class Meta:
        model = InstanceGroup
        fields = (
            "id",
            "type",
            "url",
            "related",
            "name",
            "created",
            "modified",
            "capacity",
            "consumed_capacity",
            "percent_capacity_remaining",
            "jobs_running",
            "max_concurrent_jobs",
            "max_forks",
            "jobs_total",
            "instances",
            "is_container_group",
            "credential",
            "policy_instance_percentage",
            "policy_instance_minimum",
            "policy_instance_list",
            "pod_spec_override",
            "summary_fields",
        )

    def get_related(self, obj):
        res = super(InstanceGroupSerializer, self).get_related(obj)
        res['jobs'] = self.reverse('api:instance_group_unified_jobs_list', kwargs={'pk': obj.pk})
        res['instances'] = self.reverse('api:instance_group_instance_list', kwargs={'pk': obj.pk})
        res['access_list'] = self.reverse('api:instance_group_access_list', kwargs={'pk': obj.pk})
        res['object_roles'] = self.reverse('api:instance_group_object_role_list', kwargs={'pk': obj.pk})
        if obj.credential:
            res['credential'] = self.reverse('api:credential_detail', kwargs={'pk': obj.credential_id})

        return res

    def validate_policy_instance_list(self, value):
        if self.instance and self.instance.name in [settings.DEFAULT_EXECUTION_QUEUE_NAME, settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME]:
            if self.instance.policy_instance_list != value:
                raise serializers.ValidationError(_('%s instance group policy_instance_list may not be changed.' % self.instance.name))
        for instance_name in value:
            if value.count(instance_name) > 1:
                raise serializers.ValidationError(_('Duplicate entry {}.').format(instance_name))
            if not Instance.objects.filter(hostname=instance_name).exists():
                raise serializers.ValidationError(_('{} is not a valid hostname of an existing instance.').format(instance_name))
        if value and self.instance and self.instance.is_container_group:
            raise serializers.ValidationError(_('Containerized instances may not be managed via the API'))
        return value

    def validate_policy_instance_percentage(self, value):
        if self.instance and self.instance.name in [settings.DEFAULT_EXECUTION_QUEUE_NAME, settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME]:
            if value != self.instance.policy_instance_percentage:
                raise serializers.ValidationError(
                    _('%s instance group policy_instance_percentage may not be changed from the initial value set by the installer.' % self.instance.name)
                )
        if value and self.instance and self.instance.is_container_group:
            raise serializers.ValidationError(_('Containerized instances may not be managed via the API'))
        return value

    def validate_policy_instance_minimum(self, value):
        if value and self.instance and self.instance.is_container_group:
            raise serializers.ValidationError(_('Containerized instances may not be managed via the API'))
        return value

    def validate_name(self, value):
        if self.instance and self.instance.name == settings.DEFAULT_EXECUTION_QUEUE_NAME and value != settings.DEFAULT_EXECUTION_QUEUE_NAME:
            raise serializers.ValidationError(_('%s instance group name may not be changed.' % settings.DEFAULT_EXECUTION_QUEUE_NAME))

        if self.instance and self.instance.name == settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME and value != settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME:
            raise serializers.ValidationError(_('%s instance group name may not be changed.' % settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME))

        return value

    def validate_is_container_group(self, value):
        if self.instance and self.instance.name in [settings.DEFAULT_EXECUTION_QUEUE_NAME, settings.DEFAULT_CONTROL_PLANE_QUEUE_NAME]:
            if value != self.instance.is_container_group:
                raise serializers.ValidationError(_('%s instance group is_container_group may not be changed.' % self.instance.name))

        return value

    def validate_credential(self, value):
        if value and not value.kubernetes:
            raise serializers.ValidationError(_('Only Kubernetes credentials can be associated with an Instance Group'))
        return value

    def validate(self, attrs):
        attrs = super(InstanceGroupSerializer, self).validate(attrs)

        if attrs.get('credential') and not attrs.get('is_container_group'):
            raise serializers.ValidationError({'is_container_group': _('is_container_group must be True when associating a credential to an Instance Group')})

        return attrs

    def get_ig_mgr(self):
        # Store capacity values (globally computed) in the context
        if 'task_manager_igs' not in self.context:
            instance_groups_queryset = None
            if self.parent:  # Is ListView:
                instance_groups_queryset = self.parent.instance

            tm_models = TaskManagerModels.init_with_consumed_capacity(
                instance_fields=['uuid', 'version', 'capacity', 'cpu', 'memory', 'managed_by_policy', 'enabled'],
                instance_groups_queryset=instance_groups_queryset,
            )

            self.context['task_manager_igs'] = tm_models.instance_groups
        return self.context['task_manager_igs']

    def get_consumed_capacity(self, obj):
        ig_mgr = self.get_ig_mgr()
        return ig_mgr.get_consumed_capacity(obj.name)

    def get_capacity(self, obj):
        ig_mgr = self.get_ig_mgr()
        return ig_mgr.get_capacity(obj.name)

    def get_percent_capacity_remaining(self, obj):
        capacity = self.get_capacity(obj)
        if not capacity:
            return 0.0
        consumed_capacity = self.get_consumed_capacity(obj)
        return float("{0:.2f}".format(((float(capacity) - float(consumed_capacity)) / (float(capacity))) * 100))

    def get_instances(self, obj):
        ig_mgr = self.get_ig_mgr()
        return len(ig_mgr.get_instances(obj.name))

    def get_jobs_running(self, obj):
        ig_mgr = self.get_ig_mgr()
        return ig_mgr.get_jobs_running(obj.name)


class ActivityStreamSerializer(BaseSerializer):
    changes = serializers.SerializerMethodField()
    object_association = serializers.SerializerMethodField(help_text=_("When present, shows the field name of the role or relationship that changed."))
    object_type = serializers.SerializerMethodField(help_text=_("When present, shows the model on which the role or relationship was defined."))

    def _local_summarizable_fk_fields(self, obj):
        summary_dict = copy.copy(SUMMARIZABLE_FK_FIELDS)
        # Special requests
        summary_dict['group'] = summary_dict['group'] + ('inventory_id',)
        for key in summary_dict.keys():
            if 'id' not in summary_dict[key]:
                summary_dict[key] = summary_dict[key] + ('id',)
        field_list = list(summary_dict.items())
        # Needed related fields that are not in the default summary fields
        field_list += [
            ('workflow_job_template_node', ('id', 'unified_job_template_id')),
            ('label', ('id', 'name', 'organization_id')),
            ('notification', ('id', 'status', 'notification_type', 'notification_template_id')),
            ('credential_type', ('id', 'name', 'description', 'kind', 'managed')),
            ('ad_hoc_command', ('id', 'name', 'status', 'limit')),
            ('workflow_approval', ('id', 'name', 'unified_job_id')),
            ('instance', ('id', 'hostname')),
        ]
        # Optimization - do not attempt to summarize all fields, pair down to only relations that exist
        if not obj:
            return field_list
        existing_association_types = [obj.object1, obj.object2]
        if 'user' in existing_association_types:
            existing_association_types.append('role')
        return [entry for entry in field_list if entry[0] in existing_association_types]

    class Meta:
        model = ActivityStream
        fields = (
            '*',
            '-name',
            '-description',
            '-created',
            '-modified',
            'timestamp',
            'operation',
            'changes',
            'object1',
            'object2',
            'object_association',
            'action_node',
            'object_type',
        )

    def get_fields(self):
        ret = super(ActivityStreamSerializer, self).get_fields()
        for key, field in list(ret.items()):
            if key == 'changes':
                field.help_text = _('A summary of the new and changed values when an object is created, updated, or deleted')
            if key == 'object1':
                field.help_text = _(
                    'For create, update, and delete events this is the object type that was affected. '
                    'For associate and disassociate events this is the object type associated or disassociated with object2.'
                )
            if key == 'object2':
                field.help_text = _(
                    'Unpopulated for create, update, and delete events. For associate and disassociate '
                    'events this is the object type that object1 is being associated with.'
                )
            if key == 'operation':
                field.help_text = _('The action taken with respect to the given object(s).')
        return ret

    def get_changes(self, obj):
        if obj is None:
            return {}
        try:
            return json.loads(obj.changes)
        except Exception:
            logger.warning("Error deserializing activity stream json changes")
        return {}

    def get_object_association(self, obj):
        if not obj.object_relationship_type:
            return ""
        elif obj.object_relationship_type.endswith('_role'):
            # roles: these values look like
            # "awx.main.models.inventory.Inventory.admin_role"
            # due to historical reasons the UI expects just "role" here
            return "role"
        # default case: these values look like
        # "awx.main.models.organization.Organization_notification_templates_success"
        # so instead of splitting on period we have to take after the first underscore
        try:
            return obj.object_relationship_type.split(".")[-1].split("_", 1)[1]
        except Exception:
            logger.debug('Failed to parse activity stream relationship type {}'.format(obj.object_relationship_type))
            return ""

    def get_object_type(self, obj):
        if not obj.object_relationship_type:
            return ""
        elif obj.object_relationship_type.endswith('_role'):
            return camelcase_to_underscore(obj.object_relationship_type.rsplit('.', 2)[-2])
        # default case: these values look like
        # "awx.main.models.organization.Organization_notification_templates_success"
        # so we have to take after the last period but before the first underscore.
        try:
            cls = obj.object_relationship_type.rsplit('.', 1)[0]
            return camelcase_to_underscore(cls.split('_', 1))
        except Exception:
            logger.debug('Failed to parse activity stream relationship type {}'.format(obj.object_relationship_type))
            return ""

    def get_related(self, obj):
        data = {}
        if obj.actor is not None:
            data['actor'] = self.reverse('api:user_detail', kwargs={'pk': obj.actor.pk})
        for fk, __ in self._local_summarizable_fk_fields(obj):
            if not hasattr(obj, fk):
                continue
            m2m_list = self._get_related_objects(obj, fk)
            if m2m_list:
                data[fk] = []
                id_list = []
                for item in m2m_list:
                    if getattr(item, 'id', None) in id_list:
                        continue
                    id_list.append(getattr(item, 'id', None))
                    if hasattr(item, 'get_absolute_url'):
                        url = item.get_absolute_url(self.context.get('request'))
                    else:
                        view_name = fk + '_detail'
                        url = self.reverse('api:' + view_name, kwargs={'pk': item.id})
                    data[fk].append(url)

                    if fk == 'schedule':
                        data['unified_job_template'] = item.unified_job_template.get_absolute_url(self.context.get('request'))
        if obj.setting and obj.setting.get('category', None):
            data['setting'] = self.reverse('api:setting_singleton_detail', kwargs={'category_slug': obj.setting['category']})
        return data

    def _get_related_objects(self, obj, fk):
        related_model = ActivityStream._meta.get_field(fk).related_model
        related_manager = getattr(obj, fk)
        if issubclass(related_model, PolymorphicModel) and hasattr(obj, '_prefetched_objects_cache'):
            # HACK: manually fill PolymorphicModel caches to prevent running query multiple times
            # unnecessary if django-polymorphic issue #68 is solved
            if related_manager.prefetch_cache_name not in obj._prefetched_objects_cache:
                obj._prefetched_objects_cache[related_manager.prefetch_cache_name] = list(related_manager.all())
        return related_manager.all()

    def _summarize_parent_ujt(self, obj, fk, summary_fields):
        summary_keys = {
            'job': 'job_template',
            'workflow_job_template_node': 'workflow_job_template',
            'workflow_approval_template': 'workflow_job_template',
            'workflow_approval': 'workflow_job',
            'schedule': 'unified_job_template',
        }
        if fk not in summary_keys:
            return
        related_obj = getattr(obj, summary_keys[fk], None)
        item = {}
        fields = SUMMARIZABLE_FK_FIELDS[summary_keys[fk]]
        if related_obj is not None:
            summary_fields[get_type_for_model(related_obj)] = []
            for field in fields:
                fval = getattr(related_obj, field, None)
                if fval is not None:
                    item[field] = fval
            summary_fields[get_type_for_model(related_obj)].append(item)

    def get_summary_fields(self, obj):
        summary_fields = OrderedDict()
        for fk, related_fields in self._local_summarizable_fk_fields(obj):
            try:
                if not hasattr(obj, fk):
                    continue
                m2m_list = self._get_related_objects(obj, fk)
                if m2m_list:
                    summary_fields[fk] = []
                    for thisItem in m2m_list:
                        self._summarize_parent_ujt(thisItem, fk, summary_fields)
                        thisItemDict = {}
                        for field in related_fields:
                            fval = getattr(thisItem, field, None)
                            if fval is not None:
                                thisItemDict[field] = fval
                        summary_fields[fk].append(thisItemDict)
            except ObjectDoesNotExist:
                pass
        if obj.actor is not None:
            summary_fields['actor'] = dict(id=obj.actor.id, username=obj.actor.username, first_name=obj.actor.first_name, last_name=obj.actor.last_name)
        elif obj.deleted_actor:
            summary_fields['actor'] = obj.deleted_actor.copy()
            summary_fields['actor']['id'] = None
        if obj.setting:
            summary_fields['setting'] = [obj.setting]
        return summary_fields
