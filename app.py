"""Flask chat bot backed by Amazon Bedrock (Converse API) with SQLite chat history.

Uses the same region / model / boto3 setup as test_bedrock.py.
Run:  python app.py   then open http://127.0.0.1:5000
"""
import json
import os
import re
import secrets
import time
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

try:
    # Trust the OS certificate store (needed behind proxies / antivirus that re-sign HTTPS traffic).
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import boto3
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    SSLError,
)
from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful, friendly and concise general-purpose assistant. "
    "Use Markdown for formatting when it helps readability.",
)
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1024"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.5"))

MAX_CONTEXT_MESSAGES = 50  # most recent stored messages sent to the model
MAX_MESSAGE_CHARS = 8000
MAX_TITLE_CHARS = 100

FREE_MESSAGE_LIMIT = int(os.environ.get("FREE_MESSAGE_LIMIT", "10"))  # messages per account, ever

MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_CHARS = 128
MAX_NAME_CHARS = 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOGIN_MAX_FAILURES = 5  # failed attempts per (ip, email) ...
LOGIN_WINDOW_SECONDS = 300  # ... within this window lock further attempts

app = Flask(__name__)
db.init_db()


def _load_secret_key():
    """SECRET_KEY from the environment, else a random key persisted next to the database."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(os.path.dirname(db.DB_PATH), "secret_key")
    try:
        with open(path, encoding="ascii") as f:
            key = f.read().strip()
    except OSError:
        key = ""
    if not key:
        key = secrets.token_hex(32)
        with open(path, "w", encoding="ascii") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return key


app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

_session = boto3.Session(region_name=AWS_REGION)
_bedrock = _session.client("bedrock-runtime")


# ---------- helpers ----------
def _friendly_error(error):
    if isinstance(error, NoCredentialsError):
        return (
            "AWS credentials not found. On EC2, attach an IAM role to the instance "
            "(and allow the container to reach the metadata service); locally, run `aws configure`."
        )
    if isinstance(error, ClientError):
        code = error.response["Error"]["Code"]
        message = error.response["Error"]["Message"]
        return f"Bedrock error ({code}): {message}"
    if isinstance(error, SSLError):
        return (
            "SSL certificate validation failed when connecting to AWS. "
            "If you are behind a corporate proxy, set AWS_CA_BUNDLE to your CA certificate file."
        )
    if isinstance(error, (EndpointConnectionError, ConnectTimeoutError)):
        return f"Could not reach Bedrock in {AWS_REGION}. Check your network connection."
    return "Something went wrong while contacting the model."


def _event(obj):
    return json.dumps(obj) + "\n"


def _provisional_title(text):
    title = " ".join(text.split())
    return title if len(title) <= 40 else title[:39].rstrip() + "…"


def _generate_title(user_text, reply):
    """Ask the model for a short chat title; None if anything goes wrong."""
    try:
        response = _bedrock.converse(
            modelId=MODEL_ID,
            system=[{
                "text": "You write short titles for chat conversations. Reply with a title of at most "
                        "5 words, no quotes and no trailing punctuation. Reply with the title only."
            }],
            messages=[{
                "role": "user",
                "content": [{
                    "text": f"User message:\n{user_text[:500]}\n\nAssistant reply:\n{reply[:500]}\n\n"
                            "Write the title."
                }],
            }],
            inferenceConfig={"maxTokens": 24, "temperature": 0.3},
        )
        text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
        lines = text.strip().splitlines()
        title = lines[0].strip().strip("\"'`*#").strip() if lines else ""
        return title[:60] or None
    except Exception:
        app.logger.warning("Title generation failed", exc_info=True)
        return None


def _converse_messages(conversation_id, user_text):
    history = db.get_messages(conversation_id, MAX_CONTEXT_MESSAGES) if conversation_id else []
    while history and history[0]["role"] != "user":  # Converse must start with a user turn
        history.pop(0)
    history.append({"role": "user", "content": user_text})
    return [{"role": m["role"], "content": [{"text": m["content"]}]} for m in history]


# ---------- auth helpers ----------
# Hash checked when the email is unknown, so "no such user" and "wrong password" take equally long.
_DUMMY_HASH = generate_password_hash("not-a-real-password")
_login_failures = {}  # (ip, email) -> [timestamps]; in-memory, fine for a single worker process


@app.before_request
def load_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = db.get_user(user_id)
        if g.user is None:  # account no longer exists
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Please log in to continue."), 401
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def _quota(used):
    return {"limit": FREE_MESSAGE_LIMIT, "remaining": max(0, FREE_MESSAGE_LIMIT - used)}


def _limit_message():
    return (
        f"You've used all {FREE_MESSAGE_LIMIT} free messages on this account. "
        "You can still read and manage your existing chats."
    )


def _csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = _csrf_token


def _check_csrf():
    sent = request.form.get("csrf_token", "")
    if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
        abort(400, description="Invalid or expired form. Reload the page and try again.")


def _safe_next(target):
    """Only allow same-site relative redirects (blocks open redirects like //evil.com)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return url_for("index")
    parts = urlsplit(target)
    return target if not parts.scheme and not parts.netloc else url_for("index")


def _throttled(key):
    now = time.monotonic()
    attempts = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_failures[key] = attempts
    return len(attempts) >= LOGIN_MAX_FAILURES


def _log_in(user_id):
    session.clear()  # drop any pre-login state (and rotate the CSRF token)
    session["user_id"] = user_id
    session.permanent = True


# ---------- auth pages ----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("index"))
    next_url = request.values.get("next", "")
    form = {"name": "", "email": ""}
    error = None
    if request.method == "POST":
        _check_csrf()
        form["name"] = " ".join(request.form.get("name", "").split())
        form["email"] = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not form["name"] or len(form["name"]) > MAX_NAME_CHARS:
            error = f"Enter your name (up to {MAX_NAME_CHARS} characters)."
        elif not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
            error = "Enter a valid email address."
        elif len(password) < MIN_PASSWORD_CHARS:
            error = f"Password must be at least {MIN_PASSWORD_CHARS} characters."
        elif len(password) > MAX_PASSWORD_CHARS:
            error = f"Password must be at most {MAX_PASSWORD_CHARS} characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            user_id = db.create_user(form["email"], form["name"], generate_password_hash(password))
            if user_id is None:
                error = "An account with that email already exists. Try logging in."
            else:
                _log_in(user_id)
                return redirect(_safe_next(next_url))
    return render_template(
        "signup.html", error=error, form=form, next_url=next_url,
        min_password=MIN_PASSWORD_CHARS,
    ), (400 if error else 200)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    next_url = request.values.get("next", "")
    email = ""
    error = None
    status = 200
    if request.method == "POST":
        _check_csrf()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        key = (request.remote_addr, email)
        if _throttled(key):
            error, status = "Too many failed attempts. Please wait a few minutes and try again.", 429
        else:
            user = db.get_user_by_email(email)
            ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
            if user and ok:
                _login_failures.pop(key, None)
                _log_in(user["id"])
                return redirect(_safe_next(next_url))
            _login_failures.setdefault(key, []).append(time.monotonic())
            error, status = "Incorrect email or password.", 401
    return render_template("login.html", error=error, email=email, next_url=next_url), status


@app.post("/logout")
def logout():
    _check_csrf()
    session.clear()
    return redirect(url_for("login"))


# ---------- pages ----------
@app.get("/")
@app.get("/c/<conversation_id>")
@login_required
def index(conversation_id=None):
    return render_template(
        "index.html", model_id=MODEL_ID, region=AWS_REGION, user=g.user,
        quota=_quota(g.user["messages_used"]), limit_message=_limit_message(),
    )


@app.get("/health")
def health():
    return jsonify(status="ok", model=MODEL_ID, region=AWS_REGION)


# ---------- conversations API ----------
@app.get("/api/conversations")
@login_required
def list_conversations():
    return jsonify(conversations=db.list_conversations(g.user["id"]))


@app.get("/api/conversations/<conversation_id>")
@login_required
def get_conversation(conversation_id):
    conversation = db.get_conversation(conversation_id, g.user["id"])
    if not conversation:
        return jsonify(error="Conversation not found."), 404
    conversation["messages"] = db.get_messages(conversation_id)
    return jsonify(conversation)


@app.patch("/api/conversations/<conversation_id>")
@login_required
def rename_conversation(conversation_id):
    payload = request.get_json(silent=True) or {}
    title = payload.get("title")
    title = " ".join(title.split()) if isinstance(title, str) else ""
    if not title or len(title) > MAX_TITLE_CHARS:
        return jsonify(error=f"Title must be 1-{MAX_TITLE_CHARS} characters."), 400
    if not db.set_title(conversation_id, g.user["id"], title):
        return jsonify(error="Conversation not found."), 404
    return jsonify(id=conversation_id, title=title)


@app.delete("/api/conversations/<conversation_id>")
@login_required
def delete_conversation(conversation_id):
    if not db.delete_conversation(conversation_id, g.user["id"]):
        return jsonify(error="Conversation not found."), 404
    return "", 204


# ---------- chat ----------
@app.post("/api/chat")
@login_required
def chat():
    user_id = g.user["id"]  # g is not available inside the streaming generator

    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}

    text = payload.get("message")
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="`message` must be non-empty text."), 400
    if len(text) > MAX_MESSAGE_CHARS:
        return jsonify(error=f"Messages are limited to {MAX_MESSAGE_CHARS} characters."), 400

    conversation_id = payload.get("conversation_id")
    existing = None
    if conversation_id is not None:
        existing = db.get_conversation(conversation_id, user_id) if isinstance(conversation_id, str) else None
        if not existing:
            return jsonify(error="Conversation not found."), 404

    if db.get_messages_used(user_id) >= FREE_MESSAGE_LIMIT:
        return jsonify(error=_limit_message(), quota=_quota(FREE_MESSAGE_LIMIT)), 402

    is_new = existing is None
    conversation_id = conversation_id or db.new_id()
    title = existing["title"] if existing else _provisional_title(text)
    messages = _converse_messages(None if is_new else conversation_id, text)

    def generate():
        # Reserve the message atomically (two tabs can't both take the last one). It is kept only
        # once a reply has been stored; otherwise the `finally` below gives it back.
        if not db.consume_message(user_id, FREE_MESSAGE_LIMIT):
            yield _event({"error": _limit_message(), "quota": _quota(FREE_MESSAGE_LIMIT)})
            return
        kept = False
        try:
            yield _event({"quota": _quota(db.get_messages_used(user_id))})
            yield _event({"conversation": {"id": conversation_id, "title": title}})
            parts = []
            try:
                response = _bedrock.converse_stream(
                    modelId=MODEL_ID,
                    messages=messages,
                    system=[{"text": SYSTEM_PROMPT}],
                    inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
                )
                for event in response["stream"]:
                    delta = event.get("contentBlockDelta", {}).get("delta", {})
                    if "text" in delta:
                        parts.append(delta["text"])
                        yield _event({"delta": delta["text"]})
                    elif "metadata" in event:
                        yield _event({"usage": event["metadata"].get("usage", {})})
            except GeneratorExit:
                # The user pressed Stop: keep whatever was generated so far.
                if parts:
                    db.save_exchange(conversation_id, user_id, title, text, "".join(parts))
                    kept = True
                raise
            except Exception as error:  # surfaced to the UI as a readable message
                app.logger.exception("Bedrock request failed")
                yield _event({"error": _friendly_error(error)})
                return

            reply = "".join(parts)
            if not reply.strip():
                yield _event({"error": "The model returned an empty response."})
                return

            db.save_exchange(conversation_id, user_id, title, text, reply)
            kept = True
            yield _event({"done": True})

            if is_new:
                better = _generate_title(text, reply)
                if better:
                    db.set_title(conversation_id, user_id, better)
                    yield _event({"id": conversation_id, "title": better})
        finally:
            if not kept:
                db.refund_message(user_id)

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=True)
