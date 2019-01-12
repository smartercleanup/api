from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.gis.geos import GEOSGeometry, Point, Polygon
from django.core import cache as django_cache
from django.core.urlresolvers import reverse
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.test.client import RequestFactory
from django.test.utils import override_settings
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import (views, permissions, mixins, authentication,
                            generics, exceptions, status)
from oauth2_provider.ext.rest_framework import authentication as oauth2Authentication
from rest_framework.parsers import JSONParser, FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer, BrowsableAPIRenderer
from rest_framework_jsonp.renderers import JSONPRenderer
from rest_framework.request import Request
from rest_framework.exceptions import APIException
from rest_framework_bulk import generics as bulk_generics
from social_django import views as social_views
from mock import patch
from .. import apikey
from .. import cors
from .. import models
from .. import serializers
from .. import utils
from .. import renderers
from .. import parsers
from ..cors.auth import OriginAuthentication
from ..apikey.auth import ApiKeyAuthentication
from .email_templates import EmailTemplateMixin
from .. import tasks
from .. import utils
from .content_negotiation import ShareaboutsContentNegotiation
from ..cache import cache_buffer
from ..params import (INCLUDE_INVISIBLE_PARAM, INCLUDE_PRIVATE_PARAM,
    INCLUDE_SUBMISSIONS_PARAM, NEAR_PARAM, DISTANCE_PARAM, BBOX_PARAM,
    TEXTSEARCH_PARAM, FORMAT_PARAM, PAGE_PARAM, PAGE_SIZE_PARAM,
    CALLBACK_PARAM)
from functools import wraps
from itertools import groupby, count
from collections import defaultdict
from urllib import urlencode
import re
import requests
import ujson as json
import logging
import bleach
from .authentication import ShareaboutsSessionAuth
from .permissions import (IsAdminOwnerOrReadOnly, IsLoggedInOwnerOrPublicDataOnly,
                          IsLoggedInOwner, IsLoggedInAdmin,
                          IsAllowedByDataPermissions)

logger = logging.getLogger('sa_api_v2.views')


###############################################################################
#
# View Mixins
# -----------
#


class ShareaboutsAPIRequest (Request):
    """
    A subclass of the DRF Request that allows dual authentication as a user
    and an application (client) at the same time.
    """

    def __init__(self, request, parsers=None, authenticators=None,
                 client_authenticators=None, negotiator=None,
                 parser_context=None):
        super(ShareaboutsAPIRequest, self).__init__(request,
            parsers=parsers, authenticators=authenticators,
            negotiator=negotiator, parser_context=parser_context)
        self.client_authenticators = client_authenticators

    @property
    def client(self):
        """
        Returns the client associated with the current request, as authenticated
        by the authentication classes provided to the request.
        """
        if not hasattr(self, '_client'):
            self._authenticate_client()
        return self._client

    @client.setter
    def client(self, value):
        """
        Sets the client on the current request.
        """
        self._client = value

    @property
    def client_auth(self):
        """
        Returns any non-client authentication information associated with the
        request, such as an authentication token.
        """
        if not hasattr(self, '_client_auth'):
            self._authenticate_client()
        return self._auth

    @client_auth.setter
    def client_auth(self, value):
        """
        Sets any non-client authentication information associated with the
        request, such as an authentication token.
        """
        self._client_auth = value

    @property
    def successful_authenticator(self):
        """
        Return the instance of the authentication instance class that was used
        to authenticate the request, or `None`.
        """
        authenticator = super(ShareaboutsAPIRequest, self).successful_authenticator

        if not authenticator:
            if not hasattr(self, '_client_authenticator'):
                self._authenticate_client()
            authenticator = self._client_authenticator

        return authenticator

    def _authenticate_client(self):
        """
        Attempt to authenticate the request using each authentication instance
        in turn.
        Returns a three-tuple of (authenticator, client, client_authtoken).
        """
        for authenticator in self.client_authenticators:
            try:
                client_auth_tuple = authenticator.authenticate(self)
            except exceptions.APIException:
                self._client_not_authenticated()
                raise

            if client_auth_tuple is not None:
                self._client_authenticator = authenticator
                self._client, self._client_auth = client_auth_tuple
                return

        self._client_not_authenticated()

    def _client_not_authenticated(self):
        """
        Generate a three-tuple of (authenticator, client, authtoken), representing
        an unauthenticated request.
        """
        self._client_authenticator = None
        self._client = None
        self._client_auth = None


class ClientAuthenticationMixin (object):
    """
    A view mixin that uses a ShareaboutsAPIRequest instead of a conventional
    DRF Request object.
    """

    def get_client_authenticators(self):
        """
        Instantiates and returns the list of client authenticators that this view can use.
        """
        return [auth() for auth in self.client_authentication_classes]

    def initialize_request(self, request, *args, **kwargs):
        """
        Override the initialize_request method in the base APIView so that we
        can use a custom request object.
        """
        parser_context = self.get_parser_context(request)

        return ShareaboutsAPIRequest(request,
            parsers=self.get_parsers(),
            authenticators=self.get_authenticators(),
            client_authenticators=self.get_client_authenticators(),
            negotiator=self.get_content_negotiator(),
            parser_context=parser_context)


class CorsEnabledMixin (object):
    """
    A view that puts Access-Control headers on the response.
    """
    always_allow_options = True
    SAFE_CORS_METHODS = ('GET', 'HEAD', 'TRACE')

    def finalize_response(self, request, response, *args, **kwargs):
        response = super(CorsEnabledMixin, self).finalize_response(request, response, *args, **kwargs)

        # Allow AJAX requests from anywhere for safe methods. Though OPTIONS
        # is also a safe method in that it does not modify data on the server,
        # it is used in preflight requests to determine whether a client is
        # allowed to make unsafe requests. So, we omit OPTIONS from the safe
        # methods so that clients get an honest answer.
        if request.method in self.SAFE_CORS_METHODS:
            response['Access-Control-Allow-Origin'] = request.META.get('HTTP_ORIGIN')

        # Some views don't do client authentication, but still need to allow
        # OPTIONS requests to return favorably (like the user authentication
        # view).
        elif self.always_allow_options and request.method == 'OPTIONS':
            response['Access-Control-Allow-Origin'] = request.META.get('HTTP_ORIGIN')

        # Allow AJAX requests only from trusted domains for unsafe methods.
        elif isinstance(request.client, cors.models.Origin) or request.user.is_authenticated():
            response['Access-Control-Allow-Origin'] = request.META.get('HTTP_ORIGIN')

        else:
            response['Access-Control-Allow-Origin'] = '*'

        response['Access-Control-Allow-Methods'] = ', '.join(self.allowed_methods)
        response['Access-Control-Allow-Headers'] = request.META.get('HTTP_ACCESS_CONTROL_REQUEST_HEADERS', '')
        response['Access-Control-Allow-Credentials'] = 'true'

        return response

