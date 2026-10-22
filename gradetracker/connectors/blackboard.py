"""Blackboard connector (M2).

Calls the public REST endpoints, follows paging.nextPage, and converts 401/403
into AuthExpired. Every path lives in BlackboardPaths so recon findings can
replace them in one place.

The connector takes an AuthProvider, not cookies, so M9 swaps OAuth in without
touching this file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from gradetracker import config
from gradetracker.auth.base import AuthExpired, AuthProvider
from gradetracker.connectors.base import AccessDenied, ConnectorError

# A runaway nextPage chain would otherwise loop forever.
_MAX_PAGES = 50

# Learn's availability enum is Yes | No | Disabled | Term. "Term" means the
# course is open for the duration of its term, which SIS-fed schools use
# routinely; treating it as unavailable would silently drop every course.
_AVAILABLE = {"yes", "term"}


@dataclass(frozen=True)
class BlackboardPaths:
    """The four endpoints this connector calls, all overridable via env.

    Defaults are the standard Blackboard Learn public REST paths. If recon shows
    Hamilton only serves grades from internal endpoints, the replacements go
    here and the methods that use them get an _internal_ prefix so M9 knows
    exactly what to swap. (TODO: no recon yet, so nothing is flagged internal.)
    """

    me: str = config.BB_PATH_ME
    courses: str = config.BB_PATH_COURSES
    columns: str = config.BB_PATH_COLUMNS
    grades: str = config.BB_PATH_GRADES


class BlackboardConnector:
    """Fetches one dict per sync: the user, their current courses, and grades."""

    source = "blackboard"

    def __init__(
        self,
        auth: AuthProvider,
        base_url: str,
        delay_s: float = 0.75,
        sleep: Callable[[float], None] = time.sleep,
        paths: BlackboardPaths | None = None,
        term_id: str | None = None,
    ) -> None:
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        self.delay_s = delay_s
        self.sleep = sleep
        self.paths = paths or BlackboardPaths()
        self.term_id = term_id

    # --- HTTP ------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Absolute URL for a path or a relative nextPage link."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"

    def _get(self, session, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GET, with every way Blackboard says "no" mapped to one meaning.

        401 is always an expired session. 403 is a per-resource permission
        problem, which the caller may choose to skip. A response that came back
        from another host, or a 2xx that is not JSON, means requests quietly
        followed a redirect to the SSO login page: that is an expired session
        too, and misreading it as a parse error would send the user hunting a
        connector bug.
        """
        try:
            response = session.get(self._url(url), params=params, timeout=30)
        except Exception as exc:  # requests.RequestException and friends
            raise ConnectorError(f"Request to {url} failed: {exc}") from exc

        status = response.status_code

        if status == 401:
            raise AuthExpired(f"Blackboard returned 401 for {url}. The session has expired.")
        if status == 403:
            raise AccessDenied(f"Blackboard returned 403 for {url}", status=403, url=url)

        final_host = urlparse(getattr(response, "url", "") or "").netloc
        if final_host and final_host != urlparse(self.base_url).netloc:
            raise AuthExpired(
                f"Request for {url} was redirected to {final_host}, which means the "
                "login page. The session has expired."
            )

        if not 200 <= status < 300:
            raise ConnectorError(f"Blackboard returned {status} for {url}")

        content_type = (getattr(response, "headers", None) or {}).get("Content-Type", "")
        if content_type and "json" not in content_type.lower():
            raise AuthExpired(
                f"Blackboard returned {content_type} rather than JSON for {url}, which "
                "means the login page. The session has expired."
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError(f"Blackboard returned non-JSON for {url}: {exc}") from exc

    def _paged(
        self, session, url: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every result, following paging.nextPage until it runs out.

        nextPage comes back as a relative path with its own query string, so the
        original params are not reapplied to it.
        """
        next_url = url
        next_params = params
        # The start URL counts as seen, so a nextPage pointing back at page one
        # is caught rather than quietly yielding every row twice.
        seen = {self._url(url)}

        for _ in range(_MAX_PAGES):
            payload = self._get(session, next_url, next_params)
            results = payload.get("results")
            if results is None:
                # A single-object endpoint; hand it back as the only result.
                yield payload
                return
            for row in results:
                yield row

            next_link = (payload.get("paging") or {}).get("nextPage")
            if not next_link:
                return

            absolute = self._url(next_link)
            if absolute in seen:
                # A cycle is a server bug, and silently truncating would hide it.
                raise ConnectorError(
                    f"Pagination cycle while fetching {url}: {next_link} was already fetched"
                )
            seen.add(absolute)
            next_url = next_link
            next_params = None

        raise ConnectorError(f"Pagination for {url} exceeded {_MAX_PAGES} pages")

    # --- filtering -------------------------------------------------------

    def _is_current(self, membership: dict[str, Any]) -> bool:
        """Keep available courses, optionally pinned to one term.

        Term end dates would need another request, so availability plus an
        optional exact term match is the cheap filter. It keeps last year's
        courses out of the dashboard.
        """
        course = membership.get("course") or {}
        availability = course.get("availability") or membership.get("availability") or {}
        if str(availability.get("available") or "").strip().lower() not in _AVAILABLE:
            return False
        if self.term_id and course.get("termId") != self.term_id:
            return False
        return True

    # --- fetch -----------------------------------------------------------

    def fetch(self) -> dict[str, Any]:
        """Auth, then one dict holding the user, courses, columns and grades.

        A 403 on the user's own records is an expired session. A 403 on one
        course is that course's permissions, so it lands in "skipped" and the
        sync continues; one unreadable gradebook must not cost every grade.
        """
        session = self.auth.session()

        try:
            user = self._get(session, self.paths.me)
        except AccessDenied as exc:
            raise AuthExpired(f"Blackboard denied access to your own user record: {exc}") from exc

        user_id = user.get("id")
        if not user_id:
            raise ConnectorError(f"No user id in the response from {self.paths.me}")

        courses_path = self.paths.courses.format(userId=user_id)
        try:
            memberships = list(
                self._paged(session, courses_path, params={"expand": "course"})
            )
        except AccessDenied as exc:
            raise AuthExpired(f"Blackboard denied access to your course list: {exc}") from exc

        courses: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        attempted = 0

        # Collected so a sync that finds nothing can say WHY, rather than
        # printing "0 courses" and leaving the user guessing.
        availability_seen: set[str] = set()
        terms_seen: set[str] = set()

        for membership in memberships:
            course_meta = membership.get("course") or {}
            availability_seen.add(
                str(
                    (course_meta.get("availability") or membership.get("availability") or {}).get(
                        "available"
                    )
                )
            )
            if course_meta.get("termId"):
                terms_seen.add(str(course_meta["termId"]))

            if not self._is_current(membership):
                continue

            course_id = membership.get("courseId") or (membership.get("course") or {}).get("id")
            if not course_id:
                continue

            if attempted:
                # Be a quieter client than the UI itself: ~20 requests a sync.
                self.sleep(self.delay_s)
            attempted += 1

            try:
                columns = list(
                    self._paged(session, self.paths.columns.format(courseId=course_id))
                )
                grades = list(
                    self._paged(
                        session,
                        self.paths.grades.format(courseId=course_id, userId=user_id),
                    )
                )
            except AccessDenied as exc:
                skipped.append({"courseId": course_id, "status": exc.status, "url": exc.url})
                continue

            courses.append({**membership, "columns": columns, "grades": grades})

        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": self.source,
            "user": user,
            "courses": courses,
            "skipped": skipped,
            # Diagnostics, for the "found memberships but no courses" case.
            "memberships_seen": len(memberships),
            "availability_seen": sorted(availability_seen),
            "terms_seen": sorted(terms_seen),
            "term_filter": self.term_id,
        }
