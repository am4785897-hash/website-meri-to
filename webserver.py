"""
Standalone entry point so you can run and test the auth system on its own.

    python webserver.py

index.html (the homepage) is public - anyone can view it without logging in.
Only the *other* content pages (notes.html, cheatsheets.html, etc. under
"individual html files/") and the audio files require login. CSS/JS/image/
font files stay public so every page renders styled either way.

The user logs in by clicking the "login" link on index.html - that's a
normal link to /login on this same running server, so clicking it sends
the browser straight to this server's /login route (GET shows the form,
POST on submit logs the user in). No separate server needed.

--- PATHS ---
Everything below is now RELATIVE to this file's location, instead of a
hardcoded "C:\\Users\\HP\\..." path. That means:
  - It works the same on your laptop (Windows) and on any Linux server
    you deploy to (Render, Railway, PythonAnywhere, a VPS, etc).
  - As long as this "website" folder structure stays the same relative
    to itself (i.e. you move/upload the WHOLE website/ folder together,
    not just authsystem/), nothing needs to be edited.

Expected layout (siblings of the "authsystem" folder that this file
lives in):

    website/
    |-- authsystem/                 <- this file lives here
    |   |-- webserver.py
    |   `-- templates/              <- login.html, signup.html
    |-- individual html files/      <- index.html, notes.html, css, etc.
    |-- audios/                     <- .mp3 files (add this folder when ready)
    |-- photos/                     <- .png/.jpg/.jpeg files (add when ready)
    `-- videos/                     <- not served by this file yet

If a folder (e.g. audios/, photos/) doesn't exist yet, the app still
starts fine - you'll just get a 404 if a page tries to load a file from
it until you add the folder back in.
"""

import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

from werkzeug.utils import secure_filename

try:
    import resource  # POSIX only - not available on Windows
except ImportError:
    resource = None

from flask import Flask, render_template, send_from_directory, send_file, Response, abort, request, jsonify, url_for, redirect, flash
from flask_login import login_required, current_user
from flask_wtf import CSRFProtect
from jinja2 import ChoiceLoader, FileSystemLoader

from config import Config
from extensions import db, login_manager, oauth, socketio
from models import (
    User, LearningPath, RecommendedItem, Course, Lesson, UserLessonProgress,
    DailyChallenge, UserDailyChallengeProgress, Project, UserProjectLike,
    AITutorConversation, AITutorMessage, Certificate, Badge, UserBadge,
    Note, Bookmark, XPEvent, CoinEvent, BattleQuestion, BattleAttempt, BATTLE_MODES, BATTLE_DIFFICULTIES,
    Tournament, RewardSetting, Announcement,
)
from admin import admin_bp
from battle import battle_bp, register_battle_socketio
from auth import auth_bp
from seed_data import (
    seed_learning_paths, ensure_progress_for_user, path_completion_percent,
    seed_recommended_items,
    seed_python_course, ensure_lesson_progress_for_user, course_completion_percent,
    seed_daily_challenges, get_todays_challenge, next_utc_midnight_iso, ensure_today_progress_for_user,
    seed_projects, project_gallery_view,
    seed_extra_courses,
    seed_badges, check_and_award_badges, badges_view,
    bookmarks_view,
    seed_battle_questions,
    seed_reward_settings,
)
from ai_provider import generate_tutor_reply, generate_tutor_image, ImageGenerationUnavailable, MODE_LABELS
from certificates import ensure_certificates_for_user, certificates_in_progress
from certificate_pdf import build_certificate_pdf, make_qr_png

csrf = CSRFProtect()

# ---------------------------------------------------------------------------
# Coin Shop catalog (Code Battle Arena economy - see the master spec's
# "Spend coins on AI Premium, Themes, Frames, Avatars, Challenges" line).
#
# Deliberately a plain in-code tuple, not a DB table: these are cosmetic/
# perk items an admin would rarely change, same reasoning as BATTLE_MODES
# in models.py being a plain tuple rather than an enum table. Ownership is
# NOT a new model either - it's derived from the existing CoinEvent ledger
# (reusing Coins infra instead of adding a new one): every purchase calls
# User.spend_coins(price, source=f"shop:{item id}"), so "does this user own
# item X" is just "is there a CoinEvent row with that source for them",
# and the admin Economy dashboard's mint-vs-spend view keeps working
# unmodified since shop spends already flow through the same ledger.
# 'id' must stay short - CoinEvent.source is a String(30) and we store it
# as f"shop:{id}".
# ---------------------------------------------------------------------------
SHOP_CATALOG = (
    {
        "id": "ai_premium",
        "category": "AI Premium",
        "icon": "✨",
        "name": "AI Tutor Premium (30 days)",
        "description": "Priority AI Tutor replies and image generation for 30 days.",
        "price": 800,
    },
    {
        "id": "theme_sunset",
        "category": "Themes",
        "icon": "🎨",
        "name": "Sunset Theme",
        "description": "Warm amber/rose accent theme for your dashboard.",
        "price": 150,
    },
    {
        "id": "theme_neon",
        "category": "Themes",
        "icon": "🌌",
        "name": "Neon Nights Theme",
        "description": "High-contrast neon purple/cyan accent theme.",
        "price": 150,
    },
    {
        "id": "frame_gold",
        "category": "Frames",
        "icon": "🖼️",
        "name": "Gold Avatar Frame",
        "description": "A gilded frame around your avatar, visible on the leaderboard.",
        "price": 250,
    },
    {
        "id": "frame_neon",
        "category": "Frames",
        "icon": "💠",
        "name": "Neon Avatar Frame",
        "description": "A glowing cyan frame around your avatar.",
        "price": 250,
    },
    {
        "id": "avatar_astro",
        "category": "Avatars",
        "icon": "🧑‍🚀",
        "name": "Astro Coder Avatar",
        "description": "An astronaut-coder avatar icon for your profile.",
        "price": 200,
    },
    {
        "id": "avatar_ninja",
        "category": "Avatars",
        "icon": "🥷",
        "name": "Code Ninja Avatar",
        "description": "A stealthy ninja avatar icon for your profile.",
        "price": 200,
    },
    {
        "id": "challenge_pack",
        "category": "Challenges",
        "icon": "🧠",
        "name": "Bonus Challenge Pack",
        "description": "Unlocks a set of extra hard Daily Challenges.",
        "price": 350,
    },
    {
        "id": "merch_bundle",
        "category": "Merch",
        "icon": "🎁",
        "name": "Coder Enchanté Merch Bundle",
        "description": (
            "💻 Coding notebook, 🔑 Programming keychain, ☕ Small coder mug, "
            "and a 📝 funny note card: \u201cBug tumhara hai, solution bhi "
            "tumhara hai.\u201d The top-tier grand prize for the grindiest coders."
        ),
        "price": 1000000,
    },
)

_SHOP_ITEMS_BY_ID = {item["id"]: item for item in SHOP_CATALOG}


def _shop_owned_item_ids(user) -> set:
    """Which SHOP_CATALOG ids this user has already purchased, derived
    from the CoinEvent ledger (see the SHOP_CATALOG comment above for why
    there's no separate ownership table)."""
    if not user.is_authenticated:
        return set()
    sources = {f"shop:{item['id']}" for item in SHOP_CATALOG}
    rows = (
        CoinEvent.query
        .filter(CoinEvent.user_id == user.id, CoinEvent.source.in_(sources))
        .all()
    )
    return {row.source.split("shop:", 1)[1] for row in rows}


