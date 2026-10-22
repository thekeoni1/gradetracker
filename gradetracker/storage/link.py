"""Linking platform courses to the logical course you take (M4).

Blackboard and Gradescope name the same course differently:

    Blackboard courseId   "ECON-166-01-2026F"
    Blackboard name       "ECON 166: Intermediate Microeconomics"
    Gradescope name       "ECON 166"

All three carry the same department-and-number prefix, so that prefix is the
match key and the section/term suffix is treated as noise.
"""

from __future__ import annotations

import re
import sqlite3

from gradetracker.storage.db import now_iso

# Department letters, then a number with an optional trailing letter (e.g. 166,
# 220, 101A). Anchored, because the prefix is the identifying part. Hamilton's
# Blackboard separates the parts with underscores ("CLASC_120_01_FA2026"), and
# an underscore is a word character, so the end is a lookahead rather than \b.
_CODE_RE = re.compile(r"^\s*([A-Za-z]{2,})[\s_-]*(\d+[A-Za-z]?)(?![A-Za-z0-9])")

# Blackboard tacks the term onto the courseId: "...-2026F" (year first) at
# some schools, "..._FA2026" (letters first) at Hamilton.
_TERM_SUFFIX_RE = re.compile(r"(\d{4})\s*([A-Za-z]{1,2})\s*$")
_TERM_PREFIX_RE = re.compile(r"(?<![A-Za-z])(FA|SP|SU|WI|F|S|U|W)(\d{4})(?!\d)", re.IGNORECASE)

_SEMESTER_LETTERS = {
    "f": "Fall",
    "fa": "Fall",
    "s": "Spring",
    "sp": "Spring",
    "su": "Summer",
    "u": "Summer",
    "w": "Winter",
    "wi": "Winter",
}

# A term token is not a course code: "FA2026 CALCULUS II" must not become
# "FA 2026". Accepts the letters above plus the spelled-out seasons.
_SEASON_WORDS = set(_SEMESTER_LETTERS) | {"fall", "spring", "summer", "winter"}


def _looks_like_term(letters: str, number: str) -> bool:
    return letters.lower() in _SEASON_WORDS and len(number) == 4 and number.isdigit()


def course_code(*candidates: str | None) -> str | None:
    """The first candidate that yields a department-and-number code.

    Returns the display form, e.g. "ECON 166". Candidates are tried in order,
    so pass the most structured field first (Blackboard's courseId before its
    free-text name, which can start with something like "Fall 2026").
    """
    for candidate in candidates:
        if not candidate:
            continue
        match = _CODE_RE.match(str(candidate))
        if match and not _looks_like_term(match.group(1), match.group(2)):
            return f"{match.group(1).upper()} {match.group(2).upper()}"
    return None


def code_key(code: str | None) -> str | None:
    """The comparison key: uppercase, non-alphanumerics stripped. "ECON166"."""
    if not code:
        return None
    key = re.sub(r"[^A-Za-z0-9]", "", str(code)).upper()
    return key or None


def term_from_course_id(course_id: str | None) -> str | None:
    """"ECON-166-01-2026F" -> "Fall 2026", so it matches Gradescope's format."""
    if not course_id:
        return None
    text = str(course_id)
    match = _TERM_SUFFIX_RE.search(text)
    if match:
        year, letters = match.group(1), match.group(2).lower()
    else:
        match = _TERM_PREFIX_RE.search(text)
        if not match:
            return None
        letters, year = match.group(1).lower(), match.group(2)
    semester = _SEMESTER_LETTERS.get(letters)
    return f"{semester} {year}" if semester else None


# --- queries --------------------------------------------------------------


def list_source_courses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every platform course with its logical course, if it has one."""
    return conn.execute(
        """
        SELECT sc.*, c.code AS course_code, c.name AS course_name, c.term AS course_term
        FROM source_courses AS sc
        LEFT JOIN courses AS c ON c.id = sc.course_id
        ORDER BY sc.source, sc.code, sc.name
        """
    ).fetchall()


def list_unlinked(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Platform courses not yet attached to a logical course."""
    return conn.execute(
        """
        SELECT * FROM source_courses
        WHERE course_id IS NULL
        ORDER BY source, code, name
        """
    ).fetchall()


