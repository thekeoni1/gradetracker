"""The what-if page's own JavaScript, driven in a real browser.

Two bugs shipped past a fully green suite because both live in the page's
inline script: "Set all to 85%" rewrote scores that were already in, and
"reset" left the slider in the middle of its range instead of back on the
mark. A test client renders markup and never runs a line of it, so these open
the rendered page in Chromium instead.

Over file://, not a server: the loopback connection is blocked in this
environment, and the page's script is inline either way. The stylesheet and
htmx tags are dropped, since neither resolves from a file and neither is what
is under test.
"""

from __future__ import annotations

import re

import pytest

from gradetracker.storage import db
from gradetracker.web import queries

# Collection-time skip, so a machine without the browser still runs a green
# suite. A launch that fails for any other reason skips in the fixture below.
playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

# Imported for their fixtures, which pytest resolves by name in this module.
from tests.test_web import client, empty_db, fakes, seeded_db  # noqa: E402,F401
from tests.test_web import course_ids, text  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    """One Chromium for the whole module. Both tests only read and click."""
    with playwright_api.sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch()
        except playwright_api.Error as exc:
            pytest.skip(f"Chromium will not launch here: {exc}")
        try:
            yield launched
        finally:
            launched.close()


def _problem_sets_with_one_unknown(seeded_db, course_id):
    """ECON's Problem Sets: the marked work it already holds, plus one blank.

    Both kinds of row in one category is what the set-all bug needed, because
    the regrade rows render inside the same .category-group the button
    searches.
    """
    conn = db.connect(seeded_db)
    try:
        category = int(
            conn.execute(
                "SELECT id FROM categories WHERE course_id = ? AND name = 'Problem Sets'",
                (course_id,),
            ).fetchone()["id"]
        )
        unknown_id = queries.add_manual_assignment(
            conn, course_id, "Problem Set 9", None, 50.0,
            status="ungraded", category_id=category,
        )
        conn.commit()
        marked = [
            item
            for item in queries.graded_assignments(conn, course_id)
            if item["category_id"] == category
        ]
        assert marked, "the fixture's Problem Sets should hold marked work"
        return unknown_id, marked
    finally:
        conn.close()


def _page(browser, client, course_id, tmp_path):
    """The real what-if render, in a browser, with every fold opened."""
    body = text(client.get(f"/courses/{course_id}/whatif"))
    body = re.sub(r'<link rel="stylesheet"[^>]*>', "", body)
    body = re.sub(r"<script [^>]*htmx[^>]*></script>", "", body)
    path = tmp_path / "whatif.html"
    path.write_text(body, encoding="utf-8")

    page = browser.new_page()
    page.goto(path.as_uri())
    # Unfolded, exactly as clicking each summary would. The regrade rows are
    # inside a <details>, and Playwright will not read a hidden input.
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    return page


def _row(page, assignment_id):
    return page.locator(f'.slider-row:has(input[name="score_{assignment_id}"])')


def test_set_all_fills_the_unknowns_and_leaves_the_marks_alone(
    browser, client, seeded_db, tmp_path
):
    """"85% on what is left" must not quietly rewrite work already marked."""
    econ = course_ids(seeded_db)["ECON 166"]
    unknown_id, marked = _problem_sets_with_one_unknown(seeded_db, econ)

    page = _page(browser, client, econ, tmp_path)
    try:
        group = page.locator('.category-group:has-text("Problem Sets")').first
        group.locator(".set-all").fill("85")
        group.locator(".set-all-button").click()

        # 85% of the 50 points nobody has scored yet.
        assert _row(page, unknown_id).locator("input[type=number]").input_value() == "42.5"

        for item in marked:
            row = _row(page, item["id"])
            mark = row.locator(".reset-regrade").get_attribute("data-earned")
            # Blank is how a regrade row says "keep the real score".
            assert row.locator("input[type=number]").input_value() == ""
            assert row.locator("input[type=range]").input_value() == mark
    finally:
        page.close()


def test_reset_puts_the_slider_back_on_the_mark(browser, client, seeded_db, tmp_path):
    """Not the middle of the range, which is where an empty value sanitises."""
    econ = course_ids(seeded_db)["ECON 166"]
    _, marked = _problem_sets_with_one_unknown(seeded_db, econ)
    first = marked[0]

    page = _page(browser, client, econ, tmp_path)
    try:
        row = _row(page, first["id"])
        number = row.locator("input[type=number]")
        slider = row.locator("input[type=range]")
        mark = row.locator(".reset-regrade").get_attribute("data-earned")

        number.fill("15")
        number.dispatch_event("input")
        assert slider.input_value() == "15"

        row.locator(".reset-regrade").click()

        assert number.input_value() == ""
        assert slider.input_value() == mark
        assert mark != slider.get_attribute("max")
    finally:
        page.close()