# ---------------------------------------------------------------------------
# Code Playground execution limits (see run_code() below for the full
# security note on what this does and does not protect against).
# ---------------------------------------------------------------------------
RUN_TIMEOUT_SECONDS = 8
RUN_MAX_OUTPUT_CHARS = 4000
RUN_MAX_CODE_CHARS = 20000
RUN_MAX_MEMORY_BYTES = 256 * 1024 * 1024  # 256 MB


def _limit_child_resources():
    """
    Runs inside the child process, right before it execs python3, on
    POSIX systems only (Windows has no resource module - guarded above).
    Caps CPU time / memory / output file size and - importantly - sets
    the process count limit to 0 so the submitted code can't fork/spawn
    further processes (blocks fork-bombs and further subprocess calls).
    """
    resource.setrlimit(resource.RLIMIT_CPU, (RUN_TIMEOUT_SECONDS, RUN_TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (RUN_MAX_MEMORY_BYTES, RUN_MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))

# ---------------------------------------------------------------------------
# BASE_DIR = the "authsystem" folder (where this file lives).
# WEBSITE_DIR = its parent, i.e. the top-level "website" folder.
# Every content folder is found relative to WEBSITE_DIR - no more hardcoded
# Windows paths, so this works on any machine/server without editing.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBSITE_DIR = os.path.dirname(BASE_DIR)

OTHER_FOLDER = os.path.join(WEBSITE_DIR, "individual html files")
AUDIO_FOLDER = os.path.join(WEBSITE_DIR, "audios")
IMAGES_FOLDER = os.path.join(WEBSITE_DIR, "photos")

# AI Tutor attachments - files the learner sends in, and images the tutor
# generates back. Each user gets their own subfolder (by user id) so one
# learner can never browse/guess their way into another's files.
AI_TUTOR_UPLOADS_FOLDER = os.path.join(WEBSITE_DIR, "ai_tutor_uploads")
AI_TUTOR_GENERATED_FOLDER = os.path.join(WEBSITE_DIR, "ai_tutor_generated")
os.makedirs(AI_TUTOR_UPLOADS_FOLDER, exist_ok=True)
os.makedirs(AI_TUTOR_GENERATED_FOLDER, exist_ok=True)

# File extensions that are safe to serve WITHOUT login (so the login/signup
# pages themselves can load their styling, fonts, icons, etc). Everything
# else under OTHER_FOLDER (mainly the other .html pages) requires login.
PUBLIC_STATIC_EXTENSIONS = {
    ".css", ".js", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".docx", ".pptx", ".txt",
    ".mp4"
}


def _migrate_sqlite_schema(flask_app):
    """
    db.create_all() only creates tables that don't exist yet - it never
    alters an existing table. Since instance/users.db already has real
    signed-up accounts in it, add new columns onto existing tables in
    place (SQLite supports ADD COLUMN) instead of dropping/recreating
    anything.
    """
    from sqlalchemy import inspect, text

    with flask_app.app_context():
        inspector = inspect(db.engine)
        existing_tables = set(inspector.get_table_names())

        with db.engine.begin() as conn:
            if "users" in existing_tables:
                existing_columns = {col["name"] for col in inspector.get_columns("users")}
                missing_columns = {
                    "total_xp": "INTEGER NOT NULL DEFAULT 0",
                    "streak_days": "INTEGER NOT NULL DEFAULT 0",
                    "last_active_date": "DATE",
                    "total_coins": "INTEGER NOT NULL DEFAULT 0",
                    "is_admin": "BOOLEAN NOT NULL DEFAULT 0",
                    "is_bot": "BOOLEAN NOT NULL DEFAULT 0",
                    "is_banned": "BOOLEAN NOT NULL DEFAULT 0",
                    "ban_reason": "VARCHAR(200)",
                }
                for name, ddl_type in missing_columns.items():
                    if name not in existing_columns:
                        conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl_type}"))

            if "ai_tutor_messages" in existing_tables:
                existing_columns = {col["name"] for col in inspector.get_columns("ai_tutor_messages")}
                missing_columns = {
                    "attachment_url": "VARCHAR(255)",
                    "attachment_name": "VARCHAR(255)",
                    "attachment_kind": "VARCHAR(10)",
                    "attachment_text": "TEXT",
                }
                for name, ddl_type in missing_columns.items():
                    if name not in existing_columns:
                        conn.execute(text(f"ALTER TABLE ai_tutor_messages ADD COLUMN {name} {ddl_type}"))


