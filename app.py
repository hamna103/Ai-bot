import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import jwt
import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    CHATGPT_API_KEY as DEFAULT_CHATGPT_API_KEY,
    CHATGPT_BASE_URL as DEFAULT_CHATGPT_BASE_URL,
    CHATGPT_MODEL as DEFAULT_CHATGPT_MODEL,
    DATABASE,
    JWT_ALGORITHM,
    JWT_EXPIRES_MINUTES,
    JWT_SECRET_KEY,
    SECRET_KEY,
)

chat_memory = []

app = Flask(__name__)
app.secret_key = SECRET_KEY


def get_db_connection():
    return sqlite3.connect(DATABASE)


def parse_chat_reply(model_data):
    if isinstance(model_data, dict):
        choices = model_data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            content = (message.get("content") or "").strip()
            if content:
                return content
    return ""


def local_fallback_bot(message):
    text = message.lower().strip()

    # Local fallback keeps the project working without external API.
    if any(word in text for word in ["hello", "hi", "hey"]):
        return "Hello. I am your local AI assistant. Ask me anything."
    if "name" in text:
        return "I am your university chatbot assistant."
    if "python" in text:
        return "Python is a beginner-friendly programming language used for web, AI, and automation."
    if "ai" in text:
        return "AI means Artificial Intelligence, where software can perform smart tasks like understanding and generating text."

    return f"You asked: {message}. I am running in local fallback mode right now."


def get_live_chat_settings():
    api_key = os.getenv("CHATGPT_API_KEY", "").strip() or DEFAULT_CHATGPT_API_KEY
    base_url = os.getenv("CHATGPT_BASE_URL", "").strip() or DEFAULT_CHATGPT_BASE_URL
    model = os.getenv("CHATGPT_MODEL", "").strip() or DEFAULT_CHATGPT_MODEL
    return api_key, base_url, model


def create_access_token(username):
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRES_MINUTES)
    payload = {"sub": username, "exp": expires_at}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token):
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_token_from_header():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header[7:].strip()


def require_user():
    token = get_token_from_header()
    if not token:
        return None
    return decode_access_token(token)


def get_user_from_token_or_query():
    token = get_token_from_header() or request.args.get("token", "").strip()
    if not token:
        return None, None
    username = decode_access_token(token)
    return username, token


def is_valid_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()

    if not user or not check_password_hash(user[0], password):
        return jsonify({"error": "Invalid username or password"}), 401

    access_token = create_access_token(username)
    return jsonify({"access_token": access_token, "user": username})


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    data = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not full_name or not email or not username or not password:
        return jsonify({"error": "All fields are required"}), 400
    if not is_valid_email(email):
        return jsonify({"error": "Please enter a valid email"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    hashed_password = generate_password_hash(password)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, full_name, email, password) VALUES (?, ?, ?, ?)",
            (username, full_name, email, hashed_password),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Username already exists"}), 409

    conn.close()
    return jsonify({"message": "Account created"})


@app.route("/chat", methods=["GET"])
def chat():
    user, token = get_user_from_token_or_query()
    if not user:
        return redirect(url_for("login"))
    return render_template("chat.html", token=token)


@app.route("/profile", methods=["GET"])
def profile():
    user, token = get_user_from_token_or_query()
    if not user:
        return redirect(url_for("login"))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username, full_name, email FROM users WHERE username = ?",
        (user,),
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return redirect(url_for("login"))

    profile_data = {
        "username": row[0],
        "full_name": row[1],
        "email": row[2],
    }
    return render_template("profile.html", user=profile_data, token=token)


@app.route("/get_response", methods=["POST"])
def get_response():
    user = require_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    chat_memory.append({"user": user, "text": user_message})
    fallback_message = local_fallback_bot(user_message)

    api_key, base_url, model = get_live_chat_settings()
    if not api_key:
        return jsonify({"reply": "Live AI key is missing. Please set CHATGPT_API_KEY."}), 500

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful and simple university chatbot."},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.7,
    }

    try:
        response = requests.post(base_url, headers=headers, json=payload, timeout=30)
        if response.status_code >= 400:
            return jsonify({"reply": f"Live AI request failed ({response.status_code})."}), 502

        model_data = response.json()
        reply = parse_chat_reply(model_data) or fallback_message
        return jsonify({"reply": reply})
    except Exception:
        return jsonify({"reply": fallback_message})


@app.route("/logout")
def logout():
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
    debug=True)