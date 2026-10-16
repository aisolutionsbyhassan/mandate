import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import core

subscribers = set()
background = set()


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

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "mandate"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "app.html", {})


@app.post("/v1/approvals", dependencies=[Depends(require_key)])
async def create_approval(body: ApprovalIn):
    # run in a thread: the Groq explanation can take up to 3s and must not block SSE
    a = await run_in_threadpool(core.create_approval, body.agent, body.action_type, body.payload,
                                body.context, body.reasoning, body.callback_url,
                                body.timeout_seconds, body.on_timeout)
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
    task = asyncio.create_task(after_decision(a, body.note))
    background.add(task)
    task.add_done_callback(background.discard)
    return a


async def after_decision(a, note):
    """Off the request path: tell the agent, then let the learning engine look for a pattern."""
    await notify_callback(a, note)
    try:
        proposal = await run_in_threadpool(core.learn, a)
        if proposal:
            publish("policy", proposal)
    except Exception:
        pass


@app.get("/v1/policies", dependencies=[Depends(require_key)])
async def get_policies():
    return core.policies()


@app.post("/v1/policies/{rule_id}/dismiss", dependencies=[Depends(require_key)])
async def dismiss_policy(rule_id: str):
    try:
        core.dismiss_rule(rule_id)
    except KeyError:
        raise HTTPException(404, "no pending proposal with that id")
    publish("policy", {"id": rule_id, "source": "dismissed"})
    return {"id": rule_id, "status": "dismissed"}


@app.post("/v1/policies/{rule_id}/grant", dependencies=[Depends(require_key)])
async def grant_policy(rule_id: str):
    try:
        rule = core.grant_rule(rule_id)
    except KeyError:
        raise HTTPException(404, "no pending proposal with that id")
    publish("policy", rule)
    return rule


@app.post("/demo/reset", dependencies=[Depends(require_key)])
async def demo_reset():
    core.reset_db()
    publish("reset", {})
    return {"status": "reset"}


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
