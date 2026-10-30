"""Agents asking a hub to be added ("join"), approved there by comparing a code.

An agent with DGXM_HUB_URL announces itself to that hub: its name, the address the hub reaches it at and
its agent token. The hub lists the request under Hosts until someone approves or rejects it. Both sides
show a join code derived from the token; approve only when the hub shows the same code as the machine
(installer output, its log, its start page) - another machine announcing the same name has another token
and therefore another code. After approval the agent keeps announcing itself now and then, so the hub
learns a changed address; only the holder of the approved token can change it.

Run `python -m local_llm_manager.joining code <token>` to print the code for a token.
"""

import asyncio
import base64
import hashlib
import logging
import socket
import sys
from typing import Any, Optional

import httpx

from . import __version__, config

log = logging.getLogger(__name__)

PENDING_RETRY = 30.0      # seconds between announcements while the hub has not decided
APPROVED_REFRESH = 600.0  # afterwards: tell the hub the current address every 10 minutes
ERROR_RETRY = 60.0


def join_code(token: str) -> str:
    """8 characters both sides can compare, e.g. "K7Q2-X9PM" (from the token, not revealing it)."""
    digest = base64.b32encode(hashlib.sha256(f"dgxm-join:{token}".encode()).digest()).decode()
    return f"{digest[:4]}-{digest[4:8]}"


def own_url() -> str:
    """The address the hub should use for this agent: DGXM_AGENT_URL, else this machine's name."""
    if config.AGENT_URL:
        return config.AGENT_URL
    host = config.HOST if config.HOST not in ("", "0.0.0.0", "::") else socket.gethostname().split(".")[0]
    return f"http://{host}:{config.PORT}"


class Joiner:
    """The agent side: announce to the hub until it decides, then refresh now and then."""

    def __init__(self, hub_url: str, token: str, info: Optional[dict[str, Any]] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.hub = hub_url.rstrip("/")
        self.token = token
        self.info = info or {}
        self.transport = transport
        self.state = {"hub": self.hub, "status": "starting", "code": join_code(token), "message": "",
                      "name": config.AGENT_NAME or socket.gethostname().split(".")[0], "url": own_url()}

    async def announce(self) -> str:
        body = {"name": self.state["name"], "url": self.state["url"], "token": self.token,
                "version": __version__, "info": self.info}
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self.transport) as c:
                r = await c.post(f"{self.hub}/api/join", json=body)
        except httpx.HTTPError as e:
            self.state.update(status="error", message=f"hub not reachable: {e}")
            return "error"
        if r.status_code != 200:
            detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
            self.state.update(status="error", message=f"hub answered {r.status_code}: {str(detail)[:200]}")
            return "error"
        status = r.json().get("state", "pending")
        if status != self.state["status"]:
            if status == "pending":
                log.warning("Asked the hub %s to add this machine as '%s' (%s). Approve it under Hosts there "
                            "only if it shows the code %s", self.hub, self.state["name"], self.state["url"],
                            self.state["code"])
            elif status == "approved":
                log.info("The hub %s added this machine", self.hub)
            elif status == "rejected":
                log.warning("The hub %s rejected this machine", self.hub)
        self.state.update(status=status, message="")
        return status

    async def run(self) -> None:
        while True:
            status = await self.announce()
            if status == "rejected":
                return
            await asyncio.sleep({"approved": APPROVED_REFRESH, "error": ERROR_RETRY}.get(status, PENDING_RETRY))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "code":
        print(join_code(sys.argv[2]))
    else:
        print("usage: python -m local_llm_manager.joining code <token>", file=sys.stderr)
        sys.exit(2)
