"""
auth_routes.py  —  Quantly Authentication & Broker Blueprint
Registers /auth/* and /broker/* routes on the main Flask app.
"""
from flask import Blueprint, request, jsonify, redirect, url_for, make_response
from models import create_user, verify_user, create_session, get_session_user, delete_session, save_broker, get_broker

auth_bp = Blueprint("auth", __name__)


def _get_user(request):
    """Helper: resolve logged-in user from cookie."""
    token = request.cookies.get("q_session")
    return get_session_user(token)


# ── AUTH API ──────────────────────────────────────────────────────────────────

@auth_bp.route("/api/auth/signup", methods=["POST"])
def signup():
    d = request.json or {}
    name     = d.get("full_name", "").strip()
    email    = d.get("email", "").strip()
    password = d.get("password", "").strip()
    confirm  = d.get("confirm_password", "").strip()

    if not name or not email or not password:
        return jsonify({"status": "error", "message": "All fields required"}), 400
    if len(name) < 2:
        return jsonify({"status": "error", "message": "Name too short"}), 400
    if "@" not in email or "." not in email:
        return jsonify({"status": "error", "message": "Invalid email"}), 400
    if len(password) < 8:
        return jsonify({"status": "error", "message": "Password must be at least 8 characters"}), 400
    if password != confirm:
        return jsonify({"status": "error", "message": "Passwords do not match"}), 400

    try:
        user = create_user(name, email, password)
        token = create_session(user["id"])
        resp = make_response(jsonify({"status": "ok", "redirect": "/broker"}))
        resp.set_cookie("q_session", token, httponly=True, samesite="Lax", max_age=60*60*24*30)
        return resp
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    d = request.json or {}
    email    = d.get("email", "").strip()
    password = d.get("password", "").strip()

    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password required"}), 400

    user = verify_user(email, password)
    if not user:
        return jsonify({"status": "error", "message": "Invalid email or password"}), 401

    token = create_session(user["id"])
    resp = make_response(jsonify({"status": "ok", "redirect": "/broker"}))
    resp.set_cookie("q_session", token, httponly=True, samesite="Lax", max_age=60*60*24*30)
    return resp


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    token = request.cookies.get("q_session")
    if token:
        delete_session(token)
    resp = make_response(jsonify({"status": "ok"}))
    resp.delete_cookie("q_session")
    return resp


@auth_bp.route("/api/auth/me")
def me():
    user = _get_user(request)
    if not user:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    return jsonify({"status": "ok", "user": {"id": user["id"], "full_name": user["full_name"], "email": user["email"]}})


# ── BROKER API ────────────────────────────────────────────────────────────────

@auth_bp.route("/api/broker/connect", methods=["POST"])
def broker_connect():
    user = _get_user(request)
    if not user:
        return jsonify({"status": "error", "message": "Login required"}), 401

    d = request.json or {}
    broker_name  = d.get("broker", "").strip()
    api_key      = d.get("api_key", "").strip()
    api_secret   = d.get("api_secret", "").strip()
    access_token = d.get("access_token", "").strip()

    if not broker_name:
        return jsonify({"status": "error", "message": "Broker name required"}), 400
    if not api_key:
        return jsonify({"status": "error", "message": "API Key required"}), 400

    save_broker(user["id"], broker_name, api_key, api_secret, access_token)

    # If Zerodha, also update env for existing engine
    if broker_name.lower() in ("zerodha", "zerodha kite"):
        import os
        if api_key:
            os.environ["KITE_API_KEY"] = api_key
        if api_secret:
            os.environ["KITE_API_SECRET"] = api_secret
        if access_token:
            os.environ["KITE_ACCESS_TOKEN"] = access_token

    return jsonify({"status": "ok", "redirect": "/dashboard"})


@auth_bp.route("/api/broker/guest", methods=["POST"])
def broker_guest():
    """Skip broker connection, go straight to dashboard."""
    resp = make_response(jsonify({"status": "ok", "redirect": "/dashboard"}))
    resp.set_cookie("q_mode", "guest", httponly=True, samesite="Lax", max_age=60*60*24)
    return resp


@auth_bp.route("/api/broker/status")
def broker_status():
    user = _get_user(request)
    if not user:
        return jsonify({"connected": False, "guest": True})
    broker = get_broker(user["id"])
    return jsonify({
        "connected": bool(broker),
        "broker": broker["broker_name"] if broker else None,
        "guest": False,
    })