import math
import os
import sqlite3
from datetime import date, datetime

import bcrypt
from dotenv import load_dotenv
from flask import (Flask, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_session import Session
from googleapiclient.discovery import build
import stripe

load_dotenv()

STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID        = os.getenv("STRIPE_PRICE_ID", "")
stripe.api_key = STRIPE_SECRET_KEY

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


def migrate_db():
    with get_db() as db:
        for col_def in ("stripe_customer_id TEXT", "stripe_subscription_id TEXT", "email_tips INTEGER NOT NULL DEFAULT 1"):
            try:
                db.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                db.commit()
            except sqlite3.OperationalError:
                pass


migrate_db()


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
    ctx = {"current_user": None, "stripe_publishable_key": STRIPE_PUBLISHABLE_KEY}
    if user_id:
        user = get_user_by_id(user_id)
        ctx["current_user"] = dict(user) if user else None
    return ctx


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


# ── Account & settings routes ────────────────────────────────────────────────

def _require_login():
    uid = session.get("user_id")
    if not uid:
        return None, redirect(url_for("login"))
    u = get_user_by_id(uid)
    if not u:
        session.clear()
        return None, redirect(url_for("login"))
    return u, None


@app.route("/account")
def account():
    user, resp = _require_login()
    if resp:
        return resp

    billing_date = None
    cancel_at_period_end = False

    if user["plan"] == "pro" and user["stripe_subscription_id"]:
        try:
            sub = stripe.Subscription.retrieve(user["stripe_subscription_id"])
            cancel_at_period_end = sub.cancel_at_period_end
            dt = datetime.utcfromtimestamp(sub.current_period_end)
            billing_date = f"{dt.day} {dt.strftime('%B %Y')}"
        except stripe.StripeError:
            pass

    return render_template(
        "account.html",
        user=dict(user),
        billing_date=billing_date,
        cancel_at_period_end=cancel_at_period_end,
    )


@app.route("/cancel-subscription", methods=["POST"])
def cancel_subscription():
    user, resp = _require_login()
    if resp:
        return resp

    if not user["stripe_subscription_id"]:
        flash("No active subscription found.", "error")
        return redirect(url_for("account"))

    try:
        stripe.Subscription.modify(user["stripe_subscription_id"], cancel_at_period_end=True)
        flash("Your subscription will cancel at the end of the billing period. You keep Pro access until then.", "success")
    except stripe.StripeError as e:
        flash(str(e), "error")

    return redirect(url_for("account"))


@app.route("/settings")
def settings():
    user, resp = _require_login()
    if resp:
        return resp
    return render_template("settings.html", user=dict(user))


@app.route("/settings/email", methods=["POST"])
def settings_email():
    user, resp = _require_login()
    if resp:
        return resp

    new_email = request.form.get("email", "").strip().lower()
    if not new_email:
        flash("Email address is required.", "error")
        return redirect(url_for("settings"))

    existing = get_user_by_email(new_email)
    if existing and existing["id"] != user["id"]:
        flash("That email address is already in use.", "error")
        return redirect(url_for("settings"))

    with get_db() as db:
        db.execute("UPDATE users SET email = ? WHERE id = ?", (new_email, user["id"]))
        db.commit()

    flash("Email address updated.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/password", methods=["POST"])
def settings_password():
    user, resp = _require_login()
    if resp:
        return resp

    current_pw = request.form.get("current_password", "")
    new_pw     = request.form.get("new_password", "")

    if not bcrypt.checkpw(current_pw.encode(), user["password"].encode()):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("settings"))

    if len(new_pw) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("settings"))

    hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    with get_db() as db:
        db.execute("UPDATE users SET password = ? WHERE id = ?", (hashed, user["id"]))
        db.commit()

    flash("Password updated successfully.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/notifications", methods=["POST"])
def settings_notifications():
    user, resp = _require_login()
    if resp:
        return resp

    email_tips = 1 if request.form.get("email_tips") else 0
    with get_db() as db:
        db.execute("UPDATE users SET email_tips = ? WHERE id = ?", (email_tips, user["id"]))
        db.commit()

    flash("Notification preferences saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/delete", methods=["POST"])
def settings_delete():
    user, resp = _require_login()
    if resp:
        return resp

    password = request.form.get("password", "")
    if not bcrypt.checkpw(password.encode(), user["password"].encode()):
        flash("Incorrect password — account not deleted.", "error")
        return redirect(url_for("settings"))

    if user["stripe_subscription_id"]:
        try:
            stripe.Subscription.cancel(user["stripe_subscription_id"])
        except stripe.StripeError:
            pass

    with get_db() as db:
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        db.commit()

    session.clear()
    return redirect(url_for("index"))


# ── Stripe routes ─────────────────────────────────────────────────────────────

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please log in to upgrade"}), 401

    user = get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if user["plan"] == "pro":
        return jsonify({"error": "Already on Pro plan"}), 400

    try:
        if user["stripe_customer_id"]:
            customer_id = user["stripe_customer_id"]
        else:
            customer = stripe.Customer.create(email=user["email"])
            customer_id = customer.id
            with get_db() as db:
                db.execute(
                    "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                    (customer_id, user_id),
                )
                db.commit()

        stripe_session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=url_for("success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("cancel", _external=True),
        )
        return jsonify({"url": stripe_session.url})
    except stripe.StripeError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/success")
def success():
    stripe_session_id = request.args.get("session_id", "")
    upgraded = False

    if stripe_session_id and session.get("user_id"):
        try:
            stripe_session = stripe.checkout.Session.retrieve(stripe_session_id)
            if stripe_session.payment_status == "paid":
                with get_db() as db:
                    db.execute(
                        "UPDATE users SET plan = 'pro', stripe_customer_id = ?, stripe_subscription_id = ? WHERE id = ?",
                        (stripe_session.customer, stripe_session.subscription, session.get("user_id")),
                    )
                    db.commit()
                upgraded = True
        except stripe.StripeError:
            pass

    return render_template("success.html", upgraded=upgraded)


@app.route("/cancel")
def cancel():
    return redirect(url_for("index"))


@app.route("/webhook", methods=["POST"])
def webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "", 400

    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed" and obj.get("mode") == "subscription":
        with get_db() as db:
            db.execute(
                "UPDATE users SET plan = 'pro', stripe_subscription_id = ? WHERE stripe_customer_id = ?",
                (obj["subscription"], obj["customer"]),
            )
            db.commit()

    elif event["type"] == "customer.subscription.deleted":
        with get_db() as db:
            db.execute(
                "UPDATE users SET plan = 'free', stripe_subscription_id = NULL WHERE stripe_customer_id = ?",
                (obj["customer"],),
            )
            db.commit()

    return "", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
