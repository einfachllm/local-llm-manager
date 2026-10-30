# Security Policy

## Supported versions

Security fixes are made on `main` and shipped in the next release. Only the latest release is supported.

## Reporting a vulnerability

**Please do not report security vulnerabilities in public issues, discussions or pull requests.**

Report them privately through GitHub's
[private vulnerability reporting](https://github.com/einfachllm/local-llm-manager/security/advisories/new)
(*Security → Report a vulnerability*). Please include:

- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- the affected version / commit and how the manager is deployed (hub, agent, Docker, systemd, launchd).

We aim to acknowledge reports within 7 days and to agree on a disclosure timeline with you. We are happy
to credit you in the advisory unless you prefer to stay anonymous.

## Threat model - please read before deploying

Local LLM Manager is built to **run arbitrary containers and processes** on the machines it manages:

- Anyone who can log in to the dashboard can start any Docker container on the hub and its agents,
  which is effectively **root** on those machines.
- The agent token grants the same level of control over an agent machine.
- Hub ↔ agent traffic is plain HTTP (run it over Tailscale/WireGuard, which encrypts it). The Hosts tab
  marks each host 🔒 Tailscale / 🔒 HTTPS or ⚠ plain HTTP.
- **Machines asking to be added** (`DGXM_HUB_URL` on an agent): `POST /api/join` needs no login. A request
  only lands in the list under Hosts (at most 20 open, expired after an hour); nothing is reachable for it
  until someone approves it. Approve only when the machine shows the same join code - the code is derived
  from the agent's token, so a different machine announcing the same name shows a different one. An
  approved agent may update its own address (it proves itself with its token); rejected ones are blocked
  for a week.
- **Copying weights between machines:** the hub asks the source machine (with its agent token) for a
  read-only *share* of one registered weights path. It is valid for 6 hours and readable only with its
  random ticket, which the hub hands to the copying machine; that machine never gets the source's agent
  token. Share URLs (`GET /agent/v1/shares/...`) need no dashboard login or agent token, only the ticket.
  Symlinks leaving the shared folder are not followed (except, in a Hugging Face cache, to the same
  model's blobs). Every file is checked against the source's SHA-256 on arrival, so damaged or swapped
  files are rejected - but over plain HTTP the weights themselves travel unencrypted.
- *Import script* runs user-supplied shell scripts in a restricted, but **not sandboxed**, environment.
- Agents are trusted by the hub: a compromised agent can send misleading status data to the dashboard.

What the hub does to protect the dashboard:

- HTTP Basic auth with constant-time comparison. The installers generate a random password; the hub
  refuses to start with a password under 12 characters, with fewer than 4 different characters or a
  well-known one (`change-me`, `admin`, `password`, …), and an agent refuses a token under 32 characters.
- **Brute force:** after 10 wrong logins from one address within 10 minutes, that address gets
  `429 Too Many Requests` for the rest of the window; failed logins are logged. (Behind a reverse proxy
  all clients share the proxy's address, so a lockout affects everyone using it.)
- **Files:** the database (which stores the agent tokens) and the env files are readable by their owner
  only; the manager runs with umask `077`.
- **Cross-site requests:** requests that change something (`POST`/`PUT`/`DELETE`) are rejected when the
  browser's `Origin` (or `Sec-Fetch-Site`) shows they come from another site, so a malicious web page
  cannot use your browser's saved login to start or stop models. API clients without `Origin` (curl,
  scripts) are unaffected.
- **DNS rebinding:** without a password, only requests addressed to an IP address, `localhost` or a
  local name (single-label MagicDNS name, `*.local`, `*.ts.net`) are answered.
- **Network exposure:** `DGXM_HOST=tailscale` and `DGXM_PUBLISH_IP=tailscale` limit the manager and the
  model ports to the machine's Tailscale address (see the README).
- **Framing:** `X-Frame-Options: DENY` and `frame-ancestors 'none'` against clickjacking, plus
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`.
- Host names, URLs and agent error messages are HTML-escaped in the UI.

Therefore: always set `DGXM_PASSWORD`, use a long random `DGXM_AGENT_TOKEN`, and only expose the
dashboard and agents on a trusted network, a VPN (e.g. Tailscale/WireGuard) or behind an HTTPS reverse
proxy. Reports about these documented properties are not vulnerabilities, but ways to bypass
authentication, the agent token check, or the script-import restrictions **are** - please report them.
