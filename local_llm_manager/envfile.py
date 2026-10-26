"""Fill a freshly copied .env with random secrets; used by install.sh / install-macos.sh.

    python -m local_llm_manager.envfile <path>

Replaces the example password `change-me` with a random one and puts a random agent token on the
(commented) DGXM_AGENT_TOKEN line, so agent mode only needs uncommenting. Prints the password once.
"""

import re
import secrets
import sys
from pathlib import Path


def fill(path: Path) -> str | None:
    text = path.read_text()
    password = None
    if re.search(r"^DGXM_PASSWORD=change-me\s*$", text, re.M):
        password = secrets.token_urlsafe(18)
        text = re.sub(r"^DGXM_PASSWORD=change-me\s*$", f"DGXM_PASSWORD={password}", text, flags=re.M)
    text = re.sub(r"^(#\s*)?DGXM_AGENT_TOKEN=\s*$", lambda m: f"{m.group(1) or ''}DGXM_AGENT_TOKEN={secrets.token_hex(32)}",
                  text, flags=re.M)
    path.write_text(text)
    path.chmod(0o600)
    return password


if __name__ == "__main__":
    pw = fill(Path(sys.argv[1]))
    if pw:
        print(f"Generated dashboard password (user admin): {pw}\n"
              f"It is stored in {sys.argv[1]} - change it there if you like (at least 12 characters).")
