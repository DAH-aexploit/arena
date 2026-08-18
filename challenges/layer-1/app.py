import base64
import os
import urllib.request
from flask import Flask, request

app = Flask(__name__)

BANNER = (
    "URL Fetcher — GET /fetch?url=<url>\n"
    "Internal service registry bound to loopback: GET /registry (127.0.0.1 only)\n"
)

@app.route("/")
def index():
    return BANNER

@app.route("/registry")
def registry():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return "forbidden\n", 403
    return base64.b64encode(b"http://helper-box:8080/secret")

@app.route("/health")
def health():
    return "ok"

@app.route("/fetch")
def fetch():
    url = request.args.get("url", "")
    if not url:
        return "usage: /fetch?url=<url>\n", 400
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.read()
    except Exception as e:
        return f"fetch failed: {e}\n", 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8082")))
