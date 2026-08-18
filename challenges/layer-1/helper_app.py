import os
from flask import Flask

app = Flask(__name__)
FLAG_FILE = "/flags/LAYER2_CHALL1_FLAG"

@app.route("/")
def index():
    return "helper-box internal service\n"

@app.route("/secret")
def secret():
    try:
        with open(FLAG_FILE) as f:
            return f.read().strip() + "\n"
    except FileNotFoundError:
        return "no flag set\n"

@app.route("/health")
def health():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
