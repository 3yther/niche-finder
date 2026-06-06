import math
import os
import sqlite3
from datetime import date

import bcrypt
from dotenv import load_dotenv
from flask import (Flask, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_session import Session
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"]      = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
app.config["SESSION_TYPE"]     = "filesystem"
app.config["SESSION_FILE_DIR"] = ".flask_session"
Session(app)

DB_PATH         = "niche_finder.db"
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
GUEST_LIMIT     = 3
FREE_DAILY_LIMIT = 3


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email           TEXT    UNIQUE NOT NULL,
                password        TEXT    NOT NULL,
                plan            TEXT    NOT NULL DEFAULT 'free',
                search_count    INTEGER NOT NULL DEFAULT 0,
                last_reset_date TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.commit()


init_db()


def get_user_by_id(user_id):
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(email):
    return get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def reset_daily_count_if_needed(user):
    """Reset search_count if last_reset_date is not today. Returns current count."""
    today = str(date.today())
    if user["last_reset_date"] != today:
        with get_db() as db:
            db.execute(
                "UPDATE users SET search_count = 0, last_reset_date = ? WHERE id = ?",
                (today, user["id"]),
            )
            db.commit()
        return 0
    return user["search_count"]


def increment_search_count(user_id):
    with get_db() as db:
        db.execute(
            "UPDATE users SET search_count = search_count + 1 WHERE id = ?",
            (user_id,),
        )
        db.commit()


# ── Template context ──────────────────────────────────────────────────────────

@app.context_processor
def inject_user():
    user_id = session.get("user_id")
    if user_id:
        user = get_user_by_id(user_id)
        return {"current_user": dict(user) if user else None}
    return {"current_user": None}


# ── YouTube helpers ───────────────────────────────────────────────────────────

def _youtube():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def _fetch_videos(topic):
    youtube = _youtube()
    search_resp = youtube.search().list(
        q=topic, part="snippet", type="video", maxResults=10
    ).execute()
    items = search_resp.get("items", [])
    if not items:
        return []

    video_ids   = [item["id"]["videoId"]        for item in items]
    channel_ids = [item["snippet"]["channelId"]  for item in items]

    videos_resp  = youtube.videos().list(part="statistics", id=",".join(video_ids)).execute()
    video_stats  = {v["id"]: v.get("statistics", {}) for v in videos_resp.get("items", [])}

    channels_resp = youtube.channels().list(
        part="statistics", id=",".join(set(channel_ids))
    ).execute()
    channel_stats = {c["id"]: c.get("statistics", {}) for c in channels_resp.get("items", [])}

    results = []
    for item in items:
        video_id   = item["id"]["videoId"]
        channel_id = item["snippet"]["channelId"]
        vstats = video_stats.get(video_id, {})
        cstats = channel_stats.get(channel_id, {})
        results.append({
            "title":            item["snippet"]["title"],
            "video_id":         video_id,
            "view_count":       int(vstats.get("viewCount", 0)),
            "channel_name":     item["snippet"]["channelTitle"],
            "subscriber_count": int(cstats.get("subscriberCount", 0)),
        })
    return results


# ── Scoring ───────────────────────────────────────────────────────────────────

def _demand_score(avg_views):
    if avg_views <= 0:
        return 0
    return round(min(100, math.log10(avg_views + 1) / math.log10(10_000_000 + 1) * 100))


def _competition_score(avg_subs):
    if avg_subs <= 0:
        return 100
    raw = math.log10(avg_subs + 1) / math.log10(10_000_000 + 1) * 100
    return round(max(0, 100 - raw))


def _opportunity_score(demand, competition):
    return round((demand + competition) / 2)


def _summarise(opportunity):
    if opportunity >= 75:
        return "This is a strong niche. High demand and low competition make it a great opportunity for a new creator."
    if opportunity >= 50:
        return "This is a decent niche. There's good demand but moderate competition. Focus on a specific angle to stand out."
    if opportunity >= 25:
        return "This niche is competitive. Demand exists but big channels dominate. Consider narrowing your topic."
    return "This niche is tough. Low demand and high competition make it hard to break through as a new creator."


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    data     = request.get_json(silent=True) or request.form
    email    = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if get_user_by_email(email):
        return jsonify({"error": "An account with this email already exists"}), 409

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO users (email, password) VALUES (?, ?)", (email, hashed)
        )
        db.commit()

    session["user_id"] = cursor.lastrowid
    session.pop("guest_searches", None)
    return jsonify({"ok": True}), 201


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    data     = request.get_json(silent=True) or request.form
    email    = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "")

    user = get_user_by_email(email)
    if not user or not bcrypt.checkpw(password.encode(), user["password"].encode()):
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = user["id"]
    session.pop("guest_searches", None)
    return jsonify({"ok": True}), 200


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search")
def search():
    topic = request.args.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "Missing required query parameter: topic"}), 400
    return jsonify(_fetch_videos(topic))


@app.route("/analyse")
def analyse():
    topic = request.args.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "Missing required query parameter: topic"}), 400

    user_id = session.get("user_id")

    if user_id:
        user = get_user_by_id(user_id)
        if not user:
            session.clear()
            return jsonify({"error": "Session expired. Please log in again."}), 401

        if user["plan"] == "free":
            count = reset_daily_count_if_needed(user)
            if count >= FREE_DAILY_LIMIT:
                return jsonify({"error": "upgrade to pro"}), 403

        increment_search_count(user_id)
    else:
        guest_searches = session.get("guest_searches", 0)
        if guest_searches >= GUEST_LIMIT:
            return jsonify({"error": "guest_limit"}), 403
        session["guest_searches"] = guest_searches + 1

    videos = _fetch_videos(topic)
    if not videos:
        return jsonify({"error": "No videos found for this topic"}), 404

    avg_views = sum(v["view_count"]       for v in videos) / len(videos)
    avg_subs  = sum(v["subscriber_count"] for v in videos) / len(videos)

    demand      = _demand_score(avg_views)
    competition = _competition_score(avg_subs)
    opportunity = _opportunity_score(demand, competition)

    return jsonify({
        "topic":      topic,
        "scores":     {"demand": demand, "competition": competition, "opportunity": opportunity},
        "summary":    _summarise(opportunity),
        "top_videos": [v["title"] for v in videos[:3]],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
