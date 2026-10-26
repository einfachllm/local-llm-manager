# Contributing to Local LLM Manager

Thanks for your interest in improving Local LLM Manager! Bug reports, fixes, new framework presets,
docs and ideas are all welcome.

By participating you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md).
Security issues go through [SECURITY.md](SECURITY.md), **not** public issues.

## Before you start

- **Bugs:** search [existing issues](https://github.com/einfachllm/local-llm-manager/issues) first, then
  open one with the *Bug report* template. Include your platform (DGX Spark / Linux / macOS), how you run
  the manager (systemd, Docker Compose, `run.sh`) and the relevant recipe (remove secrets!).
- **Features / larger changes:** open an issue to discuss the idea before writing a lot of code, so we can
  agree on the approach. Small fixes and docs changes can go straight to a pull request.

## Development setup

Requirements: Python 3.10+, bash. Docker is *not* needed for the tests (they use a fake docker client).

```bash
git clone https://github.com/einfachllm/local-llm-manager.git
cd local-llm-manager
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt ruff

pytest                                         # run the tests
ruff check .                                   # lint
DGXM_DB=/tmp/dev.db python -m local_llm_manager.main   # run locally on http://localhost:8090
```

### Code map

| Module | What it does |
|---|---|
| `main.py` | FastAPI app: REST API, auth, serves the UI |
| `recipes.py` | Recipe → `docker run` / process command, framework presets |
| `manager.py` | Lifecycle rules: start/stop/switch, port and memory checks |
| `executor.py` | Where things run: this machine or a remote agent (`agent.py` is the agent API) |
| `docker.py`, `processes.py` | Docker CLI wrapper, native processes |
| `importer.py`, `script_check.py` | Script import and its static safety check |
| `scheduler.py`, `timeline.py` | Schedules and the planned-runs timeline |
| `servermetrics.py`, `logstats.py` | Live stats from `/metrics` or the vLLM log line |
| `db.py` | SQLite storage |
| `static/` | The web UI: plain HTML/CSS/JS with no build step - edit and reload |

Tests use a fake docker client (`tests/conftest.py`), so they run anywhere.

### CI

[`ci.yml`](.github/workflows/ci.yml) runs on pushes to `main` and on pull requests: Ruff, ShellCheck,
the tests on Python 3.10-3.13 (and macOS) and a Python package build. It does not build the Docker image;
[`docker.yml`](.github/workflows/docker.yml) does that only when started by hand and publishes `:latest`.

## Pull requests

1. Fork the repository and create a branch from `main` (e.g. `fix/port-check`, `feat/tgi-preset`).
2. Keep the change focused: one fix or feature per pull request.
3. Add or update tests in `tests/` for behaviour changes; bug fixes should come with a test that fails
   without the fix.
4. Update `README.md` when you change user-visible behaviour, configuration or the API, and add a line
   under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
5. Make sure `pytest` and `ruff check .` pass. CI runs them on Python 3.10-3.13 and macOS, runs
   ShellCheck on the install scripts and checks that the Python package builds.
6. Fill in the pull request template. A maintainer will review; please respond to feedback by pushing
   new commits (no need to force-push).

### Code style

- Follow the style of the surrounding code: small modules, type hints where they help, short docstrings
  and comments that explain *why*. Line length is 120.
- Keep dependencies minimal. New runtime dependencies need a good reason - mention it in the PR.
- No frontend build tooling: the UI must keep working as static files.
- Anything that executes commands (docker, native processes, script import) is security-sensitive:
  never pass user input through a shell, and explain the safety reasoning in the PR.

### Commit messages

Write a short summary line in the imperative mood ("Add TGI preset", "Fix port check for IPv6"),
optionally followed by a blank line and a body explaining what and why.

### Developer Certificate of Origin

By contributing you certify that you wrote the contribution or otherwise have the right to submit it
under the project's license (see the [DCO](https://developercertificate.org/)). Please sign off your
commits with `git commit -s`, which adds a `Signed-off-by:` line.

## License

Local LLM Manager is licensed under the [Apache License 2.0](LICENSE). Unless you explicitly state
otherwise, any contribution you intentionally submit for inclusion is licensed under the same terms,
as described in section 5 of the license.

## Releases (maintainers)

1. Move the `## [Unreleased]` entries in `CHANGELOG.md` to a new `## [X.Y.Z] - YYYY-MM-DD` section.
2. Set `__version__ = "X.Y.Z"` in `local_llm_manager/__init__.py`; merge this into `main`.
3. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.

The [release workflow](.github/workflows/release.yml) checks that the tag matches `__version__`, runs
the tests, publishes `ghcr.io/einfachllm/local-llm-manager:X.Y.Z` and `:X.Y`, and creates a
GitHub Release with the wheel, the sdist and the changelog section. Tags like `vX.Y.Z-rc.1` become
pre-releases and do not move `:X.Y`. To update `:latest`, run the *Docker latest* workflow on `main`
by hand (*Actions → Docker latest → Run workflow*).
