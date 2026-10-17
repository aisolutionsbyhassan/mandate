import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
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


async def _late_callback(approval, note):
    await asyncio.sleep(3)
    await notify_callback(approval, note)


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


SESSION_COOKIE = "mandate_session"
SESSION_TTL = 7 * 24 * 3600


def _secret():
    return (os.environ.get("MANDATE_SESSION_SECRET") or os.environ.get("MANDATE_API_KEY", "")).encode()


def invite_code():
    """Workspace invite code for new accounts. Derived from the server secret, so it needs no
    storage, survives redeploys, and is not the agent API key."""
    h = hmac.new(_secret(), b"mandate-invite-v1", hashlib.sha256).hexdigest()[:12].upper()
    return f"MND-{h[:4]}-{h[4:8]}-{h[8:]}"


def make_session(username):
    payload = f"{username}|{int(time.time()) + SESSION_TTL}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig


def read_session(token):
    """The signed-in user for a session cookie, or None."""
    try:
        raw, sig = (token or "").split(".", 1)
        payload = base64.urlsafe_b64decode(raw.encode()).decode()
        good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        username, exp = payload.rsplit("|", 1)
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return core.get_user(username)
    except (ValueError, UnicodeDecodeError):
        pass
    return None


def require_key(request: Request, x_mandate_key: Optional[str] = Header(None)):
    """Agents send the x-mandate-key header; people send a signed session cookie."""
    expected = os.environ.get("MANDATE_API_KEY", "")
    if not expected:
        raise HTTPException(503, "MANDATE_API_KEY is not configured on the server")
    if x_mandate_key and hmac.compare_digest(x_mandate_key.encode(), expected.encode()):
        return {"kind": "agent"}
    user = read_session(request.cookies.get(SESSION_COOKIE))
    if user:
        return {"kind": "user", "user": user}
    raise HTTPException(401, "sign in, or send a valid x-mandate-key header")


def _set_session(response, request, username):
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(SESSION_COOKIE, make_session(username), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", secure=https)


class ApprovalIn(BaseModel):
    agent: str
    action_type: str
    payload: dict = {}
    context: dict = {}
    reasoning: str = ""
    callback_url: Optional[str] = None
    timeout_seconds: int = Field(300, ge=1, le=86400)
    on_timeout: str = "deny"
    source: Optional[str] = None


class SignupIn(BaseModel):
    username: str
    name: str
    password: str
    invite_code: str


class LoginIn(BaseModel):
    username: str
    password: str


class DecideIn(BaseModel):
    decision: str
    final_payload: Optional[dict] = None
    decided_by: str = "human"
    note: Optional[str] = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "mandate"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "app.html", {})


@app.post("/auth/signup")
async def signup(body: SignupIn, request: Request, response: Response):
    """Creating an account needs the workspace invite code (shown to signed-in people)."""
    if not _secret() or not hmac.compare_digest(body.invite_code.strip().upper().encode(), invite_code().encode()):
        raise HTTPException(403, "that invite code is not valid")
    try:
        user = await run_in_threadpool(core.create_user, body.username, body.name, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except core.Conflict as e:
        raise HTTPException(409, str(e))
    _set_session(response, request, user["username"])
    return user


@app.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    user = await run_in_threadpool(core.verify_user, body.username, body.password)
    if not user:
        raise HTTPException(401, "wrong username or password")
    _set_session(response, request, user["username"])
    return user


@app.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "signed out"}


@app.get("/auth/me")
async def me(request: Request):
    user = read_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(401, "not signed in")
    return user


@app.get("/v1/connect-info")
async def connect_info(who: dict = Depends(require_key)):
    """The agent API key, for the Connect wizard. Only for signed-in people."""
    if who["kind"] != "user":
        raise HTTPException(403, "only signed-in people can view the agent key")
    return {"api_key": os.environ.get("MANDATE_API_KEY", "")}


