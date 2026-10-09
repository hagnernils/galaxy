"""Galaxy-only Django login route and view.

Combines the URL route and its single view in one module since they are
tightly coupled and small; ROOT_URLCONF in galaxy_settings.py points here.
Gated by GALAXY_INTERACTIVE=true, consistent with galaxy_settings.py.
"""

import json
import os

from django.contrib.auth import get_user_model, login
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import path
from django.views.decorators.http import require_GET

from escriptorium.urls import urlpatterns as escriptorium_urlpatterns

if os.environ.get("GALAXY_INTERACTIVE", "").lower() != "true":
    raise RuntimeError("Galaxy login route requires GALAXY_INTERACTIVE=true")


@require_GET
def galaxy_login(request):
    if os.environ.get("GALAXY_INTERACTIVE", "").lower() != "true":
        return HttpResponseForbidden("Galaxy login is disabled")

    galaxy_user_id = os.environ.get("GALAXY_USER_ID", "")
    email = os.environ.get("GALAXY_USER_EMAIL", "")
    if not galaxy_user_id or not email:
        return HttpResponseBadRequest("Missing Galaxy session context")

    username = f"galaxy_{galaxy_user_id}"
    try:
        user = get_user_model().objects.get(
            username=username, email=email, is_active=True, is_staff=False, is_superuser=False
        )
    except get_user_model().DoesNotExist:
        return HttpResponseForbidden("Galaxy user mapping is unavailable")

    report_path = os.path.join(os.environ.get("GALAXY_WORKSPACE", ""), "outputs", "session-report.json")
    try:
        with open(report_path, encoding="utf-8") as report_file:
            document_id = json.load(report_file)["document"]["id"]
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return HttpResponseBadRequest("Imported document context is unavailable")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("document-images", pk=document_id)


urlpatterns = [
    path("galaxy/login/", galaxy_login, name="galaxy-login"),
] + escriptorium_urlpatterns
