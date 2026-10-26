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
- The Schedules tab shows the hub's time zone (also `timezone` in `GET /api/meta`); Docker Compose
  mounts the host's `/etc/localtime` so schedules no longer run in UTC inside the container.
- `pyproject.toml`, so the manager can be installed with `pip install .` (command `local-llm-manager`).

### Security
- `DGXM_HOST=tailscale` / `DGXM_PUBLISH_IP=tailscale`: the manager and the model ports listen only on the
  machine's Tailscale IP (docker `-p <ip>:port`, `{bind_host}` placeholder for native and host-network
  recipes; presets use it). README recommends Tailscale, also for phone access.
- Cross-site request forgery: another website could make a logged-in browser start, stop, switch or
  free memory (bodyless `POST`s). State-changing requests from a foreign `Origin` are now rejected.
- Without a password, only IP addresses, `localhost` and local/Tailscale names are accepted as `Host`
  (DNS rebinding).
- Security headers against clickjacking (`X-Frame-Options`, `frame-ancestors`) and MIME sniffing.
- Password policy: the hub refuses passwords under 12 characters, with fewer than 4 different characters
  or well-known ones (`change-me`, `admin`, …); agents refuse tokens under 32 characters.
- Failed logins are rate-limited per address (10 per 10 minutes, then 429) and logged.
- The installers generate a random dashboard password and agent token instead of `change-me`.
- The database (agent tokens) is created owner-only (`600`, folder `700`); the manager runs with umask `077`.
- A non-ASCII username or password in Basic auth caused a 500 error instead of 401.
- Host names and agent URLs were inserted into the page as HTML without escaping (XSS).
- The Blur toggle showed the first and last characters of host names; it now shows aliases like
  `host-3f2a`, and also covers the host dropdowns, port hints and host error messages.

### Fixed
- Screenshots replaced with a demo setup; example IP/host name in code comments replaced.
- Script import looks for bash 4 or newer (also in `/opt/homebrew/bin`, `/usr/local/bin`) and says
  "run `brew install bash`" if there is none, instead of failing with "command not found" on macOS.

### Changed
- `install.sh` restarts the service, so rerunning it after `git pull` upgrades a running install.
- Shorter README focused on setup, networking between machines, schedules and security; developer
  details moved to CONTRIBUTING.md.
- CI (pushes and pull requests) no longer builds the Docker image. `:latest` is published only by a
  manual run of the new `docker.yml` workflow; release tags publish `:X.Y.Z` and `:X.Y`.

## [0.1.0]

### Added
- Initial version: recipe management for Docker and native-process model servers, start/stop/switch,
  live logs and server metrics, cron and one-shot schedules with a timeline, script import with safety
  check, hub/agent mode for several machines.
