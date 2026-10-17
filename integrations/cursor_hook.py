"""Mandate preToolUse hook for Cursor.

Sends Cursor's Shell / Write / Delete tool calls to Mandate before they run and blocks until a rule or a
human decides. Standard library only.

.cursor/hooks.json:
    {"version": 1, "hooks": {"preToolUse": [{"command": "python .cursor/hooks/mandate_hook.py",
      "matcher": "Shell|Write|Delete", "timeout": 600, "failClosed": true}]}}

.cursor/hooks/mandate.json (Cursor hooks can't set env vars; env MANDATE_URL / MANDATE_API_KEY win if set):
    {"url": "https://your-mandate", "api_key": "..."}

approved / auto-approved -> allow     edited -> allow with updated_input
denied / timed out / policy -> deny, and agent_message tells Cursor's AI not to retry
Mandate unreachable -> deny (Cursor does not enforce "ask" for preToolUse, so this fails closed)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

_CFG = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mandate.json"), encoding="utf-8") as f:
        _CFG = json.load(f)
except (OSError, ValueError):
    pass

URL = (os.environ.get("MANDATE_URL") or _CFG.get("url") or "http://localhost:8000").rstrip("/")
KEY = os.environ.get("MANDATE_API_KEY") or _CFG.get("api_key") or ""
AGENT = os.environ.get("MANDATE_AGENT") or _CFG.get("agent") or "cursor-agent"
TIMEOUT = int(os.environ.get("MANDATE_TIMEOUT") or _CFG.get("timeout") or 300)
PREVIEW = 1500
# set "debug": true in mandate.json (or MANDATE_HOOK_DEBUG=1) to log every call to <temp>/mandate_cursor_hook.log
DEBUG = bool(os.environ.get("MANDATE_HOOK_DEBUG") or _CFG.get("debug"))


def _log(msg):
    if DEBUG:
        try:
            import tempfile
            with open(os.path.join(tempfile.gettempdir(), "mandate_cursor_hook.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + str(msg)[:2000] + "\n")
        except OSError:
            pass


def _call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"x-mandate-key": KEY, "content-type": "application/json",
                                          "user-agent": "mandate-hook/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _pick(d, *keys):
    return next((d[k] for k in keys if d.get(k) not in (None, "")), "")


def to_action(tool, inp):
    """Map a Cursor tool call to (action_type, payload, reasoning, editable_key_map)."""
    if tool == "Shell":
        return ("run_command", {"command": _pick(inp, "command")},
                "Cursor's agent wants to run this command.", {"command": "command"})
    if tool in ("Write", "Delete"):
        path_key = next((k for k in ("file_path", "path", "target_file") if inp.get(k)), "file_path")
        payload = {"file_path": inp.get(path_key, "")}
        if tool == "Delete":
            payload["operation"] = "delete"
            return "write_file", payload, "Cursor's agent wants to delete this file.", {"file_path": path_key}
        content = str(_pick(inp, "content", "contents", "code_edit", "new_string"))
        payload["content"] = content[:PREVIEW] + (f"… [{len(content) - PREVIEW} more chars]" if len(content) > PREVIEW else "")
        return "write_file", payload, "Cursor's agent wants to create or overwrite this file.", {"file_path": path_key}
    return None


def respond(permission, user_message, agent_message=None, updated=None):
    out = {"permission": permission, "user_message": user_message, "agent_message": agent_message or user_message}
    if updated is not None:
        out["updated_input"] = updated
    _log("OUT " + json.dumps(out))
    print(json.dumps(out))
    sys.exit(0)


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", "replace").lstrip("﻿")
    _log(f"IN python={sys.executable} cwd={os.getcwd()} url={URL} key_set={bool(KEY)} stdin={raw}")
    event = json.loads(raw)
    tool, inp = event.get("tool_name", ""), event.get("tool_input") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"command": inp}
    mapped = to_action(tool, inp)
    if not mapped:
        respond("allow", "Not an action Mandate gates.")
    action_type, payload, reasoning, editable = mapped
    words = action_type.replace("_", " ")

    try:
        a = _call("POST", "/v1/approvals", {
            "agent": AGENT, "source": "cursor", "action_type": action_type, "payload": payload,
            "context": {"cwd": event.get("cwd") or inp.get("working_directory"), "tool": tool},
            "reasoning": reasoning, "timeout_seconds": TIMEOUT})
    except (urllib.error.URLError, OSError, ValueError) as e:
        respond("deny", f"Mandate is unreachable ({e}), so this {words} was blocked.",
                "Mandate (the human approval layer) is unreachable, so this action was blocked. "
                "Tell the user and wait for them; do not try to work around it.")

    if a["status"] == "pending":
        deadline = time.time() + TIMEOUT + 15
        while a.get("status") == "pending" and time.time() < deadline:
            time.sleep(2)
            try:
                a = _call("GET", f"/v1/approvals/{a['id']}")
            except (urllib.error.URLError, OSError, ValueError):
                pass  # network blip: keep waiting, never run unapproved

    who, status = a.get("decided_by") or "a human", a.get("status")
    if status == "auto_approved":
        respond("allow", f"Mandate auto-approved this under rule {a.get('rule_id')}.")
    if status == "approved":
        respond("allow", f"Approved by {who} in Mandate.")
    if status == "edited":
        final, updated = a.get("final_payload") or {}, dict(inp)
        for ours, theirs in editable.items():
            if ours in final:
                updated[theirs] = final[ours]
        respond("allow", f"{who} edited this in Mandate before approving.", updated=updated)
    if status == "denied" and who == "rule":
        respond("deny", f"Blocked by Mandate policy {a.get('rule_id')}.",
                f"A Mandate policy ({a.get('rule_id')}) never allows this {words}. Do not retry it or work around it; "
                "tell the user it is blocked by policy.")
    if status == "denied":
        respond("deny", f"{who} denied this {words} in Mandate.",
                f"A human ({who}) denied this {words} in Mandate. Do not retry it or work around it; "
                "tell the user and ask how they want to proceed.")
    respond("deny", f"Nobody approved this {words} in Mandate within {TIMEOUT}s.",
            f"Nobody approved this {words} in Mandate in time, so it was blocked. Ask the user before trying again.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail closed: Cursor would otherwise run the tool
        _log(f"ERROR {type(e).__name__}: {e}")
        respond("deny", f"Mandate hook error ({type(e).__name__}: {e}); the action was blocked.")
