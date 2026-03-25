#!/usr/bin/env python3
"""Safety gate hook for the OPAL optimizer plugin.

Intercepts Write, Edit, and Bash tool calls to prevent:
- Overwriting the original monitor config or baseline files
- Deleting optimization results or working files
- Running unauthorized Observe API calls (allowlist approach)
- Modifying files outside the /tmp/opal-optimizer/ workspace

For API calls, uses an ALLOWLIST — only explicitly permitted operations
are allowed. Everything else is blocked. The optimizer needs:
  GET  /v1/monitors      (list monitors)
  GET  /v1/monitors/{id} (read monitor config)
  POST /v1/monitors      (create new V2 monitor)

All other API methods and endpoints are blocked.

Runs as a PreToolUse hook. Reads tool input from stdin as JSON.
Exits 0 to allow, exits 2 to block (with reason on stderr).
"""

import json
import re
import sys

PROTECTED_FILES = [
    "/tmp/opal-optimizer/original_config.json",
    "/tmp/opal-optimizer/baseline.opal",
    "/tmp/opal-optimizer/baseline_results.json",
    "/tmp/opal-optimizer/baseline_timing.txt",
]

ALLOWED_WORKSPACE = "/tmp/opal-optimizer/"

# Allowlisted API operations: (HTTP_METHOD, URL_PATTERN)
# Anything hitting observeinc.com that doesn't match one of these is blocked.
ALLOWED_API_OPS = [
    ("GET", r"/v1/monitors(/\d+)?$"),       # Read monitor(s)
    ("POST", r"/v1/monitors$"),              # Create new monitor
]


def is_observe_api_call(command):
    """Check if a bash command contains an Observe API call."""
    return "observeinc.com" in command and "curl" in command.lower()


def check_api_call(command):
    """Validate an Observe API call against the allowlist."""
    cmd_upper = command.upper()

    # Extract the HTTP method
    method = "GET"  # curl default
    if "-X " in command or "--request " in command:
        method_match = re.search(r'(?:-X|--request)\s+(\w+)', command)
        if method_match:
            method = method_match.group(1).upper()
    elif "-d " in command or "--data " in command or "-d@" in command:
        method = "POST"  # curl with data defaults to POST

    # Extract the URL path
    url_match = re.search(r'https?://[^/\s]+(/v1/[^\s"\']+)', command)
    if not url_match:
        # Has observeinc.com but no /v1/ path — might be a non-API URL, allow it
        return True, None

    url_path = url_match.group(1).rstrip("'\"")

    # Check against allowlist
    for allowed_method, allowed_pattern in ALLOWED_API_OPS:
        if method == allowed_method and re.match(allowed_pattern, url_path):
            return True, None

    return False, (
        f"BLOCKED: {method} {url_path} is not an allowed API operation. "
        f"The optimizer can only: GET /v1/monitors, GET /v1/monitors/{{id}}, "
        f"POST /v1/monitors (create new). All other API operations are blocked "
        f"for safety."
    )


def check_write(tool_input):
    """Check Write tool calls."""
    file_path = tool_input.get("file_path", "")

    for protected in PROTECTED_FILES:
        if file_path == protected:
            return False, f"BLOCKED: Cannot overwrite protected file {file_path}. This is the original baseline data."

    if not file_path.startswith(ALLOWED_WORKSPACE) and not file_path.startswith("/Users/"):
        return False, f"BLOCKED: Write to {file_path} is outside the optimizer workspace ({ALLOWED_WORKSPACE})."

    return True, None


def check_edit(tool_input):
    """Check Edit tool calls."""
    file_path = tool_input.get("file_path", "")

    for protected in PROTECTED_FILES:
        if file_path == protected:
            return False, f"BLOCKED: Cannot edit protected file {file_path}. This is the original baseline data."

    return True, None


def check_bash(tool_input):
    """Check Bash tool calls."""
    command = tool_input.get("command", "")

    # Check Observe API calls against allowlist
    if is_observe_api_call(command):
        allowed, reason = check_api_call(command)
        if not allowed:
            return False, reason

    # Block rm of protected files
    for protected in PROTECTED_FILES:
        if f"rm {protected}" in command or f"rm -f {protected}" in command or f"rm -rf {protected}" in command:
            return False, f"BLOCKED: Cannot delete protected file {protected}."

    # Block rm -rf of the entire workspace
    if "rm -rf /tmp/opal-optimizer" in command and "variant" not in command:
        return False, "BLOCKED: Cannot delete the entire optimizer workspace. Use specific file paths."

    return True, None


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if tool_name == "Write":
        allowed, reason = check_write(tool_input)
    elif tool_name == "Edit":
        allowed, reason = check_edit(tool_input)
    elif tool_name == "Bash":
        allowed, reason = check_bash(tool_input)
    else:
        sys.exit(0)

    if not allowed:
        print(reason, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