def find_course_by_code(
    conn: sqlite3.Connection, code: str | None, term: str | None = None
) -> sqlite3.Row | None:
    """An existing logical course whose normalised code matches.

    Prefers a same-term match, so last year's ECON 166 does not absorb this
    year's assignments.
    """
    key = code_key(code)
    if not key:
        return None

    matches = [
        row
        for row in conn.execute("SELECT * FROM courses").fetchall()
        if code_key(row["code"]) == key
    ]
    if not matches:
        return None
    if term:
        for row in matches:
            if row["term"] == term:
                return row
        # A code match in a different term is not the same course.
        return None
    return matches[0]


def suggest_link(conn: sqlite3.Connection, source_course: sqlite3.Row) -> sqlite3.Row | None:
    """The logical course this platform course probably belongs to, or None."""
    return find_course_by_code(conn, source_course["code"], source_course["term"])


def create_course(
    conn: sqlite3.Connection, code: str, name: str | None = None, term: str | None = None
) -> int:
    """Create a logical course, or return the existing one for that code+term."""
    existing = find_course_by_code(conn, code, term)
    if existing is not None:
        return int(existing["id"])

    cursor = conn.execute(
        "INSERT INTO courses (code, name, term, created_at) VALUES (?, ?, ?, ?)",
        (code, name, term, now_iso()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def link(conn: sqlite3.Connection, source_course_id: int, course_id: int) -> bool:
    """Attach a platform course to a logical course.

    Returns True when this attach is the one that made the course draw on
    both platforms, so the caller can say what that now means.
    """
    conn.execute(
        "UPDATE source_courses SET course_id = ? WHERE id = ?",
        (course_id, source_course_id),
    )
    conn.commit()
    return default_preference(conn, course_id)


def platforms_of(conn: sqlite3.Connection, course_id: int) -> set[str]:
    """Which platforms feed this course."""
    return {
        row["source"]
        for row in conn.execute(
            "SELECT DISTINCT source FROM source_courses WHERE course_id = ?",
            (course_id,),
        )
    }


def default_preference(conn: sqlite3.Connection, course_id: int) -> bool:
    """Prefer Gradescope the first time a course draws on both platforms.

    Returns True only when it actually chose one, which is what the caller
    announces. A course already told which platform to keep is left with it,
    and a course fed by two sections of one platform is not this moment at
    all -- there is no copy to choose between there.

    This is the moment the rule is explicable, because attaching the second
    platform is the user's own action. Doing it at the next sync instead
    would mean a dozen rows quietly vanishing for reasons nobody had said.
    """
    # Local import: duplicates reads the engine, which must not be pulled in
    # just to parse a course code.
    from gradetracker.storage import duplicates

    if not {"blackboard", "gradescope"} <= platforms_of(conn, course_id):
        return False

    chose = duplicates.get_preference(conn, course_id) is None
    if chose:
        duplicates.set_preference(conn, course_id, "gradescope")

    # Applied here, not left to the next sync: on an install that has already
    # synced, both copies exist right now, so waiting would mean up to four
    # hours of double counting after a notice saying the copy is excluded.
    duplicates.apply_preference(conn, course_id)
    return chose


def unlink(conn: sqlite3.Connection, source_course_id: int) -> None:
    """Detach a platform course, e.g. after a wrong confirmation."""
    conn.execute(
        "UPDATE source_courses SET course_id = NULL WHERE id = ?", (source_course_id,)
    )
    conn.commit()


def link_auto(conn: sqlite3.Connection) -> list[dict]:
    """Accept every suggestion without prompting.

    Needed for scheduled runs and tests. Returns one record per source course
    describing what happened, so the caller can print it.
    """
    actions: list[dict] = []
    for source_course in list_unlinked(conn):
        code = source_course["code"]
        if not code:
            actions.append(
                {
                    "source_course_id": source_course["id"],
                    "source": source_course["source"],
                    "name": source_course["name"],
                    "action": "skipped",
                    "reason": "no course code could be parsed",
                }
            )
            continue

        existing = suggest_link(conn, source_course)
        if existing is not None:
            course_id = int(existing["id"])
            action = "linked"
        else:
            course_id = create_course(
                conn, code, source_course["name"], source_course["term"]
            )
            action = "created"

        preferred = link(conn, int(source_course["id"]), course_id)
        actions.append(
            {
                "source_course_id": source_course["id"],
                "source": source_course["source"],
                "name": source_course["name"],
                "action": action,
                "course_id": course_id,
                "code": code,
                "term": source_course["term"],
                # True when this attach brought in the second platform.
                "preferred": preferred,
            }
        )
    return actions
