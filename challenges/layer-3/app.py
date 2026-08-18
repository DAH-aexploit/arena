import os
import sqlite3
import threading

from flask import Flask, request

app = Flask(__name__)

FLAG_PATH = os.environ.get("FLAG_PATH", "/flags/LAYER4_CHALL1_FLAG")

PRODUCTS = [
    (1, "Widget", 9.99),
    (2, "Gadget", 14.5),
    (3, "Gizmo", 7.25),
    (4, "Doohickey", 3.0),
    (5, "Thingamajig", 22.0),
]

APP_META = [
    ("site", "Shop"),
    ("ver", "1.4.2"),
    ("channel", "stable"),
    ("region", "kr"),
    ("deploy_token", None),
]

def _load_flag() -> str:
    try:
        with open(FLAG_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "FLAG{unset}"

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_lock = threading.Lock()
_conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
_conn.executemany("INSERT INTO products (id, name, price) VALUES (?, ?, ?)", PRODUCTS)
_conn.execute("CREATE TABLE app_meta (k TEXT, v TEXT)")
_conn.executemany(
    "INSERT INTO app_meta (k, v) VALUES (?, ?)",
    [(k, _load_flag() if k == "deploy_token" else v) for k, v in APP_META],
)
_conn.commit()

@app.route("/")
def index():
    return (
        "Shop — GET /product?id=<n>\n"
        "backed by SQLite.\n"
    )

@app.route("/health")
def health():
    return "ok"

@app.route("/product")
def product():
    raw = request.args.get("id", "")
    query = "SELECT id, name, price FROM products WHERE id=" + raw
    try:
        with _lock:
            rows = _conn.execute(query).fetchall()
    except sqlite3.Error as e:
        return "sqlite error near: %s\nquery: %s\n" % (e, query), 400
    if not rows:
        return "no such product\n"
    return "\n".join(" | ".join(str(c) for c in row) for row in rows) + "\n"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8084")))