@app.get("/v1/invite-code")
async def get_invite_code(who: dict = Depends(require_key)):
    """The invite code teammates need to create an account. Only for signed-in people."""
    if who["kind"] != "user":
        raise HTTPException(403, "only signed-in people can view the invite code")
    return {"invite_code": invite_code()}


HOOK_FILES = {"claude_code_hook.py", "cursor_hook.py"}


@app.get("/integrations/{name}", response_class=PlainTextResponse)
def integration_file(name: str):
    """Hook scripts, so the Connect wizard can offer a one-line download. They contain no secrets."""
    if name not in HOOK_FILES:
        raise HTTPException(404, "no such integration file")
    with open(os.path.join(BASE_DIR, "integrations", name), encoding="utf-8") as f:
        return f.read()


@app.post("/v1/approvals", dependencies=[Depends(require_key)])
async def create_approval(body: ApprovalIn):
    # run in a thread: the Groq explanation can take up to 3s and must not block SSE
    a = await run_in_threadpool(core.create_approval, body.agent, body.action_type, body.payload,
                                body.context, body.reasoning, body.callback_url,
                                body.timeout_seconds, body.on_timeout, None, body.source)
    publish("approval", a)
    if a["status"] == "denied" and a.get("callback_url"):
        # blocked by a never-allow rule: tell callback agents too (n8n's Wait node needs a moment to start listening)
        task = asyncio.create_task(_late_callback(a, "blocked by policy"))
        background.add(task)
        task.add_done_callback(background.discard)
    return {"id": a["id"], "status": a["status"], "risk_score": a["risk_score"],
            "risk_reasons": a["risk_reasons"], "rule_id": a["rule_id"], "decided_by": a["decided_by"]}


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


@app.get("/v1/agents", dependencies=[Depends(require_key)])
async def get_agents():
    return await run_in_threadpool(core.agents)


@app.post("/v1/decide/{approval_id}")
async def decide(approval_id: str, body: DecideIn, who: dict = Depends(require_key)):
    # a signed-in person is recorded by their account name, whatever the body says
    decided_by = who["user"]["name"] if who["kind"] == "user" else body.decided_by
    try:
        a = core.decide(approval_id, body.decision, body.final_payload, decided_by)
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


class RuleIn(BaseModel):
    name: str
    action: str
    conditions: dict = {}
    active: Optional[bool] = None


@app.post("/v1/policies", dependencies=[Depends(require_key)])
async def create_policy(body: RuleIn):
    try:
        rule = core.save_rule(None, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    publish("policy", rule)
    return rule


class DraftIn(BaseModel):
    text: str


@app.post("/v1/policies/draft", dependencies=[Depends(require_key)])
async def draft_policy(body: DraftIn):
    """AI translates plain English into a rule. Nothing is saved; a human reviews and grants it."""
    try:
        return await run_in_threadpool(core.draft_rule, body.text)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.put("/v1/policies/{rule_id}", dependencies=[Depends(require_key)])
async def update_policy(rule_id: str, body: RuleIn):
    try:
        rule = core.save_rule(rule_id, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "no editable mandate with that id")
    publish("policy", rule)
    return rule


@app.post("/v1/policies/{rule_id}/enable", dependencies=[Depends(require_key)])
async def enable_policy(rule_id: str):
    return _toggle(rule_id, True)


@app.post("/v1/policies/{rule_id}/disable", dependencies=[Depends(require_key)])
async def disable_policy(rule_id: str):
    return _toggle(rule_id, False)


def _toggle(rule_id, active):
    try:
        rule = core.set_rule_active(rule_id, active)
    except KeyError:
        raise HTTPException(404, "no editable mandate with that id")
    publish("policy", rule)
    return rule


@app.delete("/v1/policies/{rule_id}", dependencies=[Depends(require_key)])
async def delete_policy(rule_id: str):
    try:
        core.delete_rule(rule_id)
    except KeyError:
        raise HTTPException(404, "no editable mandate with that id")
    publish("policy", {"id": rule_id, "source": "deleted"})
    return {"id": rule_id, "status": "deleted"}


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
