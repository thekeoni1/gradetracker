# Grade Tracker

Built against Hamilton College's Blackboard and Gradescope, by a student, for
personal use. It syncs your grades from both, applies each syllabus's grading
rules, and shows a live overall grade with what-if scenarios.

**Everything stays on your own machine.** There is no server, no account, and
nothing to sign up for. Nothing of yours leaves the laptop except the requests
this app makes to Blackboard and Gradescope themselves, as you. Your grades go
into a database in your own user folder; your Gradescope password goes into the
OS credential store; your Blackboard password never passes through this code at
all, because you type it into a real browser window.

It is one person's tool, published in case it is useful. No warranty, and
nobody is on call when Blackboard changes shape.

## Installing on Windows

Written for someone who has never opened a terminal. You need two free
programs, then six lines.

**1. Install Python.** Go to [python.org/downloads](https://www.python.org/downloads/)
and get Python 3.11 or newer. In the installer, **tick "Add python.exe to
PATH"** on the first screen before clicking Install. That tick is what lets the
commands below find Python; without it they fail with "python is not
recognized".

**2. Install Git.** Get it from [git-scm.com/download/win](https://git-scm.com/download/win)
and click through the installer with the defaults.

**3. Open a terminal.** Press the Windows key, type `powershell`, press Enter.
A blue window with a text prompt appears. You type a line and press Enter; it
does the thing and gives you the prompt back. That is the whole interface.

**4. Run these six lines**, one at a time, waiting for each to finish. The
first one picks where the app lives — your home folder is fine.

```
git clone https://github.com/thekeoni1/gradetracker.git
cd gradetracker
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m playwright install chromium
copy .env.example .env
```

The fourth line downloads the libraries and the fifth downloads a copy of
Chromium for the Blackboard login; together they take a few minutes and a few
hundred megabytes. They print a lot. As long as you get the prompt back without
the word `error`, it worked.

**5. Tell it where Blackboard is.** Open `.env` (`notepad .env`) and set

```
BB_BASE_URL=https://blackboard.hamilton.edu
```

Everything else in `.env` is optional. Save and close.

Those `.venv\Scripts\python.exe -m` prefixes are deliberate: they work in a
fresh window with nothing set up. The shorter `gt` form below needs the
environment activated first, which is one more thing to get wrong:

```
.venv\Scripts\activate
```

If that prints something about "running scripts is disabled on this system",
Windows is refusing to run the activation script. Either run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again, or
skip activating entirely and write `.venv\Scripts\gt.exe` everywhere this
README says `gt`.

## First run

In order, once. Each one is a line you type and press Enter.

1. **Set `BB_BASE_URL`** in `.env`, if you have not already.
2. `gt login blackboard` — opens a real Chromium window. Sign in through your
   school's SSO and approve Duo yourself; tick "Remember me" if it is offered.
   The window closes on its own once it detects you are in. Your password never
   passes through this code.
3. `gt login gradescope` — prompts for your email and password, checks them with
   one real login, then stores them in the OS credential store.
4. `gt sync` — fetches both platforms, saves the raw responses, and fills the
   database.
5. `gt link` — says which Blackboard course and which Gradescope course are the
   same course you take. `gt link --auto` accepts every suggestion. When the two
   platforms disagree about the code — Gradescope's "ECONOMICS 166" is
   Blackboard's "ECON 166" — decline the suggestion and type the code you
   already have, or do it later: `gt link --list` numbers every platform course,
   `gt link --set 7 "ECON 166"` attaches number 7 to that course, and
   `gt link --unlink 7` detaches it again. Courses you dropped stay unlinked.
6. `gt rules "ECON 166"` — type in the categories, weights, drop-lowest counts
   and letter cutoffs from the syllabus, then confirm them. Repeat per course.
   A category whose items count unequally takes rank weights, highest first:
   "the midterm you score highest on is worth 23% and the other 17%" is
   `23, 17`, and they must add up to the category's own weight.
   `--file` takes JSON instead; see `docs/rules-example.json`.
7. `gt map "ECON 166"` — put any remaining assignments into categories.
8. `gt serve` — opens the dashboard at http://127.0.0.1:5000 and keeps syncing
   every four hours while it runs.

Check `gt grade` at any point to see where things stand from the terminal.

If Blackboard redirects you to a different hostname, `gt login blackboard` says
so and refuses, rather than saving a login whose cookies belong to the other
host. Set `BB_BASE_URL` to the host it names and log in again.

## At another school

Set `BB_BASE_URL` to that school's Blackboard host and the rest should follow:
nothing above is Hamilton-specific by design, and the SSO step works by handing
you a real browser, so whatever your school puts in front of Blackboard is
between you and that window.

Two things to expect. The public REST paths this app reads are overridable with
the `BB_PATH_*` settings in `.env`, because a school can serve grades from
somewhere else. And course-code parsing has only ever been seen against
Hamilton's `DEPT_123_01_FA2026` form — a school numbering its courses
differently may get courses it has to link by hand with `gt link --set`, which
works fine, just less automatically.

## On a Mac

The app runs. Logins, sync, link, rules, map, serve and grade all work, with
`.venv/bin/python -m` and `.venv/bin/gt` in place of the Windows paths, and
`source .venv/bin/activate` to activate. Four differences, all of them things
nobody has needed yet rather than things that are broken:

- **Where data lives** is `~/.local/share/gradetracker` instead of
  `%LOCALAPPDATA%\GradeTracker`. Same contents.
- **The Gradescope password** goes into the macOS Keychain, which is what
  `keyring` uses there. That part is as good as on Windows.
- **`gt schedule install` does not install anything.** It prints the launchd
  plist you would need and exits. `gt serve`'s own four-hourly sync works
  normally, so the gap is only "sync while the app is closed".
- **Notifications print to the terminal** instead of raising a desktop
  notification: a `[notify] ...` line on stderr, which you will only see if you
  are looking at the window `gt serve` is running in.

And one real gap worth knowing before you put your grades on a Mac: **the
Blackboard session cookies are stored unencrypted.** On Windows they are
encrypted with DPAPI, which ties the file to your Windows login. There is no
equivalent backend written for macOS, so the file is written plaintext with
mode `0600` — other accounts on the machine cannot read it, but anything running
as you can, and so can a backup that copies it off the machine. The proper fix
is a Keychain-backed key, the same way the Gradescope password already works,
and it is deferred until someone actually runs this on a Mac. These are live
session tokens that let the holder read your Blackboard as you, for about six
hours.

## Commands

| Command | What it does |
| --- | --- |
| `gt login blackboard\|gradescope` | Authenticate a source |
| `gt status` | Whether each saved session still works |
| `gt sync [--source S] [--raw-only]` | Fetch, dump raw, normalise into the database |
| `gt link [--auto] [--list] [--set ID COURSE] [--unlink ID]` | Attach platform courses to the course you take |
| `gt rules COURSE [--file F] [--show]` | Enter or review a course's grading rules |
| `gt map COURSE [--auto] [--set ID NAME]` | Put assignments into categories |
| `gt grade [COURSE]` | Current and projected grade, with warnings |
| `gt exclude ID [--undo]` | Stop counting one assignment (e.g. a duplicate copy) |
| `gt prefer COURSE gradescope\|blackboard\|none` | Which platform wins when both carry the same work |
| `gt serve [--port N] [--tailscale] [--host A] [--no-browser] [--no-scheduler]` | Run the dashboard |
| `gt schedule install\|uninstall\|status` | Sync on a schedule even when the app is closed (Windows) |

Pick **one** of the two schedulers, not both: `gt serve` already syncs every
four hours while it is open, and `gt schedule install` registers an OS task that
does the same when it is not. Running both is safe -- a lock file in the data
directory means only one sync runs at a time, and the loser exits quietly -- but
it is twice the requests for no benefit. Use the OS task if you want syncing
without leaving the dashboard open, and `gt serve --no-scheduler` if you want
both the task and the dashboard.

The scheduled task runs a console command, so expect a console window to flash
briefly every four hours. That is cosmetic and left as-is for now.

## The dashboard

`gt serve` binds to 127.0.0.1 only and has no login, so it is reachable from
your machine and nowhere else. Screens: every course with its current and
projected grade, a course page with category bars and per-assignment controls,
what-if sliders, "what do I need on the final", the rules form, assignment
mapping, connections, and course links. The mapping screen sets every row in
one submit, with an "apply to all rows" helper for a course whose nineteen
codelets all belong in the same category. Both the what-if and the what-do-I-
need screens answer per category as well as per assignment: what a whole
section needs, and what is still blank in it. Each category row there assumes
only that category's items are unknown, so the rows read one at a time rather
than adding up.

A category with nothing to guess at -- the final neither platform has posted
yet, or a section where everything is already marked -- is still on the what-if
screen rather than missing from it, as a closed block saying which it is. Open
it and you can add a placeholder: a name, a point value, and it gets a slider
like everything else, so the 25% final can be planned for before anyone posts
it. A category that already has sliders offers the same thing, quietly, for
the second midterm nobody has posted yet. Placeholders are ordinary by-hand
items. Sync leaves them alone, the
projection fills them with that category's average until you type a score, and
you can remove them from the what-if screen or from the course page. A point
value above zero is required, because an item worth nothing out of nothing is
invisible to both the sliders and the projection.

Every grade carries where it came from: the rule origin and confirmation date,
the last sync per source, a warning if anything is unmapped or unconfirmed, and
a flag when the same assignment appears on both platforms. A confident-looking
wrong number is the worst failure here, so those signals sit next to the number
rather than in a log. A mirrored assignment is matched even when one platform
adds a word to the name, as Gradescope's "Quiz 1 ALL" does to Blackboard's
"Quiz 1", and the course page offers to keep one platform and exclude the
other, pair by pair or all at once.

For a course where every assignment is mirrored, resolving the pairs one by
one is a chore that never ends, so a course fed by both platforms keeps
Gradescope by default. That default is set the moment you link a course to its
second platform -- which is when `gt link` says what it now means, and when
any copies already mirrored are set aside, rather than four hours later; change it
on the course page ("When both platforms carry the same assignment keep: ...")
or with `gt prefer MATH 116 none`. With a platform set, each sync excludes the
other platform's copy as it arrives, prints a line saying how many it
excluded, marks those rows `auto` so you can see which exclusions were yours
and which were the rule's, and lists each one under Recent changes on the
dashboard. The rule only ever excludes, never counts something again, and it
leaves alone any row you have excluded or included yourself -- so an "include"
click survives every later sync. Pairs it cannot settle, such as the same name
twice within one platform, still appear on the duplicates card.
`gt prefer MATH 116 gradescope` does the same thing from the terminal, and
`gt prefer MATH 116 none` goes back to being asked (rows already excluded stay
excluded).

## Your grades on your phone

The dashboard already works at phone width, so reading it on a phone is a
networking question rather than a design one. `gt serve` listens on 127.0.0.1
by default, which is why only this machine can reach it.

`gt serve --tailscale` listens on this machine's Tailscale address instead, so
any device signed into your own tailnet can open it and nothing else can. Install
[Tailscale](https://tailscale.com/download) on the laptop and the phone, sign
both into the same account, then:

```
gt serve --tailscale
```

It prints the address to open on the phone, e.g. `http://100.x.y.z:5000/`.
That same address works in the laptop's own browser. Add the page to the home
screen and it behaves like an app.

Three things to know.

**The dashboard has no login.** On your own tailnet that is fine, because only
your own devices are on it. It is also why `--host` is fussy: it takes a
loopback address, or an address `tailscale ip` lists for this machine, and
refuses everything else including a wildcard. A LAN address would hand every
grade to whoever else is on that wifi, and an address merely *looking* like
Tailscale's is not enough -- a phone hotspot hands out addresses from the same
range on the wifi interface, and only the daemon's own list tells those apart.

**Anyone on the same tailnet can read it.** That is your own devices by
default, but a tailnet you share -- a family account, or a node someone invited
you to -- means those people can open the dashboard too, grades and all. If
your tailnet is not just you, do not leave this running.

**The laptop has to be awake.** This serves from your machine: a closed lid
means no dashboard. Nothing is hosted anywhere else. A friend who wants the
same thing installs their own copy and runs their own `gt login` -- Tailscale
only ever connects one person's own devices. Traffic between them is encrypted
by Tailscale itself, so plain HTTP here is not sent in the clear.

Two people never share one dashboard. It has no login, so there is nothing to
tell them apart and no way to show one of them only their own grades: a friend
does all of this on their own tailnet, from their own install.

## Where data lives

Everything is under `%LOCALAPPDATA%\GradeTracker` (on a Mac or Linux,
`~/.local/share/gradetracker`), deliberately **not** in this folder, because the
project may sit in OneDrive and that would sync session tokens to the cloud and
lock the browser profile's SQLite mid-sync. Override with `GT_DATA_DIR`.

```
%LOCALAPPDATA%\GradeTracker\
  grades.db                       the database
  raw\<source>\<timestamp>.json   the last 30 untouched responses per source
  profiles\blackboard\            the Chromium profile from gt login
  profiles\blackboard.cookies.bin the session cookies, DPAPI-encrypted
  syllabi\                        (unused until syllabus upload exists)
```

The cookie snapshot is encrypted with Windows DPAPI, so a copy of the file is
useless on another machine or under another login. It exists because Chromium
discards session cookies when a persistent profile closes, and Blackboard's auth
cookies are session cookies: they are captured while the login window is still
open, which is what lets a later sync run with no browser at all. On anything
other than Windows there is no DPAPI and the file is plaintext at mode 0600; see
**On a Mac** above.

The Gradescope password is in the Windows Credential Manager (or the macOS
Keychain) under the service `gradetracker`, never in a file or the database.

One wrinkle worth knowing: `gradescopeapi` 1.8.1 sends the login POST with the
credentials in the URL query string, which means a network error during login
would otherwise put the password into an error message, and from there into the
database and the dashboard. The login call is patched to send them in the
request body instead, and every message that gets stored or displayed is run
through a redaction pass as a second line of defence.

## Notifications

New grades and expired sessions raise a Windows toast. An expired session
notifies once per expiry rather than once per failed sync. `GT_NOTIFY=0` turns
all of it off. The first ever sync of a source records every assignment as new
but stays silent, so you do not get thirty toasts on day one. Off Windows the
same notifications print to stderr instead.

## What is deferred

- **OAuth (M9).** Blackboard access is currently a saved browser session, which
  expires and needs reconnecting. The Connections page tracks how long each
  login lasts; those numbers are the evidence for asking Hamilton IT to register
  this as a REST integration. `auth/oauth_auth.py` is a stub, and the
  `AuthProvider` seam means swapping it in touches no connector, engine or UI
  code.
- **Syllabus extraction.** Grading rules are typed in by hand and confirmed.
  Fetching the syllabus off Blackboard and reading it with Claude is not built;
  `syllabus/extract.py` is a stub explaining why.
- **Soak testing.** The scheduler, reconnect flow and change detection are
  built and unit-tested but have not run for a week against real data.
- **A Keychain secretbox and a launchd scheduler**, which are what a Mac
  install is missing; see **On a Mac**.

## Development

```
.venv\Scripts\python.exe -m pytest        # the whole suite
.venv\Scripts\python.exe -m pytest -q tests/test_engine.py
```

The grade engine in `gradetracker/engine/` is pure: no database, network or UI
imports, so the maths is testable on its own and the dashboard and the CLI share
one implementation. `storage/load.py` is the only module that knows both SQL and
the engine's dataclasses.

Fixtures under `tests/fixtures/` are synthetic but realistically shaped. They
are not from real recon, so the Blackboard endpoint paths are configurable via
`BB_PATH_*` in `.env` and may need swapping once the real API shape is known.

## Licence

MIT; see [LICENSE](LICENSE).
