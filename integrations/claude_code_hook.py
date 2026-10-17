"""Mandate PreToolUse hook for Claude Code.

Sends Bash / Write / Edit tool calls to Mandate before they run and blocks until a rule
or a human decides. Standard library only, so it runs anywhere Claude Code runs.

.claude/settings.json:
    {
      "env": {"MANDATE_URL": "https://your-mandate", "MANDATE_API_KEY": "..."},
      "hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit",
        "hooks": [{"type": "command", "timeout": 600,
                   "command": "python \"$CLAUDE_PROJECT_DIR/.claude/hooks/mandate_hook.py\""}]}]}
    }

approved / auto-approved -> allow     edited -> allow with the human's values
denied / timed out       -> deny, and the reason tells Claude a human said no
Mandate unreachable      -> ask, so Claude Code falls back to its normal permission prompt
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("MANDATE_URL", "http://localhost:8000").rstrip("/")
KEY = os.environ.get("MANDATE_API_KEY", "")
AGENT = os.environ.get("MANDATE_AGENT", "claude-code")
TIMEOUT = int(os.environ.get("MANDATE_TIMEOUT", "300"))
PREVIEW = 1500


def _call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"x-mandate-key": KEY, "content-type": "application/json",
                                          "user-agent": "mandate-hook/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _preview(text, truncated, key):
    text = str(text or "")
    if len(text) > PREVIEW:
        truncated.add(key)
        return text[:PREVIEW] + f"… [{len(text) - PREVIEW} more chars]"
    return text


def to_action(tool, inp):
    """Map a Claude Code tool call to (action_type, payload, reasoning, truncated_keys)."""
    truncated = set()
    if tool == "Bash":
        return ("run_command", {"command": inp.get("command", "")},
                inp.get("description") or "Claude Code wants to run this command.", truncated)
    if tool == "Write":
        return ("write_file", {"file_path": inp.get("file_path", ""),
                               "content": _preview(inp.get("content"), truncated, "content")},
                "Claude Code wants to create or overwrite this file.", truncated)
    if tool in ("Edit", "MultiEdit"):
        payload = {"file_path": inp.get("file_path", "")}
        if tool == "Edit":
            payload["old_string"] = _preview(inp.get("old_string"), truncated, "old_string")
            payload["new_string"] = _preview(inp.get("new_string"), truncated, "new_string")
        else:
            payload["edits"] = len(inp.get("edits") or [])
            truncated.add("edits")
        return "write_file", payload, "Claude Code wants to edit this file.", truncated
    return None


def respond(decision, reason, updated=None):
    out = {"hookEventName": "PreToolUse", "permissionDecision": decision,
           "permissionDecisionReason": reason}
    if updated is not None:
        out["updatedInput"] = updated
    print(json.dumps({"hookSpecificOutput": out}))
    sys.exit(0)


def main():
    event = json.load(sys.stdin)
    tool, inp = event.get("tool_name", ""), event.get("tool_input") or {}
    mapped = to_action(tool, inp)
    if not mapped:
        sys.exit(0)  # not a tool Mandate gates: let Claude Code decide as usual
    action_type, payload, reasoning, truncated = mapped

    try:
        a = _call("POST", "/v1/approvals", {
            "agent": AGENT, "source": "claude_code", "action_type": action_type, "payload": payload,
            "context": {"cwd": event.get("cwd"), "tool": tool, "session_id": event.get("session_id")},
            "reasoning": reasoning, "timeout_seconds": TIMEOUT})
    except (urllib.error.URLError, OSError, ValueError) as e:
        respond("ask", f"Mandate unreachable ({e}); asking in the terminal instead.")

    if a["status"] == "pending":
        print(f"Mandate: waiting for human approval · {a['id']}", file=sys.stderr)
        deadline = time.time() + TIMEOUT + 15
        while a.get("status") == "pending" and time.time() < deadline:
            time.sleep(2)
            try:
                a = _call("GET", f"/v1/approvals/{a['id']}")
            except (urllib.error.URLError, OSError, ValueError):
                pass  # network blip: keep waiting, never run unapproved

    who = a.get("decided_by") or "a human"
    status = a.get("status")
    if status == "auto_approved":
        respond("allow", f"Mandate auto-approved under rule {a.get('rule_id')} (risk {a.get('risk_score')}).")
    if status == "approved":
        respond("allow", f"Approved by {who} in Mandate.")
    if status == "edited":
        final = a.get("final_payload") or {}
        updated = dict(inp)
        for k, v in final.items():
            if k in inp and k not in truncated:
                updated[k] = v
        changes = ", ".join(f"{k}={v!r}" for k, v in final.items() if k in inp and inp.get(k) != v and k not in truncated)
        respond("allow", f"{who} edited this in Mandate before approving ({changes or 'no changes'}).", updated)
    if status == "denied" and who == "rule":
        respond("deny", f"A Mandate policy ({a.get('rule_id')}) never allows this {action_type.replace('_', ' ')}. "
                        "Do not retry it or work around it; tell the user it is blocked by policy.")
    if status == "denied":
        respond("deny", f"A human ({who}) denied this {action_type.replace('_', ' ')} in Mandate. "
                        "Do not retry it or work around it; tell the user and ask how they want to proceed.")
    respond("deny", f"Nobody approved this {action_type.replace('_', ' ')} in Mandate within {TIMEOUT}s, "
                    "so it was blocked. Ask the user before trying again.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # a crashed hook would let the tool run, so fall back to asking
        respond("ask", f"Mandate hook error ({type(e).__name__}: {e}); asking in the terminal instead.")
