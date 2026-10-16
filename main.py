import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

import core

subscribers = set()


def publish(event_type, data):
    for q in list(subscribers):
        try:
            q.put_nowait({"type": event_type, "data": data})
        except asyncio.QueueFull:
            pass


async def notify_callback(approval, note=None):
    """POST the decision to the agent's callback_url. Never raises."""
    url = approval.get("callback_url")
    if not url:
        return
    body = {"id": approval["id"], "status": approval["status"],
            "final_payload": approval.get("final_payload"),
            "decided_by": approval.get("decided_by"), "note": note}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=body)
    except Exception:
        pass


async def expiry_loop():
    while True:
        await asyncio.sleep(5)
        try:
            for a in core.expire_stale():
                publish("decision", a)
                await notify_callback(a, "timed out waiting for a human")
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_app):
    core.init_db()
    task = asyncio.create_task(expiry_loop())
    yield
    task.cancel()


app = FastAPI(title="Mandate", lifespan=lifespan)


def require_key(x_mandate_key: Optional[str] = Header(None)):
    expected = os.environ.get("MANDATE_API_KEY", "")
    if not expected:
        raise HTTPException(503, "MANDATE_API_KEY is not configured on the server")
    if not x_mandate_key or not hmac.compare_digest(x_mandate_key.encode(), expected.encode()):
        raise HTTPException(401, "invalid or missing x-mandate-key header")


class ApprovalIn(BaseModel):
    agent: str
    action_type: str
    payload: dict = {}
    context: dict = {}
    reasoning: str = ""
    callback_url: Optional[str] = None
    timeout_seconds: int = Field(300, ge=1, le=86400)
    on_timeout: str = "deny"


class DecideIn(BaseModel):
    decision: str
    final_payload: Optional[dict] = None
    decided_by: str = "human"
    note: Optional[str] = None

LANDING = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mandate</title>
<style>
  :root { --bg:#0b0d10; --panel:#12151a; --border:#232830; --text:#e8eaed; --muted:#8b93a1; --accent:#6ee7b7; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: radial-gradient(1200px 600px at 50% -10%, #16202b 0%, var(--bg) 60%);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  main { text-align: center; max-width: 640px; }
  .brand { letter-spacing: .32em; font-weight: 700; font-size: 14px; color: var(--muted); margin-bottom: 40px; }
  .live {
    display: inline-flex; align-items: center; gap: 12px;
    border: 1px solid var(--border); background: var(--panel);
    padding: 10px 18px; border-radius: 999px; font-size: 14px; color: var(--muted); margin-bottom: 28px;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent);
         box-shadow: 0 0 0 0 rgba(110,231,183,.6); animation: pulse 1.8s infinite; }
  @keyframes pulse { 70% { box-shadow: 0 0 0 10px rgba(110,231,183,0); } 100% { box-shadow: 0 0 0 0 rgba(110,231,183,0); } }
  h1 { font-size: clamp(36px, 7vw, 60px); line-height: 1.05; margin: 0 0 18px; letter-spacing: -.02em; }
  h1 span { color: var(--accent); }
  p.tag { font-size: 19px; color: var(--muted); margin: 0 0 56px; }
  footer { border-top: 1px solid var(--border); padding-top: 22px; font-size: 13px; color: var(--muted); letter-spacing: .04em; }
</style>
</head>
<body>
<main>
  <div class="brand">MANDATE</div>
  <div class="live"><span class="dot"></span>All systems operational</div>
  <h1>Mandate is <span>live</span></h1>
  <p class="tag">Human-in-the-loop control layer for AI agents</p>
  <footer>Agentika 2.0 · NaSCon 2026 · FAST NUCES Islamabad</footer>
</main>
</body>
</html>
"""


@app.get("/health")
def health():
    return {"status": "ok", "service": "mandate"}


@app.get("/", response_class=HTMLResponse)
def index():
    return LANDING


@app.post("/v1/approvals", dependencies=[Depends(require_key)])
async def create_approval(body: ApprovalIn):
    a = core.create_approval(body.agent, body.action_type, body.payload, body.context,
                             body.reasoning, body.callback_url, body.timeout_seconds,
                             body.on_timeout)
    publish("approval", a)
    return {"id": a["id"], "status": a["status"], "risk_score": a["risk_score"],
            "risk_reasons": a["risk_reasons"]}


@app.get("/v1/approvals/{approval_id}", dependencies=[Depends(require_key)])
async def get_approval(approval_id: str):
    core.expire_stale()
    a = core.get_approval(approval_id)
    if not a:
        raise HTTPException(404, "approval not found")
    return a


@app.get("/v1/approvals", dependencies=[Depends(require_key)])
async def list_approvals(status: Optional[str] = None, limit: int = Query(50, ge=1, le=200)):
    return core.list_approvals(status, limit)


@app.post("/v1/decide/{approval_id}", dependencies=[Depends(require_key)])
async def decide(approval_id: str, body: DecideIn):
    try:
        a = core.decide(approval_id, body.decision, body.final_payload, body.decided_by)
    except KeyError:
        raise HTTPException(404, "approval not found")
    except core.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    publish("decision", a)
    await notify_callback(a, body.note)
    return a


@app.get("/v1/policies", dependencies=[Depends(require_key)])
async def get_policies():
    return core.policies()


@app.post("/v1/policies/{rule_id}/grant", dependencies=[Depends(require_key)])
async def grant_policy(rule_id: str):
    try:
        rule = core.grant_rule(rule_id)
    except KeyError:
        raise HTTPException(404, "no pending proposal with that id")
    publish("policy", rule)
    return rule


@app.get("/events")
async def events(request: Request):
    q = asyncio.Queue(maxsize=100)
    subscribers.add(q)

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
