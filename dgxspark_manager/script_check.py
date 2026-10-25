"""Static review of a pasted starter script before the importer runs it.

Lists every risky call per line as one flat list of risks. Each message says whether the call is
skipped during import (the import only really runs a few read-only tools, see
importer.ALLOWED_TOOLS) or whether the import cannot block it (absolute-path programs, file writes,
PATH changes, /dev/tcp, awk/sed side effects). Any risk means the import needs an explicit
"I trust this script".

It is a line-based heuristic, not a shell parser: it can miss things and flag false positives.
"""

import re
from typing import Any

# Command position: start of line or after ; & | ( ` $( and common prefixes (then, do, exec, ...).
_CMD = r"(?:^|[;&|(`]|\$\(|\b(?:then|do|else|exec|command|nohup|time|env|xargs|sudo)\s)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"

# Each rule: (pattern, message, view). "code" sees the line with quoted text blanked out (except
# $(...) inside double quotes), so messages like `echo "run: sudo ..."` don't trigger; "raw" sees
# everything, needed for awk/sed programs which live inside single quotes.

# Calls the restricted import run cannot stop.
NOT_BLOCKED = [
    (_CMD + r"(/(?!dev/null)[^\s;|&)`'\"]+)", "Runs a program by absolute path ({0})", "code"),
    (r"(?<![<0-9&])>>?\s*((?:/|~|\$HOME|\$\{HOME\})(?!dev/(?:null|stdout|stderr)\b)[^\s;|&)]*)", "Writes to a file ({0})", "code"),
    (r"(?:^|[;&|\s])(?:export\s+)?PATH\+?=", "Changes PATH, which can re-enable commands that are blocked during import", "code"),
    (r"/dev/(?:tcp|udp)/", "Opens a network connection through bash's /dev/tcp", "raw"),
    (r"\b(?:unset\s+-f\s+command_not_found_handle|command_not_found_handle\s*\(\))", "Overrides the hook the importer uses to block commands", "code"),
    (r"\benable\s+-[a-z]*f", "Loads bash builtins from a file", "code"),
    (r"\b[gm]?awk\b[^\n]*\bsystem\s*\(", "awk system() runs shell commands", "raw"),
    (r"\b[gm]?awk\b[^\n]*(?:print|printf)[^\n]*>\s*\"", "awk writes to a file", "raw"),
    (r"\bsed\b[^\n|;]*\s-[a-zA-Z]*i", "sed -i edits files in place", "code"),
    (r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", "Downloads and runs a script (curl | sh)", "code"),
]

# Commands the restricted import run records and skips.
SKIPPED = [
    (r"(?<![\w./-])rm\s", "rm deletes files", "code"),
    (r"(?<![\w./-])(?:dd|mkfs(?:\.\w+)?|fdisk|parted|wipefs)\b", "Disk-level tool", "code"),
    (r"\b(?:curl|wget)\b", "Network download", "code"),
    (r"\bsudo\b", "sudo (root privileges)", "code"),
    (r"\b(?:ssh|scp|rsync|sftp)\b", "Remote access / copy", "code"),
    (r"\b(?:nc|ncat|netcat|socat|telnet)\b", "Raw network tool", "code"),
    (r"\b(?:chmod|chown|chgrp|setfacl)\b", "Changes file permissions or ownership", "code"),
    (r"\b(?:systemctl|service|crontab)\s", "Changes services or scheduled jobs", "code"),
    (r"(?<![\w./-])(?:kill|pkill|killall)\s", "Kills processes", "code"),
    (r"\b(?:pip3?|apt(?:-get)?|yum|dnf|npm|conda)\s+(?:install|remove|uninstall)", "Installs or removes software", "code"),
    (r"\bgit\s+(?:clone|pull|fetch)", "Downloads code", "code"),
    (r"\bpython3?\s+-c\b", "Runs inline Python", "code"),
    (r"\bbase64\s+(?:-d|--decode)", "Decodes hidden content (base64)", "code"),
    (r"\b(?:shutdown|reboot|poweroff|halt)\b", "Shuts down or reboots", "code"),
    (r"/proc/sys/", "Writes kernel settings under /proc/sys", "code"),
    (r"\bmkdir\b|\btouch\b|\bmv\s|\bcp\s|\bln\s", "Creates, moves or copies files", "code"),
]


def _strip_comment(line: str) -> str:
    """Drop a trailing # comment that is outside quotes (good enough for typical scripts)."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t;"):
            return line[:i]
    return line


def _code_only(line: str) -> str:
    """Blank out quoted text, keeping $(...) command substitutions inside double quotes."""
    out, quote, depth = [], None, 0
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == "'":
            out.append("'" if ch == "'" else " ")
            if ch == "'":
                quote = None
        elif quote == '"':
            if line.startswith("$(", i):
                depth += 1
                out.append("$(")
                i += 2
                continue
            if depth:
                depth -= ch == ")"
                out.append(ch)
            elif ch == "\\" and i + 1 < len(line):
                out.append("  ")
                i += 2
                continue
            else:
                out.append('"' if ch == '"' else " ")
                if ch == '"':
                    quote = None
        else:
            if ch in "'\"":
                quote = ch
            out.append(ch)
        i += 1
    return "".join(out)


def check_script(text: str) -> dict[str, Any]:
    findings = []
    seen = set()
    for n, raw in enumerate(text.splitlines(), start=1):
        if n == 1 and raw.startswith("#!"):
            continue
        line = _strip_comment(raw)
        if not line.strip():
            continue
        views = {"raw": line, "code": _code_only(line)}
        for rules, note in ((NOT_BLOCKED, "not blocked during import"), (SKIPPED, "skipped during import")):
            for pattern, message, view in rules:
                m = re.search(pattern, views[view])
                if not m:
                    continue
                arg = next((g for g in m.groups() if g), "") if m.groups() else ""
                text_ = f"{message.format(arg)} – {note}"
                if (n, text_) in seen:
                    continue
                seen.add((n, text_))
                findings.append({"line": n, "code": raw.strip()[:200], "message": text_,
                                 "skipped": rules is SKIPPED})
    findings.sort(key=lambda f: f["line"])
    return {"findings": findings, "count": len(findings)}
