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
- Hub ↔ agent traffic is plain HTTP.
- *Import script* runs user-supplied shell scripts in a restricted, but **not sandboxed**, environment.

Therefore: always set `DGXM_PASSWORD`, use a long random `DGXM_AGENT_TOKEN`, and only expose the
dashboard and agents on a trusted network, a VPN (e.g. Tailscale/WireGuard) or behind an HTTPS reverse
proxy. Reports about these documented properties are not vulnerabilities, but ways to bypass
authentication, the agent token check, or the script-import restrictions **are** - please report them.
