import base64
import json
import os
from flask import Flask, request

app = Flask(__name__)
FLAG_FILE = os.environ.get("FLAG_FILE", "/flags/LAYER3_CHALL1_FLAG")

def _read_flag() -> str:
    try:
        with open(FLAG_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

@app.route("/")
def index():
    return "Admin Portal — admin area at /admin. Get a session at /login first.\n"

@app.route("/health")
def health():
    return "ok"

@app.route("/login")
def login():
    token = base64.b64encode(
        json.dumps({"user": "guest", "role": "user"}).encode()
    ).decode()
    resp = app.make_response(
        "login form: session issued as cookie 'session' (admin area at /admin)\n"
    )
    resp.set_cookie("session", token)
    return resp

@app.route("/admin")
def admin():
    raw = request.cookies.get("session")
    if not raw:
        return "unauthorized: no session — authenticate at /login first\n", 401
    try:
        data = json.loads(base64.b64decode(raw).decode())
    except Exception:
        return "bad session\n", 400
    if data.get("role") == "admin":
        flag = _read_flag()
        return (flag + "\n") if flag else ("no flag set\n", 500)
    return f"forbidden: session role \"{data.get('role')}\" is not \"admin\"\n", 403

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8083")))