class FilteredResourceMixin (object):
    """
    A view mixin that filters queryset of ModelWithDataBlob results based on
    the URL query parameters.
    """
    def filter_queryset(self, queryset):
        # Filter by any provided primary keys
        pk_list = self.kwargs.get('pk_list', None)
        if pk_list is not None:
            pk_list = pk_list.split(',')
            queryset = queryset.filter(pk__in=pk_list)

        # These filters will have been applied when constructing the queryset
        special_filters = set([FORMAT_PARAM, PAGE_PARAM, PAGE_SIZE_PARAM(),
            INCLUDE_SUBMISSIONS_PARAM, INCLUDE_PRIVATE_PARAM,
            INCLUDE_INVISIBLE_PARAM, NEAR_PARAM, DISTANCE_PARAM,
            TEXTSEARCH_PARAM, BBOX_PARAM, CALLBACK_PARAM(self)])

        # Filter by full-text search
        textsearch_filter = self.request.GET.get(TEXTSEARCH_PARAM, None)
        if textsearch_filter:
            queryset = queryset.filter(data__icontains=textsearch_filter)

        # Then filter by attributes
        for key, values in self.request.GET.iterlists():
            if key not in special_filters:
                # Filter quickly for indexed values
                if self.get_dataset().indexes.filter(attr_name=key).exists():
                    queryset = queryset.filter_by_index(key, *values)

                # Filter slowly for other values
                else:
                    excluded = []
                    for obj in queryset:
                        if hasattr(obj, key):
                            if getattr(obj, key) not in values:
                                queryset = queryset.exclude(pk=obj.pk)
                        else:
                            # Is it in the data blob?
                            data = json.loads(obj.data)
                            if key not in data or data[key] not in values:
                                excluded.append(obj.pk)
                    queryset = queryset.exclude(pk__in=excluded)


        return queryset


class LocatedResourceMixin (object):
    """
    A view mixin that orders queryset results by distance from a geometry, if
    requested.
    """
    def locate_queryset(self, queryset):
        if NEAR_PARAM in self.request.GET:
            try:
                reference = utils.to_geom(self.request.GET[NEAR_PARAM])
            except ValueError:
                raise QueryError(detail='Invalid parameter for "%s": %r' % (NEAR_PARAM, self.request.GET[NEAR_PARAM]))
            queryset = queryset.distance(reference).order_by('distance')

        if DISTANCE_PARAM in self.request.GET:
            if NEAR_PARAM not in self.request.GET:
                raise QueryError(detail='You must specify a "%s" parameter when using "%s"' % (NEAR_PARAM, DISTANCE_PARAM))

            try:
                max_dist = utils.to_distance(self.request.GET[DISTANCE_PARAM])
            except ValueError:
                raise QueryError(detail='Invalid parameter for "%s": %r' % (DISTANCE_PARAM, self.request.GET[DISTANCE_PARAM]))
            # Since the NEAR_PARAM is already in the query parameters, we can
            # use the `reference` geometry here.
            queryset = queryset.filter(geometry__distance_lt=(reference, max_dist))

        if BBOX_PARAM in self.request.GET:
            bounds = self.request.GET[BBOX_PARAM].split(',')
            if len(bounds) != 4:
                raise QueryError(detail='Invalid parameter for "%s": %r' % (BBOX_PARAM, self.request.GET[BBOX_PARAM]))

            boundingbox = Polygon.from_bbox(bounds)
            queryset = queryset.filter(geometry__within=boundingbox)

        return queryset


class OwnedResourceMixin (ClientAuthenticationMixin, CorsEnabledMixin):
    """
    A view mixin that retrieves the username of the resource owner, as provided
    in the URL, and stores it on the request object.

    Permissions
    -----------
    Owned resource views are available for reading to all users, and available
    for writing to the owner, logged in by key or directly. Only the owner
    logged in directly is allowed to read invisible resources or private data
    attributes on visible resources.
    """
    renderer_classes = (JSONRenderer, JSONPRenderer, BrowsableAPIRenderer, renderers.PaginatedCSVRenderer)
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    permission_classes = (IsAdminOwnerOrReadOnly, IsAllowedByDataPermissions)
    authentication_classes = (authentication.BasicAuthentication, oauth2Authentication.OAuth2Authentication, ShareaboutsSessionAuth)
    client_authentication_classes = (ApiKeyAuthentication, OriginAuthentication)
    content_negotiation_class = ShareaboutsContentNegotiation

    owner_username_kwarg = 'owner_username'
    dataset_slug_kwarg = 'dataset_slug'

    @csrf_exempt
    def dispatch(self, request, *args, **kwargs):
        request.allowed_username = kwargs[self.owner_username_kwarg]

        # Make sure the request has access to the dataset, since client
        # authentication must check against it.
        request.get_dataset = self.get_dataset

        return super(OwnedResourceMixin, self).dispatch(request, *args, **kwargs)

    def get_submitter(self):
        user = self.request.user
        return user if user.is_authenticated() else None

    def get_owner(self, force=False):
        if force or not hasattr(self, '_owner'):
            if (hasattr(self, 'owner_username_kwarg') and
                self.owner_username_kwarg in self.kwargs):

                owner_username = self.kwargs[self.owner_username_kwarg]
                self._owner = get_object_or_404(models.User.objects.all().prefetch_related('_groups', '_groups__permissions'), username=owner_username)
            else:
                self._owner = None
        return self._owner

    @classmethod
    def _get_dataset_from_db(cls, owner_username, dataset_slug):
        return get_object_or_404(
            models.DataSet.objects.all()\
                .select_related('owner')\
                .prefetch_related('permissions')\
                .prefetch_related('keys')\
                .prefetch_related('keys__permissions')\
                .prefetch_related('origins')\
                .prefetch_related('origins__permissions')
            , slug=dataset_slug, owner__username=owner_username)

    @classmethod
    def _get_dataset_from_cache(cls, owner_username, dataset_slug):
        from ..cache import DataSetCache
        ds_cache = DataSetCache()

        return ds_cache.get_instance(
            owner_username=owner_username,
            dataset_slug=dataset_slug)

    @classmethod
    def _save_dataset_in_cache(cls, dataset, owner_username, dataset_slug):
        from ..cache import DataSetCache
        ds_cache = DataSetCache()

        ds_cache.set_instance(dataset,
            owner_username=owner_username,
            dataset_slug=dataset_slug)

    def get_dataset(self, force=False):
        if force or not hasattr(self, '_dataset'):
            if (hasattr(self, 'owner_username_kwarg') and
                hasattr(self, 'dataset_slug_kwarg') and
                self.owner_username_kwarg in self.kwargs and
                self.dataset_slug_kwarg in self.kwargs):

                owner_username = self.kwargs[self.owner_username_kwarg]
                dataset_slug = self.kwargs[self.dataset_slug_kwarg]

                self._dataset = (
                    self._get_dataset_from_cache(owner_username, dataset_slug) or
                    self._get_dataset_from_db(owner_username, dataset_slug)
                )

                # Remember the owner in case we don't already
                self._owner = self._dataset.owner

                self._save_dataset_in_cache(self._dataset, owner_username, dataset_slug)
            else:
                self._dataset = None
        return self._dataset

    def is_verified_object(self, obj, ObjType=None):
        # Get the instance parameters from the cache
        ObjType = ObjType or self.model
        params = ObjType.cache.get_cached_instance_params(obj.pk, lambda: obj)

        # Make sure that the instance parameters match what we got in the URL.
        # We do not want to risk assuming a user owns a place, for example, just
        # because their username is in the URL.
        for attr in self.kwargs:
            if attr in params and unicode(self.kwargs[attr]) != unicode(params[attr]):
                return False

        return True

    def verify_object(self, obj, ObjType=None):
        # If the object is invisible, check that include_invisible is on
        if not getattr(obj, 'visible', True):
            if INCLUDE_INVISIBLE_PARAM not in self.request.GET:
                raise QueryError(detail='You must explicitly request invisible resources with the "include_invisible" parameter.')

        if not self.is_verified_object(obj, ObjType):
            raise Http404


