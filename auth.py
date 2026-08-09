"""
Authentication blueprint: signup, login, logout.

This is a Flask Blueprint, not a full app - it's meant to be *registered*
onto whatever Flask app object your project already has. See webserver.py
for how it's wired in.

After login/signup, users land on the real site homepage ("home" route,
i.e. your index.html) instead of a separate dashboard page.
"""

import re

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db, oauth
from models import User

auth_bp = Blueprint("auth", __name__, template_folder="templates")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The site owner's account always has admin access, no manual DB edit
# needed. Checked on every signup/login/OAuth-login (not just once at
# creation) so it self-heals even if this account already existed
# before this list did, or is_admin ever got reset some other way.
OWNER_ADMIN_EMAILS = {"devanujkumarm@gmail.com"}


def _grant_admin_if_owner(user: User) -> None:
    if user.email and user.email.lower() in OWNER_ADMIN_EMAILS and not user.is_admin:
        user.is_admin = True


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        errors = []
        if len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        if not EMAIL_RE.match(email):
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm_password:
            errors.append("Passwords do not match.")
        if User.query.filter_by(username=username).first():
            errors.append("That username is already taken.")
        if User.query.filter_by(email=email).first():
            errors.append("That email is already registered.")

        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("auth.signup"))

        user = User(username=username, email=email)
        user.set_password(password)  # hashed - raw password is never stored
        _grant_admin_if_owner(user)
        db.session.add(user)
        db.session.commit()

        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("signup.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier.lower())
        ).first()

        # Deliberately generic error message below (doesn't reveal whether
        # the username/email exists) - avoids leaking which accounts exist.
        if user and user.check_password(password):
            if user.is_banned:
                flash(
                    f"This account has been suspended{f': {user.ban_reason}' if user.ban_reason else '.'}",
                    "error",
                )
                return redirect(url_for("auth.login"))
            _grant_admin_if_owner(user)
            db.session.commit()
            session.permanent = True
            login_user(user, remember=remember)
            flash(f"Welcome back, {user.username}!", "success")
            next_page = request.args.get("next")
            # Only redirect to relative paths, to avoid open-redirect issues
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("home"))

        flash("Invalid username/email or password.", "error")
        return redirect(url_for("auth.login"))

    return render_template("login.html")


def _unique_username_from(base: str) -> str:
    """Turn e.g. 'priya' into a free username, adding a number if taken."""
    base = re.sub(r"[^a-zA-Z0-9_]", "", base).lower() or "user"
    candidate = base
    suffix = 1
    while User.query.filter_by(username=candidate).first():
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def _login_or_create_oauth_user(provider: str, oauth_id: str, email: str, name: str):
    """
    Shared logic for both Google and GitHub:
    - If we've seen this exact provider+oauth_id before, log that user in.
    - Else if the email already has a normal (password) account, link this
      OAuth provider onto that existing account instead of making a duplicate.
    - Else create a brand new account for them.
    """
    user = User.query.filter_by(oauth_provider=provider, oauth_id=oauth_id).first()

    if user is None and email:
        user = User.query.filter_by(email=email.lower()).first()
        if user:
            # Existing username/password account with this email - link it.
            user.oauth_provider = provider
            user.oauth_id = oauth_id

    if user is None:
        user = User(
            username=_unique_username_from(name or (email.split("@")[0] if email else provider)),
            email=(email or f"{provider}_{oauth_id}@no-email.example").lower(),
            oauth_provider=provider,
            oauth_id=oauth_id,
        )
        db.session.add(user)

    _grant_admin_if_owner(user)
    db.session.commit()

    if user.is_banned:
        flash(
            f"This account has been suspended{f': {user.ban_reason}' if user.ban_reason else '.'}",
            "error",
        )
        return redirect(url_for("auth.login"))

    session.permanent = True
    login_user(user)
    flash(f"Welcome, {user.username}!", "success")
    return redirect(url_for("home"))


@auth_bp.route("/login/google")
def google_login():
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/login/google/callback")
def google_callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.userinfo()
    return _login_or_create_oauth_user(
        provider="google",
        oauth_id=userinfo["sub"],
        email=userinfo.get("email", ""),
        name=userinfo.get("name") or userinfo.get("given_name", ""),
    )


@auth_bp.route("/login/github")
def github_login():
    redirect_uri = url_for("auth.github_callback", _external=True)
    return oauth.github.authorize_redirect(redirect_uri)


@auth_bp.route("/login/github/callback")
def github_callback():
    oauth.github.authorize_access_token()
    profile = oauth.github.get("user").json()

    email = profile.get("email")
    if not email:
        # GitHub only returns a public email if the user has one set to
        # public - otherwise we ask for it via a separate scoped endpoint.
        emails = oauth.github.get("user/emails").json()
        primary = next((e for e in emails if e.get("primary")), None)
        email = (primary or {}).get("email", "")

    return _login_or_create_oauth_user(
        provider="github",
        oauth_id=str(profile["id"]),
        email=email or "",
        name=profile.get("name") or profile.get("login", ""),
    )


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
