# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Open-source project files: contribution guide, code of conduct, security policy, issue and pull
  request templates, Dependabot configuration.
- Release workflow: tagging `vX.Y.Z` publishes versioned multi-arch Docker images and a GitHub Release
  with the Python wheel and sdist.
- CI: Ruff and ShellCheck lint, tests on Python 3.10-3.13 and macOS, Python package build check.
- `pyproject.toml`, so the manager can be installed with `pip install .` (command `local-llm-manager`).

### Changed
- Pushes to `main` publish the Docker image as `:edge`; `:latest` now points to the latest release.

## [0.1.0]

### Added
- Initial version: recipe management for Docker and native-process model servers, start/stop/switch,
  live logs and server metrics, cron and one-shot schedules with a timeline, script import with safety
  check, hub/agent mode for several machines.