class ProtectedOwnedResourceMixin (OwnedResourceMixin):
    """
    A base class for views that require an extra layer of protection. This mixin
    does not allow access to private data queries unless the user is logged in
    directly as the owner of the resource.
    """
    permission_classes = (IsLoggedInOwnerOrPublicDataOnly,) + OwnedResourceMixin.permission_classes


class CachedResourceMixin (object):
    @property
    def cache_prefix(self):
        return self.request.path

    def get_cache_prefix(self):
        return self.cache_prefix

    def get_cache_metakey(self):
        prefix = self.cache_prefix
        return prefix + '_keys'

    @csrf_exempt
    def dispatch(self, request, *args, **kwargs):
        # Only do the cache for GET, OPTIONS, or HEAD method.
        if request.method.upper() not in permissions.SAFE_METHODS:
            return super(CachedResourceMixin, self).dispatch(request, *args, **kwargs)

        self.request = request

        # Check whether the response data is in the cache.
        key = self.get_cache_key(request, *args, **kwargs)
        response_data = django_cache.cache.get(key) or None

        # Also check whether the request cache key is managed in the cache.
        # This is important, because if it's not managed, then we'll never
        # know when to invalidate it. If it's not managed we should just
        # assume that it's invalid.
        metakey = self.get_cache_metakey()
        keyset = django_cache.cache.get(metakey) or set()

        if (response_data is not None) and (key in keyset):
            cached_response = self.respond_from_cache(response_data)
            handler_name = request.method.lower()

            def cached_handler(*args, **kwargs):
                return cached_response

            # Patch the HTTP method
            with patch.object(self, handler_name, new=cached_handler):
                response = super(CachedResourceMixin, self).dispatch(request, *args, **kwargs)
        else:
            response = super(CachedResourceMixin, self).dispatch(request, *args, **kwargs)

            # Only cache on OK resposne
            if response.status_code == 200:
                self.cache_response(key, response)

        # Save all the buffered data to the cache
        cache_buffer.flush()

        # Disable client-side caching. Cause IE wrongly assumes that it should
        # cache.
        response['Cache-Control'] = 'no-cache'
        return response

    def get_cache_key(self, request, *args, **kwargs):
        querystring = request.META.get('QUERY_STRING', '')
        contenttype = request.META.get('HTTP_ACCEPT', '')

        if not hasattr(request, 'user') or not request.user.is_authenticated():
            groups = ''
        else:
            dataset = None
            if hasattr(self, 'get_dataset'):
                dataset = self.get_dataset()

            if dataset:
                if request.user.id == dataset.owner_id:
                    groups = '__owners__'
                else:
                    group_set = []
                    for group in request.user._groups.all():
                        if group.dataset_id == dataset.id:
                            group_set.append(group.name)
                    groups = ','.join(group_set)
            else:
                groups = ''

        # TODO: Eliminate the jQuery cache busting parameter for now. Get
        # rid of this after the old API has been deprecated.
        cache_buster_pattern = re.compile(r'&?_=\d+')
        querystring = re.sub(cache_buster_pattern, '', querystring)

        return ':'.join([self.cache_prefix, contenttype, querystring, groups])

    def respond_from_cache(self, cached_data):
        # Given some cached data, construct a response.
        content, status, headers = cached_data
        response = Response(content, status=status, headers=dict(headers))
        return response

    def cache_response(self, key, response):
        data = response.data
        status = response.status_code
        headers = response.items()

        # Cache enough info to recreate the response.
        django_cache.cache.set(key, (data, status, headers), settings.API_CACHE_TIMEOUT)

        # Also, add the key to the set of pages cached from this view.
        meta_key = self.get_cache_metakey()
        keys = django_cache.cache.get(meta_key) or set()
        keys.add(key)
        django_cache.cache.set(meta_key, keys, settings.API_CACHE_TIMEOUT)

        return response


class Sanitizer(object):
    """
    Strip out non-whitelisted HTML tags and attributes in an object of submitted data.
    """
    def sanitize(self, obj):
        field_whitelist = [
            'geometry', 'showMetadata', 'published', 'datasetSlug',
            'datasetId', 'location_type', 'style', 'user_token', 'url-title']
        tag_whitelist = [
            'div', 'p', 'img', 'a', 'em', 'i', 'code', 'b', 's', 'u',
            'li', 'ol', 'ul', 'strong', 'br', 'hr', 'span', 'h1',
            'h2', 'h3', 'h4', 'h5', 'h6', 'iframe', 'html', 'head',
            'body', 'button'
        ]
        attribute_whitelist = {
            '*': ['style'],
            'img': ['src', 'alt', 'height', 'width'],
            'a': ['href'],
            'iframe': ['frameborder', 'allowfullscreen', 'src', 'width', 'height'],
            'div': ['id']
        }
        styles_whitelist = [
            'color', 'background-color', 'background-image'
        ]

        for field_name, value in obj.iteritems():
            if field_name in field_whitelist or value is None:
                continue
            if type(value) is list:
                for i in range(len(value)):
                    value[i] = bleach.clean(
                        value[i],
                        strip=True,
                        tags=tag_whitelist,
                        attributes=attribute_whitelist,
                        styles=styles_whitelist
                    )
                obj[field_name] = value
            elif type(value) is dict:
                for k, v in value.iteritems():
                    value[k] = bleach.clean(
                        v,
                        strip=True,
                        tags=tag_whitelist,
                        attributes=attribute_whitelist,
                        styles=styles_whitelist
                    )
            elif type(value) is str:
                obj[field_name] = bleach.clean(
                    value,
                    strip=True,
                    tags=tag_whitelist,
                    attributes=attribute_whitelist,
                    styles=styles_whitelist
                )


###############################################################################
#
# Exceptions
# ----------
#

