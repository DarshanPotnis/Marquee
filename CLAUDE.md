# CLAUDE.md — Working agreement for Marquee

Read `docs/PLAN.md` before doing anything. It is the source of truth for scope, architecture, the data model and the phases. Work on **one phase at a time** and don't start the next phase until I say so.

## How we work

1. **Plan before code.** At the start of every phase, explain the plan in plain English (short, with an analogy where it helps). List the files you'll create or change and the tests you'll write. Then wait for my OK.
2. **Brute force first, then optimize.** Ship the simplest correct version, show with real numbers where it breaks, then improve it.
3. **Tests first for logic.** Windows, transform, client and checks get tests before implementation. A test only counts once it has been seen failing without the fix.
4. **Verify against real data. Never invent API fields.** Check every field name against `docs/api-notes.md` and the real sample in `local/`. If you're not sure a field exists, say so and check. Don't guess.
5. **"Done" has a definition:** `ruff check` is clean, `mypy` (strict) passes, `pytest` is green, and the phase's done check from PLAN.md has been run for real, with the command output shown. Never claim a phase is done otherwise.
6. **Report surprises with numbers.** When real data does something unexpected (a cap, a missing field, an odd status), stop, show the numbers, and add a line to `docs/STATUS.md`.
7. **Small commits:** one per logical step, with conventional prefixes (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
8. **Prefer new files over large edits to existing ones.** If a change to an existing file is big, explain why first.

## Code standards

- Python 3.12, type hints everywhere, `dataclasses` (frozen where possible) for rows and settings.
- **Pure core, I/O at the edges.** `windows.py`, `transform.py`, `changes.py` and the check rules have no network or database calls. Only `tm_client.py`, `db.py`, `load.py` and `ingest.py` touch the outside world.
- **SQL:** schema lives in `migrations/`. Queries are always parameterized. Never format API values into SQL strings.
- **Errors:** fail loudly with context. No bare `except`. Retries happen only where PLAN.md §6 says.
- **Logging:** standard `logging`, with one summary line per run (windows, calls, reported, fetched, unique, status). **Never log the API key** or any URL that contains it.
- **Config** comes from environment variables (`.env` locally, repository secrets in CI). Database URLs use Marquee's own names, `MARQUEE_DATABASE_URL` and `MARQUEE_TEST_DATABASE_URL`; the generic `DATABASE_URL` is never read. A Marquee variable set differently in the shell and in `.env` is refused. `.env` and `local/` are gitignored.
- **Dependencies:** only `httpx`, `psycopg[binary]`, `python-dotenv` and `streamlit`, plus `pytest`, `ruff` and `mypy` for development. Anything else needs a reason and my approval.
- **Tests:** unit tests use no network (`httpx.MockTransport`, an injected clock and sleep). Integration tests use `MARQUEE_TEST_DATABASE_URL` (local Docker Postgres or a Neon dev branch) and skip cleanly if it isn't set.
- Keep functions small and named for what they do. Comments explain **why**, not what.

## Data and the provider's terms

- Official Ticketmaster API only. **No scraping.**
- Raw data is kept only for `RAW_RETENTION_DAYS` (pruned every run). See PLAN.md §8.
- **Never commit real API data.** Test fixtures are synthetic: the same shape as a real response, with invented values.
- The API key never appears in saved params, logs, exceptions, test snapshots or commits.

## Communication style

- Plain English. Define any jargon in one line the first time you use it.
- Lead with the result, then the detail.
- For major decisions, write a record in `docs/decisions/NNN-title.md`: the problem, the options, the choice, why, and what would change my mind.
