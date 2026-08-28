# Development Guide

## Prerequisites

Orbital Signal requires:

- Python 3.12 or newer
- `uv`
- Git
- GitHub CLI for pull-request and release commands
- Network access for live USAspending ingestion

Verify the tools:

```bash
python3 --version
uv --version
git --version
gh --version
```

## Clone the repository

```bash
cd ~/dev/hermes-lab/apps

git clone https://github.com/J-Varela/orbital-signal.git

cd orbital-signal
```

If the repository already exists:

```bash
cd ~/dev/hermes-lab/apps/orbital-signal

git switch main
git pull --ff-only
```

## Install the environment

Run:

```bash
uv sync
```

`uv sync` automatically creates `.venv` when needed and installs the project,
runtime dependencies, development dependencies, and locked dependency versions.

Manual virtual-environment activation is optional. Project commands should
normally be run through `uv run`.

Examples:

```bash
uv run pytest
uv run ruff check .
uv run orbital-signal
```

## Configuration

Copy the example environment file only when local overrides are needed:

```bash
cp .env.example .env
```

Available settings:

```dotenv
ORBITAL_SIGNAL_USASPENDING_BASE_URL=https://api.usaspending.gov
ORBITAL_SIGNAL_HTTP_TIMEOUT_SECONDS=30
```

The `.env` file is ignored by Git and must not contain committed secrets.

USAspending currently requires no API key.

## Run the application

Start the development server with the installed command:

```bash
uv run orbital-signal
```

Or invoke Uvicorn directly:

```bash
uv run uvicorn orbital_signal.api:app --reload
```

The API is available at:

```text
http://127.0.0.1:8000
```

Interactive API documentation:

```text
http://127.0.0.1:8000/docs
```

Verify the health endpoint:

```bash
curl -s http://127.0.0.1:8000/health \
  | python3 -m json.tool
```

Expected shape:

```json
{
  "status": "ok",
  "version": "0.1.0-alpha.2"
}
```

## Live ingestion

Keep the API running in one terminal. In another terminal:

```bash
curl -s -X POST \
  "http://127.0.0.1:8000/api/v1/ingestions/usaspending?start_date=2026-01-01&end_date=2026-08-25" \
  | python3 -m json.tool
```

List all retained signals:

```bash
curl -s \
  "http://127.0.0.1:8000/api/v1/signals?minimum_score=4&limit=25" \
  | python3 -m json.tool
```

List only startup candidates:

```bash
curl -s \
  "http://127.0.0.1:8000/api/v1/signals?minimum_score=4&limit=25&startup_candidates_only=true" \
  | python3 -m json.tool
```

Live ingestion uses an external public service and may return different results
over time.

## Quality gates

Every change must pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

A successful result should include:

```text
All checks passed!
```

and a fully passing pytest run.

The current FastAPI test client may emit an upstream `httpx2` deprecation
warning. A warning does not fail the test suite, but it remains tracked as
technical debt.

## Focused testing

During development, run the smallest relevant test file first.

Examples:

```bash
uv run pytest tests/test_relevance.py -vv
uv run pytest tests/test_quality.py -vv
uv run pytest tests/test_usaspending.py -vv
uv run pytest tests/test_services.py -vv
uv run pytest tests/test_api.py -vv
```

Before committing, always run the complete quality-gate sequence.

## Formatting and lint fixes

Format Python files:

```bash
uv run ruff format .
```

Apply safe automatic lint fixes:

```bash
uv run ruff check . --fix
```

Then inspect the changes and rerun:

```bash
git diff
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Automatic fixes should still be reviewed before committing.

## Branch workflow

Development uses short-lived branches and pull requests.

Start from an updated `main`:

```bash
git switch main
git pull --ff-only
git status
```

The working tree should be clean before creating a branch.

Create a focused branch:

```bash
git switch -c <type>/<short-description>
```

Recommended prefixes:

| Prefix | Purpose |
| --- | --- |
| `feat/` | New product capability |
| `fix/` | Bug correction |
| `docs/` | Documentation |
| `test/` | Test-only improvement |
| `refactor/` | Internal restructuring |
| `chore/` | Dependencies or maintenance |

Examples:

```text
feat/postgres-persistence
fix/award-date-semantics
docs/project-foundation
test/usaspending-pagination
chore/dependency-refresh
```

Avoid combining unrelated changes in one branch.

## Inspect changes

Before staging:

```bash
git status --short
git diff
git diff --check
```

`git diff --check` detects whitespace errors.

For untracked files, inspect them directly before staging:

```bash
sed -n '1,240p' path/to/file
```

## Stage and review

Stage only the intended files:

```bash
git add <file-or-directory>
```

Review the staged result:

```bash
git status --short
git diff --cached --stat
git diff --cached
```

Do not commit `.env`, `.venv`, caches, credentials, generated secrets, or local
editor state.

## Commit messages

Use concise conventional commit messages:

```text
<type>: <imperative summary>
```

Examples:

```text
feat: add durable signal persistence
fix: preserve award start date semantics
docs: document project architecture and workflow
test: cover bounded pagination behavior
chore: upgrade pytest to 9.1.1
```

Create the commit:

```bash
git commit -m "docs: document project architecture and workflow"
```

Verify it:

```bash
git log -1 --oneline --decorate
git status
```

## Push the branch

```bash
git push -u origin <branch-name>
```

For the current documentation branch:

```bash
git push -u origin docs/project-foundation
```

The `-u` option establishes the upstream branch, so later pushes can use:

```bash
git push
```

## Create a pull request

Create a pull request from the terminal:

```bash
gh pr create \
  --base main \
  --head <branch-name> \
  --title "<pull-request title>" \
  --body-file - <<'EOF'