def create_app():
    flask_app = Flask(__name__)
    flask_app.config.from_object(Config)

    # Lets render_template() find pages that live in "individual html
    # files/" (like index.html, notes.html) in addition to authsystem's
    # own templates/ folder.
    flask_app.jinja_loader = ChoiceLoader([
        flask_app.jinja_loader,
        FileSystemLoader(OTHER_FOLDER),
    ])

    db.init_app(flask_app)
    csrf.init_app(flask_app)
    login_manager.init_app(flask_app)
    oauth.init_app(flask_app)

    # Speed Battle's live matchmaking/rooms (battle.py). Exempt Socket.IO's
    # own route from CSRFProtect: its polling-transport fallback sends
    # plain POSTs that never carry Flask-WTF's csrf_token, and it isn't
    # an HTML <form> submission CSRF protection was meant to guard -
    # every event handler below still re-checks
    # current_user.is_authenticated itself. flask_wtf has no built-in
    # path-based exemption, so grab the view function Flask-SocketIO
    # just registered and exempt that directly.
    socketio.init_app(flask_app, cors_allowed_origins=[])
    for rule in flask_app.url_map.iter_rules():
        if rule.rule.startswith("/socket.io"):
            csrf.exempt(flask_app.view_functions[rule.endpoint])
    register_battle_socketio(socketio)

    # "Continue with Google" - only registered if credentials are set, so
    # the app still starts fine even before you've set these up.
    if flask_app.config["GOOGLE_CLIENT_ID"]:
        oauth.register(
            name="google",
            client_id=flask_app.config["GOOGLE_CLIENT_ID"],
            client_secret=flask_app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    # "Continue with GitHub"
    if flask_app.config["GITHUB_CLIENT_ID"]:
        oauth.register(
            name="github",
            client_id=flask_app.config["GITHUB_CLIENT_ID"],
            client_secret=flask_app.config["GITHUB_CLIENT_SECRET"],
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id):
        user = User.query.get(int(user_id))
        # A banned account is treated as logged out on every request from
        # here on - see models.py's User.is_banned docstring for why this
        # is checked here rather than only at login time (an admin can
        # ban someone who's mid-session).
        if user is not None and user.is_banned:
            return None
        return user

    @flask_app.context_processor
    def inject_announcement():
        """Makes the current site-wide announcement (Admin > Users)
        available to every template via `announcement`, so
        base_dashboard.html can render the banner on first page load
        without needing JS - battle.py's socket push
        (site:announcement_updated) is what keeps it live after that,
        on tabs that are already open."""
        row = Announcement.query.get(1)
        if row is None or not row.is_active or not row.message.strip():
            return {"announcement": None}
        return {"announcement": row}

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(admin_bp, url_prefix="/admin")
    flask_app.register_blueprint(battle_bp, url_prefix="/battle")

    @flask_app.route("/")
    def home():
        # index.html is public - it shows to everyone, logged in or not.
        # index.html itself checks {% if current_user.is_authenticated %}
        # to show "Welcome, username" + logout link, or the login link
        # if not logged in.
        return render_template("index.html", user=current_user)

    # Maps a path's slug to the existing dashboard.css modifier class
    # (.tv-path-card--hacking / --aiml / --appdev) that gives each card
    # its themed cover gradient. Shared by every page that lists paths.
    PATH_CSS_CLASS = {
        "ethical-hacking": "hacking",
        "ai-ml": "aiml",
        "app-development": "appdev",
    }

    # Human-readable label for a LearningPath.slug / RecommendedItem.category /
    # Project.category / DailyChallenge.category - they all share the same 3
    # slugs, so one mapping covers Recommended-for-You filters, the Projects
    # gallery filters, and the Daily Challenge category pill.
    LEARNING_CATEGORY_LABELS = {
        "ethical-hacking": "Ethical Hacking",
        "ai-ml": "AI/ML",
        "app-development": "App Development",
    }

    def _learning_paths_overview(user):
        """All 3 paths with each one's real completion % for this user
        (initializes progress rows on first view of a path - first step
        active, everything else locked). Shared by the dashboard's
        'Continue Learning' cards and the /roadmaps path-switcher tabs."""
        paths = []
        for path in LearningPath.query.order_by(LearningPath.order_index).all():
            paths.append({
                "slug": path.slug,
                "title": path.title,
                "subtitle": path.subtitle,
                "icon": path.icon,
                "percent": path_completion_percent(user, path),
                "css_class": PATH_CSS_CLASS.get(path.slug, "hacking"),
            })
        return paths

    def _roadmap_widget(user, featured_path, link_endpoint):
        """Builds the two-row roadmap dict (title/subtitle/row1/row2) for
        one path. `link_endpoint` is whichever route rendered the widget
        (dashboard vs. roadmaps), so each step's anchor link comes back
        to the right page with `?path=<slug>#<step_id>`."""
        roadmap = {"title": "Complete Your Roadmap", "subtitle": "", "row1": [], "row2": []}
        if featured_path is None:
            return roadmap

        progress_rows = ensure_progress_for_user(user, featured_path)
        roadmap["title"] = f"{featured_path.title} Roadmap"
        roadmap["subtitle"] = f"Complete roadmap to master {featured_path.title}"
        for progress in progress_rows:
            bucket = "row1" if progress.step.row == 1 else "row2"
            roadmap[bucket].append({
                "name": progress.step.name,
                "status": progress.status,
                "href": url_for(link_endpoint, path=featured_path.slug) + f"#{progress.step_id}",
            })
        return roadmap

    def _todays_challenge_card(user):
        """Builds the dict the challenge-card template fragment needs
        (today's title/description/reward/status/reset countdown). Shared
        by the dashboard widget and the standalone /daily-challenges page
        so the two never drift out of sync."""
        todays_challenge = get_todays_challenge()
        if todays_challenge is None:
            return None
        my_progress = ensure_today_progress_for_user(user, todays_challenge)
        return {
            "slug": todays_challenge.slug,
            "title": todays_challenge.title,
            "description": todays_challenge.description,
            "category": todays_challenge.category,
            "reward_xp": todays_challenge.reward_xp,
            "status": my_progress.status,
            "reset_at_iso": next_utc_midnight_iso(),
        }

    @flask_app.route("/dashboard")
    @login_required
    def dashboard():
        # Keeps the daily streak honest (increments/resets based on the
        # user's last visit) before we read streak_days back out below.
        current_user.register_activity()
        db.session.commit()

        # Real leaderboard rank, computed from every user's actual XP -
        # no hardcoded "#48".
        higher_xp_count = User.query.filter(User.total_xp > current_user.total_xp).count()
        total_users = User.query.count()
        rank = higher_xp_count + 1
        rank_percentile = max(1, round(rank / total_users * 100)) if total_users else 100

        stats = {
            "streak_days": current_user.streak_days,
            "total_xp": current_user.total_xp,
            "level": current_user.level,
            "rank": rank,
            "rank_percentile": rank_percentile,
        }

        # All 3 paths, each with a real completion % for "Continue
        # Learning" (initializes this user's progress rows on first view
        # of a path - first step active, everything else locked).
        paths = _learning_paths_overview(current_user)

        # Roadmap widget shows whichever path the user asked for via
        # ?path=<slug> (falls back to Ethical Hacking, the featured path).
        featured_slug = request.args.get("path", "ethical-hacking")
        featured_path = LearningPath.query.filter_by(slug=featured_slug).first() \
            or LearningPath.query.order_by(LearningPath.order_index).first()

        roadmap = _roadmap_widget(current_user, featured_path, "dashboard")

        # Recommended for You grid. Pills filter client-side by
        # data-category, so we just hand over every item + the distinct
        # categories present (in a fixed, sensible order) to build the pills.
        recommended_items = RecommendedItem.query.order_by(RecommendedItem.order_index).all()
        present_categories = {item.category for item in recommended_items}
        recommended_filters = [{"key": "popular", "label": "Popular"}] + [
            {"key": slug, "label": label}
            for slug, label in LEARNING_CATEGORY_LABELS.items()
            if slug in present_categories
        ]

        # "Run" now executes real Python via the /api/run-code route below.
        # TODO(step 6.1+): swap the resource-limited subprocess for a real
        # container/VM sandbox (Docker/gVisor/nsjail/firecracker) before
        # opening signup to the public - see run_code()'s docstring.
        playground = {
            "languages": ["Python"],
            "starter_code": (
                "import socket\n\n"
                "target = input(\"Enter target IP: \")\n"
                "port = int(input(\"Enter port: \"))\n\n"
                "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                "result = s.connect_ex((target, port))\n\n"
                "if result == 0:\n"
                "    print(f\"Port {port} is Open on {target}\")\n"
                "else:\n"
                "    print(f\"Port {port} is Closed on {target}\")\n\n"
                "s.close()"
            ),
            "stdin_placeholder": "127.0.0.1\n22",
            "output_placeholder": (
                "Enter target IP: 127.0.0.1\nEnter port: 22\nPort 22 is Open on 127.0.0.1"
            ),
        }

        # Daily Challenge - same challenge + same UTC reset time for every
        # user today; per-user status tracked separately so one user
        # completing it doesn't affect anyone else's card. Shared with the
        # standalone /daily-challenges page via _todays_challenge_card().
        daily_challenge = _todays_challenge_card(current_user)

        # Project Gallery - real per-user like state baked in server-side.
        projects = project_gallery_view(current_user)

        return render_template(
            "dashboard.html",
            stats=stats,
            roadmap=roadmap,
            paths=paths,
            recommended_items=recommended_items,
            recommended_filters=recommended_filters,
            playground=playground,
            daily_challenge=daily_challenge,
            projects=projects,
            active_page="dashboard",
        )

    @flask_app.route("/roadmaps")
    @login_required
    def roadmaps():
        """Standalone Roadmaps page: switch between all 3 learning paths
        (?path=<slug>) and see that path's full step-by-step roadmap,
        reusing the exact same widget/CSS as the dashboard card."""
        paths = _learning_paths_overview(current_user)

        featured_slug = request.args.get("path", "ethical-hacking")
        featured_path = LearningPath.query.filter_by(slug=featured_slug).first() \
            or LearningPath.query.order_by(LearningPath.order_index).first()

        roadmap = _roadmap_widget(current_user, featured_path, "roadmaps")

        return render_template(
            "roadmaps.html",
            paths=paths,
            roadmap=roadmap,
            featured_slug=featured_path.slug if featured_path else featured_slug,
            active_page="roadmaps",
        )

    @flask_app.route("/projects")
    @login_required
    def projects_page():
        """Standalone Project Gallery: every project, with client-side
        category filter pills (same data-categories pattern the
        dashboard's Recommended-for-You grid already uses)."""
        projects = project_gallery_view(current_user)

        present_categories = {p["category"] for p in projects}
        project_filters = [{"key": "all", "label": "All Projects"}] + [
            {"key": slug, "label": label}
            for slug, label in LEARNING_CATEGORY_LABELS.items()
            if slug in present_categories
        ]

        return render_template(
            "projects.html",
            projects=projects,
            project_filters=project_filters,
            active_page="projects",
        )

    LEADERBOARD_PERIODS = {"week", "month", "all"}

    def _period_start(period):
        """UTC start-of-window for a leaderboard period. None for 'all'
        (all-time uses User.total_xp directly, not the XP ledger)."""
        now = datetime.now(timezone.utc)
        if period == "week":
            monday = now.date() - timedelta(days=now.weekday())
            return datetime.combine(monday, datetime.min.time(), tzinfo=timezone.utc)
        if period == "month":
            return datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        return None

    @flask_app.route("/leaderboard")
    @login_required
    def leaderboard():
        """Real leaderboard, no placeholder rows. All-time ranks by
        User.total_xp (same number the dashboard rank card uses). Week/
        month rank by summing the XPEvent ledger since the start of that
        window - so a user with a huge all-time total but nothing earned
        this week correctly doesn't top the weekly board."""
        period = request.args.get("period", "week")
        if period not in LEADERBOARD_PERIODS:
            period = "week"
        window_start = _period_start(period)

        if window_start is None:
            top_users = User.query.filter_by(is_bot=False).order_by(User.total_xp.desc()).limit(50).all()
            ranked = [(u.id, u.username, u.total_xp) for u in top_users]
        else:
            xp_sum = db.func.coalesce(db.func.sum(XPEvent.amount), 0)
            ranked = (
                db.session.query(User.id, User.username, xp_sum.label("period_xp"))
                .join(XPEvent, XPEvent.user_id == User.id)
                .filter(XPEvent.created_at >= window_start, User.is_bot == False)  # noqa: E712 - SQLAlchemy needs == here, not `is`
                .group_by(User.id)
                .order_by(xp_sum.desc())
                .limit(50)
                .all()
            )

        rows = [
            {"rank": i + 1, "username": username, "xp": xp, "is_current_user": user_id == current_user.id}
            for i, (user_id, username, xp) in enumerate(ranked)
        ]

        # If the viewer isn't in the top 50, still show them their real
        # rank/XP below the list instead of just omitting them.
        my_row = None
        if not any(row["is_current_user"] for row in rows):
            if window_start is None:
                my_xp = current_user.total_xp
                higher_count = User.query.filter(User.total_xp > my_xp).count()
            else:
                my_xp = (
                    db.session.query(db.func.coalesce(db.func.sum(XPEvent.amount), 0))
                    .filter(XPEvent.user_id == current_user.id, XPEvent.created_at >= window_start)
                    .scalar()
                )
                higher_count = (
                    db.session.query(XPEvent.user_id)
                    .filter(XPEvent.created_at >= window_start)
                    .group_by(XPEvent.user_id)
                    .having(db.func.coalesce(db.func.sum(XPEvent.amount), 0) > my_xp)
                    .count()
                )
            my_row = {"rank": higher_count + 1, "username": current_user.username, "xp": my_xp, "is_current_user": True}

        return render_template(
            "leaderboard.html",
            rows=rows,
            my_row=my_row,
            period=period,
            active_page="leaderboard",
        )

    # -----------------------------------------------------------------
    # Python Mentor Course
    # -----------------------------------------------------------------
    def _get_course(course_slug):
        return Course.query.filter_by(slug=course_slug).first()

    def _youtube_embed_url(url):
        """Turns a youtu.be / youtube.com/watch link into an /embed/ link
        the iframe can actually play. Mirrors the same logic the old
        'individual html files/python course.html' page used client-side,
        just done once here instead of on every page load in JS."""
        if not url:
            return None
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            video_id = None
            if "youtu.be" in parsed.netloc:
                video_id = parsed.path.lstrip("/")
            elif "youtube.com" in parsed.netloc:
                if parsed.path == "/watch":
                    video_id = parse_qs(parsed.query).get("v", [None])[0]
                elif parsed.path.startswith("/embed/"):
                    return url
            return f"https://www.youtube.com/embed/{video_id}" if video_id else url
        except Exception:
            return url

    @flask_app.route("/courses")
    @login_required
    def courses():
        # Reuses the same 3 cover-color themes the "Continue Learning"
        # path cards already use, so these cards look consistent rather
        # than falling back to an unstyled default.
        css_class_by_slug = {
            "web-penetration-testing": "hacking",
            "machine-learning-basics": "aiml",
            "python-fundamentals": "aiml",
            "flutter-app-development": "appdev",
        }
        all_courses = Course.query.order_by(Course.order_index).all()
        courses_view = [{
            "slug": c.slug,
            "title": c.title,
            "subtitle": c.subtitle,
            "icon": c.icon,
            "lesson_count": c.lessons.count(),
            "percent": course_completion_percent(current_user, c),
            "css_class": css_class_by_slug.get(c.slug, "aiml"),
        } for c in all_courses]
        return render_template("courses.html", courses=courses_view, active_page="courses")

    @flask_app.route("/python-course")
    @login_required
    def python_course_overview():
        """Old URL, kept working for anyone with it bookmarked - redirects
        to the generic course route below."""
        return redirect(url_for("course_overview", course_slug="python-fundamentals"))

    @flask_app.route("/python-course/<lesson_slug>")
    @login_required
    def python_lesson(lesson_slug):
        """Old URL, kept working for anyone with it bookmarked - redirects
        to the generic course route below."""
        return redirect(url_for("course_lesson", course_slug="python-fundamentals", lesson_slug=lesson_slug))

    @flask_app.route("/course/<course_slug>")
    @login_required
    def course_overview(course_slug):
        """Sends the learner straight to their next lesson (or the first
        one, on a brand-new account) instead of an extra landing page.
        Works for any course - python-fundamentals, web-penetration-testing,
        machine-learning-basics, flutter-app-development, or any future one."""
        course = _get_course(course_slug)
        if course is None:
            abort(404)
        progress_rows = ensure_lesson_progress_for_user(current_user, course)
        target = next((p for p in progress_rows if p.status != "complete"), progress_rows[-1])
        return redirect(url_for("course_lesson", course_slug=course_slug, lesson_slug=target.lesson.slug))

    @flask_app.route("/course/<course_slug>/<lesson_slug>")
    @login_required
    def course_lesson(course_slug, lesson_slug):
        course = _get_course(course_slug)
        if course is None:
            abort(404)

        lesson = Lesson.query.filter_by(course_id=course.id, slug=lesson_slug).first()
        if lesson is None:
            abort(404)

        progress_rows = ensure_lesson_progress_for_user(current_user, course)
        progress_by_lesson = {p.lesson_id: p for p in progress_rows}
        my_progress = progress_by_lesson[lesson.id]

        # A locked lesson can't be viewed directly (no skipping ahead via URL) -
        # send the learner back to whichever lesson they're actually on.
        if my_progress.status == "locked":
            active = next((p for p in progress_rows if p.status == "active"), progress_rows[0])
            return redirect(url_for("course_lesson", course_slug=course_slug, lesson_slug=active.lesson.slug))

        lessons = list(course.lessons)
        index = lessons.index(lesson)
        prev_lesson = lessons[index - 1] if index > 0 else None
        next_lesson = lessons[index + 1] if index < len(lessons) - 1 else None

        sidebar_lessons = [{
            "slug": l.slug,
            "title": l.title,
            "status": progress_by_lesson[l.id].status,
        } for l in lessons]

        return render_template(
            "python-lesson.html",
            course=course,
            lesson=lesson,
            my_progress=my_progress,
            prev_lesson=prev_lesson,
            next_lesson=next_lesson,
            sidebar_lessons=sidebar_lessons,
            percent=course_completion_percent(current_user, course),
            embed_url=_youtube_embed_url(lesson.video_url),
            is_bookmarked=Bookmark.query.filter_by(
                user_id=current_user.id, target_type="lesson", target_id=lesson.id
            ).first() is not None,
            active_page="courses",
        )

    @flask_app.route("/api/lesson-complete", methods=["POST"])
    @login_required
    def api_lesson_complete():
        """Marks a lesson complete, awards its XP once, and unlocks the next
        lesson in the course. Idempotent - replaying it awards no extra XP.
        Works for any course (course_slug defaults to python-fundamentals
        for old clients that don't send one yet)."""
        data = request.get_json(silent=True) or {}
        course = _get_course(data.get("course_slug", "python-fundamentals"))
        if course is None:
            return jsonify({"error": "Course not found."}), 404
        lesson = Lesson.query.filter_by(course_id=course.id, slug=data.get("lesson_slug", "")).first()
        if lesson is None:
            return jsonify({"error": "Lesson not found."}), 404

        progress_rows = ensure_lesson_progress_for_user(current_user, course)
        my_progress = next(p for p in progress_rows if p.lesson_id == lesson.id)

        if my_progress.status != "complete":
            my_progress.status = "complete"
            my_progress.completed_at = db.func.now()
            current_user.award_xp(lesson.xp_reward, source="lesson")

            lessons = list(course.lessons)
            index = lessons.index(lesson)
            if index + 1 < len(lessons):
                next_progress = next(p for p in progress_rows if p.lesson_id == lessons[index + 1].id)
                if next_progress.status == "locked":
                    next_progress.status = "active"

            db.session.commit()

        new_badges = check_and_award_badges(current_user)

        return jsonify({
            "xp_awarded": lesson.xp_reward,
            "total_xp": current_user.total_xp,
            "level": current_user.level,
            "percent": course_completion_percent(current_user, course),
            "new_badges": [
                {"slug": b.slug, "title": b.title, "icon": b.icon, "description": b.description}
                for b in new_badges
            ],
        })

    @flask_app.route("/api/lesson-quiz-check", methods=["POST"])
    @login_required
    def api_lesson_quiz_check():
        """Checks a quick-check answer server-side (so the correct index
        never has to be sent to the browser) and pays out the quiz XP
        bonus exactly once per user per lesson. Works for any course."""
        data = request.get_json(silent=True) or {}
        course = _get_course(data.get("course_slug", "python-fundamentals"))
        if course is None:
            return jsonify({"error": "Course not found."}), 404
        lesson = Lesson.query.filter_by(course_id=course.id, slug=data.get("lesson_slug", "")).first()
        if lesson is None or lesson.quiz_correct_index is None:
            return jsonify({"error": "No quiz for this lesson."}), 404

        chosen_index = data.get("chosen_index")
        is_correct = chosen_index == lesson.quiz_correct_index

        xp_awarded = 0
        if is_correct:
            progress_rows = ensure_lesson_progress_for_user(current_user, course)
            my_progress = next(p for p in progress_rows if p.lesson_id == lesson.id)
            if not my_progress.quiz_correct:
                my_progress.quiz_correct = True
                current_user.award_xp(lesson.quiz_xp_bonus, source="quiz")
                xp_awarded = lesson.quiz_xp_bonus
                db.session.commit()

        new_badges = check_and_award_badges(current_user) if xp_awarded else []

        return jsonify({
            "correct": is_correct,
            "explanation": lesson.quiz_explanation,
            "xp_awarded": xp_awarded,
            "total_xp": current_user.total_xp,
            "new_badges": [
                {"slug": b.slug, "title": b.title, "icon": b.icon, "description": b.description}
                for b in new_badges
            ],
        })

    @flask_app.route("/daily-challenges")
    @login_required
    def daily_challenges():
        """Standalone Daily Challenges page: today's card (same widget/JS
        as the dashboard) plus this learner's streak stats and a short
        history of past days' challenges."""
        daily_challenge = _todays_challenge_card(current_user)
        if daily_challenge is not None:
            daily_challenge["category_label"] = LEARNING_CATEGORY_LABELS.get(
                daily_challenge["category"], daily_challenge["category"]
            )

        completed_count = UserDailyChallengeProgress.query.filter_by(
            user_id=current_user.id, status="complete"
        ).count()

        history_rows = (
            db.session.query(UserDailyChallengeProgress, DailyChallenge)
            .join(DailyChallenge, UserDailyChallengeProgress.challenge_id == DailyChallenge.id)
            .filter(UserDailyChallengeProgress.user_id == current_user.id)
            .order_by(UserDailyChallengeProgress.challenge_date.desc())
            .limit(14)
            .all()
        )

        total_xp_earned = sum(
            challenge.reward_xp for progress, challenge in history_rows if progress.status == "complete"
        )

        history = [
            {
                "date": progress.challenge_date.strftime("%b %d"),
                "title": challenge.title,
                "category_label": LEARNING_CATEGORY_LABELS.get(challenge.category, challenge.category),
                "reward_xp": challenge.reward_xp,
                "status": progress.status,
            }
            for progress, challenge in history_rows
        ]

        challenge_stats = {
            "current_streak": current_user.streak_days,
            "completed_count": completed_count,
            "total_xp_earned": total_xp_earned,
        }

        return render_template(
            "daily-challenges.html",
            daily_challenge=daily_challenge,
            challenge_stats=challenge_stats,
            history=history,
            active_page="challenges",
        )

    @flask_app.route("/api/daily-challenge/start", methods=["POST"])
    @login_required
    def api_daily_challenge_start():
        """Flips today's challenge from 'not_started' to 'started' for this
        user. Idempotent - re-posting once started/complete is a no-op."""
        challenge = get_todays_challenge()
        if challenge is None:
            return jsonify({"error": "No challenge is active today."}), 404

        progress = ensure_today_progress_for_user(current_user, challenge)
        if progress.status == "not_started":
            progress.status = "started"
            db.session.commit()

        return jsonify({"status": progress.status})

    @flask_app.route("/api/daily-challenge/complete", methods=["POST"])
    @login_required
    def api_daily_challenge_complete():
        """Marks today's challenge complete and awards its XP exactly once
        per user per day, no matter how many times this is called."""
        challenge = get_todays_challenge()
        if challenge is None:
            return jsonify({"error": "No challenge is active today."}), 404

        progress = ensure_today_progress_for_user(current_user, challenge)
        xp_awarded = 0
        if progress.status != "complete":
            progress.status = "complete"
            progress.completed_at = db.func.now()
            current_user.award_xp(challenge.reward_xp, source="challenge")
            xp_awarded = challenge.reward_xp
            db.session.commit()

        new_badges = check_and_award_badges(current_user) if xp_awarded else []

        return jsonify({
            "status": progress.status,
            "xp_awarded": xp_awarded,
            "total_xp": current_user.total_xp,
            "level": current_user.level,
            "new_badges": [
                {"slug": b.slug, "title": b.title, "icon": b.icon, "description": b.description}
                for b in new_badges
            ],
        })

    @flask_app.route("/api/project-like/<slug>", methods=["POST"])
    @login_required
    def api_project_like(slug):
        """Toggles this user's ❤ on a project and returns the fresh count."""
        project = Project.query.filter_by(slug=slug).first()
        if project is None:
            return jsonify({"error": "Project not found."}), 404

        existing_like = UserProjectLike.query.filter_by(
            user_id=current_user.id, project_id=project.id
        ).first()

        if existing_like is None:
            db.session.add(UserProjectLike(user_id=current_user.id, project_id=project.id))
            liked = True
        else:
            db.session.delete(existing_like)
            liked = False
        db.session.commit()

        return jsonify({"liked": liked, "likes_count": project.likes_count()})

    # -----------------------------------------------------------------
    # Badges
    # -----------------------------------------------------------------
    @flask_app.route("/badges")
    @login_required
    def badges():
        # Catches anyone who earned a qualifying badge before this feature
        # existed (e.g. already had 500 XP) - they see it unlocked on
        # their very first visit instead of only from here on.
        check_and_award_badges(current_user)
        rows = badges_view(current_user)
        earned_count = sum(1 for r in rows if r["earned"])
        return render_template(
            "badges.html",
            badge_rows=rows,
            earned_count=earned_count,
            total_count=len(rows),
            active_page="badges",
        )

    # -----------------------------------------------------------------
    # Notes
    # -----------------------------------------------------------------
    @flask_app.route("/notes")
    @login_required
    def notes():
        my_notes = Note.query.filter_by(user_id=current_user.id).order_by(Note.updated_at.desc()).all()
        return render_template("notes.html", notes=my_notes, active_page="notes")

    @flask_app.route("/api/notes", methods=["POST"])
    @login_required
    def api_notes_create():
        data = request.get_json(silent=True) or {}
        note = Note(
            user_id=current_user.id,
            title=(data.get("title") or "Untitled note")[:160],
            content=data.get("content", ""),
            course_slug=data.get("course_slug") or None,
        )
        db.session.add(note)
        db.session.commit()
        return jsonify({
            "id": note.id, "title": note.title, "content": note.content,
            "updated_at": note.updated_at.strftime("%d %b, %H:%M"),
        })

    @flask_app.route("/api/notes/<int:note_id>", methods=["PUT"])
    @login_required
    def api_notes_update(note_id):
        note = Note.query.filter_by(id=note_id, user_id=current_user.id).first()
        if note is None:
            return jsonify({"error": "Note not found."}), 404
        data = request.get_json(silent=True) or {}
        if "title" in data:
            note.title = (data.get("title") or "Untitled note")[:160]
        if "content" in data:
            note.content = data.get("content", "")
        db.session.commit()
        return jsonify({
            "id": note.id, "title": note.title, "content": note.content,
            "updated_at": note.updated_at.strftime("%d %b, %H:%M"),
        })

    @flask_app.route("/api/notes/<int:note_id>", methods=["DELETE"])
    @login_required
    def api_notes_delete(note_id):
        note = Note.query.filter_by(id=note_id, user_id=current_user.id).first()
        if note is None:
            return jsonify({"error": "Note not found."}), 404
        db.session.delete(note)
        db.session.commit()
        return jsonify({"deleted": True})

    # -----------------------------------------------------------------
    # Bookmarks
    # -----------------------------------------------------------------
    @flask_app.route("/bookmarks")
    @login_required
    def bookmarks():
        return render_template("bookmarks.html", bookmarks=bookmarks_view(current_user), active_page="bookmarks")

    @flask_app.route("/api/bookmark/toggle", methods=["POST"])
    @login_required
    def api_bookmark_toggle():
        data = request.get_json(silent=True) or {}
        target_type = data.get("target_type")
        target_id = data.get("target_id")
        if target_type not in ("lesson", "project") or not isinstance(target_id, int):
            return jsonify({"error": "Invalid bookmark target."}), 400

        existing = Bookmark.query.filter_by(
            user_id=current_user.id, target_type=target_type, target_id=target_id,
        ).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
            return jsonify({"bookmarked": False})

        db.session.add(Bookmark(user_id=current_user.id, target_type=target_type, target_id=target_id))
        db.session.commit()
        return jsonify({"bookmarked": True})

    # -----------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------
    @flask_app.route("/settings")
    @login_required
    def settings():
        return render_template("settings.html", active_page="settings")

    @flask_app.route("/api/settings/profile", methods=["POST"])
    @login_required
    def api_settings_profile():
        data = request.get_json(silent=True) or {}
        new_username = (data.get("username") or "").strip()
        if len(new_username) < 3:
            return jsonify({"error": "Username must be at least 3 characters."}), 400
        clash = User.query.filter(User.username == new_username, User.id != current_user.id).first()
        if clash:
            return jsonify({"error": "That username is already taken."}), 400
        current_user.username = new_username
        db.session.commit()
        return jsonify({"username": current_user.username})

    @flask_app.route("/api/settings/password", methods=["POST"])
    @login_required
    def api_settings_password():
        if not current_user.password_hash:
            return jsonify({"error": "This account signs in with Google/GitHub - there's no site password to change."}), 400

        data = request.get_json(silent=True) or {}
        current_password = data.get("current_password", "")
        new_password = data.get("new_password", "")

        if not current_user.check_password(current_password):
            return jsonify({"error": "Current password is incorrect."}), 400
        if len(new_password) < 8:
            return jsonify({"error": "New password must be at least 8 characters."}), 400

        current_user.set_password(new_password)
        db.session.commit()
        return jsonify({"success": True})

    # -----------------------------------------------------------------
    # Certificates
    # -----------------------------------------------------------------
    @flask_app.route("/certificates")
    @login_required
    def certificates():
        ensure_certificates_for_user(current_user)  # cheap no-op for anything already issued

        earned = (
            Certificate.query.filter_by(user_id=current_user.id)
            .order_by(Certificate.issued_at.desc())
            .all()
        )
        in_progress = certificates_in_progress(current_user)

        return render_template(
            "certificates.html",
            earned=earned,
            in_progress=in_progress,
            active_page="certificates",
        )

    @flask_app.route("/certificates/<uid>/download")
    @login_required
    def certificate_download(uid):
        certificate = Certificate.query.filter_by(certificate_uid=uid).first_or_404()
        if certificate.user_id != current_user.id:
            abort(403)

        verify_url = url_for("verify_certificate", uid=uid, _external=True)
        pdf_buffer = build_certificate_pdf(certificate, current_user.username, verify_url)
        return send_file(
            pdf_buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{certificate.certificate_uid}.pdf",
        )

    @flask_app.route("/certificates/<uid>/qr.png")
    @login_required
    def certificate_qr(uid):
        certificate = Certificate.query.filter_by(certificate_uid=uid).first_or_404()
        if certificate.user_id != current_user.id:
            abort(403)

        verify_url = url_for("verify_certificate", uid=uid, _external=True)
        png_bytes = make_qr_png(verify_url)
        return Response(png_bytes, mimetype="image/png")

    @flask_app.route("/verify/<uid>")
    def verify_certificate(uid):
        """
        Public, no login required - this is the page a recruiter or
        anyone else lands on after scanning the QR code / clicking a
        shared certificate link, to confirm it's real.
        """
        certificate = Certificate.query.filter_by(certificate_uid=uid).first()
        return render_template("verify.html", certificate=certificate)

    # -----------------------------------------------------------------
    # AI Tutor
    # -----------------------------------------------------------------
    def _get_or_create_active_conversation(user):
        """Returns this user's most recent chat, or starts a fresh one on
        their very first visit to the AI Tutor page."""
        conversation = (
            AITutorConversation.query
            .filter_by(user_id=user.id)
            .order_by(AITutorConversation.created_at.desc())
            .first()
        )
        if conversation is None:
            conversation = AITutorConversation(user_id=user.id, title="New Chat")
            db.session.add(conversation)
            db.session.commit()
        return conversation

    def _message_to_dict(m: AITutorMessage) -> dict:
        """Everything the frontend needs to render one chat bubble,
        including an attachment if there is one. Deliberately leaves
        attachment_text (the extracted file content used only for AI
        context) out - the learner never needs to see that dumped back
        at them."""
        return {
            "role": m.role,
            "content": m.content,
            "attachment_url": m.attachment_url,
            "attachment_name": m.attachment_name,
            "attachment_kind": m.attachment_kind,
        }

    def _history_for_ai(conversation) -> list[dict]:
        """Same message list, but with any extracted file text folded
        back into `content` for THIS call only - so the AI actually sees
        what was in an uploaded file, without that text ever being
        stored in / re-shown as the visible chat bubble."""
        history = []
        for m in conversation.messages:
            content = m.content
            if m.attachment_text:
                content += f"\n\n--- Attached file: {m.attachment_name} ---\n{m.attachment_text}\n--- end of file ---"
            history.append({"role": m.role, "content": content})
        return history

    def _save_uploaded_file(upload, user):
        """
        Validates and saves one uploaded file to this user's own
        subfolder. Returns (url, display_name, kind, extracted_text) -
        extracted_text is None for images (no OCR/vision wired in here -
        see the module docstring in ai_provider.py for how to add that
        later) and the file's text content (capped) for text/code files.
        Raises ValueError with a learner-facing message on anything not
        allowed.
        """
        original_name = secure_filename(upload.filename or "")
        ext = os.path.splitext(original_name)[1].lower()
        if not original_name or not ext:
            raise ValueError("That file doesn't have a recognizable name/extension.")

        is_text = ext in Config.AI_TUTOR_TEXT_EXTENSIONS
        is_image = ext in Config.AI_TUTOR_IMAGE_EXTENSIONS
        if not (is_text or is_image):
            raise ValueError(f"'{ext}' files aren't supported yet - try a text/code file or an image.")

        user_folder = os.path.join(AI_TUTOR_UPLOADS_FOLDER, str(user.id))
        os.makedirs(user_folder, exist_ok=True)

        stored_name = f"{uuid.uuid4().hex}_{original_name}"
        disk_path = os.path.join(user_folder, stored_name)
        upload.save(disk_path)

        url = url_for("ai_tutor_uploaded_file", filename=f"{user.id}/{stored_name}")
        extracted_text = None
        if is_text:
            try:
                with open(disk_path, "r", encoding="utf-8", errors="replace") as f:
                    extracted_text = f.read(Config.AI_TUTOR_MAX_EXTRACTED_CHARS)
            except OSError:
                extracted_text = None

        return url, original_name, ("image" if is_image else "file"), extracted_text

    @flask_app.route("/ai-tutor")
    @login_required
    def ai_tutor():
        conversation = _get_or_create_active_conversation(current_user)
        messages = [_message_to_dict(m) for m in conversation.messages]
        quick_actions = [
            {"mode": "doubt", "label": "Explain a concept", "starter": "Can you explain "},
            {"mode": "code_review", "label": "Review my code", "starter": "Please review this code:\n\n"},
            {"mode": "bug", "label": "Help me with bug", "starter": "I'm getting this error and I'm not sure why:\n\n"},
            {"mode": "project", "label": "Recommend next topic", "starter": "Based on what I've learned so far, what should I study next?"},
            {"mode": "quiz", "label": "Quiz me", "starter": "Quiz me on "},
            {"mode": "career", "label": "Career guidance", "starter": "What skills should I focus on to get job-ready in "},
            {"mode": "interview", "label": "Interview prep", "starter": "Give me an interview question about "},
            {"mode": "image_gen", "label": "Generate an image", "starter": "Generate an image of "},
        ]
        return render_template(
            "ai-tutor.html",
            messages=messages,
            quick_actions=quick_actions,
            active_page="ai_tutor",
        )

    @flask_app.route("/api/ai-tutor/message", methods=["POST"])
    @login_required
    def api_ai_tutor_message():
        # Two request shapes reach this route: plain JSON (typed message,
        # no file) and multipart/form-data (a file was attached). Both
        # end up giving us the same three values.
        uploaded_file = None
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            user_text = (request.form.get("message") or "").strip()
            mode = request.form.get("mode", "general")
            uploaded_file = request.files.get("file")
        else:
            data = request.get_json(silent=True) or {}
            user_text = (data.get("message") or "").strip()
            mode = data.get("mode", "general")

        if not user_text and not uploaded_file:
            return jsonify({"error": "Message can't be empty."}), 400
        if len(user_text) > 4000:
            return jsonify({"error": "That message is too long."}), 400

        conversation = _get_or_create_active_conversation(current_user)

        # --- Handle an attached file, if any ---
        attachment_url = attachment_name = attachment_kind = attachment_text = None
        if uploaded_file and uploaded_file.filename:
            try:
                attachment_url, attachment_name, attachment_kind, attachment_text = _save_uploaded_file(
                    uploaded_file, current_user
                )
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            if not user_text:
                user_text = f"Sent a file: {attachment_name}"

        user_message = AITutorMessage(
            conversation_id=conversation.id, role="user", mode=mode, content=user_text,
            attachment_url=attachment_url, attachment_name=attachment_name,
            attachment_kind=attachment_kind, attachment_text=attachment_text,
        )
        db.session.add(user_message)
        if conversation.title == "New Chat":
            conversation.title = user_text[:60]
        db.session.commit()

        # --- Image generation branch ---
        if mode == "image_gen":
            prompt = user_text
            try:
                image = generate_tutor_image(prompt)
                user_folder = os.path.join(AI_TUTOR_GENERATED_FOLDER, str(current_user.id))
                os.makedirs(user_folder, exist_ok=True)
                stored_name = f"{uuid.uuid4().hex}.png"
                image.save(os.path.join(user_folder, stored_name))

                reply_url = url_for("ai_tutor_generated_file", filename=f"{current_user.id}/{stored_name}")
                reply_text = f"Here's what I generated for: \"{prompt}\""
                assistant_message = AITutorMessage(
                    conversation_id=conversation.id, role="assistant", mode=mode, content=reply_text,
                    attachment_url=reply_url, attachment_name="generated-image.png", attachment_kind="image",
                )
                db.session.add(assistant_message)
                db.session.commit()
                return jsonify({
                    "reply": reply_text, "mode_label": MODE_LABELS.get(mode, "Image Generation"),
                    "attachment_url": reply_url, "attachment_name": "generated-image.png", "attachment_kind": "image",
                })
            except ImageGenerationUnavailable as e:
                reply_text = str(e)
                db.session.add(AITutorMessage(
                    conversation_id=conversation.id, role="assistant", mode=mode, content=reply_text,
                ))
                db.session.commit()
                return jsonify({"reply": reply_text, "mode_label": MODE_LABELS.get(mode, "Image Generation")})

        # --- Normal text reply (with any attached file's extracted text as context) ---
        history = _history_for_ai(conversation)
        reply_text = generate_tutor_reply(history, mode)

        db.session.add(AITutorMessage(
            conversation_id=conversation.id, role="assistant", mode=mode, content=reply_text,
        ))
        db.session.commit()

        return jsonify({"reply": reply_text, "mode_label": MODE_LABELS.get(mode, "General Chat")})

    @flask_app.route("/api/ai-tutor/new-chat", methods=["POST"])
    @login_required
    def api_ai_tutor_new_chat():
        conversation = AITutorConversation(user_id=current_user.id, title="New Chat")
        db.session.add(conversation)
        db.session.commit()
        return jsonify({"conversation_id": conversation.id})

    @flask_app.route("/ai-tutor-uploads/<path:filename>")
    @login_required
    def ai_tutor_uploaded_file(filename):
        # filename is always "<user_id>/<stored_name>" (see _save_uploaded_file)
        # - refuse to serve it if it's not this learner's own file.
        if not filename.startswith(f"{current_user.id}/"):
            abort(403)
        return send_from_directory(AI_TUTOR_UPLOADS_FOLDER, filename)

    @flask_app.route("/ai-tutor-generated/<path:filename>")
    @login_required
    def ai_tutor_generated_file(filename):
        if not filename.startswith(f"{current_user.id}/"):
            abort(403)
        return send_from_directory(AI_TUTOR_GENERATED_FOLDER, filename)

    @flask_app.route("/playground")
    @login_required
    def code_playground():
        """Standalone, full-size Code Playground - same shared TVPlayground
        JS/engine as the dashboard widget and every lesson's playground,
        just given its own page with more room and a snippet library."""
        return render_template("code-playground.html", active_page="playground")

    @flask_app.route("/code-battle-arena")
    @login_required
    def code_battle_arena():
        """Code Battle Arena landing page. Speed Battle, Output Prediction,
        Code Completion, AI Battle, and Tournament are live (battle.py);
        Bug Hunt, Blind Coding, and Team Battle still render as 'Coming
        Soon' in the template pending their own build-out."""
        next_tournament = (
            Tournament.query
            .filter_by(status="registration")
            .order_by(Tournament.starts_at.asc())
            .first()
        )
        return render_template(
            "code-battle-arena.html",
            active_page="battle_arena",
            shop_preview_items=SHOP_CATALOG[:4],
            shop_owned_ids=_shop_owned_item_ids(current_user),
            next_tournament=next_tournament,
        )

    @flask_app.route("/shop")
    @login_required
    def shop():
        """Coin Shop - spend Student Coins earned in the Code Battle Arena
        on cosmetics/perks. See SHOP_CATALOG above for the item list and
        _shop_owned_item_ids() for how ownership is derived from the
        existing CoinEvent ledger instead of a new table."""
        owned_ids = _shop_owned_item_ids(current_user)
        categories = []
        seen = set()
        for item in SHOP_CATALOG:
            if item["category"] not in seen:
                seen.add(item["category"])
                categories.append(item["category"])
        return render_template(
            "shop.html",
            active_page="shop",
            categories=categories,
            catalog=SHOP_CATALOG,
            owned_ids=owned_ids,
        )

    @flask_app.route("/shop/purchase/<item_id>", methods=["POST"])
    @login_required
    def shop_purchase(item_id):
        item = _SHOP_ITEMS_BY_ID.get(item_id)
        if item is None:
            abort(404)

        owned_ids = _shop_owned_item_ids(current_user)
        if item_id in owned_ids:
            flash(f"You already own {item['name']}.", "info")
            return redirect(url_for("shop"))

        ok = current_user.spend_coins(item["price"], source=f"shop:{item_id}")
        if not ok:
            flash(f"Not enough Student Coins for {item['name']} - you need {item['price']}.", "error")
            return redirect(url_for("shop"))

        db.session.commit()
        flash(f"Purchased {item['name']}! 🎉", "success")
        return redirect(url_for("shop"))

    @flask_app.route("/api/run-code", methods=["POST"])
    @login_required
    def run_code():
        """
        Executes a Python snippet submitted from the Code Playground and
        returns its stdout/stderr as JSON.

        SECURITY NOTE (read before deploying this publicly): this gives any
        logged-in user real code execution on the server. What's in place:
          - CPU time, memory, and output-file-size caps + a wall-clock
            subprocess timeout (RUN_TIMEOUT_SECONDS)
          - RLIMIT_NPROC=0 on POSIX - blocks fork-bombs / further child
            processes started from the submitted code.
            IMPORTANT: this ONLY works if the Flask process itself is
            NOT running as root - Linux ignores RLIMIT_NPROC entirely for
            uid 0. Run this app as a dedicated non-root OS user in
            production (standard practice for any web server anyway), or
            this specific protection silently does nothing.
          - runs `python3 -I` (isolated mode: ignores env vars, doesn't
            put the cwd on sys.path) with cwd set to a scratch temp dir
          - stdout/stderr truncated to RUN_MAX_OUTPUT_CHARS
        What's deliberately NOT restricted: network access. Ethical-hacking
        lessons here use socket/nmap-style code, so this does not sandbox
        networking away - it can still reach the internet.
        This is resource-limiting, not a real sandbox. It does not isolate
        the filesystem, does not stop reading files the server process can
        read, and (on Windows, where `resource` doesn't exist) has NO
        limits at all beyond the timeout. Before opening signup to the
        public, replace this with a real container/VM sandbox.
        """
        data = request.get_json(silent=True) or {}
        code = data.get("code", "")
        stdin_data = data.get("stdin", "") or ""
        language = data.get("language", "Python")

        if language != "Python":
            return jsonify({"error": f"\"{language}\" isn't supported yet - only Python runs right now."}), 400
        if not code.strip():
            return jsonify({"stdout": "", "stderr": "", "exit_code": None})
        if len(code) > RUN_MAX_CODE_CHARS:
            return jsonify({"error": "That code is too long to run here."}), 400

        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", code],
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_SECONDS,
                cwd=tempfile.gettempdir(),
                preexec_fn=_limit_child_resources if resource else None,
            )
            return jsonify({
                "stdout": proc.stdout[:RUN_MAX_OUTPUT_CHARS],
                "stderr": proc.stderr[:RUN_MAX_OUTPUT_CHARS],
                "exit_code": proc.returncode,
            })
        except subprocess.TimeoutExpired:
            return jsonify({
                "stdout": "",
                "stderr": f"Timed out after {RUN_TIMEOUT_SECONDS} seconds.",
                "exit_code": None,
            })
        except Exception as e:
            return jsonify({"stdout": "", "stderr": f"Execution failed: {e}", "exit_code": None}), 500

    @flask_app.route("/assets/<path:filename>")
    def assets(filename):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in PUBLIC_STATIC_EXTENSIONS and not current_user.is_authenticated:
            # Blocks direct/unauthorized access to the other content pages
            # (notes.html, cheatsheets.html, etc.) - only CSS/JS/images/fonts
            # bypass login.
            abort(401)
        return send_from_directory(OTHER_FOLDER, filename)

    @flask_app.route("/audios/<path:filename>")
    @login_required
    def audio(filename):
        return send_from_directory(AUDIO_FOLDER, filename)

    @flask_app.route("/images/<path:filename>")
    def images(filename):
        # Images (site logo, favicon, etc.) stay public - if you want these
        # locked down too, add @login_required here.
        return send_from_directory(IMAGES_FOLDER, filename)

    @login_manager.unauthorized_handler
    def unauthorized():
        from flask import redirect, url_for, request, flash
        flash("Please log in to access this page.", "info")
        return redirect(url_for("auth.login", next=request.path))

    _migrate_sqlite_schema(flask_app)

    with flask_app.app_context():
        db.create_all()
        seed_learning_paths()
        seed_recommended_items()
        seed_python_course()
        seed_extra_courses()
        seed_daily_challenges()
        seed_projects()
        seed_badges()
        seed_battle_questions()
        seed_reward_settings()

    return flask_app


if __name__ == "__main__":
    webapp = create_app()
    print("Open this exact URL in your browser: http://127.0.0.1:5000/")
    # socketio.run() (not webapp.run()) - it wraps Werkzeug with the
    # same WSGI server but also handles the WebSocket upgrade Speed
    # Battle's live matchmaking/rooms need (see extensions.py's
    # socketio + battle.py's register_battle_socketio).
    socketio.run(webapp, debug=True)