import math
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")


def _youtube():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def _fetch_videos(topic):
    """Search YouTube and return enriched video data for a topic."""
    youtube = _youtube()

    search_resp = youtube.search().list(
        q=topic, part="snippet", type="video", maxResults=10
    ).execute()

    items = search_resp.get("items", [])
    if not items:
        return []

    video_ids = [item["id"]["videoId"] for item in items]
    channel_ids = [item["snippet"]["channelId"] for item in items]

    videos_resp = youtube.videos().list(
        part="statistics", id=",".join(video_ids)
    ).execute()
    video_stats = {v["id"]: v.get("statistics", {}) for v in videos_resp.get("items", [])}

    channels_resp = youtube.channels().list(
        part="statistics", id=",".join(set(channel_ids))
    ).execute()
    channel_stats = {c["id"]: c.get("statistics", {}) for c in channels_resp.get("items", [])}

    results = []
    for item in items:
        video_id = item["id"]["videoId"]
        channel_id = item["snippet"]["channelId"]
        vstats = video_stats.get(video_id, {})
        cstats = channel_stats.get(channel_id, {})
        results.append({
            "title": item["snippet"]["title"],
            "video_id": video_id,
            "view_count": int(vstats.get("viewCount", 0)),
            "channel_name": item["snippet"]["channelTitle"],
            "subscriber_count": int(cstats.get("subscriberCount", 0)),
        })
    return results


def _demand_score(avg_views):
    """0–100: logarithmic scale where 10M avg views = 100."""
    if avg_views <= 0:
        return 0
    return round(min(100, math.log10(avg_views + 1) / math.log10(10_000_000 + 1) * 100))


def _competition_score(avg_subs):
    """0–100: 100 = low competition (few subs), 0 = high competition (many subs)."""
    if avg_subs <= 0:
        return 100
    raw = math.log10(avg_subs + 1) / math.log10(10_000_000 + 1) * 100
    return round(max(0, 100 - raw))


def _summarise(opportunity):
    if opportunity >= 75:
        return "This is a strong niche. High demand and low competition make it a great opportunity for a new creator."
    if opportunity >= 50:
        return "This is a decent niche. There's good demand but moderate competition. Focus on a specific angle to stand out."
    if opportunity >= 25:
        return "This niche is competitive. Demand exists but big channels dominate. Consider narrowing your topic."
    return "This niche is tough. Low demand and high competition make it hard to break through as a new creator."


def _opportunity_score(demand, competition):
    """0–100: rewards high demand and low competition equally."""
    return round((demand + competition) / 2)


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

    videos = _fetch_videos(topic)
    if not videos:
        return jsonify({"error": "No videos found for this topic"}), 404

    avg_views = sum(v["view_count"] for v in videos) / len(videos)
    avg_subs = sum(v["subscriber_count"] for v in videos) / len(videos)

    demand = _demand_score(avg_views)
    competition = _competition_score(avg_subs)
    opportunity = _opportunity_score(demand, competition)

    top_titles = [v["title"] for v in videos[:3]]
    summary = _summarise(opportunity)

    return jsonify({
        "topic": topic,
        "scores": {
            "demand": demand,
            "competition": competition,
            "opportunity": opportunity,
        },
        "summary": summary,
        "top_videos": top_titles,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)
