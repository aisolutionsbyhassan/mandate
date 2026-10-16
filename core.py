"""Mandate core: SQLite, deterministic risk scoring, policy engine.

Mandate never makes a judgment call. It routes: rules a human granted
auto-approve, everything else escalates to a human.
(The Groq explanation layer arrives in Phase 3, the learning engine in Phase 4.)
"""
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("MANDATE_DB", "mandate.db")

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

IRREVERSIBILITY = {"delete_account": 40, "issue_refund": 30, "email_customer": 10}
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
    out["description"] = describe_conditions(out["conditions"])
    return out


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

    return {"score": min(100, score), "signals": signals, "floor": floor}


def explain(action_type, payload, context, risk):
    """Reasons + impact line. Fallback built from deterministic signals;
    the Groq layer (Phase 3) will sit in front of this and fall back to it."""
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
    else:
        impact = "The action runs immediately and may not be reversible."
    return {"reasons": reasons, "impact_if_wrong": impact}


# ---------------------------------------------------------------- policy engine

def describe_conditions(cond):
    parts = []
    if cond.get("action_type"):
        parts.append(cond["action_type"].replace("_", " "))
    if "amount_lt" in cond:
        parts.append(f"amount under ${cond['amount_lt']:,.0f}")
    if "order_age_days_lt" in cond:
        parts.append(f"order under {cond['order_age_days_lt']:.0f} days old")
    return ", ".join(parts) or "any action"


def rule_matches(cond, action_type, payload, context):
    if cond.get("action_type") and cond["action_type"] != action_type:
        return False
    if "amount_lt" in cond:
        amount = _num((payload or {}).get("amount"))
        if amount is None or not amount < cond["amount_lt"]:
            return False
    if "order_age_days_lt" in cond:
        age = _num((context or {}).get("order_age_days"))
        if age is None or not age < cond["order_age_days_lt"]:
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


# ---------------------------------------------------------------- approvals

_SELECT = ("SELECT a.*, r.name AS rule_name FROM approvals a "
           "LEFT JOIN rules r ON r.id = a.rule_id ")


def _insert_approval(a):
    _exec("""INSERT INTO approvals (id, agent, action_type, payload_json, context_json, reasoning,
             risk_score, risk_reasons_json, impact_if_wrong, status, decided_by, decided_at,
             final_payload_json, rule_id, callback_url, timeout_seconds, on_timeout, created_at)
             VALUES (:id,:agent,:action_type,:payload_json,:context_json,:reasoning,:risk_score,
             :risk_reasons_json,:impact_if_wrong,:status,:decided_by,:decided_at,
             :final_payload_json,:rule_id,:callback_url,:timeout_seconds,:on_timeout,:created_at)""", a)


def create_approval(agent, action_type, payload, context, reasoning="", callback_url=None,
                    timeout_seconds=300, on_timeout="deny", created_at=None):
    payload, context = payload or {}, context or {}
    risk = score_risk(action_type, payload, context)
    verdict, rule = evaluate(action_type, payload, context, risk)
    expl = explain(action_type, payload, context, risk)
    reasons = expl["reasons"]
    if verdict == "escalate" and rule:
        reasons = [f"Policy: {rule['name']}"] + reasons[:3]
    auto = verdict == "auto"
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
        "created_at": created_at or now,
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
        c.commit()
    if not _query("SELECT 1 FROM rules LIMIT 1"):
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
    ]
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
            "rule_id": "small-refund-30d" if auto else None, "callback_url": None,
            "timeout_seconds": 300, "on_timeout": "deny", "created_at": _iso(created),
        })
    _exec("UPDATE rules SET times_used=2 WHERE id='small-refund-30d'")
