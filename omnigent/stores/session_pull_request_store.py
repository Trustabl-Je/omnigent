"""Persistent store for the pull requests linked to each session.

Hosts report the PRs they observe and the states they fetch; this store keeps
them in one JSON document on the session's metadata row
(``omnigent_conversation_metadata.pull_requests``) so the Pull Requests tab can
list them while the host is offline. Re-reporting a host's registry converges
on the same document, and a poll that changes nothing does not write.
Unlinking drops the PR and records an unlink that ignores later reports until
the host stops reporting the PR.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal, cast

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from omnigent.db import compression
from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)

_logger = logging.getLogger(__name__)

Relationship = Literal["created", "worked_on", "attached", "inferred"]

MAX_PRS_PER_SESSION = 50
MAX_UNLINKS_PER_SESSION = 50
# Below the 16,384-byte limit on a stored column value, after compression.
_MAX_STORED_BYTES = 16_000

# A stronger relationship survives any later report; "created" is never downgraded.
_RELATIONSHIP_RANK: dict[str, int] = {"inferred": 0, "worked_on": 1, "attached": 2, "created": 3}
_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
# GitHub's own limit on a PR title.
_TITLE_MAX = 256
# Hosts re-report the branch PR on every poll; coarser steps keep those polls read-only.
_SEEN_STEP_S = 60.0
_STATE_REFRESH_S = 60.0
# Longer than any proxied request, so a response begun before an unlink can't revive it.
_UNLINK_SETTLE_S = 600.0


@dataclass(frozen=True)
class PullRequestReport:
    """One PR as a host's registry describes it; times are epoch seconds.

    :ivar url: Canonical PR URL, as the host's git provider normalized it.
    :ivar provider: Git provider id, e.g. ``"github"``.
    :ivar host: Forge host, e.g. ``"github.com"``.
    :ivar repository: Base repository, e.g. ``"owner/repo"``.
    :ivar number: The PR number.
    :ivar relationship: How the PR joined the session.
    :ivar first_seen_at: When the PR was first observed.
    :ivar last_seen_at: When the PR was last observed.
    :ivar title: The host's latest title, or ``None`` when it has none.
    """

    url: str
    provider: str
    host: str
    repository: str
    number: int
    relationship: Relationship
    first_seen_at: float
    last_seen_at: float
    title: str | None = None


@dataclass(frozen=True)
class StoredPullRequest(PullRequestReport):
    """A linked PR with the last state its host fetched.

    :ivar state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``, or ``None`` until fetched.
    :ivar is_draft: Whether the PR was a draft at that fetch.
    :ivar state_checked_at: Epoch seconds of that fetch's request, or ``None``.
    """

    state: str | None = None
    is_draft: bool = False
    state_checked_at: float | None = None


@dataclass(frozen=True)
class PullRequestPage:
    """One page of a session's linked PRs in display order.

    :ivar entries: The PRs on this page.
    :ivar has_more: Whether PRs follow the last one.
    """

    entries: list[StoredPullRequest]
    has_more: bool


@dataclass(frozen=True)
class _Unlink:
    url: str
    unlinked_at: float


@dataclass(frozen=True)
class _Document:
    """A session's linked PRs in display order, and its pending unlinks oldest first."""

    prs: tuple[StoredPullRequest, ...] = ()
    unlinked: tuple[_Unlink, ...] = ()


_DOCUMENT = TypeAdapter(_Document)


def _decode(text: str | None) -> _Document:
    if text is None:
        return _Document()
    try:
        return _DOCUMENT.validate_json(text)
    except (ValidationError, ValueError):
        _logger.warning("Ignoring an unreadable session pull request document", exc_info=True)
        return _Document()


def _encode(document: _Document) -> str:
    return _DOCUMENT.dump_json(document).decode()