class QueryError(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = 'Malformed or missing query parameters.'

    def __init__(self, detail=None):
        self.detail = detail or self.default_detail


###############################################################################
#
# Resource Views
# --------------
#

class ShareaboutsAPIRootView (views.APIView):
    """
    Welcome to the Shareabouts API. The Shareabouts API is the data storage
    and data management component that powers the
    [Shareabouts web application](https://github.com/openplans/shareabouts).
    It is a REST API for flexibly storing data about places.

    The Shareabouts API supports a number of authentication methods, including
    basic auth for users, and OAuth for constructing applications against the
    API. The API also allows you to easily build Twitter and Facebook
    authentication into your own application build on the Shareabouts API.

    The best place to start browsing is in your datasets. Use the link at the
    top-right to log in.

    """
    def get(self, request):
        user = request.user

        response_data = {}

        if user.is_authenticated():
            response_data['your datasets'] = request.build_absolute_uri(
                reverse('dataset-list', kwargs={'owner_username': user.username})
            )

        if user.is_superuser:
            response_data['all datasets'] = request.build_absolute_uri(
                reverse('admin-dataset-list')
            )

        return Response(response_data)


class PlaceInstanceView (Sanitizer, CachedResourceMixin, LocatedResourceMixin, OwnedResourceMixin, FilteredResourceMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET
    ---
    Get a specific place

    **Authentication**: Basic, session, or key auth *(optional)*

    **Request Parameters**:

      * `include_submissions`

        List the submissions in each submission set instead of just a summary of
        the set.

      * `include_invisible` *(only direct auth)*

        Show the place even if it is set as. You must specify use this flag to
        view an invisible place. The flag will also apply to submissions, if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request invisible resoruces.

      * `include_private` *(only direct auth)*

        Show private data attributes on the place, and on any submissions if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request private attributes.

    PUT
    ---
    Update a place

    **Authentication**: Basic, session, or key auth *(required)*

    DELETE
    ------
    Delete a place

    **Authentication**: Basic, session, or key auth *(required)*

    ------------------------------------------------------------
    """

    model = models.Place
    serializer_class = serializers.PlaceSerializer
    renderer_classes = (renderers.GeoJSONRenderer, renderers.GeoJSONPRenderer) + OwnedResourceMixin.renderer_classes[2:]
    parser_classes = (parsers.GeoJSONParser,) + OwnedResourceMixin.parser_classes[1:]

    # Override update() here to support HTML sanitization
    def update(self, request, *args, **kwargs):
        Sanitizer.sanitize(self, request.data)

        partial = kwargs.pop('partial', False)
        self.object = self.get_object_or_none()

        if self.object is None:
            created = True
            save_kwargs = {'force_insert': True}
            success_status_code = status.HTTP_201_CREATED
        else:
            created = False
            save_kwargs = {'force_update': True}
            success_status_code = status.HTTP_200_OK

        serializer = self.get_serializer(self.object, data=request.data, partial=partial)

        if serializer.is_valid():
            user = self.request.user
            if serializer.instance is not None and serializer.instance.submitter is not None:
                user = serializer.instance.submitter
            if 'submitter' in serializer.validated_data:
                user = serializer.validated_data['submitter']
            self.object = serializer.save(
                submitter=user if user is not None and user.is_authenticated() else None,
                **save_kwargs
            )
            return Response(serializer.data, status=success_status_code)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get_object_or_none(self, pk=None):
        if pk is None:
            pk = self.kwargs['place_id']
        try:
            return self.model.objects\
                .filter(pk=pk)\
                .select_related('dataset', 'dataset__owner', 'submitter')\
                .prefetch_related('submitter__social_auth',
                                  'submissions',
                                  'submissions__attachments',
                                  'attachments')\
                .get()
        except self.model.DoesNotExist:
            return None

    def get_object_or_404(self, pk=None):
        obj = self.get_object_or_none(pk)
        if obj is None:
            raise Http404
        return obj

    def get_object(self, queryset=None):
        place_id = self.kwargs['place_id']
        obj = self.get_object_or_404(place_id)
        self.verify_object(obj)
        return obj


class CompletePlaceListRequestView (OwnedResourceMixin, generics.RetrieveAPIView):
    def get(self, request, *args, **kwargs):
        dataset = self.get_dataset()
        cache_key = dataset.cache.get_bulk_data_cache_key(dataset.pk, 'places',
            include_submissions=(INCLUDE_SUBMISSIONS_PARAM in request.GET),
            include_private=(INCLUDE_PRIVATE_PARAM in request.GET),
            include_invisible=(INCLUDE_INVISIBLE_PARAM in request.GET))
        result = super(CompletePlaceListRequestView, self).get(request, *args, **kwargs)
        return result


class PlaceListMixin (object):
    pass


class PlaceListView (Sanitizer, CachedResourceMixin, LocatedResourceMixin, OwnedResourceMixin, FilteredResourceMixin, EmailTemplateMixin, bulk_generics.ListCreateBulkUpdateAPIView):
    """

    GET
    ---
    Get all the places in a dataset

    **Authentication**: Basic, session, or key auth *(optional)*

    **Request Parameters**:

      * `include_submissions`

        List the submissions in each submission set instead of just a summary of
        the set.

      * `include_invisible` *(only direct auth)*

        Show the place even if it is set as. You must specify use this flag to
        view an invisible place. The flag will also apply to submissions, if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request invisible resoruces.

      * `include_private` *(only direct auth)*

        Show private data attributes on the place, and on any submissions if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request private attributes.

      * `near=<reference_geometry>`

        Order the place list by distance from some reference geometry. The
        reference geometry may be represented as
        [GeoJSON](http://www.geojson.org/geojson-spec.html) or
        [WKT](http://en.wikipedia.org/wiki/Well-known_text), or as a
        comma-separated latitude and longitude, if it is a point.

      * `distance_lt=<distance>`

        When used in conjunction with the `near` parameter, can filter the
        places returned to only those within the given distance of the
        reference geometry. The distance may just be a number, or a number
        with a unit string -- e.g., `123`, `123.45`, `123 km`, `123.45 mi`.
        If only a number is specified, the unit meters (m) is assumed. For all
        available units, see [the GeoDjango docs](https://docs.djangoproject.com/en/dev/ref/contrib/gis/measure/#supported-units).

      * `bounds=<left>,<top>,<right>,<bottom>`

        Restrict the places to those within the given bounding box. This is a
        comma-separated list of 4 numeric values: western longitude, northern
        latitude, eastern longitude, southern latitude.

      * `<attr>=<value>`

        Filter the place list to only return the places where the attribute is
        equal to the given value. *The attribute should be indexed.*

    POST
    ----

    Create a place

    **Authentication**: Basic, session, or key auth *(required)*

    ------------------------------------------------------------
    """

    serializer_class = serializers.PlaceSerializer
    pagination_class = serializers.FeatureCollectionPagination
    renderer_classes = (renderers.GeoJSONRenderer, renderers.GeoJSONPRenderer) + OwnedResourceMixin.renderer_classes[2:]
    parser_classes = (parsers.GeoJSONParser,) + OwnedResourceMixin.parser_classes[1:]

    # Overriding create so we can sanitize submitted fields, which may
    # contain raw HTML intended to be rendered in the client
    def create(self, request, *args, **kwargs):
        Sanitizer.sanitize(self, request.data)

        serializer = self.get_serializer(data=request.data)

        if serializer.is_valid():
            user = self.request.user
            if 'submitter' in serializer.validated_data:
                user = serializer.validated_data['submitter']
            self.object = serializer.save(
                force_insert=True,
                submitter=user if user is not None and user.is_authenticated() else None,
                dataset=self.get_dataset()
            )
            self.post_save(self.object, created=True)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED,
                            headers=headers)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get_cache_metakey(self):
        metakey_kwargs = self.kwargs.copy()
        metakey_kwargs.pop('pk_list', None)
        prefix = reverse('place-list', kwargs=metakey_kwargs)
        return prefix + '_keys'

    def post_save(self, obj, created):
        # Get all place/add webhooks since we just added a place.
        if not created:
            return

        webhooks = obj.dataset.webhooks.filter(submission_set='places').filter(event='add')

        if len(webhooks):
            self.trigger_webhooks(webhooks, obj)

        self.send_email_notification(obj, submission_set_name='places')

    def get_queryset(self):
        dataset = self.get_dataset()
        queryset = self.locate_queryset(
            self.filter_queryset(models.Place.objects.all())
        )

        # If the user is not allowed to request invisible data then we won't
        # be here in the first place.
        if INCLUDE_INVISIBLE_PARAM not in self.request.GET:
            queryset = queryset.filter(visible=True)

        # If we're updating, limit the queryset to the items that are being
        # updated.
        if self.request.method.upper() == 'PUT':
            data = self.request.data
            ids = [obj['id'] for obj in data if 'id' in obj]
            queryset = queryset.filter(pk__in=ids)

        queryset = queryset.filter(dataset=dataset)\
            .select_related('dataset', 'dataset__owner', 'submitter')\
            .prefetch_related(
                'submitter__social_auth',
                'submitter___groups',
                'submitter___groups__dataset',
                'submitter___groups__dataset__owner',
                'submissions',
                'attachments')

        if INCLUDE_SUBMISSIONS_PARAM in self.request.GET:
            queryset = queryset.prefetch_related(
                'submissions',
                'submissions__submitter',
                'submissions__submitter__social_auth',
                'submissions__submitter___groups',
                'submissions__attachments')

        return queryset

    def trigger_webhooks(self, webhooks, obj):
        """
        Serializes the place object to GeoJSON and POSTs it to each webhook
        """
        serializer = serializers.PlaceSerializer(obj)
        # Update request to include private data. We need everything since
        # we can't PATCH on the API yet.
        temp_get = self.request.GET.copy()
        temp_get['include_private'] = 'on'
        self.request.GET = temp_get
        serializer.context = {'request': self.request}

        # Render the place as GeoJSON
        renderer = renderers.GeoJSONRenderer()
        data = renderer.render(serializer.data)

        # POST to each webhook
        for webhook in webhooks:
            status_code = 'None'
            try:
                response = requests.post(webhook.url, data=data)
                status_code = str(response.status_code)
                response.raise_for_status()
                logger.info('[WEBHOOK] Place %d added and POSTed to %s. Status: %s',
                            obj.id, webhook.url, status_code)
            except requests.exceptions.RequestException as e:
                logger.error('[WEBHOOK] Place %d added but could not be POSTed to %s. Status: %s',
                             obj.id, webhook.url, status_code)
                logger.error(e)


class SubmissionInstanceView (CachedResourceMixin, OwnedResourceMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET
    ---
    Get a particular submission

    **Authentication**: Basic, session, or key auth *(optional)*

    **Request Parameters**:

      * `include_invisible` *(only direct auth)*

        Show the submission even if it is set as invisible. You must specify use
        this flag to view an invisible submission. Only the dataset owner is
        allowed to request invisible resoruces.

      * `include_private` *(only direct auth)*

        Show private data attributes on the submission. Only the dataset owner
        is allowed to request private attributes.

    PUT
    ---
    Update a submission

    **Authentication**: Basic, session, or key auth *(required)*

    DELETE
    ------
    Delete a submission

    **Authentication**: Basic, session, or key auth *(required)*

    ------------------------------------------------------------
    """

    model = models.Submission
    serializer_class = serializers.SubmissionSerializer
    submission_set_name_kwarg = 'submission_set_name' # Set here so that the data permission checker has access

    def get_object_or_404(self, pk):
        try:
            return self.model.objects\
                .filter(pk=pk)\
                .select_related(
                    'dataset',
                    'dataset__owner',
                    'place_model',
                    'place_model__dataset',
                    'place_model__dataset__owner',
                    'submitter')\
                .prefetch_related('attachments', 'submitter__social_auth')\
                .get()
        except self.model.DoesNotExist:
            raise Http404

    def get_object(self, queryset=None):
        submission_id = self.kwargs['submission_id']
        obj = self.get_object_or_404(submission_id)
        self.verify_object(obj)
        return obj


class SubmissionListView (CachedResourceMixin, OwnedResourceMixin, FilteredResourceMixin, EmailTemplateMixin, bulk_generics.ListCreateBulkUpdateAPIView):
    """

    GET
    ---
    Get all the submissions in a place's submission set

    **Authentication**: Basic, session, or key auth *(optional)*

    **Request Parameters**:

      * `include_invisible` *(only direct auth)*

        Show the place even if it is set as. You must specify use this flag to
        view an invisible place. The flag will also apply to submissions, if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request invisible resoruces.

      * `include_private` *(only direct auth)*

        Show private data attributes on the place, and on any submissions if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request private attributes.

      * `<attr>=<value>`

        Filter the place list to only return the places where the attribute is
        equal to the given value. *The attribute should be indexed.*

    POST
    ----

    Create a submission

    **Authentication**: Basic, session, or key auth *(required)*

    ------------------------------------------------------------
    """

    model = models.Submission
    serializer_class = serializers.SubmissionSerializer
    pagination_class = serializers.MetadataPagination

    place_id_kwarg = 'place_id'
    submission_set_name_kwarg = 'submission_set_name'

    def get_cache_metakey(self):
        metakey_kwargs = self.kwargs.copy()
        metakey_kwargs.pop('pk_list', None)
        prefix = reverse('submission-list', kwargs=metakey_kwargs)
        return prefix + '_keys'

    def get_place_model(self, dataset):
        place_id = self.kwargs[self.place_id_kwarg]
        place = get_object_or_404(models.Place, dataset=dataset, id=place_id)
        return place

    def perform_create(self, serializer):
        dataset = self.get_dataset()
        user = self.request.user
        if 'submitter' in serializer.validated_data and serializer.validated_data['submitter'] is not None:
            user = serializer.validated_data['submitter']
        submission = serializer.save(
            dataset=dataset,
            place_model=self.get_place_model(dataset),
            set_name=self.kwargs[self.submission_set_name_kwarg],
            submitter=user if user.is_authenticated() else None,
        )
        self.send_email_notification(submission, submission_set_name=submission.set_name)

    def get_queryset(self):
        dataset = self.get_dataset()
        place = self.get_place_model(dataset)
        submission_set_name = self.kwargs[self.submission_set_name_kwarg]
        queryset = self.filter_queryset(models.Submission.objects.all())

        if submission_set_name != 'submissions':
            queryset = queryset.filter(set_name=submission_set_name)

        # If the user is not allowed to request invisible data then we won't
        # be here in the first place -- auth or permissions woulda got us.
        if INCLUDE_INVISIBLE_PARAM not in self.request.GET:
            queryset = queryset.filter(visible=True)

        # If we're updating, limit the queryset to the items that are being
        # updated.
        if self.request.method.upper() == 'PUT':
            data = self.request.data
            ids = [obj['id'] for obj in data if 'id' in obj]
            queryset = queryset.filter(pk__in=ids)

        result = queryset.filter(place_model=place)\
            .select_related(
                'dataset',
                'dataset__owner',
                'place_model',
                'place_model__dataset',
                'place_model__dataset__owner',
                'submitter')\
            .prefetch_related('attachments', 'submitter__social_auth', 'submitter___groups')

        return result


class DataSetSubmissionListView (CachedResourceMixin, ProtectedOwnedResourceMixin, FilteredResourceMixin, generics.ListAPIView):
    """

    GET
    ---
    Get all the submissions across a dataset's place's submission sets

    **Authentication**: Basic, session, or key auth *(optional)*

    **Request Parameters**:

      * `include_invisible` *(only direct auth)*

        Show the place even if it is set as. You must specify use this flag to
        view an invisible place. The flag will also apply to submissions, if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request invisible resoruces.

      * `include_private` *(only direct auth)*

        Show private data attributes on the place, and on any submissions if the
        `include_submissions` flag is set. Only the dataset owner is allowed to
        request private attributes.

      * `<attr>=<value>`

        Filter the place list to only return the places where the attribute is
        equal to the given value. *The attribute should be indexed.*

    ------------------------------------------------------------
    """

    model = models.Submission
    serializer_class = serializers.SubmissionSerializer
    pagination_class = serializers.MetadataPagination

    submission_set_name_kwarg = 'submission_set_name'

    def get_cache_metakey(self):
        metakey_kwargs = self.kwargs.copy()
        metakey_kwargs.pop('pk_list', None)
        prefix = reverse('dataset-submission-list', kwargs=metakey_kwargs)
        return prefix + '_keys'

    def get_queryset(self):
        dataset = self.get_dataset()
        submission_set_name = self.kwargs[self.submission_set_name_kwarg]
        queryset = self.filter_queryset(models.Submission.objects.all())

        if submission_set_name != 'submissions':
            queryset = queryset.filter(set_name=submission_set_name)

        # If the user is not allowed to request invisible data then we won't
        # be here in the first place -- auth or permissions woulda got us.
        if INCLUDE_INVISIBLE_PARAM not in self.request.GET:
            queryset = queryset.filter(visible=True)

        return queryset.filter(dataset=dataset)\
            .select_related(
                'dataset',
                'dataset__owner',
                'place_model',
                'place_model__dataset',
                'place_model__dataset__owner',
                'submitter')\
            .prefetch_related('attachments', 'submitter__social_auth', 'submitter___groups')


class DataSetInstanceView (ProtectedOwnedResourceMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET
    ---
    Get a particular submission

    **Authentication**: Basic, session, or key auth *(optional)*

    **Request Parameters**:

      * `include_invisible` *(only direct auth)*

        Count visible and invisible places and submissions in the dataset. Only
        the dataset owner is allowed to request invisible resoruces.

    PUT
    ---
    Update a submission

    **Authentication**: Basic or session auth *(required)*

    DELETE
    ------
    Delete a submission

    **Authentication**: Basic or session auth *(required)*

    ------------------------------------------------------------
    """

    model = models.DataSet
    serializer_class = serializers.DataSetSerializer
    authentication_classes = (authentication.BasicAuthentication, oauth2Authentication.OAuth2Authentication, ShareaboutsSessionAuth)
    client_authentication_classes = ()
    always_allow_options = True

    def get_object_or_404(self, owner_username, dataset_slug):
        try:
            return self.model.objects\
                .filter(slug=dataset_slug, owner__username=owner_username)\
                .get()
        except self.model.DoesNotExist:
            raise Http404

    def get_serializer_context(self):
        context = super(DataSetInstanceView, self).get_serializer_context()
        include_invisible = INCLUDE_INVISIBLE_PARAM in self.request.GET

        return context

    def get_object(self, queryset=None):
        dataset_slug = self.kwargs[self.dataset_slug_kwarg]
        owner_username = self.kwargs[self.owner_username_kwarg]
        obj = self.get_object_or_404(owner_username, dataset_slug)
        self.verify_object(obj)
        return obj

    def put(self, request, owner_username, dataset_slug):
        response = super(DataSetInstanceView, self).put(request, owner_username=owner_username, dataset_slug=dataset_slug, partial=True)
        if 'slug' in response.data and response.data['slug'] != dataset_slug:
            response.status_code = 301
            response['Location'] = response.data['url']
        return response


class DataSetMetadataView (ProtectedOwnedResourceMixin, generics.RetrieveAPIView):
    """
    GET
    ---
    Get the metadata about a particular dataset. This includes api keys,
    allowed origins, permissions associated with each, groups, etc.

    **Authentication**: Basic or session (required)

    ------------------------------------------------------------
    """

    model = models.DataSet
    serializer_class = serializers.SimpleDataSetSerializer
    authentication_classes = (authentication.BasicAuthentication, oauth2Authentication.OAuth2Authentication, ShareaboutsSessionAuth)
    client_authentication_classes = ()
    permission_classes = (IsLoggedInOwner,)
    always_allow_options = True

    def get_object_or_404(self, owner_username, dataset_slug):
        try:
            return self.model.objects\
                .filter(slug=dataset_slug, owner__username=owner_username)\
                .prefetch_related(
                    'permissions',
                    'groups',
                    'groups__permissions',
                    'keys',
                    'keys__permissions',
                    'origins',
                    'origins__permissions')\
                .get()
        except self.model.DoesNotExist:
            raise Http404

    def get_object(self, queryset=None):
        dataset_slug = self.kwargs[self.dataset_slug_kwarg]
        owner_username = self.kwargs[self.owner_username_kwarg]
        obj = self.get_object_or_404(owner_username, dataset_slug)
        self.verify_object(obj)
        return obj


class DataSetKeyListView (ProtectedOwnedResourceMixin, generics.ListAPIView):
    """
    """

    model = apikey.models.ApiKey
    serializer_class = serializers.ApiKeySerializer
    authentication_classes = (authentication.BasicAuthentication, oauth2Authentication.OAuth2Authentication, ShareaboutsSessionAuth)
    permission_classes = (IsLoggedInOwner,)
    client_authentication_classes = ()
    always_allow_options = True

    def get_queryset(self):
        dataset = self.get_dataset()
        queryset = super(DataSetKeyListView, self).get_queryset()
        return queryset.filter(dataset=dataset)


class DataSetListMixin (object):
    """
    Common aspects for dataset list views.
    """

    model = models.DataSet
    serializer_class = serializers.DataSetSerializer
    pagination_class = serializers.MetadataPagination
    authentication_classes = (authentication.BasicAuthentication, oauth2Authentication.OAuth2Authentication, ShareaboutsSessionAuth)
    client_authentication_classes = ()
    always_allow_options = True

    @utils.memo
    def get_all_submission_sets(self):
        """
        Return a dictionary whose keys are dataset ids and values are a
        corresponding list of submission set summary information for the
        submisisons on that dataset's places.
        """
        include_invisible = INCLUDE_INVISIBLE_PARAM in self.request.GET
        summaries = models.Submission.objects.filter(dataset__in=self.get_queryset())
        if not include_invisible:
            summaries = summaries.filter(visible=True)

        # Unset any default ordering
        summaries = summaries.order_by()

        summaries = summaries.values('dataset', 'set_name').annotate(length=Count('dataset'))

        sets = defaultdict(list)
        for summary in summaries:
            sets[summary['dataset']].append(summary)

        return dict(sets.items())


class DataSetListView (DataSetListMixin, ProtectedOwnedResourceMixin, generics.ListCreateAPIView):
    """

    GET
    ---
    Get all the datasets for a dataset owner

    **Authentication**: Basic, or session auth *(optional)*

    **Request Parameters**:

      * `include_invisible` *(only direct auth)*

        Count visible and invisible places and submissions in the dataset. Only
        the dataset owner is allowed to request invisible resoruces.

    POST
    ----

    Create a dataset. You can clone an existing dataset by either:

      * specifying a `X-Shareabouts-Clone` header, or
      * including a `clone` querystring parameter.

    In either case, the value of the header/parameter can be one of three
    things:

      * the URL of the dataset to be cloned,
      * the id of the dataset to be cloned, or
      * a comma-separated pair of the dataset owner name and slug

    To clone a dataset, the authenticated user must have enough permission on
    the cloned object to read private and invisible data.

    By default, the API will automatically find a unique slug by appending an
    integer to the end of a non-unique slug.

    **Authentication**: Basic or session auth *(required)*

    ------------------------------------------------------------
    """

    client_authentication_classes = ()

    def perform_create(self, serializer):
        serializer.save(owner=self.get_owner())
        serializer.instance.keys.create(
            dataset=serializer.data,
            key=apikey.models.generate_unique_api_key()
        )

    def get_queryset(self):
        owner = self.get_owner()
        queryset = models.DataSet.objects.all()
        return queryset.filter(owner=owner).order_by('id')

    def create(self, request, owner_username):
        if 'HTTP_X_SHAREABOUTS_CLONE' in request.META or 'clone' in request.GET:
            return self.clone(request, owner_username=owner_username)
        else:
            return super(DataSetListView, self).create(request, owner_username=owner_username)

    def get_object_to_clone(self, clone_header):
        try:
            dataset = None

            # Try to parse it as a comma-separated (owner, slug) pair
            try:
                owner_username, dataset_slug = clone_header.split(',')
                dataset = models.DataSet.objects.all().get(owner__username=owner_username, slug=dataset_slug)
            except ValueError:
                pass

            # Try to parse it as a single dataset id
            try:
                dataset_id = int(clone_header)
                dataset = models.DataSet.objects.all().get(id=dataset_id)
            except ValueError:
                pass

            # Try to parse it as a full URL
            from urlparse import urlparse
            url = urlparse(clone_header)
            if url.scheme and url.netloc and url.path:
                match = re.match(r'^/api/v2/(?P<owner_username>[^/]+)/datasets/(?P<dataset_slug>[^/]+)$', url.path)
                if match:
                    owner_username = match.group('owner_username')
                    dataset_slug = match.group('dataset_slug')
                    dataset = models.DataSet.objects.all().get(owner__username=owner_username, slug=dataset_slug)

        except models.DataSet.DoesNotExist:
            return None

        return dataset

    def clone(self, request, owner_username):
        clone_header = request.META.get('HTTP_X_SHAREABOUTS_CLONE') or request.GET.get('clone')

        # Make sure we have a thing to clone
        if not clone_header:
            return Response({'errors': ['No object specified to clone']}, status=400)

        original = self.get_object_to_clone(clone_header)
        if original is None:
            return Response({'errors': ['No object available to clone for "%s"' % clone_header]}, status=404)

        # Make sure we would have access if we were getting that whole thing
        fake_request = RequestFactory().get(
            path=reverse('dataset-detail', args=[original.owner.username, original.slug]),
            data=dict(include_invisible=True, include_private=True))
        self.check_object_permissions(fake_request, original)

        # Clone the object using the override values from the request. Only
        # do a shallow clone during the request. Schedule the deep clone to
        # run in a background process.
        overrides = {}
        queryset = self.get_queryset()

        for field in ('slug', 'display_name'):
            if field in request.data: overrides[field] = request.data[field]

        # - - Make sure slug is unique.
        if 'slug' in overrides:
            slug = overrides['slug']
            try:
                queryset.get(slug=slug)
            except models.DataSet.DoesNotExist:
                pass
            else:
                return Response({'errors': {'slug': 'DataSet with this slug already exists'}}, status=409)
        else:
            slugs = set([ds.slug for ds in queryset])
            unique_slug = original.slug
            for uniquifier in count(2):
                if unique_slug not in slugs: break
                unique_slug = '-'.join([original.slug, str(uniquifier)])
            overrides['slug'] = unique_slug

        clone = original.clone(overrides=overrides, commit=False)
        clone.save()
        tasks.clone_related_dataset_data.apply_async(args=[original.id, clone.id])

        serializer = self.get_serializer(instance=clone)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED,
                        headers=headers)


class AdminDataSetListView (CachedResourceMixin, DataSetListMixin, generics.ListAPIView):
    """

    GET
    ---
    Get all the datasets

    **Authentication**: Basic or session auth *(required)*

    **Request Parameters**:

      * `include_invisible`

        Count visible and invisible places and submissions in the dataset. Only
        the dataset owner is allowed to request invisible resoruces.

    ------------------------------------------------------------
    """

    queryset = models.DataSet.objects.all()
    permission_classes = (IsLoggedInAdmin,)
    content_negotiation_class = ShareaboutsContentNegotiation


class AttachmentInstanceView (ProtectedOwnedResourceMixin, generics.RetrieveUpdateAPIView):
    """

    GET
    ---
    Get a particular attachment

    **Authentication**: Basic, session, or key auth *(optional)*

    PATCH
    -----
    Update an attachment's metadata, though not the attachment itself

    **Authentication**: Basic, session, or key auth *(required)*

    ------------------------------------------------------------
    """
    
    model = models.Attachment
    serializer_class = serializers.AttachmentInstanceSerializer

    def partial_update(self, *args, **kwargs):
        attachment_id = self.kwargs['attachment_id']
        attachment = self.get_object_or_404(attachment_id)
        attachment.clear_instance_cache()
        return super(AttachmentInstanceView, self).partial_update(*args, **kwargs) 

    def get_object_or_404(self, pk):
        try:
            return self.model.objects\
                .filter(pk=pk)\
                .get()
        except self.model.DoesNotExist:
            raise Http404

    def get_object(self, queryset=None):
        attachment_id = self.kwargs['attachment_id']
        obj = self.get_object_or_404(attachment_id)
        self.verify_object(obj)
        return obj


class AttachmentListView (OwnedResourceMixin, FilteredResourceMixin, generics.ListCreateAPIView):
    """

    GET
    ---
    Get all the attachments for a place or submission

    **Authentication**: Basic, session, or key auth *(optional)*

    POST
    ----
    Attach a file to a place or submission

    **Authentication**: Basic, session, or key auth *(required)*

    ------------------------------------------------------------
    """

    model = models.Attachment
    serializer_class = serializers.AttachmentListSerializer
    pagination_class = serializers.MetadataPagination

    thing_id_kwarg = 'thing_id'
    submission_set_name_kwarg = 'submission_set_name'

    def get_thing(self):
        thing_id = self.kwargs[self.thing_id_kwarg]
        dataset = self.get_dataset()
        thing = get_object_or_404(models.SubmittedThing, dataset=dataset, id=thing_id)

        if self.submission_set_name_kwarg in self.kwargs:
            # the thing is a Submission
            obj = thing.submission
            ObjType = models.Submission
        else:
            # the thing is a Place
            obj = thing.place
            ObjType = models.Place
        self.verify_object(obj, ObjType)

        return thing

    def perform_create(self, serializer):
        serializer.save(thing=self.get_thing())

    def get_queryset(self):
        thing = self.get_thing()
        queryset = self.filter_queryset(models.Attachment.objects.all())
        return queryset.filter(thing=thing)


class ActionListView (CachedResourceMixin, OwnedResourceMixin, generics.ListAPIView):
    """

    GET
    ---

    Get the activity for a dataset

    **Authentication**: Basic, session, or key auth *(optional)*

    ------------------------------------------------------------
    """
    serializer_class = serializers.ActionSerializer
    pagination_class = serializers.MetadataPagination

    def get_queryset(self):
        dataset = self.get_dataset()
        queryset = models.Action.objects.all()\
            .filter(thing__dataset=dataset)\
            .select_related(
                'thing',
                'thing__place',       # It will have this if it's a place
                'thing__submission',  # It will have this if it's a submission
                'thing__submission__place_model',
                'thing__submission__place_model__dataset',
                'thing__submission__place_model__dataset__owner',

                'thing__submitter',
                'thing__dataset',
                'thing__dataset__owner')\
            .prefetch_related(
                'thing__submitter___groups__dataset__owner',
                'thing__submitter__social_auth',

                'thing__place__attachments',
                'thing__submission__attachments',

                'thing__place__submissions')

        if INCLUDE_INVISIBLE_PARAM not in self.request.GET:
            queryset = queryset.filter(thing__visible=True)\
                .filter(Q(thing__place__isnull=False) |
                        Q(thing__submission__place_model__visible=True))

        return queryset


###############################################################################
#
# Client Authentication Views
# ---------------------------
#

class ClientAuthListView (OwnedResourceMixin, generics.ListCreateAPIView):
    authentication_classes = (authentication.BasicAuthentication, oauth2Authentication.OAuth2Authentication, ShareaboutsSessionAuth)
    client_authentication_classes = ()
    permission_classes = (IsLoggedInOwner,)

    def get_queryset(self):
        qs = super(ClientAuthListView, self).get_queryset()
        dataset = self.get_dataset()
        return qs.filter(dataset=dataset)

    def get_serializer(self, instance=None, data=None, many=False, partial=False):
        if isinstance(data, dict):
            dataset = self.get_dataset()
            data = data.copy()
            data['dataset'] = dataset.id
        return super(ClientAuthListView, self).get_serializer(
            instance=instance, data=data, many=many, partial=partial)


class ApiKeyListView (ClientAuthListView):
    model = apikey.models.ApiKey


class OriginListView (ClientAuthListView):
    model = cors.models.Origin


###############################################################################
#
# User Session Views
# ------------------
#

class UserInstanceView (OwnedResourceMixin, generics.RetrieveAPIView):
    model = models.User
    client_authentication_classes = ()
    always_allow_options = True
    serializer_class = serializers.FullUserSerializer
    SAFE_CORS_METHODS = ('GET', 'HEAD', 'TRACE', 'OPTIONS')

    def get_queryset(self):
        return models.User.objects.all()\
            .prefetch_related('social_auth')

    def get_object(self, queryset=None):
        owner_username = self.kwargs[self.owner_username_kwarg]
        owner = get_object_or_404(self.get_queryset(), username=owner_username)
        return owner


class CurrentUserInstanceView (CorsEnabledMixin, views.APIView):
    renderer_classes = (renderers.NullJSONRenderer, renderers.NullJSONPRenderer, BrowsableAPIRenderer, renderers.PaginatedCSVRenderer)
    content_negotiation_class = ShareaboutsContentNegotiation
    authentication_classes = (ShareaboutsSessionAuth,)

    # Since this view only affects the local session, make it always safe for
    # CORS requests.
    SAFE_CORS_METHODS = ('GET', 'HEAD', 'TRACE', 'OPTIONS', 'POST', 'DELETE')

    def get(self, request):
        if request.user.is_authenticated():
            user_url = reverse('user-detail', args=[request.user.username])
            return HttpResponseRedirect(user_url + '?' + request.GET.urlencode(), status=303)
        else:
            return Response(None, headers={'cache-control': 'private, max-age=0, no-cache'})

    def post(self, request):
        from django.contrib.auth import authenticate, login

        field_errors = {}
        if 'username' not in request.data:
            field_errors['username'] = 'You must supply a "username" parameter.'
        if 'password' not in request.data:
            field_errors['password'] = 'You must supply a "password" parameter.'
        if field_errors:
            return Response({'errors': field_errors}, status=400)

        username, password = request.data['username'], request.data['password']
        user = authenticate(username=username, password=password)

        if user is None:
            return Response({'errors': {'__all__': 'Invalid username or password.'}}, status=401)

        login(request, user)
        user_url = reverse('user-detail', args=[user.username])
        # use the absolute url for CORS protection:
        user_url = request.build_absolute_uri(user_url)

        # Is cross-origin?
        if 'HTTP_ORIGIN' in request.META:
            return HttpResponse(content=user_url, status=200, content_type='text/plain')
        else:
            return HttpResponseRedirect(user_url, status=303)

    def delete(self, request):
        from django.contrib.auth import logout

        logout(request)
        return HttpResponse(status=204)


class SessionKeyView (CorsEnabledMixin, views.APIView):
    renderer_classes = (JSONRenderer, JSONPRenderer, BrowsableAPIRenderer)
    content_negotiation_class = ShareaboutsContentNegotiation

    def get(self, request):
        request.session.set_expiry(0)
        request.session.save()

        return Response({
            settings.SESSION_COOKIE_NAME: request.session.session_key,
        }, headers={'cache-control': 'private, max-age=0, no-cache'})


###############################################################################
#
# Social Authentication Views
# ---------------------------
#

def capture_referer(view_func):
    """
    A wrapper for views that redirect with a 'next' parameter to any
    arbitrary URL. Normally, Django (and social-auth) internals only allow
    redirecting to paths on the current host.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        client_next = request.GET.get('next', '')
        client_error_next = request.GET.get('error_next', client_next)
        referer = request.META.get('HTTP_REFERER')

        if referer:
            client_next = utils.build_relative_url(referer, client_next)
            client_error_next = utils.build_relative_url(referer, client_error_next)
        else:
            return HttpResponseBadRequest('Referer header must be set.')

        request.GET = request.GET.copy()
        request.GET['next'] = reverse('redirector') + '?' + urlencode({'target': client_next})

        request.session['client_next'] = client_next
        request.session['client_error_next'] = client_error_next

        return view_func(request, *args, **kwargs)

    return wrapper

remote_social_login = capture_referer(social_views.auth)
remote_logout = capture_referer(auth_views.logout)

def remote_social_login_error(request):
    error_redirect_url = request.session.get('client_error_next')
    return redirector(request, target=error_redirect_url)

# social_auth_login = use_social_auth_headers(social_views.auth)
# social_auth_complete = use_social_auth_headers(social_views.complete)

def redirector(request, target=None):
    """
    Simple view to redirect to external URL.
    """
    try:
        target = target if target is not None else request.GET['target']
    except KeyError:
        return HttpResponseBadRequest('No target specified to redirect to.')

    return HttpResponseRedirect(target)
