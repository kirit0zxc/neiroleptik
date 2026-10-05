# CI and pull requests

CI runs on pull requests into `main`, on pushes to `main`, and manually from Actions.

## Current state

The frontend (`frontend/`) exists and its job runs typecheck, tests and build.
The backend exists: Django checks, migration checks and tests run against
PostgreSQL 16. TTS tests use small WAV fixtures and mock model loading;
large voice models are not downloaded by normal PR CI.
CI result requires Repository checks and every detected component to succeed.
Unknown statuses (including abandoned) fail. Skipped is allowed only for a
component that is absent. The result policy has regression tests.

## Expected project layout

- `frontend/`: React + TypeScript, Node.js 24, npm.
- `backend/`: Django, Python 3.12, PostgreSQL 16 in CI; ML dependencies as needed.

Frontend CI starts when `frontend/package.json` exists. Commit
`frontend/package-lock.json` and provide these npm scripts:

- `typecheck`: check TypeScript without emitting files.
- `test:ci`: run frontend tests once, without watch mode.
- `build`: produce the frontend build.

Backend CI starts when `backend/manage.py` exists. Commit
`backend/requirements.txt` with the app's dependencies. CI installs them, checks
Django configuration, checks for missing migrations, and runs Django tests.
Configure Django to read `DJANGO_SECRET_KEY` and either `DATABASE_URL` or the
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, and
`POSTGRES_PORT` environment variables supplied by the workflow. The database is
disposable and contains no production data. Django tests need permission to create
their own test database (the CI PostgreSQL user has it).

ML checks run on CPU. Add focused tests and small test fixtures; full training and
large model/dataset downloads should use a separate workflow.

The API contract is specs/openapi.json. Backend tests check request boundaries,
CSRF and playable WAV responses. Frontend tests check the ServerTTS transport,
audio lifecycle and cancellation. A real browser with the installed Piper model
must additionally verify audible playback; that is not replaced by these tests.

## Review and merge

Open a small PR into `main`, wait for `CI result`, and ask `@kirit0zxc` to review it.
Changes to shared API formats must be agreed with the other developer before
implementation.

`.github/CODEOWNERS` assigns all files to `@kirit0zxc`. On the current private
GitHub Free repository, CODEOWNERS and branch protection do not enforce approval.
The rule for `main` is prepared for a supported paid plan (GitHub Pro for a personal
repository; GitHub Team for an organization):

- require a PR and one approval;
- require code-owner review after upgrading (the checkbox is disabled on Free
  and must be enabled separately);
- dismiss stale approvals after changes;
- require successful `CI result` and an up-to-date branch;
- require conversation resolution;
- disallow force pushes and branch deletion.

Other collaborators can leave reviews, but with code-owner enforcement enabled
their approvals do not replace the owner's approval. A PR author cannot approve
their own PR. Admin bypass remains available to the repository owner for their own
PRs; this means the owner can merge without an approval. Everyone should still
check the latest CI result before merging. On Free all merge rules are a team
agreement, not a technical block.
