"""RefundBot: a demo support agent that must ask Mandate before risky actions.

    python -m agents.refundbot --scenario small   # $20 refund, auto-approved, never pauses
    python -m agents.refundbot --scenario medium  # $260 refund, pauses; approve 3x and Mandate proposes a rule
    python -m agents.refundbot --scenario large   # $9,400 refund, pauses for a human

Reads MANDATE_URL, MANDATE_API_KEY, GROQ_API_KEY from the environment.
"""
import argparse
import json
import os

from sdk import ApprovalDenied, ApprovalTimeout, Mandate

MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
POLICY_LIMIT_DAYS = 30

FAKE_ORDERS = {
    "1201": {"customer": "Sara Khan", "email": "sara.khan@example.com", "user_id": "u_1201",
             "total": 20, "age_days": 5, "prior_refunds_60d": 0, "item": "Phone case"},
    "4471": {"customer": "Ali Raza", "email": "ali.raza@example.com", "user_id": "u_4471",
             "total": 9400, "age_days": 94, "prior_refunds_60d": 3, "item": "Laptop bundle (x4)"},
    "1188": {"customer": "Usman Tariq", "email": "usman.t@example.com", "user_id": "u_1188",
             "total": 45, "age_days": 12, "prior_refunds_60d": 0, "item": "Headphones"},
    "3302": {"customer": "Ayesha Malik", "email": "ayesha.m@example.com", "user_id": "u_3302",
             "total": 260, "age_days": 21, "prior_refunds_60d": 1, "item": "Smartwatch"},
    "2290": {"customer": "Bilal Ahmed", "email": "bilal.a@example.com", "user_id": "u_2290",
             "total": 15, "age_days": 40, "prior_refunds_60d": 0, "item": "USB cable"},
}

SCENARIOS = {
    "small": ("1201", "Hi, my phone case from order #1201 arrived cracked. Can I get my $20 back?"),
    "medium": ("3302", "Hi, it's Ayesha. The smartwatch from order #3302 stopped charging after two weeks. "
                       "Can I get my $260 back?"),
    "large": ("4471", "This is Ali Raza. Order #4471 was a disaster. I want a full refund of $9,400 "
                      "right now or I'm going to the press."),
}

mandate = Mandate(agent_name="refundbot")


@mandate.requires_approval(action_type="issue_refund")
def issue_refund(order_id, amount, reason):
    print(f"  -> Refunded ${float(amount):,.2f} on order #{order_id} ({reason})")
    return f"Refund of ${float(amount):,.2f} issued for order #{order_id}."


@mandate.requires_approval(action_type="email_customer")
def email_customer(email, subject, body):
    print(f"  -> Emailed {email}: {subject}")
    return f"Email sent to {email}."


@mandate.requires_approval(action_type="delete_account")
def delete_account(user_id):
    print(f"  -> Deleted account {user_id}")
    return f"Account {user_id} deleted."


TOOL_CALLS = []
TOOLS = {"issue_refund": issue_refund, "email_customer": email_customer, "delete_account": delete_account}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "issue_refund", "description": "Refund money to the customer for an order.",
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string"}, "amount": {"type": "number"},
            "reason": {"type": "string", "description": "One short sentence on why."}},
            "required": ["order_id", "amount", "reason"]}}},
    {"type": "function", "function": {
        "name": "email_customer", "description": "Send an email to a customer.",
        "parameters": {"type": "object", "properties": {
            "email": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["email", "subject", "body"]}}},
    {"type": "function", "function": {
        "name": "delete_account", "description": "Permanently delete a customer account.",
        "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}},
                       "required": ["user_id"]}}},
]


def context_for(order_id):
    o = FAKE_ORDERS.get(str(order_id).lstrip("#"), {})
    return {"customer": o.get("customer"), "order_age_days": o.get("age_days"),
            "policy_limit_days": POLICY_LIMIT_DAYS, "prior_refunds_60d": o.get("prior_refunds_60d"),
            "order_total": o.get("total"), "item": o.get("item")}


def run_tool(name, args):
    """Call a gated tool. Mandate's decision comes back to the model as the tool result."""
    order_id = args.get("order_id") or next(
        (k for k, o in FAKE_ORDERS.items() if o["email"] == args.get("email") or o["user_id"] == args.get("user_id")), "")
    reasoning = args.get("reason") or f"RefundBot wants to {name.replace('_', ' ')} while handling a support ticket."
    TOOL_CALLS.append(name)
    try:
        return TOOLS[name](**args, mandate_context=context_for(order_id), mandate_reasoning=reasoning)
    except ApprovalDenied as e:
        return f"DENIED by a human: {e}. Do not retry; tell the customer politely."
    except ApprovalTimeout as e:
        return f"No human decided in time: {e}. Do not retry."


def system_prompt(order_id):
    o = FAKE_ORDERS[order_id]
    return ("You are RefundBot, a customer support agent for an electronics store. "
            f"Order #{order_id}: customer {o['customer']} ({o['email']}), item {o['item']}, "
            f"total ${o['total']:,}, placed {o['age_days']} days ago, "
            f"{o['prior_refunds_60d']} refunds in the last 60 days. Refund policy: {POLICY_LIMIT_DAYS} days. "
            "You never enforce policy yourself: every action goes through Mandate, an approval layer "
            "where rules or a human decide. So always call issue_refund exactly once for the amount the "
            "customer asks for, even if it looks outside policy, and put your concerns in the reason. "
            "Do not email the customer or delete accounts unless explicitly asked. "
            "After the tool returns, reply to the customer in two sentences.")


def run_with_groq(order_id, message):
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"], timeout=20)
    messages = [{"role": "system", "content": system_prompt(order_id)},
                {"role": "user", "content": message}]
    for _ in range(4):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOL_SPECS,
                                              tool_choice="auto", temperature=0)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            print(f"RefundBot calls {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
            result = run_tool(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    return "(stopped after 4 steps)"


def run_scripted(order_id, message):
    """Same flow without the LLM, so the demo still runs if Groq is down."""
    o = FAKE_ORDERS[order_id]
    args = {"order_id": order_id, "amount": o["total"],
            "reason": {"1201": "Customer reports the order arrived damaged.",
                       "3302": "Smartwatch stopped charging within policy window."}.get(
                order_id, "Customer demands a full refund and threatens to escalate.")}
    print(f"RefundBot calls issue_refund({json.dumps(args)})")
    result = run_tool("issue_refund", args)
    if result.startswith(("DENIED", "No human")):
        return f"Sorry {o['customer']}, a team member reviewed this and we can't issue that refund right now."
    return f"{result} Thanks for your patience, {o['customer']}."


def main():
    p = argparse.ArgumentParser(description="RefundBot demo agent")
    p.add_argument("--scenario", choices=SCENARIOS, default="small")
    p.add_argument("--no-llm", action="store_true", help="skip Groq and run the scripted plan")
    a = p.parse_args()
    order_id, message = SCENARIOS[a.scenario]
    print(f"Customer: {message}\n")
    reply = None
    if not a.no_llm and os.environ.get("GROQ_API_KEY"):
        try:
            reply = run_with_groq(order_id, message)
        except Exception as e:
            if TOOL_CALLS:  # a gated action already went through Mandate; don't repeat it
                reply = f"(Groq failed after the action: {type(e).__name__})"
            else:
                print(f"(Groq unavailable: {type(e).__name__} — using scripted plan)")
    if reply is None:
        reply = run_scripted(order_id, message)
    print(f"\nRefundBot: {reply}")


if __name__ == "__main__":
    main()
