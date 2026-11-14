"""Authorised project scope for task handoffs and project memory.

A project is an Open WebUI knowledge collection that the caller can read. The
only authority is Open WebUI's knowledge ACL (owner, direct or group read
grant, public read grant), read with the caller's own credential from
``GET /api/v1/knowledge/``. Pinned Open WebUI 0.13.3 applies the same owner and
read-grant rules to knowledge collections in /api/v1/retrieval/query/collection,
so a project usable here is a collection the caller can also retrieve from.

A project id supplied by a client is only a lookup key: access comes from the
caller's own listing, never from the string. Storage keys and memory groups
are derived here from the authenticated user id and the authorised project id.

A chat folder (an Open WebUI "project") is the other kind of memory project. Its
id is never a tool argument: Open WebUI sends the chat id in a connection header,
and the folder is read from that chat with the caller's own credential, then
checked with a folder read the caller is allowed (owner or read grant).
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3

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


# Chat folders. Open WebUI fills this header from the chat being answered
# ({{CHAT_ID}} in the tool-server connection's headers, apps/components/assistant/webui.yaml).
CHAT_HEADER = "x-openwebui-chat-id"
# Temporary and channel chats are not saved chats and have no folder.
UNSAVED_CHAT = ("temporary:", "local:", "channel:")
# A chat read returns its whole history; long chats are larger than other reads.
MAX_CHAT_BYTES = 16 * 1024 * 1024
# Folder memory keys cannot collide with knowledge collection ids, which never contain ":".
FOLDER_KEY = "folder:"
MEMORY_MODES = ("project_and_user", "project_only")
DEFAULT_MODE = "project_and_user"
MAX_PROJECT_ONLY_FOLDERS = 200


async def chat_folder(fetch, webui, headers, chat_id):
    """Return (folder id, name) for the folder of a chat the caller can read, or None.

    The chat and the folder are both read with the caller's own credential, so a
    chat id that is not the caller's (or not shared with them) resolves to nothing
    usable, and the folder must be one the caller owns or has a read grant on.
    """
    if not chat_id or chat_id.startswith(UNSAVED_CHAT):
        return None
    if not PROJECT.fullmatch(chat_id):
        raise Refusal(400, "invalid_chat_id")
    try:
        chat = await fetch("GET", webui + "/api/v1/chats/" + chat_id, headers=headers, max_bytes=MAX_CHAT_BYTES)
    except Refusal as exc:
        if exc.status in {401, 403, 404}:
            raise Refusal(403, "chat_not_authorized") from None
        raise
    if not isinstance(chat, dict) or chat.get("id") != chat_id:
        raise Refusal(502, "chat_lookup_invalid")
    folder = chat.get("folder_id")
    if folder is None:
        return None
    if not isinstance(folder, str) or not PROJECT.fullmatch(folder):
        raise Refusal(502, "chat_lookup_invalid")
    try:
        record = await fetch("GET", webui + "/api/v1/folders/" + folder, headers=headers)
    except Refusal as exc:
        if exc.status in {401, 403, 404}:
            raise Refusal(403, "project_not_authorized") from None
        raise
    if not isinstance(record, dict) or record.get("id") != folder:
        raise Refusal(502, "chat_lookup_invalid")
    name = record.get("name")
    return folder, name[:120] if isinstance(name, str) else ""


def folder_memory_group(user_id, folder):
    """This user's memory group for one chat folder: the ``p_`` formula with a folder key."""
    return memory_group(user_id, FOLDER_KEY + folder)


class MemoryModes:
    """Per-user, per-folder project-only switch in /state/project-memory.sqlite3.

    Only "project_only" folders have a row; the default needs none. Rows hold a
    derived owner key, never the Open WebUI user id. A deleted folder leaves an
    unreachable row: its chats are gone, so nothing resolves to it.
    """

    def __init__(self, state_dir, clock=None):
        self.database = Path(state_dir) / "project-memory.sqlite3"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def owner(user_id):
        return "m_" + hashlib.sha256(("workstation-memory-mode:" + user_id).encode()).hexdigest()[:32]

    def connect(self):
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database.is_symlink():
            raise Refusal(503, "memory_mode_store_unavailable")
        db = sqlite3.connect(self.database, timeout=2, isolation_level=None)
        db.execute("CREATE TABLE IF NOT EXISTS project_only (owner TEXT NOT NULL, folder TEXT NOT NULL, "
                   "updated_at TEXT NOT NULL, PRIMARY KEY (owner, folder))")
        return db

    def get(self, user_id, folder):
        try:
            with closing(self.connect()) as db:
                row = db.execute("SELECT 1 FROM project_only WHERE owner = ? AND folder = ?",
                                 (self.owner(user_id), folder)).fetchone()
        except (sqlite3.Error, OSError):
            raise Refusal(503, "memory_mode_store_unavailable") from None
        return "project_only" if row else DEFAULT_MODE

    def set(self, user_id, folder, mode):
        """Store the mode; return the previous one."""
        if mode not in MEMORY_MODES:
            raise Refusal(400, "invalid_memory_mode")
        owner = self.owner(user_id)
        updated_at = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")
        try:
            with closing(self.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    found = db.execute("SELECT 1 FROM project_only WHERE owner = ? AND folder = ?",
                                       (owner, folder)).fetchone()
                    if mode == "project_only" and not found:
                        if db.execute("SELECT COUNT(*) FROM project_only WHERE owner = ?",
                                      (owner,)).fetchone()[0] >= MAX_PROJECT_ONLY_FOLDERS:
                            raise Refusal(413, "memory_mode_limit_reached")
                        db.execute("INSERT INTO project_only VALUES (?, ?, ?)", (owner, folder, updated_at))
                    elif mode == DEFAULT_MODE:
                        db.execute("DELETE FROM project_only WHERE owner = ? AND folder = ?", (owner, folder))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except (sqlite3.Error, OSError):
            raise Refusal(503, "memory_mode_store_unavailable") from None
        return "project_only" if found else DEFAULT_MODE
