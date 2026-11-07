from flask import Flask, send_from_directory, request, jsonify
from pathlib import Path
from contextlib import contextmanager
import sqlite3
import json
import hmac
import hashlib
import time
from urllib.parse import parse_qsl

BASE_DIR = Path.home() / "mafioso_bot"
DB_PATH = BASE_DIR / "mafioso.db"
ENV_PATH = BASE_DIR / ".env"

def load_env():
    data = {}
    for line in ENV_PATH.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data

BOT_TOKEN = load_env().get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not found in .env")

app = Flask(__name__)

@contextmanager
def get_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    with get_db() as db:
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(users)")
        }
        if "power" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN power INTEGER NOT NULL DEFAULT 1"
            )

def verify_telegram(init_data):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None

        auth_date = int(pairs.get("auth_date", "0"))
        now = int(time.time())
        if auth_date > now + 60 or now - auth_date > 86400:
            return None

        check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(pairs.items())
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        user = json.loads(pairs.get("user", "{}"))
        return int(user["id"]), user.get("username", "")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None

def authenticated_user():
    header = request.headers.get("Authorization", "")
    if not header.startswith("tma "):
        return None
    return verify_telegram(header[4:])

def user_row(db, user_id, username):
    db.execute(
        "INSERT INTO users (user_id, username) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (user_id, username)
    )
    return db.execute(
        "SELECT balance, energy, power, banned FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

@app.route("/")
def home():
    return send_from_directory(str(Path(__file__).parent), "index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "Mafioso"})

@app.route("/api/state")
def state():
    identity = authenticated_user()
    if not identity:
        return jsonify({"error": "Unauthorized"}), 401

    user_id, username = identity
    with get_db() as db:
        row = user_row(db, user_id, username)
        if row["banned"]:
            return jsonify({"error": "Banned"}), 403

        return jsonify({
            "user_id": user_id,
            "username": username,
            "balance": row["balance"],
            "energy": row["energy"],
            "power": row["power"],
            "upgrade_cost": row["power"] * 100
        })

@app.route("/api/tap", methods=["POST"])
def tap():
    identity = authenticated_user()
    if not identity:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        count = int(data.get("count", 1))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid count"}), 400

    if count < 1 or count > 20:
        return jsonify({"error": "Invalid count"}), 400

    user_id, username = identity

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = user_row(db, user_id, username)

        if row["banned"]:
            return jsonify({"error": "Banned"}), 403

        accepted = min(count, row["energy"])
        if accepted < 1:
            return jsonify({
                "error": "Not enough energy",
                "balance": row["balance"],
                "energy": row["energy"],
                "power": row["power"]
            }), 400

        earned = accepted * row["power"]
        db.execute(
            "UPDATE users SET balance=balance+?, energy=energy-? "
            "WHERE user_id=?",
            (earned, accepted, user_id)
        )

        updated = db.execute(
            "SELECT balance, energy, power FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        return jsonify({
            "balance": updated["balance"],
            "energy": updated["energy"],
            "power": updated["power"],
            "accepted": accepted
        })

@app.route("/api/upgrade", methods=["POST"])
def upgrade():
    identity = authenticated_user()
    if not identity:
        return jsonify({"error": "Unauthorized"}), 401

    user_id, username = identity

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = user_row(db, user_id, username)

        if row["banned"]:
            return jsonify({"error": "Banned"}), 403

        cost = row["power"] * 100

        if row["balance"] < cost:
            return jsonify({
                "error": "Not enough coins",
                "balance": row["balance"],
                "power": row["power"],
                "upgrade_cost": cost
            }), 400

        db.execute(
            "UPDATE users SET balance=balance-?, power=power+1 "
            "WHERE user_id=?",
            (cost, user_id)
        )

        updated = db.execute(
            "SELECT balance, energy, power FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        return jsonify({
            "balance": updated["balance"],
            "energy": updated["energy"],
            "power": updated["power"],
            "upgrade_cost": updated["power"] * 100
        })

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
