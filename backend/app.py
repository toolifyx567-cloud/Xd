import json
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import yt_dlp
import requests
import time
import os
import sqlite3
import random
import string
from datetime import datetime

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import threading


load_dotenv()  # load .env variables

EMAIL_HOST = os.getenv("EMAIL_HOST")        # e.g., smtp.gmail.com
EMAIL_PORT = int(os.getenv("EMAIL_PORT"))   # e.g., 587
EMAIL_USER = os.getenv("EMAIL_USER")        # your email
EMAIL_PASS = os.getenv("EMAIL_PASS")        # app password

def send_email(to_email, subject, message):
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(message, 'plain'))

        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print("EMAIL ERROR:", e)
        return False

app = Flask(__name__)
CORS(app)

DB_FILE = "stats.db"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
CACHE_TTL = 86400

# -----------------------------
# DATABASE INIT
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY,
        requests INTEGER DEFAULT 0,
        cache_hits INTEGER DEFAULT 0,
        downloads INTEGER DEFAULT 0,
        videos_served INTEGER DEFAULT 0,
        mb_served REAL DEFAULT 0
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE,
        timestamp INTEGER
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS video_cache (
        url TEXT PRIMARY KEY,
        data TEXT,
        timestamp INTEGER
    )
    """)

    c.execute("INSERT OR IGNORE INTO stats (id) VALUES (1)")

    conn.commit()
    conn.close()

init_db()


# -----------------------------
# STATS FUNCTIONS
# -----------------------------
def increment_stat(field, amount=1):

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute(
        f"UPDATE stats SET {field} = {field} + ? WHERE id = 1",
        (amount,)
    )

    conn.commit()
    conn.close()


# -----------------------------
# CACHE
# -----------------------------
def save_cache(url, data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO video_cache(url,data,timestamp) VALUES(?,?,?)",
        (url, json.dumps(data), int(time.time()))
    )
    conn.commit()
    conn.close()


def load_cache(url):

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute(
        "SELECT data,timestamp FROM video_cache WHERE url=?",
        (url,)
    )

    row = c.fetchone()
    conn.close()

    if not row:
        return None

    data, timestamp = row

    if time.time() - timestamp > CACHE_TTL:
        return None

    return json.loads(data)


# -----------------------------
# EMAIL API (async welcome email)
# -----------------------------
@app.route("/save-email", methods=["POST"])
def save_email():
    data = request.get_json()
    email = data.get("email")

    if not email:
        return jsonify({"success": False})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO emails(email,timestamp) VALUES(?,?)",
            (email, int(time.time()))
        )
        conn.commit()
    except:
        pass
    conn.close()

    # Send welcome email in a separate thread (non-blocking)
    subject = "Welcome to ToolifyX!"
    message = (
        "Hi there,\n\n"
        "Thanks for subscribing! You'll now receive updates whenever we add new tools.\n\n"
        "— Team ToolifyX"
    )
    threading.Thread(target=send_email, args=(email, subject, message)).start()

    # Return success immediately
    return jsonify({"success": True})



# -----------------------------
# EMAIL ADMIN SENDER (background)
# -----------------------------
@app.route("/admin/send-newsletter", methods=["POST"])
def send_newsletter():
    data = request.get_json()
    password = data.get("password")
    subject = data.get("subject")
    message = data.get("message")

    if password != ADMIN_PASSWORD:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT email FROM emails")
    emails = [e[0] for e in c.fetchall()]
    conn.close()

    if not emails:
        return jsonify({"success": False, "message": "No subscribers found"})

    # Background thread for sending emails
    def send_all_emails():
        for email in emails:
            try:
                msg = MIMEMultipart()
                msg['From'] = "ToolifyX <toolifyx567@gmail.com>"
                msg['To'] = email
                msg['Subject'] = subject
                msg.attach(MIMEText(message, 'plain'))

                server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
                server.starttls()
                server.login(EMAIL_USER, EMAIL_PASS)
                server.send_message(msg)
                server.quit()

                print(f"Sent to {email}")
            except Exception as e:
                print(f"Failed to send to {email}: {e}")

    threading.Thread(target=send_all_emails).start()

    return jsonify({"success": True, "message": f"Started sending {len(emails)} emails in background"})

# -----------------------------
# URL NORMALIZER
# -----------------------------
def normalize_twitter_url(url):

    url = url.strip()

    if "x.com" in url:
        url = url.replace("x.com", "twitter.com")

    if "mobile.twitter.com" in url:
        url = url.replace("mobile.twitter.com", "twitter.com")

    if "?" in url:
        url = url.split("?")[0]

    return url


# -----------------------------
# EXTRACT VIDEO (re-usable)
# -----------------------------
def extract_video_info(url):

    try:
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "format": "best",
            "nocheckcertificate": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            },
            "ignoreerrors": True
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return None

        # desired qualities: 480, 720, 1080, 2160 (2k/4k)
        allowed_heights = [480, 720, 1080, 2160]
        added = set()
        videos = []

        # iterate formats and map each to the closest allowed height
        for f in info.get("formats", []):
            if f.get("ext") != "mp4":
                continue

            h = f.get("height")
            if not h:
                continue

            closest = min(allowed_heights, key=lambda x: abs(x - h))

            if closest in added:
                continue

            size = f.get("filesize") or f.get("filesize_approx") or 0
            filesize_mb = round(size / 1024 / 1024, 2) if size else None

            quality_label = "2k/4k (2160p)" if closest == 2160 else f"{closest}p"

            videos.append({
                "url": f.get("url"),
                "quality": quality_label,
                "height": closest,
                "filesize": size,
                "filesize_mb": filesize_mb
            })

            added.add(closest)

        if not videos:
            return None

        # sort from highest -> lowest
        videos.sort(key=lambda x: x["height"], reverse=True)

        return {
            "success": True,
            "title": info.get("title") or "Untitled Video",
            "author": info.get("uploader", ""),
            "thumbnail": info.get("thumbnail", ""),
            "videos": videos
        }

    except Exception as e:
        import traceback
        print("EXTRACTION ERROR:", traceback.format_exc())
        return None


# -----------------------------
# DOWNLOAD INFO
# -----------------------------
@app.route("/download", methods=["POST"])
def download():
    # count the request
    increment_stat("requests")

    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"success": False, "message": "No URL provided"}), 400

    # normalize URL
    url = normalize_twitter_url(url)

    # check cache first
    cached = load_cache(url)
    if cached:
        increment_stat("cache_hits")
        # Add videoId for re-fetch support
        cached["videoId"] = url
        return jsonify(cached)

    result = extract_video_info(url)

    if not result:
        return jsonify({"success": False, "message": "Extraction failed"}), 500

    # Add videoId for re-fetch support
    result["videoId"] = url

    # cache the result
    save_cache(url, result)

    return jsonify(result)


# -----------------------------
# PROXY STREAM + RESUMABLE DOWNLOAD (RE-FETCH FRESH URL)
# -----------------------------
@app.route("/proxy")
def proxy():

    # Support both old "url" param and new "videoId" param
    video_url = request.args.get("url")
    video_id = request.args.get("videoId")
    mode = request.args.get("mode", "download")
    quality = request.args.get("quality", "")  # e.g. "720p" or "1080p"

    # If videoId provided, re-extract fresh URLs
    if video_id and not video_url:
        result = extract_video_info(video_id)
        if result:
            videos = result.get("videos", [])
            # If quality specified, find matching video
            if quality:
                for v in videos:
                    if v["quality"] == quality:
                        video_url = v["url"]
                        break
            # Fallback to best quality
            if not video_url and videos:
                video_url = videos[0]["url"]
        else:
            return jsonify({"success": False, "message": "Could not re-fetch video. Link may be expired or invalid."}), 500

    if not video_url:
        return jsonify({"success": False, "message": "No video URL"}), 400

    increment_stat("downloads")

    try:
        # Parse Range header from client
        range_header = request.headers.get("Range")

        # Build request headers to forward to source
        source_headers = {}
        if range_header:
            source_headers["Range"] = range_header

        # Request from source with range support
        r = requests.get(
            video_url,
            headers=source_headers,
            stream=True,
            timeout=15
        )

        random_id = ''.join(
            random.choices(
                string.ascii_uppercase +
                string.digits,
                k=6
            )
        )

        filename = f"ToolifyX Downloader_{random_id}.mp4"

        # Determine response status
        status_code = 206 if r.status_code == 206 else 200

        # Build response headers
        response_headers = {
            "Content-Type": r.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
        }

        # Forward Content-Range if source sent it
        if "Content-Range" in r.headers:
            response_headers["Content-Range"] = r.headers["Content-Range"]

        # Forward Content-Length
        if "Content-Length" in r.headers:
            response_headers["Content-Length"] = r.headers["Content-Length"]

        # Content-Disposition based on mode
        if mode == "preview":
            response_headers["Content-Disposition"] = f'inline; filename="{filename}"'
        else:
            response_headers["Content-Disposition"] = f'attachment; filename="{filename}"'

        # Stream generator with larger chunks for speed
        def generate():
            for chunk in r.iter_content(chunk_size=262144):  # 256KB chunks
                if chunk:
                    yield chunk

        return Response(
            generate(),
            status=status_code,
            headers=response_headers
        )

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# -----------------------------
# HEALTH
# -----------------------------
@app.route("/")
def home():

    return jsonify({

        "status": "ok",

        "service": "ToolifyX Downloader API"
    })


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 5000)
    )

    app.run(
        host="0.0.0.0",
        port=port
  )