## Summary

- describe the first material change
- describe the second material change
- state important boundaries or limitations

## Verification

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run pytest`

## Follow-up

- list work intentionally left for another pull request
EOF
```

A pull request should explain what changed, how it was verified, and what remains
outside its scope.

## Inspect a pull request

View the pull request in the terminal:

```bash
gh pr view
gh pr diff
gh pr checks
```

Attempting to open a graphical browser from WSL may fail:

```bash
gh pr view --web
```

That browser error does not affect the pull request. Display its URL instead:

```bash
gh pr view --json url --jq .url
```

The URL can then be opened from Windows or another browser.

## Merge a pull request

After reviewing the diff and confirming checks:

```bash
gh pr merge <number> --squash --delete-branch
```

Synchronize the local repository:

```bash
git switch main
git pull --ff-only
git status
git log --oneline --decorate -5
```

Squash merging keeps `main` focused while the pull request preserves the
development discussion and individual branch history.

## Dependency changes

Inspect outdated packages without modifying the environment:

```bash
uv pip list --outdated
```

Add or update a direct development dependency through `uv`:

```bash
uv add --dev "pytest>=9.1.1,<10.0.0"
```

Update the lockfile when intended:

```bash
uv lock --upgrade
uv sync
```

Then run all quality gates:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Do not directly force-upgrade transitive dependencies such as `pydantic-core`.
The top-level package constraint should determine compatible transitive
versions.

Dependency updates should normally use their own branch and pull request.

## Version locations

Orbital Signal currently stores its version in two files:

`pyproject.toml` uses Python's normalized pre-release format:

```toml
version = "0.1.0a2"
```

`src/orbital_signal/__init__.py` uses the user-facing release format:

```python
__version__ = "0.1.0-alpha.2"
```

Both locations must be updated together.

For Alpha 3, the values would be:

```toml
version = "0.1.0a3"
```

```python
__version__ = "0.1.0-alpha.3"
```

The Git tag would be:

```text
v0.1.0-alpha.3
```

A future refactor may establish one version source to eliminate this duplication.

## Changelog workflow

During normal development, add user-visible changes beneath:

```markdown
## [Unreleased]
```

At release time:

1. Create a dated section for the new version.
2. Move relevant entries out of `Unreleased`.
3. Leave a fresh empty `Unreleased` section.
4. Update comparison links at the bottom of `CHANGELOG.md`.
5. Confirm every statement matches released behavior.

The changelog should not describe planned work as if it is already implemented.

## Release workflow

Releases are created only from an updated, tested `main` branch.

### 1. Prepare a release branch

```bash
git switch main
git pull --ff-only
git switch -c chore/release-0.1.0-alpha.3
```

### 2. Update release metadata

Update:

- `pyproject.toml`
- `src/orbital_signal/__init__.py`
- `CHANGELOG.md`
- Documentation containing the displayed version, when applicable

Refresh the local package:

```bash
uv sync
```

Verify both versions:

```bash
uv run python - <<'PY'
from importlib.metadata import version

import orbital_signal

print("package:", version("orbital-signal"))
print("runtime:", orbital_signal.__version__)
PY
```

### 3. Run quality gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

### 4. Commit and push

```bash
git add \
  pyproject.toml \
  uv.lock \
  src/orbital_signal/__init__.py \
  CHANGELOG.md

git commit -m "chore: prepare v0.1.0-alpha.3"

git push -u origin chore/release-0.1.0-alpha.3
```

Include additional documentation files in `git add` if they changed.

### 5. Create and merge the release pull request

```bash
gh pr create \
  --base main \
  --head chore/release-0.1.0-alpha.3 \
  --title "chore: prepare v0.1.0-alpha.3" \
  --body "Prepare and verify the Alpha 3 release metadata."
```

After review:

```bash
gh pr merge --squash --delete-branch
```

### 6. Tag the merged commit

```bash
git switch main
git pull --ff-only
git status
```

Confirm the working tree is clean, then create the annotated tag:

```bash
git tag -a v0.1.0-alpha.3 \
  -m "Add durable Orbital Signal persistence"
```

Push the tag:

```bash
git push origin v0.1.0-alpha.3
```

Verify the remote tag:

```bash
git ls-remote --tags origin v0.1.0-alpha.3
```

Tags are immutable release markers and should not be moved after publication.

### 7. Create the GitHub release

```bash
gh release create v0.1.0-alpha.3 \
  --title "Orbital Signal v0.1.0-alpha.3" \
  --generate-notes
```

Inspect it:

```bash
gh release view v0.1.0-alpha.3
```

## Release checklist

Before publishing a release, confirm:

- The release commit is on `main`.
- The working tree is clean.
- Package and runtime versions agree.
- Ruff lint passes.
- Ruff formatting passes.
- The full test suite passes.
- The changelog is dated and accurate.
- Documentation reflects current behavior.
- No secrets or local environment files are tracked.
- The annotated tag points to the intended commit.
- The tag exists on GitHub.
- Release notes identify important limitations.

## Current development priorities

The planned order after Alpha 2 is:

1. PostgreSQL persistence.
2. Alembic migrations.
3. Durable ingestion runs.
4. Company identity and aliases.
5. Idempotent database upserts.
6. Clear award-date semantics.
7. Additional public data sources.
8. Scheduled ingestion and monitoring.

Each milestone should remain independently testable and should be delivered
through a focused pull request.