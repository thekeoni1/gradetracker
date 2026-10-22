-- SQLite schema (M4). Applied idempotently on every connection.
--
-- Both platforms collapse into one model: a logical course has categories, and
-- categories hold assignments. One real course can pull assignments from both
-- platforms (problem sets on Gradescope, exams on Blackboard), which is why
-- source_courses sits between courses and assignments.
--
-- JSON columns are declared TEXT because that is what SQLite actually stores;
-- "JSON" as a type name would take NUMERIC affinity, which is misleading.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- The course you actually take, across platforms.
CREATE TABLE IF NOT EXISTS courses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,              -- normalised, e.g. "ECON 166"
    name       TEXT,
    term       TEXT,
    created_at TEXT NOT NULL,
    -- Which platform wins when both carry the same assignment. NULL means
    -- "ask me": the duplicates card keeps flagging the pairs instead.
    preferred_source TEXT
                     CHECK (preferred_source IN ('blackboard', 'gradescope')),
    -- One course per code per term, so gt link --auto cannot create duplicates.
    UNIQUE (code, term)
);

-- A course as one platform sees it. course_id stays NULL until you link it.
CREATE TABLE IF NOT EXISTS source_courses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,            -- blackboard | gradescope
    platform_id  TEXT NOT NULL,
    name         TEXT,
    code         TEXT,
    term         TEXT,
    course_id    INTEGER REFERENCES courses (id) ON DELETE SET NULL,
    raw          TEXT,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source, platform_id)
);

-- Grading buckets. weight is NULL in points-based courses.
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    weight      REAL,
    drop_lowest INTEGER NOT NULL DEFAULT 0,
    -- JSON list of per-rank weights, highest first, e.g. "[23, 17]" for a
    -- syllabus that weighs the better midterm 23% and the other 17%. NULL for
    -- the ordinary rule, where the bucket is its own points earned/possible.
    rank_weights TEXT,
    origin      TEXT NOT NULL DEFAULT 'manual'
                CHECK (origin IN ('syllabus', 'gradebook', 'manual')),
    UNIQUE (course_id, name)
);

-- Every graded item from either platform, plus manual ones you add by hand.
CREATE TABLE IF NOT EXISTS assignments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_course_id INTEGER NOT NULL REFERENCES source_courses (id) ON DELETE CASCADE,
    platform_id      TEXT NOT NULL,
    name             TEXT,
    category_id      INTEGER REFERENCES categories (id) ON DELETE SET NULL,
    -- Set when you choose a category by hand; sync must never overwrite it.
    category_locked  INTEGER NOT NULL DEFAULT 0,
    earned           REAL,
    possible         REAL,
    status           TEXT NOT NULL
                     CHECK (status IN ('graded', 'submitted', 'ungraded', 'missing', 'excused')),
    due_at           TEXT,
    extra_credit     INTEGER NOT NULL DEFAULT 0,
    missing_as_zero  INTEGER NOT NULL DEFAULT 1,
    -- A row you created yourself (paper exam, participation). Sync leaves these alone.
    manual           INTEGER NOT NULL DEFAULT 0,
    -- Hidden by hand, e.g. the duplicate copy of an assignment mirrored to both
    -- platforms. Kept in the table so the UI can show it greyed out.
    excluded         INTEGER NOT NULL DEFAULT 0,
    -- Who excluded it: 'user' for a click or a CLI call, 'auto' for the
    -- preferred-platform rule. The rule may only touch rows with neither, so
    -- a deliberate "count this again" is never undone by the next sync.
    exclusion_source TEXT CHECK (exclusion_source IN ('auto', 'user')),
    raw              TEXT,
    last_seen_at     TEXT NOT NULL,
    UNIQUE (source_course_id, platform_id)
);

-- Confirmed grading rules, one per course.
CREATE TABLE IF NOT EXISTS schemes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id    INTEGER NOT NULL UNIQUE REFERENCES courses (id) ON DELETE CASCADE,
    mode         TEXT NOT NULL CHECK (mode IN ('weighted', 'points')),
    cutoffs      TEXT,                     -- JSON: {"A": 93, "A-": 90, ...}
    notes        TEXT,                     -- curves, participation, anything non-mechanical
    origin       TEXT,
    confirmed_at TEXT,
    -- A flat bonus added to the final percentage, e.g. a course-wide curve.
    bonus_percent REAL NOT NULL DEFAULT 0,
    -- Set when a newer syllabus appears: "rules may be outdated" until re-confirmed.
    outdated     INTEGER NOT NULL DEFAULT 0
);

-- Syllabus files. Unused until M6's upload path; kept here so the schema is in one place.
CREATE TABLE IF NOT EXISTS syllabi (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id         INTEGER NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    origin            TEXT,
    file_path         TEXT,
    content_hash      TEXT,
    extraction_status TEXT,
    created_at        TEXT NOT NULL
);

-- One row per sync attempt.
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'auth_expired', 'error')),
    raw_path    TEXT,
    message     TEXT
);

-- Named sets of hypothetical scores (M7).
CREATE TABLE IF NOT EXISTS scenarios (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    overrides  TEXT,                       -- JSON: {assignment_id: score}
    created_at TEXT NOT NULL,
    UNIQUE (course_id, name)
);

-- What changed since the previous sync (M8).
CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES assignments (id) ON DELETE CASCADE,
    -- 'excluded' is the app's own decision rather than the platform's: the
    -- preferred-platform rule setting aside a mirrored copy.
    kind          TEXT NOT NULL CHECK (kind IN ('new', 'score', 'status', 'excluded')),
    old_value     TEXT,
    new_value     TEXT,
    seen_at       TEXT NOT NULL
);

-- Per-source connection state, for the reconnect banner (M8).
CREATE TABLE IF NOT EXISTS connections (
    source          TEXT PRIMARY KEY,
    state           TEXT,
    last_success_at TEXT,
    last_login_at   TEXT,
    last_expired_at TEXT,
    notified_at     TEXT
);

-- How long each login lasted, which is the evidence for the OAuth pitch:
-- "cookie sessions last N hours, an official integration would not expire".
CREATE TABLE IF NOT EXISTS session_spans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    logged_in_at TEXT NOT NULL,
    expired_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_spans_source ON session_spans (source, logged_in_at);
CREATE INDEX IF NOT EXISTS idx_source_courses_course ON source_courses (course_id);
CREATE INDEX IF NOT EXISTS idx_assignments_source_course ON assignments (source_course_id);
CREATE INDEX IF NOT EXISTS idx_assignments_category ON assignments (category_id);
CREATE INDEX IF NOT EXISTS idx_categories_course ON categories (course_id);
CREATE INDEX IF NOT EXISTS idx_sync_runs_source ON sync_runs (source, started_at);
CREATE INDEX IF NOT EXISTS idx_changes_assignment ON changes (assignment_id, seen_at);
