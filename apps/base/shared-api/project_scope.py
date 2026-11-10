"""Authorised project scope for task handoffs and project memory.

A project is an Open WebUI knowledge collection that the caller can read. The
only authority is Open WebUI's knowledge ACL (owner, direct or group read
grant, public read grant), read with the caller's own credential from
``GET /api/v1/knowledge/``. Pinned Open WebUI 0.12.5 applies the same owner and
read-grant rules to knowledge collections in /api/v1/retrieval/query/collection,
so a project usable here is a collection the caller can also retrieve from.

A project id supplied by a client is only a lookup key: access comes from the
caller's own listing, never from the string. Storage keys and memory groups
are derived here from the authenticated user id and the authorised project id.
"""
import hashlib
import re

from boundary import Refusal


PROJECT = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
# The pinned listing returns 30 knowledge bases per page; read at most 10 pages.
MAX_PAGES = 10


async def accessible_projects(fetch, webui, headers, want=None):
    """Return ({project id: name}, complete) readable by the caller.

    ``fetch`` is ServiceTools.upstream_json, which already sends the caller's
    own credential and maps WebUI 401/403/404 to a refusal. Listing stops early
    once ``want`` is found.
    """
    projects, seen = {}, 0
    for page in range(1, MAX_PAGES + 1):
        result = await fetch("GET", webui + "/api/v1/knowledge/?page=" + str(page), headers=headers)
        items = result.get("items") if isinstance(result, dict) else None
        total = result.get("total") if isinstance(result, dict) else None
        if not isinstance(items, list) or type(total) is not int:
            raise Refusal(502, "project_listing_invalid")
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and PROJECT.fullmatch(item["id"]):
                name = item.get("name")
                projects[item["id"]] = name[:120] if isinstance(name, str) else ""
        seen += len(items)
        if not items or seen >= total:
            return projects, True
        if want is not None and want in projects:
            return projects, False
    return projects, False


async def require_project(fetch, webui, headers, project):
    """Return (id, name) for a project the caller can read, or refuse."""
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise Refusal(400, "invalid_project")
    projects, complete = await accessible_projects(fetch, webui, headers, want=project)
    if project in projects:
        return project, projects[project]
    if not complete:
        raise Refusal(503, "project_listing_incomplete")
    # Missing and unreadable collections are indistinguishable by design.
    raise Refusal(403, "project_not_authorized")


def memory_group(user_id, project=None):
    """Graphiti group: user-wide (legacy ``u_``) or this user's project (``p_``).

    The ``u_`` formula is unchanged from the first memory release, so existing
    user-wide memory stays reachable without moving any data.
    """
    if project is None:
        return "u_" + hashlib.sha256(("workstation-memory:" + user_id).encode()).hexdigest()[:32]
    return "p_" + hashlib.sha256(("workstation-memory-project:" + user_id + "\0" + project).encode()).hexdigest()[:32]


def handoff_owner(user_id):
    """Handoff rows store a derived owner key, not the WebUI user id."""
    return "h_" + hashlib.sha256(("workstation-handoff:" + user_id).encode()).hexdigest()[:32]
