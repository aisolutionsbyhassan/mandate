"""Mandate core: SQLite, deterministic risk scoring, policy engine.

Mandate never makes a judgment call. It routes: rules a human granted
auto-approve, everything else escalates to a human. The LLM only explains.
(The learning engine arrives in Phase 4.)
"""
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("MANDATE_DB", "mandate.db")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
EXPLAIN_PROMPT = (
    "You explain to a human reviewer why an AI agent's action was flagged. You never recommend "
    "approving or denying, and you never change the risk score. Use the concrete numbers given. "
    'Return only JSON: {"reasons": [exactly 3 short strings, max 12 words each], '
    '"impact_if_wrong": "one line, max 20 words, what happens if this goes through by mistake"}'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  agent TEXT, action_type TEXT, payload_json TEXT, context_json TEXT,
  reasoning TEXT, risk_score INTEGER, risk_reasons_json TEXT, impact_if_wrong TEXT,
  status TEXT, decided_by TEXT, decided_at TEXT, final_payload_json TEXT,
  rule_id TEXT, callback_url TEXT, timeout_seconds INTEGER, on_timeout TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS rules (
  id TEXT PRIMARY KEY,
  name TEXT, conditions_json TEXT, action TEXT, source TEXT,
  times_used INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created_at TEXT
);
"""

SOURCES = ("python", "langgraph", "n8n", "claude_code", "make", "zapier", "http")
IRREVERSIBILITY = {"delete_account": 40, "issue_refund": 30, "email_customer": 10,
                   "run_command": 10, "write_file": 5}
DECISIONS = {"approved": "approved", "approve": "approved", "denied": "denied",
             "deny": "denied", "edited": "edited", "edit": "edited"}


class Conflict(Exception):
    pass


# ---------------------------------------------------------------- db helpers

def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _query(sql, args=()):
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _exec(sql, args=()):
    with closing(_conn()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.rowcount


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _present(row):
    out = dict(row)
    for col, key, default in (("payload_json", "payload", {}), ("context_json", "context", {}),
                              ("risk_reasons_json", "risk_reasons", []),
                              ("final_payload_json", "final_payload", None)):
        raw = out.pop(col, None)
        out[key] = json.loads(raw) if raw else default
    return out


def _present_rule(row):
    out = dict(row)
    out["conditions"] = json.loads(out.pop("conditions_json") or "{}")
    out["pitch"] = out["conditions"].pop("pitch", None)
    out["description"] = describe_conditions(out["conditions"])
    return out


# ---------------------------------------------------------------- coding-agent risk (Phase 7)

SECRET_FILE = re.compile(r"(^|[\\/])(\.env(\.(?!example\b)[\w.-]+)?|id_rsa|id_ed25519|[^\\/\s]*\.pem|[^\\/\s]*\.key"
                         r"|secrets?\.\w+|credentials[\w.]*)$", re.I)
COMMAND_FLOORS = [
    (r"\brm\b[^;&|]*\s(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b", "recursive delete (rm -r)"),
    (r"\bgit\s+push\b[^;&|]*\s(--force(-with-lease)?|-f)\b", "force push rewrites shared history"),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", "destroys database data"),
    (r"\bmkfs\b|\bdd\b[^;&|]*\bof=/dev/", "wipes a disk"),
    (r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", "pipes a remote script into a shell"),
    (r">\s*\S*\.env\b(?!\.example)", "overwrites a secrets file"),
]
COMMAND_SIGNALS = [
    (r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-\w*f", 25, "discards uncommitted work"),
    (r"\b(kubectl\s+(apply|delete)|terraform\s+(apply|destroy)|fly\s+deploy|docker\s+push|npm\s+publish"
     r"|helm\s+(install|upgrade)|vercel\b[^;&|]*--prod|(gcloud|aws)\b[^;&|]*\bdeploy)\b", 30, "deploys or publishes to production"),
    (r"\bgit\s+push\b", 20, "publishes commits to a shared remote"),
    (r"\bsudo\b", 20, "runs with root privileges"),
    (r"\b(rm|rmdir|del)\b", 15, "deletes files"),
    (r"\b(chmod|chown)\b", 10, "changes file permissions"),
]
SAFE_COMMANDS = {"ls", "dir", "pwd", "cat", "head", "tail", "wc", "echo", "grep", "rg", "find", "which", "where",
                 "whoami", "date", "tree", "file", "stat", "du", "df", "sort", "uniq", "cut", "diff", "ps", "type",
                 "test", "["}
SAFE_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe"}


def is_readonly_command(cmd):
    """True only if every piece of the command just reads (ls, cat, git status...)."""
    cmd = re.sub(r"\d*>\s*/dev/null|\d*>&\d", " ", cmd or "")  # discarding output writes nothing
    if not cmd.strip() or re.search(r"[>`]|\$\(", cmd):
        return False
    for seg in re.split(r"&&|\|\||;|\|", cmd):
        toks = seg.split()
        if not toks:
            continue
        if any(SECRET_FILE.search(t.strip("'\"")) for t in toks):
            return False
        if toks[0] == "git":
            if len(toks) < 2 or toks[1] not in SAFE_GIT:
                return False
        elif toks[0] not in SAFE_COMMANDS or (toks[0] == "find" and {"-delete", "-exec", "-execdir", "-ok"} & set(toks)):
            return False
    return True


def code_risk(action_type, payload):
    """Extra points, signals and hard floor for coding-agent actions."""
    pts, signals, floor = 0, [], None
    if action_type == "run_command":
        cmd = str(payload.get("command") or "")
        floor = next((why for pat, why in COMMAND_FLOORS if re.search(pat, cmd, re.I)), None)
        for pat, p, why in COMMAND_SIGNALS:
            if re.search(pat, cmd, re.I):
                pts += p
                signals.append(f"Command {why} (+{p})")
    elif action_type == "write_file":
        path = str(payload.get("file_path") or "")
        if SECRET_FILE.search(path):
            floor = f"editing {os.path.basename(path)} can leak or break secrets"
    return pts, signals, floor


# ---------------------------------------------------------------- risk scoring

def score_risk(action_type, payload, context):
    """Deterministic 0-100 score. Returns {score, signals, floor}."""
    payload, context = payload or {}, context or {}
    amount = _num(payload.get("amount"))
    score, signals = 0, []

    floor = action_type == "delete_account" or (amount is not None and amount > 500)
    if action_type == "delete_account":
        signals.append("Hard floor: deleting an account always needs a human")
    elif floor:
        signals.append(f"Hard floor: amount ${amount:,.0f} is over $500, a human must decide")

    age, limit = _num(context.get("order_age_days")), _num(context.get("policy_limit_days"))
    if age is not None and limit is not None and age > limit:
        score += 15
        signals.append(f"Order is {age:.0f} days old, policy limit is {limit:.0f} days (+15)")

    if amount is not None and amount >= 50:
        pts = min(30, round(30 * (amount - 50) / 4950))
        score += pts
        signals.append(f"Amount ${amount:,.0f} (+{pts})")

    irr = IRREVERSIBILITY.get(action_type, 0)
    if irr:
        score += irr
        signals.append(f"{action_type} is hard to undo (+{irr})")

    recipients = _num(context.get("recipients"))
    if recipients is not None and recipients > 100:
        score += 20
        signals.append(f"Blast radius: {recipients:.0f} recipients (+20)")

    pts, code_signals, code_floor = code_risk(action_type, payload)
    score += pts
    signals.extend(code_signals)
    if code_floor:
        floor = True
        score = max(score, 70)
        signals.insert(0, f"Hard floor: {code_floor}, a human must decide")

    return {"score": min(100, score), "signals": signals, "floor": floor}


def llm_explain(action_type, payload, context, reasoning, risk):
    """Groq writes the reasons + impact line. 3s timeout; any failure returns the
    deterministic fallback. It only explains — status and score never depend on it."""
    fallback = explain(action_type, payload, context, risk)
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return fallback
    try:
        from groq import Groq

        client = Groq(api_key=key, timeout=3, max_retries=0)
        resp = client.chat.completions.create(
            model=GROQ_MODEL, temperature=0, max_tokens=400,
            response_format={"type": "json_object"}, extra_body={"reasoning_effort": "low"},
            messages=[{"role": "system", "content": EXPLAIN_PROMPT},
                      {"role": "user", "content": json.dumps({
                          "action_type": action_type, "payload": payload, "context": context,
                          "agent_reasoning": reasoning, "risk_score": risk["score"],
                          "signals": risk["signals"]})}])
        data = json.loads(resp.choices[0].message.content)
        reasons = [str(r).strip()[:140] for r in data["reasons"] if str(r).strip()][:3]
        impact = str(data["impact_if_wrong"]).strip()[:200]
        if len(reasons) < 2 or not impact:
            return fallback
        return {"reasons": reasons, "impact_if_wrong": impact}
    except Exception:
        return fallback


def explain(action_type, payload, context, risk):
    """Reasons + impact line built only from the deterministic signals."""
    amount = _num((payload or {}).get("amount"))
    reasons = risk["signals"][:4] or ["No elevated risk signals found"]
    if action_type == "delete_account":
        impact = "The customer's account and data are permanently lost."
    elif action_type == "issue_refund":
        impact = (f"${amount:,.0f} leaves the business and usually can't be recovered."
                  if amount is not None else "Money leaves the business and usually can't be recovered.")
    elif action_type == "email_customer":
        n = _num((context or {}).get("recipients"))
        impact = (f"An email goes to {n:.0f} people and can't be recalled." if n and n > 1
                  else "An email goes to the customer and can't be recalled.")
    elif action_type == "run_command":
        impact = "The command runs on the developer's machine and its effects may not be reversible."
    elif action_type == "write_file":
        impact = f"{(payload or {}).get('file_path', 'The file')} is overwritten; uncommitted contents may be lost."
    else:
        impact = "The action runs immediately and may not be reversible."
    return {"reasons": reasons, "impact_if_wrong": impact}


# ---------------------------------------------------------------- policy engine

def describe_conditions(cond):
    parts = []
    if cond.get("readonly_command"):
        return "read-only shell commands (ls, cat, grep, git status/log/diff…)"
    if cond.get("action_type"):
        parts.append(cond["action_type"].replace("_", " "))
    if "amount_lt" in cond:
        parts.append(f"amount under ${cond['amount_lt']:,.0f}")
    if "order_age_days_lt" in cond:
        parts.append(f"order under {cond['order_age_days_lt']:.0f} days old")
    if "recipients_lt" in cond:
        parts.append(f"under {cond['recipients_lt']:.0f} recipients")
    return ", ".join(parts) or "any action"


def rule_matches(cond, action_type, payload, context):
    if cond.get("action_type") and cond["action_type"] != action_type:
        return False
    if cond.get("readonly_command") and not (
            action_type == "run_command" and is_readonly_command((payload or {}).get("command"))):
        return False
    if "amount_lt" in cond:
        amount = _num((payload or {}).get("amount"))
        if amount is None or not amount < cond["amount_lt"]:
            return False
    if "order_age_days_lt" in cond:
        age = _num((context or {}).get("order_age_days"))
        if age is None or not age < cond["order_age_days_lt"]:
            return False
    if "recipients_lt" in cond:
        n = _num((context or {}).get("recipients"))
        if n is None or not n < cond["recipients_lt"]:
            return False
    return True


def evaluate(action_type, payload, context, risk):
    """Returns ("auto", rule) or ("escalate", rule_or_None). Never judges, only routes."""
    rules = _query("SELECT * FROM rules WHERE active=1")
    for r in rules:
        r["conditions"] = json.loads(r["conditions_json"])
    ask = next((r for r in rules if r["action"] == "always_ask"
                and rule_matches(r["conditions"], action_type, payload, context)), None)
    if risk["floor"] or ask:
        return "escalate", ask
    auto = next((r for r in rules if r["action"] == "auto_approve"
                 and rule_matches(r["conditions"], action_type, payload, context)), None)
    return ("auto", auto) if auto else ("escalate", None)


def autonomy_score():
    """auto_approved / total over the last 50 approvals, as a 0-100 percent."""
    rows = _query("SELECT status FROM approvals ORDER BY created_at DESC, rowid DESC LIMIT 50")
    if not rows:
        return 0
    return round(100 * sum(r["status"] == "auto_approved" for r in rows) / len(rows))


def policies():
    rules = [_present_rule(r) for r in
             _query("SELECT * FROM rules WHERE active=1 ORDER BY created_at, rowid")]
    proposals = [_present_rule(r) for r in
                 _query("SELECT * FROM rules WHERE source='proposed' AND active=0 ORDER BY created_at DESC")]
    return {"rules": rules, "proposals": proposals, "autonomy_score": autonomy_score()}


def grant_rule(rule_id):
    n = _exec("UPDATE rules SET active=1, source='learned' WHERE id=? AND source='proposed' AND active=0",
              (rule_id,))
    if not n:
        raise KeyError(rule_id)
    return _present_rule(_query("SELECT * FROM rules WHERE id=?", (rule_id,))[0])


# ---------------------------------------------------------------- learning engine

NOUNS = {"issue_refund": ("refunds", "refund"), "email_customer": ("customer emails", "email")}
PITCH_PROMPT = (
    "You are Mandate, an approval layer for AI agents. A human approved several similar agent actions. "
    "Write ONE friendly sentence (max 30 words) addressed to them that says how many they approved, "
    "states the exact limits given, and asks if you should handle these yourself from now on. "
    "Mention only the limits provided, keep their exact numbers, and say 'under'/'fewer than', never 'up to'. "
    "Style example: 'You approved 3 similar refunds — all under $50, all within 30 days. "
    "Should I handle these myself from now on?' "
    'End with a question mark. Return only JSON: {"pitch": "..."}'
)


def _round_up(x, step):
    """Smallest multiple of step strictly above x: 45 -> 50, 50 -> 60, 260 -> 300."""
    return (int(x // step) + 1) * step


def _pitch(action_type, n, cond):
    plural = NOUNS.get(action_type, (action_type.replace("_", " ") + "s",))[0]
    limits = []
    if "amount_lt" in cond:
        limits.append(f"all under ${cond['amount_lt']:,.0f}")
    if "order_age_days_lt" in cond:
        limits.append(f"all within {cond['order_age_days_lt']:.0f} days")
    if "recipients_lt" in cond:
        limits.append(f"all to fewer than {cond['recipients_lt']:.0f} people")
    template = (f"You approved {n} similar {plural}" + (" — " + ", ".join(limits) if limits else "")
                + ". Should I handle these myself from now on?")
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return template
    try:
        from groq import Groq

        client = Groq(api_key=key, timeout=3, max_retries=0)
        resp = client.chat.completions.create(
            model=GROQ_MODEL, temperature=0.3, max_tokens=300,
            response_format={"type": "json_object"}, extra_body={"reasoning_effort": "low"},
            messages=[{"role": "system", "content": PITCH_PROMPT},
                      {"role": "user", "content": json.dumps({
                          "approved_count": n, "kind": plural, "limits": {k: v for k, v in {
                              "amount_under_usd": cond.get("amount_lt"),
                              "order_age_under_days": cond.get("order_age_days_lt"),
                              "recipients_fewer_than": cond.get("recipients_lt")}.items() if v is not None}})}])
        pitch = str(json.loads(resp.choices[0].message.content)["pitch"]).strip()
        return pitch if pitch.endswith("?") and 20 < len(pitch) < 260 else template
    except Exception:
        return template


def learn(approval):
    """After a human approval: if the last 3 approvals of this action type share an envelope
    no active rule covers, propose a rule (inactive until a human grants it)."""
    if approval.get("status") != "approved":
        return None
    at = approval["action_type"]
    if at in ("delete_account", "run_command", "write_file"):  # never learn blanket rules for these
        return None
    rows = [_present(r) for r in _query(
        "SELECT * FROM approvals WHERE action_type=? AND status='approved' "
        "ORDER BY decided_at DESC, rowid DESC LIMIT 3", (at,))]
    if len(rows) < 3:
        return None
    if _query("SELECT 1 FROM rules WHERE source='proposed' AND active=0 "
              "AND json_extract(conditions_json, '$.action_type')=?", (at,)):
        return None  # one open proposal per action type
    if _query("SELECT 1 FROM rules WHERE source='dismissed' AND created_at>=? "
              "AND json_extract(conditions_json, '$.action_type')=?",
              (min(r["decided_at"] for r in rows), at)):
        return None  # human said "keep asking" after some of these approvals

    cond = {"action_type": at}
    amounts = [_num(r["payload"].get("amount")) for r in rows]
    if all(a is not None for a in amounts):
        mx = max(amounts)
        if mx >= 500:  # above the hard floor a rule could never apply
            return None
        cond["amount_lt"] = min(_round_up(mx, 10 if mx < 100 else 50), 500)
    ages = [_num(r["context"].get("order_age_days")) for r in rows]
    if all(a is not None for a in ages):
        cond["order_age_days_lt"] = _round_up(max(ages), 5)
    recips = [_num(r["context"].get("recipients")) for r in rows]
    if all(n is not None for n in recips):
        cond["recipients_lt"] = _round_up(max(recips), 10)

    rules = _query("SELECT * FROM rules WHERE active=1")
    for r in rules:
        r["conditions"] = json.loads(r["conditions_json"])
    for row in rows:
        if any(r["action"] == "always_ask" and rule_matches(r["conditions"], at, row["payload"], row["context"])
               for r in rules):
            return None
    if all(any(r["action"] == "auto_approve" and rule_matches(r["conditions"], at, row["payload"], row["context"])
               for r in rules) for row in rows):
        return None  # already covered

    short = NOUNS.get(at, (None, at.replace("_", "-")))[1]
    rule_id = f"{short}-under-{cond['amount_lt']:.0f}" if "amount_lt" in cond else f"{short}-learned"
    if "order_age_days_lt" in cond:
        rule_id += f"-{cond['order_age_days_lt']:.0f}d"
    if _query("SELECT 1 FROM rules WHERE id=?", (rule_id,)):
        rule_id += "-" + uuid.uuid4().hex[:4]
    plural = NOUNS.get(at, (at.replace("_", " ") + "s",))[0]
    name = f"Auto-approve {plural} " + " ".join(
        ([f"under ${cond['amount_lt']:,.0f}"] if "amount_lt" in cond else [])
        + ([f"within {cond['order_age_days_lt']:.0f} days"] if "order_age_days_lt" in cond else [])
        + ([f"to fewer than {cond['recipients_lt']:.0f} recipients"] if "recipients_lt" in cond else []))
    stored = {**cond, "pitch": _pitch(at, len(rows), cond)}
    _exec("INSERT INTO rules VALUES (?,?,?,?,?,?,?,?)",
          (rule_id, name.strip(), json.dumps(stored), "auto_approve", "proposed", 0, 0, _iso(_now())))
    return _present_rule(_query("SELECT * FROM rules WHERE id=?", (rule_id,))[0])


def dismiss_rule(rule_id):
    """'Keep asking': park the proposal. A new one needs 3 fresh approvals after this moment."""
    if not _exec("UPDATE rules SET source='dismissed', created_at=? WHERE id=? AND source='proposed' AND active=0",
                 (_iso(_now()), rule_id)):
        raise KeyError(rule_id)


# ---------------------------------------------------------------- approvals

_SELECT = ("SELECT a.*, r.name AS rule_name FROM approvals a "
           "LEFT JOIN rules r ON r.id = a.rule_id ")


def _insert_approval(a):
    _exec("""INSERT INTO approvals (id, agent, action_type, payload_json, context_json, reasoning,
             risk_score, risk_reasons_json, impact_if_wrong, status, decided_by, decided_at,
             final_payload_json, rule_id, callback_url, timeout_seconds, on_timeout, created_at, source)
             VALUES (:id,:agent,:action_type,:payload_json,:context_json,:reasoning,:risk_score,
             :risk_reasons_json,:impact_if_wrong,:status,:decided_by,:decided_at,
             :final_payload_json,:rule_id,:callback_url,:timeout_seconds,:on_timeout,:created_at,:source)""", a)


def create_approval(agent, action_type, payload, context, reasoning="", callback_url=None,
                    timeout_seconds=300, on_timeout="deny", created_at=None, source=None):
    payload, context = payload or {}, context or {}
    source = (source or "http").strip().lower()
    if source not in SOURCES:
        source = "http"
    risk = score_risk(action_type, payload, context)
    verdict, rule = evaluate(action_type, payload, context, risk)
    auto = verdict == "auto"
    # Auto-approvals stay instant; the LLM only explains what a human will look at.
    expl = (explain(action_type, payload, context, risk) if auto
            else llm_explain(action_type, payload, context, reasoning, risk))
    reasons = expl["reasons"]
    if risk["floor"] and not auto:
        floor = next(s for s in risk["signals"] if s.startswith("Hard floor"))
        if floor not in reasons:
            reasons = [floor] + reasons[:3]
    if verdict == "escalate" and rule:
        reasons = [f"Policy: {rule['name']}"] + reasons[:3]
    now = _iso(_now())
    row = {
        "id": "apr_" + uuid.uuid4().hex[:8], "agent": agent, "action_type": action_type,
        "payload_json": json.dumps(payload), "context_json": json.dumps(context),
        "reasoning": reasoning or "", "risk_score": risk["score"],
        "risk_reasons_json": json.dumps(reasons), "impact_if_wrong": expl["impact_if_wrong"],
        "status": "auto_approved" if auto else "pending",
        "decided_by": "rule" if auto else None, "decided_at": now if auto else None,
        "final_payload_json": json.dumps(payload) if auto else None,
        "rule_id": rule["id"] if auto else None, "callback_url": callback_url,
        "timeout_seconds": timeout_seconds, "on_timeout": on_timeout,
        "created_at": created_at or now, "source": source,
    }
    _insert_approval(row)
    if auto:
        _exec("UPDATE rules SET times_used = times_used + 1 WHERE id=?", (rule["id"],))
    return get_approval(row["id"])


def _is_expired(row):
    created = datetime.fromisoformat(row["created_at"])
    return _now() > created + timedelta(seconds=row["timeout_seconds"] or 300)


def expire_stale():
    """Flip pending approvals past their timeout to timed_out. Returns them."""
    out = []
    for r in _query("SELECT * FROM approvals WHERE status='pending'"):
        if _is_expired(r) and _exec(
                "UPDATE approvals SET status='timed_out', decided_by='timeout', decided_at=? "
                "WHERE id=? AND status='pending'", (_iso(_now()), r["id"])):
            out.append(get_approval(r["id"]))
    return out


def get_approval(approval_id):
    rows = _query(_SELECT + "WHERE a.id=?", (approval_id,))
    return _present(rows[0]) if rows else None


def list_approvals(status=None, limit=50):
    expire_stale()
    sql, args = _SELECT, []
    if status:
        sql += "WHERE a.status=? "
        args.append(status)
    sql += "ORDER BY a.created_at DESC, a.rowid DESC LIMIT ?"
    return [_present(r) for r in _query(sql, args + [limit])]


def agents():
    """Every agent that has called Mandate, derived from the approvals table."""
    expire_stale()
    out = {}
    for r in _query("SELECT agent, source, status, created_at FROM approvals "
                    "ORDER BY created_at DESC, rowid DESC"):
        a = out.setdefault(r["agent"], {"name": r["agent"], "source": r["source"] or "http",
                                        "actions": 0, "paused": 0, "last_seen": r["created_at"],
                                        "_recent": []})
        a["actions"] += 1
        a["paused"] += r["status"] == "pending"
        if len(a["_recent"]) < 50:
            a["_recent"].append(r["status"] == "auto_approved")
    for a in out.values():
        recent = a.pop("_recent")
        a["autonomy"] = round(100 * sum(recent) / len(recent))
        a["status"] = "paused" if a["paused"] else "idle"
    return sorted(out.values(), key=lambda a: a["last_seen"], reverse=True)


def decide(approval_id, decision, final_payload=None, decided_by="human"):
    """Record a human decision. KeyError=missing, Conflict=not pending, ValueError=bad input."""
    status = DECISIONS.get((decision or "").lower())
    if not status:
        raise ValueError("decision must be approved, denied or edited")
    if status == "edited" and not isinstance(final_payload, dict):
        raise ValueError("edited decisions need a final_payload object")
    rows = _query("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not rows:
        raise KeyError(approval_id)
    row = rows[0]
    if row["status"] != "pending" or _is_expired(row):
        raise Conflict(f"approval is already {row['status']}")
    final = (final_payload if status == "edited"
             else json.loads(row["payload_json"]) if status == "approved" else None)
    n = _exec("UPDATE approvals SET status=?, decided_by=?, decided_at=?, final_payload_json=? "
              "WHERE id=? AND status='pending'",
              (status, decided_by or "human", _iso(_now()),
               json.dumps(final) if final is not None else None, approval_id))
    if not n:
        raise Conflict("approval was just decided by someone else")
    return get_approval(approval_id)


# ---------------------------------------------------------------- boot + seed

def init_db():
    with closing(_conn()) as c:
        c.executescript(SCHEMA)
        if "source" not in [r["name"] for r in c.execute("PRAGMA table_info(approvals)")]:
            c.execute("ALTER TABLE approvals ADD COLUMN source TEXT")  # Phase 6 migration
        c.commit()
    if not _query("SELECT 1 FROM rules LIMIT 1"):
        _seed()


def reset_db():
    """Wipe everything and reseed, for demo retakes."""
    with closing(_conn()) as c:
        c.execute("DELETE FROM approvals")
        c.execute("DELETE FROM rules")
        c.commit()
    _seed()


def _seed():
    now = _iso(_now())
    _exec("INSERT INTO rules VALUES (?,?,?,?,?,?,?,?)",
          ("small-refund-30d", "Auto-approve refunds under $50 within 30 days",
           json.dumps({"action_type": "issue_refund", "amount_lt": 50, "order_age_days_lt": 30}),
           "auto_approve", "seed", 0, 1, now))
    _exec("INSERT INTO rules VALUES (?,?,?,?,?,?,?,?)",
          ("ask-delete-account", "Always ask before deleting an account",
           json.dumps({"action_type": "delete_account"}), "always_ask", "seed", 0, 1, now))
    _exec("INSERT INTO rules VALUES (?,?,?,?,?,?,?,?)",
          ("readonly-commands", "Auto-approve read-only shell commands",
           json.dumps({"action_type": "run_command", "readonly_command": True}),
           "auto_approve", "seed", 0, 1, now))

    # (agent, action, payload, context, reasoning, status, decided_by, hours_ago, latency_s, final)
    history = [
        ("refundbot", "issue_refund", {"order_id": "1198", "amount": 20, "reason": "arrived damaged"},
         {"order_age_days": 3, "policy_limit_days": 30}, "Item arrived broken.",
         "auto_approved", "rule", 5.0, 0, None),
        ("refundbot", "issue_refund", {"order_id": "1187", "amount": 35, "reason": "wrong size"},
         {"order_age_days": 12, "policy_limit_days": 30}, "Customer ordered the wrong size.",
         "auto_approved", "rule", 4.2, 0, None),
        ("refundbot", "email_customer", {"email": "sara@example.com", "subject": "Your refund is on its way",
                                         "body": "Hi Sara, your refund has been processed."},
         {"recipients": 1}, "Confirming the refund to the customer.", "approved", "hamza", 3.6, 14, None),
        ("refundbot", "issue_refund", {"order_id": "1102", "amount": 180, "reason": "changed mind"},
         {"order_age_days": 40, "policy_limit_days": 30}, "Customer wants money back after 40 days.",
         "denied", "hamza", 2.9, 22, None),
        ("n8n-support", "issue_refund", {"order_id": "1155", "amount": 320, "reason": "late delivery"},
         {"order_age_days": 20, "policy_limit_days": 30}, "Parcel arrived two weeks late.",
         "edited", "hamza", 2.1, 31, {"order_id": "1155", "amount": 250, "reason": "late delivery"}),
        ("refundbot", "delete_account", {"user_id": "u_5530"},
         {}, "User asked to be removed.", "denied", "hamza", 1.4, 9, None),
        ("make-invoicing", "issue_refund", {"order_id": "1210", "amount": 42, "reason": "duplicate charge"},
         {"order_age_days": 2, "policy_limit_days": 30}, "Customer was billed twice.",
         "auto_approved", "rule", 1.0, 0, None),
        ("zapier-leads", "email_customer", {"email": "team@acme.example", "subject": "Quick follow-up",
                                            "body": "Hi, following up on your demo request."},
         {"recipients": 1}, "Lead asked for a follow-up.", "approved", "hamza", 0.7, 11, None),
        ("claude-code", "run_command", {"command": "git push --force origin main"},
         {}, "Pushing the rebased branch.", "denied", "hamza", 0.4, 6, None),
        ("claude-code", "run_command", {"command": "git status"},
         {}, "Checking the working tree before committing.", "auto_approved", "rule", 0.3, 0, None),
    ]
    seed_sources = {"refundbot": "python", "n8n-support": "n8n", "make-invoicing": "make",
                    "zapier-leads": "zapier", "claude-code": "claude_code"}
    for agent, action, payload, ctx, why, status, by, hrs, lat, final in history:
        created = _now() - timedelta(hours=hrs)
        risk = score_risk(action, payload, ctx)
        expl = explain(action, payload, ctx, risk)
        auto = status == "auto_approved"
        _insert_approval({
            "id": "apr_" + uuid.uuid4().hex[:8], "agent": agent, "action_type": action,
            "payload_json": json.dumps(payload), "context_json": json.dumps(ctx), "reasoning": why,
            "risk_score": risk["score"], "risk_reasons_json": json.dumps(expl["reasons"]),
            "impact_if_wrong": expl["impact_if_wrong"], "status": status, "decided_by": by,
            "decided_at": _iso(created + timedelta(seconds=lat)),
            "final_payload_json": json.dumps(final or payload) if status in ("approved", "edited", "auto_approved") else None,
            "rule_id": ("readonly-commands" if action == "run_command" else "small-refund-30d") if auto else None,
            "callback_url": None,
            "timeout_seconds": 300, "on_timeout": "deny", "created_at": _iso(created),
            "source": seed_sources.get(agent, "http"),
        })
    _exec("UPDATE rules SET times_used=3 WHERE id='small-refund-30d'")
    _exec("UPDATE rules SET times_used=1 WHERE id='readonly-commands'")