def _fit(document: _Document) -> _Document:
    """Drop the document's last PRs, then its oldest unlinks, until it can be stored."""
    # An empty document always fits, so this ends.
    while len(compression.encode(_encode(document)) or b"") > _MAX_STORED_BYTES:
        if document.prs:
            document = replace(document, prs=document.prs[:-1])
        else:
            document = replace(document, unlinked=document.unlinked[1:])
    return document


def _clean_title(title: str | None) -> str | None:
    return title.strip()[:_TITLE_MAX] if title and title.strip() else None


def _display_order(prs: Iterable[StoredPullRequest]) -> tuple[StoredPullRequest, ...]:
    """Explicit PRs newest first, then inferred PRs oldest first."""

    def key(pr: StoredPullRequest) -> tuple[bool, float, str]:
        inferred = pr.relationship == "inferred"
        return inferred, pr.first_seen_at if inferred else -pr.last_seen_at, pr.url

    return tuple(sorted(prs, key=key))


def _new_entry(report: PullRequestReport) -> StoredPullRequest:
    return StoredPullRequest(**{**asdict(report), "title": _clean_title(report.title)})


def _merge(
    entry: StoredPullRequest, report: PullRequestReport, requested_at: float | None
) -> StoredPullRequest:
    """Fold a report into a linked PR."""
    relationship = entry.relationship
    if _RELATIONSHIP_RANK[report.relationship] > _RELATIONSHIP_RANK[relationship]:
        relationship = report.relationship
    last_seen = entry.last_seen_at
    if report.last_seen_at >= last_seen + _SEEN_STEP_S:
        last_seen = report.last_seen_at
    title = _clean_title(report.title)
    # A response requested before the last full fetch carries an older title.
    fresh = (
        requested_at is None
        or entry.state_checked_at is None
        or requested_at >= entry.state_checked_at
    )
    return replace(
        entry,
        relationship=relationship,
        first_seen_at=min(entry.first_seen_at, report.first_seen_at),
        last_seen_at=last_seen,
        title=title if title is not None and fresh else entry.title,
    )


