"""Mandate SDK: gate any Python function behind human approval.

    mandate = Mandate("http://localhost:8000", api_key, agent_name="refundbot")

    @mandate.requires_approval(action_type="issue_refund")
    def issue_refund(order_id, amount, reason): ...

    issue_refund(order_id="4471", amount=9400, reason="...",
                 mandate_context={"order_age_days": 94}, mandate_reasoning="...")

The call blocks until Mandate (a rule or a human) decides. Fails closed: if
Mandate can't be reached, the function does NOT run.
"""
import functools
import inspect
import os
import sys
import time

import httpx


class ApprovalDenied(Exception):
    pass


class ApprovalTimeout(Exception):
    pass


class Mandate:
    def __init__(self, base_url=None, api_key=None, agent_name="agent",
                 timeout_seconds=300, poll_interval=2):
        self.base_url = (base_url or os.environ.get("MANDATE_URL", "http://localhost:8000")).rstrip("/")
        self.api_key = api_key or os.environ.get("MANDATE_API_KEY", "")
        self.agent_name = agent_name
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self.client = httpx.Client(base_url=self.base_url, timeout=10,
                                   headers={"x-mandate-key": self.api_key})

    def requires_approval(self, action_type):
        def decorator(fn):
            sig = inspect.signature(fn)

            @functools.wraps(fn)
            def wrapper(*args, mandate_context=None, mandate_reasoning="", **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                payload = dict(bound.arguments)
                a = self._ask(action_type, payload, mandate_context or {}, mandate_reasoning)

                if a["status"] == "auto_approved":
                    print(f"AUTO-APPROVED · {a['id']} · risk {a['risk_score']} · running now")
                    return fn(**payload)

                a = self._wait(a["id"])
                who = a.get("decided_by") or "human"
                if a["status"] == "approved":
                    print(f"APPROVED by {who} — resuming")
                    return fn(**payload)
                if a["status"] == "edited":
                    final = {k: v for k, v in (a.get("final_payload") or {}).items() if k in sig.parameters}
                    changes = ", ".join(f"{k}={v}" for k, v in final.items() if payload.get(k) != v)
                    print(f"EDITED by {who} — resuming with {changes or 'no changes'}")
                    return fn(**{**payload, **final})
                if a["status"] == "denied":
                    print(f"DENIED by {who} — not running {fn.__name__}")
                    raise ApprovalDenied(f"{action_type} denied by {who}")
                print(f"TIMED OUT — nobody decided on {a['id']}, not running {fn.__name__}")
                raise ApprovalTimeout(f"{action_type} timed out waiting for a human")

            return wrapper
        return decorator

    def _ask(self, action_type, payload, context, reasoning):
        r = self.client.post("/v1/approvals", json={
            "agent": self.agent_name, "action_type": action_type, "payload": payload,
            "context": context, "reasoning": reasoning, "timeout_seconds": self.timeout_seconds,
        })
        r.raise_for_status()
        return r.json()

    def _wait(self, approval_id):
        start, last_poll = time.time(), 0.0
        while True:
            elapsed = int(time.time() - start)
            sys.stdout.write(f"\rWaiting for human approval · {approval_id} · {elapsed // 60}:{elapsed % 60:02d} ")
            sys.stdout.flush()
            if time.time() - last_poll >= self.poll_interval:
                last_poll = time.time()
                try:
                    a = self.client.get(f"/v1/approvals/{approval_id}").json()
                    if a.get("status") not in (None, "pending"):
                        sys.stdout.write("\n")
                        return a
                except httpx.HTTPError:
                    pass  # network blip: keep waiting, never run unapproved
            time.sleep(1)
