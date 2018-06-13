from __future__ import absolute_import
from __future__ import unicode_literals
from django.conf import settings
import django.core.exceptions
from django.template.response import TemplateResponse
from django.utils.deprecation import MiddlewareMixin

from corehq import toggles
from corehq.apps.users.models import CouchUser, InvalidUser, AnonymousCouchUser
from corehq.apps.users.util import username_to_user_id
from corehq.toggles import PUBLISH_CUSTOM_REPORTS

SESSION_USER_KEY_PREFIX = "session_user_doc_%s"


class UsersMiddleware(MiddlewareMixin):

    def __init__(self, get_response=None):
        super(UsersMiddleware, self).__init__(get_response)
        # Normally we'd expect this class to be pulled out of the middleware list, too,
        # but in case someone forgets, this will stop this class from being used.
        found_domain_app = False
        for app_name in settings.INSTALLED_APPS:
            if app_name == "users" or app_name.endswith(".users"):
                found_domain_app = True
                break
        if not found_domain_app:
            raise django.core.exceptions.MiddlewareNotUsed

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.analytics_enabled = True
        if 'domain' in view_kwargs:
            request.domain = view_kwargs['domain']
        if 'org' in view_kwargs:
            request.org = view_kwargs['org']
        if request.user and request.user.is_authenticated:
            user_id = username_to_user_id(request.user.username)
            request.couch_user = CouchUser.get_by_user_id(user_id)
            if not request.couch_user.analytics_enabled:
                request.analytics_enabled = False
            if 'domain' in view_kwargs:
                domain = request.domain
                if not request.couch_user:
                    request.couch_user = InvalidUser()
                if request.couch_user:
                    request.couch_user.current_domain = domain
        return None