class SessionPullRequestStore:
    """SQLAlchemy-backed store of the pull requests linked to each session."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy URI of the database holding
            ``omnigent_conversation_metadata``, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store", immediate=True
        )

    def _metadata_rows(self, conversation_ids: Sequence[str]) -> Select[tuple[str, str | None]]:
        return select(SqlConversationMetadata.id, SqlConversationMetadata.pull_requests).where(
            SqlConversationMetadata.workspace_id == current_workspace_id(),
            SqlConversationMetadata.id.in_(conversation_ids),
        )

    def _load(self, session: Session, conversation_id: str, *, lock: bool) -> _Document | None:
        """Read the session's document, or ``None`` when the session does not exist."""
        stmt = self._metadata_rows([conversation_id])
        row = session.execute(stmt.with_for_update() if lock else stmt).one_or_none()
        return None if row is None else _decode(row.pull_requests)

    def _read(self, conversation_id: str) -> _Document:
        with self._session("get_session_pull_requests") as session:
            return self._load(session, conversation_id, lock=False) or _Document()

    def _update(
        self, operation: str, conversation_id: str, change: Callable[[_Document], _Document]
    ) -> _Document | None:
        """Apply ``change`` to the session's document, writing only when it changes it.

        :returns: The document before the write, or ``None`` when nothing was written.
        """

        def apply(current: _Document) -> _Document:
            updated = change(current)
            # Fitting can undo the change, e.g. a re-reported PR that never fit.
            return current if updated == current else _fit(updated)

        with self._session("get_session_pull_requests") as session:
            current = self._load(session, conversation_id, lock=False)
        if current is None or apply(current) == current:
            return None

        def write(session: Session) -> _Document | None:
            current = self._load(session, conversation_id, lock=True)
            if current is None:
                return None
            updated = apply(current)
            if updated == current:
                return None
            session.execute(
                update(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id == conversation_id,
                )
                .values(pull_requests=_encode(updated))
            )
            return current

        return cast(
            "_Document | None", run_write_transaction(self._session_immediate, operation, write)
        )

    def list_page(
        self, conversation_id: str, *, limit: int, after_url: str | None = None
    ) -> PullRequestPage:
        """Return a page of the session's linked PRs in display order.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param limit: The most PRs to return.
        :param after_url: The last PR of the previous page, or ``None`` for the first page.
        :raises LookupError: ``after_url`` is not linked to the session.
        """
        prs = list(self._read(conversation_id).prs)
        start = 0
        if after_url is not None:
            start = next((i + 1 for i, pr in enumerate(prs) if pr.url == after_url), -1)
            if start < 0:
                raise LookupError(after_url)
        return PullRequestPage(
            entries=prs[start : start + limit], has_more=len(prs) > start + limit
        )

    def list_for_session(self, conversation_id: str) -> list[StoredPullRequest]:
        """Return all of the session's linked PRs in display order.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        """
        return list(self._read(conversation_id).prs)

    def has_links(self, conversation_id: str) -> bool:
        """Return whether the session links any PR.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        """
        return bool(self._read(conversation_id).prs)

    def primary_for_sessions(
        self, conversation_ids: Sequence[str]
    ) -> dict[str, StoredPullRequest]:
        """Return each session's first linked PR in display order.

        :param conversation_ids: Sessions to summarize; ones without PRs are absent.
        """
        if not conversation_ids:
            return {}
        stmt = self._metadata_rows(list(dict.fromkeys(conversation_ids)))
        with self._session("list_primary_session_pull_requests") as session:
            rows = session.execute(stmt).all()
        primaries: dict[str, StoredPullRequest] = {}
        for row in rows:
            prs = _decode(row.pull_requests).prs
            if prs:
                primaries[row.id] = prs[0]
        return primaries

    def is_unlinked(self, conversation_id: str, url: str) -> bool:
        """Return whether the user unlinked ``url`` and the host may still report it.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: A PR URL.
        """
        return any(unlink.url == url for unlink in self._read(conversation_id).unlinked)

    def pending_unlinks(self, conversation_id: str, *, limit: int) -> list[str]:
        """Return URLs the user unlinked that the host may still report, newest first.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param limit: The most URLs to return.
        """
        unlinked = self._read(conversation_id).unlinked
        return [unlink.url for unlink in reversed(unlinked)][:limit]

    def settle_unlinks(
        self, conversation_id: str, reported_urls: Sequence[str], *, requested_at: float
    ) -> bool:
        """Forget pending unlinks the host no longer reports.

        Only unlinks made :data:`_UNLINK_SETTLE_S` before the request are
        forgotten, so a slower response begun before the unlink cannot revive it.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param reported_urls: Every PR URL in the host's response.
        :param requested_at: Epoch seconds the server sent that request.
        :returns: Whether any unlink was forgotten.
        """
        reported = set(reported_urls)
        cutoff = requested_at - _UNLINK_SETTLE_S

        def change(document: _Document) -> _Document:
            kept = tuple(
                unlink
                for unlink in document.unlinked
                if unlink.url in reported or unlink.unlinked_at > cutoff
            )
            return replace(document, unlinked=kept)

        return (
            self._update("settle_session_pull_request_unlinks", conversation_id, change)
            is not None
        )

    def record(
        self,
        conversation_id: str,
        reports: Sequence[PullRequestReport],
        *,
        requested_at: float | None = None,
    ) -> bool:
        """Merge reported PRs into the session; unlinked ones are ignored.

        A session already linking :data:`MAX_PRS_PER_SESSION` PRs gains no new ones.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param reports: PRs as the host's registry describes them.
        :param requested_at: Epoch seconds the server requested the report, or
            ``None`` when unknown. A report older than a PR's last full fetch
            keeps that fetch's title.
        :returns: Whether the session's PRs changed.
        """
        unique = {report.url: report for report in reports}
        if not unique:
            return False

        def change(document: _Document) -> _Document:
            unlinked = {unlink.url for unlink in document.unlinked}
            prs = {pr.url: pr for pr in document.prs}
            for url, report in unique.items():
                if url in prs:
                    prs[url] = _merge(prs[url], report, requested_at)
                elif url not in unlinked and len(prs) < MAX_PRS_PER_SESSION:
                    prs[url] = _new_entry(report)
            return replace(document, prs=_display_order(prs.values()))

        previous = self._update("record_session_pull_requests", conversation_id, change)
        return previous is not None

    def attach(self, conversation_id: str, report: PullRequestReport) -> None:
        """Link a PR the user chose, clearing an earlier unlink.

        A session at :data:`MAX_PRS_PER_SESSION` drops its last PR in display order.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param report: The PR as the host that verified it describes it.
        """
        attached = replace(report, relationship="attached")

        def change(document: _Document) -> _Document:
            prs = {pr.url: pr for pr in document.prs}
            existing = prs.get(report.url)
            prs[report.url] = (
                _new_entry(attached) if existing is None else _merge(existing, attached, None)
            )
            ordered = _display_order(prs.values())
            if len(ordered) > MAX_PRS_PER_SESSION:
                dropped = next(pr for pr in reversed(ordered) if pr.url != report.url)
                ordered = tuple(pr for pr in ordered if pr.url != dropped.url)
            unlinked = tuple(unlink for unlink in document.unlinked if unlink.url != report.url)
            return _Document(prs=ordered, unlinked=unlinked)

        self._update("attach_session_pull_request", conversation_id, change)

    def remove(self, conversation_id: str, url: str) -> bool:
        """Unlink a PR, remembering it so later reports cannot link it again.

        The oldest unlinks are forgotten beyond :data:`MAX_UNLINKS_PER_SESSION`.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The linked PR's URL.
        :returns: Whether the PR was linked.
        """
        unlinked_at = now_epoch_us() / 1_000_000

        def change(document: _Document) -> _Document:
            unlinked = [unlink for unlink in document.unlinked if unlink.url != url]
            unlinked.append(_Unlink(url=url, unlinked_at=unlinked_at))
            return _Document(
                prs=tuple(pr for pr in document.prs if pr.url != url),
                unlinked=tuple(unlinked[-MAX_UNLINKS_PER_SESSION:]),
            )

        previous = self._update("unlink_session_pull_request", conversation_id, change)
        return previous is not None and any(pr.url == url for pr in previous.prs)

    def update_state(
        self,
        conversation_id: str,
        url: str,
        *,
        state: str,
        is_draft: bool,
        title: str | None,
        checked_at: float,
    ) -> bool:
        """Store the state a host fetched for a linked PR, and refresh its title.

        A fetch older than the stored one, an unknown state, or a PR that is
        not linked is ignored. An unchanged state is rewritten at most once a
        minute, just to advance the last sync time.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The linked PR's URL.
        :param state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``.
        :param is_draft: Whether the PR is a draft.
        :param title: The fetched title, or ``None`` to keep the stored one.
        :param checked_at: Epoch seconds the server requested the fetch.
        :returns: Whether the PR changed.
        """
        if state not in _STATES:
            return False
        fetched_title = _clean_title(title)

        def refresh(entry: StoredPullRequest) -> StoredPullRequest:
            if entry.url != url or (
                entry.state_checked_at is not None and entry.state_checked_at > checked_at
            ):
                return entry
            changed = (entry.state, entry.is_draft) != (state, is_draft) or (
                fetched_title is not None and fetched_title != entry.title
            )
            stale = (
                entry.state_checked_at is None
                or checked_at - entry.state_checked_at >= _STATE_REFRESH_S
            )
            if not (changed or stale):
                return entry
            return replace(
                entry,
                state=state,
                is_draft=is_draft,
                state_checked_at=checked_at,
                title=fetched_title or entry.title,
            )

        def change(document: _Document) -> _Document:
            return replace(document, prs=tuple(refresh(pr) for pr in document.prs))

        return (
            self._update("update_session_pull_request_state", conversation_id, change) is not None
        )
