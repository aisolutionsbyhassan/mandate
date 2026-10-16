from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Mandate")

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
