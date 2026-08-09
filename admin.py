"""
Admin blueprint for the Code Battle Arena.

This is a Flask Blueprint, registered onto the main app in webserver.py
(url_prefix="/admin"), same pattern as auth_bp. Every route here is
gated by admin_required, which layers an is_admin check on top of the
usual login_required.

Currently covers the Question Bank (create/edit/delete/toggle-active
BattleQuestion rows) - the first thing an admin needs before any battle
mode can run for real. Tournaments, Rewards, Economy, and Analytics
panels from the master spec are meant to land in this same blueprint
later, not separate files, so there's one admin_required gate and one
admin nav section for all of it.
"""

import json
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, render_template, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user

from extensions import db, socketio
from models import (
    BattleQuestion, BATTLE_MODES, BATTLE_DIFFICULTIES, Tournament, TournamentParticipant,
    User, CoinEvent, XPEvent, RewardSetting, Battle, BattleParticipant, BattleAttempt, TournamentMatch,
    Announcement,
)
# Owner's email is the one account moderation actions can never touch
# (ban/demote/delete) - same list auth.py uses to auto-grant it admin,
# imported from there rather than duplicated so the two can't drift.
from auth import OWNER_ADMIN_EMAILS
# start_tournament/TOURNAMENT_SIZES live in battle.py, not here - Tournament
# admin actions (create/start-now/cancel) just flip Tournament rows and
# reuse battle.py's own bracket-generation logic rather than duplicating
# it, the same way this file never duplicates BattleQuestion's judging
# logic either.
from battle import start_tournament, TOURNAMENT_SIZES

admin_bp = Blueprint("admin", __name__, template_folder="templates")


def admin_required(view_func):
    """Like login_required, but also 403s a logged-in non-admin instead
    of letting them through. Stacked so an anonymous visitor still gets
    the normal login redirect rather than a bare 403."""
    @login_required
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped


def _parse_question_form(form):
    """Shared by question_new/question_edit - pulls and lightly
    validates the submitted fields. Returns (data_dict, errors_list);
    data_dict's keys match BattleQuestion column names 1:1 so callers
    can do BattleQuestion(**data) or setattr-loop it onto an existing row."""
    errors = []

    mode = form.get("mode", "")
    difficulty = form.get("difficulty", "medium")
    title = form.get("title", "").strip()
    prompt = form.get("prompt", "").strip()
    starter_code = form.get("starter_code", "").strip() or None
    buggy_code = form.get("buggy_code", "").strip() or None
    predict_code = form.get("predict_code", "").strip() or None
    expected_output = form.get("expected_output", "").strip() or None
    hidden_tests_raw = form.get("hidden_tests", "").strip()
    time_limit_raw = form.get("time_limit_seconds", "300").strip()

    if mode not in BATTLE_MODES:
        errors.append("Choose a valid battle mode.")
    if difficulty not in BATTLE_DIFFICULTIES:
        errors.append("Choose a valid difficulty.")
    if len(title) < 3:
        errors.append("Title must be at least 3 characters.")
    if len(prompt) < 10:
        errors.append("Prompt must be at least 10 characters.")

    hidden_tests = None
    if hidden_tests_raw:
        try:
            parsed = json.loads(hidden_tests_raw)
            if not isinstance(parsed, list):
                raise ValueError("hidden_tests must be a JSON list")
            hidden_tests = json.dumps(parsed)
        except (ValueError, TypeError):
            errors.append(
                'Hidden tests must be valid JSON - a list like '
                '[{"input": "...", "expected": "..."}].'
            )
    try:
        time_limit_seconds = int(time_limit_raw)
        if time_limit_seconds <= 0:
            raise ValueError
    except ValueError:
        errors.append("Time limit must be a positive number of seconds.")
        time_limit_seconds = 300

    # Mode-specific required fields - a question that can't actually be
    # judged shouldn't save silently and fail later inside a live battle.
    if mode == "output_prediction" and not expected_output:
        errors.append("Output Prediction questions need an expected output.")
    if mode == "bug_hunt" and not buggy_code:
        errors.append("Bug Hunt questions need buggy code to fix.")

    return {
        "mode": mode,
        "difficulty": difficulty,
        "title": title,
        "prompt": prompt,
        "starter_code": starter_code,
        "buggy_code": buggy_code,
        "predict_code": predict_code,
        "expected_output": expected_output,
        "hidden_tests": hidden_tests,
        # Kept alongside the parsed/normalized value so the form can
        # redisplay exactly what the admin typed if hidden_tests failed
        # to parse - not a BattleQuestion column, strip before saving.
        "hidden_tests_raw": hidden_tests_raw,
        "time_limit_seconds": time_limit_seconds,
    }, errors


@admin_bp.route("/questions")
@admin_required
def questions_list():
    mode_filter = request.args.get("mode", "all")
    query = BattleQuestion.query
    if mode_filter != "all" and mode_filter in BATTLE_MODES:
        query = query.filter_by(mode=mode_filter)
    questions = query.order_by(BattleQuestion.created_at.desc()).all()

    counts_by_mode = {
        mode: BattleQuestion.query.filter_by(mode=mode).count() for mode in BATTLE_MODES
    }

    return render_template(
        "admin_questions.html",
        active_page="admin_questions",
        questions=questions,
        modes=BATTLE_MODES,
        mode_filter=mode_filter,
        counts_by_mode=counts_by_mode,
        total_count=BattleQuestion.query.count(),
    )


@admin_bp.route("/questions/new", methods=["GET", "POST"])
@admin_required
def question_new():
    if request.method == "POST":
        data, errors = _parse_question_form(request.form)
        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "admin_question_form.html", active_page="admin_questions",
                modes=BATTLE_MODES, difficulties=BATTLE_DIFFICULTIES,
                question=None, form_data=data,
            )

        question = BattleQuestion(created_by_id=current_user.id, **{k: v for k, v in data.items() if k != "hidden_tests_raw"})
        db.session.add(question)
        db.session.commit()
        flash(f"Question '{question.title}' created.", "success")
        return redirect(url_for("admin.questions_list"))

    return render_template(
        "admin_question_form.html", active_page="admin_questions",
        modes=BATTLE_MODES, difficulties=BATTLE_DIFFICULTIES,
        question=None, form_data=None,
    )


@admin_bp.route("/questions/<int:question_id>/edit", methods=["GET", "POST"])
@admin_required
def question_edit(question_id):
    question = BattleQuestion.query.get_or_404(question_id)

    if request.method == "POST":
        data, errors = _parse_question_form(request.form)
        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "admin_question_form.html", active_page="admin_questions",
                modes=BATTLE_MODES, difficulties=BATTLE_DIFFICULTIES,
                question=question, form_data=data,
            )

        for key, value in data.items():
            if key == "hidden_tests_raw":
                continue
            setattr(question, key, value)
        db.session.commit()
        flash(f"Question '{question.title}' updated.", "success")
        return redirect(url_for("admin.questions_list"))

    return render_template(
        "admin_question_form.html", active_page="admin_questions",
        modes=BATTLE_MODES, difficulties=BATTLE_DIFFICULTIES,
        question=question, form_data=None,
    )


@admin_bp.route("/questions/<int:question_id>/toggle-active", methods=["POST"])
@admin_required
def question_toggle_active(question_id):
    question = BattleQuestion.query.get_or_404(question_id)
    question.is_active = not question.is_active
    db.session.commit()
    flash(f"'{question.title}' is now {'active' if question.is_active else 'inactive'}.", "success")
    return redirect(url_for("admin.questions_list", mode=request.form.get("mode_filter", "all")))


@admin_bp.route("/questions/<int:question_id>/delete", methods=["POST"])
@admin_required
def question_delete(question_id):
    question = BattleQuestion.query.get_or_404(question_id)
    title = question.title
    db.session.delete(question)
    db.session.commit()
    flash(f"Question '{title}' deleted.", "success")
    return redirect(url_for("admin.questions_list"))


# ---------------------------------------------------------------------------
# Tournaments
#
# Create/start/cancel only - there's no "edit a live bracket" here on
# purpose. Once a Tournament leaves 'registration' its participants and
# rounds are locked in by battle.py's start_tournament, same as how a
# Speed Battle match can't be edited mid-fight; the only admin lever
# once it's running is cancel (see tournament_cancel's docstring).
# ---------------------------------------------------------------------------

@admin_bp.route("/tournaments")
@admin_required
def tournaments_list():
    tournaments = Tournament.query.order_by(Tournament.starts_at.desc()).all()
    participant_counts = {t.id: t.participants.count() for t in tournaments}
    return render_template(
        "admin_tournaments.html", active_page="admin_tournaments",
        tournaments=tournaments, participant_counts=participant_counts,
    )


@admin_bp.route("/tournaments/new", methods=["GET", "POST"])
@admin_required
def tournament_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        difficulty = request.form.get("difficulty", "medium")
        starts_at_raw = request.form.get("starts_at", "").strip()
        try:
            max_participants = int(request.form.get("max_participants", "8"))
        except ValueError:
            max_participants = 0

        errors = []
        if len(title) < 3:
            errors.append("Title must be at least 3 characters.")
        if difficulty not in BATTLE_DIFFICULTIES:
            errors.append("Choose a valid difficulty.")
        if max_participants not in TOURNAMENT_SIZES:
            errors.append(f"Bracket size must be one of: {', '.join(str(s) for s in TOURNAMENT_SIZES)}.")
        starts_at = None
        try:
            # <input type="datetime-local"> posts "YYYY-MM-DDTHH:MM" -
            # treated as UTC (this app has no per-user timezone setting
            # anywhere else either, so staying consistent with that).
            starts_at = datetime.strptime(starts_at_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            errors.append("Choose a valid start date/time.")

        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "admin_tournament_form.html", active_page="admin_tournaments",
                difficulties=BATTLE_DIFFICULTIES, sizes=TOURNAMENT_SIZES,
                form_data=request.form,
            )

        tournament = Tournament(
            title=title, difficulty=difficulty, max_participants=max_participants,
            starts_at=starts_at, created_by_id=current_user.id,
        )
        db.session.add(tournament)
        db.session.commit()
        flash(f"Tournament '{tournament.title}' created.", "success")
        return redirect(url_for("admin.tournaments_list"))

    return render_template(
        "admin_tournament_form.html", active_page="admin_tournaments",
        difficulties=BATTLE_DIFFICULTIES, sizes=TOURNAMENT_SIZES, form_data=None,
    )


@admin_bp.route("/tournaments/<int:tournament_id>/start-now", methods=["POST"])
@admin_required
def tournament_start_now(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    if tournament.status != "registration":
        flash("This tournament has already started.", "warning")
    elif start_tournament(tournament):
        flash(f"'{tournament.title}' has started - round 1 is live.", "success")
    else:
        flash("Couldn't start - at least 2 people need to register first. Tournament cancelled.", "warning")
    return redirect(url_for("admin.tournaments_list"))


@admin_bp.route("/tournaments/<int:tournament_id>/cancel", methods=["POST"])
@admin_required
def tournament_cancel(tournament_id):
    """Cancels a tournament in any non-completed state. If it's already
    'active', whichever match is currently live still finishes and gets
    judged normally (see _advance_tournament_after_match's status check)
    - this just stops the bracket from generating any further round
    after that, rather than yanking players out of a match in progress."""
    tournament = Tournament.query.get_or_404(tournament_id)
    if tournament.status == "completed":
        flash("Can't cancel a tournament that's already finished.", "warning")
    else:
        tournament.status = "cancelled"
        db.session.commit()
        flash(f"'{tournament.title}' cancelled.", "success")
    return redirect(url_for("admin.tournaments_list"))


# ---------------------------------------------------------------------------
# Rewards - admin-tunable coin amounts (RewardSetting rows). battle.py
# reads these through _reward_amount() at payout time, so a change here
# takes effect on the very next match finalized, no restart needed. See
# models.py's RewardSetting docstring and seed_data.py's
# DEFAULT_REWARD_SETTINGS for the spec's baseline numbers.
# ---------------------------------------------------------------------------

@admin_bp.route("/rewards", methods=["GET", "POST"])
@admin_required
def rewards():
    if request.method == "POST":
        settings = {s.key: s for s in RewardSetting.query.all()}
        errors = []
        for key, setting in settings.items():
            raw = request.form.get(f"amount_{key}", "").strip()
            try:
                amount = int(raw)
                if amount < 0:
                    raise ValueError
            except ValueError:
                errors.append(f"'{setting.label}' needs a whole number \u2265 0 - left unchanged.")
                continue
            setting.amount = amount
        db.session.commit()
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            flash("Reward amounts updated - takes effect on the next match.", "success")
        return redirect(url_for("admin.rewards"))

    settings = RewardSetting.query.order_by(RewardSetting.key).all()
    return render_template("admin_rewards.html", active_page="admin_rewards", settings=settings)


# ---------------------------------------------------------------------------
# Economy - read-only view over the CoinEvent ledger: how many Student
# Coins have been minted (battle/tournament payouts) vs. spent (shop
# purchases), broken down by source, plus who's holding the most and
# the most recent activity. Nothing here mutates state - see the
# Rewards section above for the one lever that does (payout amounts).
# ---------------------------------------------------------------------------

@admin_bp.route("/economy")
@admin_required
def economy():
    from sqlalchemy import func

    minted = (
        db.session.query(func.coalesce(func.sum(CoinEvent.amount), 0))
        .filter(CoinEvent.amount > 0).scalar()
    )
    spent = (
        db.session.query(func.coalesce(func.sum(CoinEvent.amount), 0))
        .filter(CoinEvent.amount < 0).scalar()
    )
    spent = abs(spent)
    in_circulation = db.session.query(func.coalesce(func.sum(User.total_coins), 0)).scalar()

    by_source = (
        db.session.query(CoinEvent.source, func.sum(CoinEvent.amount), func.count(CoinEvent.id))
        .group_by(CoinEvent.source)
        .order_by(func.sum(CoinEvent.amount).desc())
        .all()
    )

    top_earners = (
        User.query.filter_by(is_bot=False)
        .order_by(User.total_coins.desc())
        .limit(10)
        .all()
    )

    recent_events = (
        CoinEvent.query.order_by(CoinEvent.created_at.desc()).limit(50).all()
    )
    recent_user_ids = {e.user_id for e in recent_events}
    recent_users = {u.id: u for u in User.query.filter(User.id.in_(recent_user_ids)).all()} if recent_user_ids else {}

    return render_template(
        "admin_economy.html", active_page="admin_economy",
        minted=minted, spent=spent, in_circulation=in_circulation,
        by_source=by_source, top_earners=top_earners,
        recent_events=recent_events, recent_users=recent_users,
    )


# ---------------------------------------------------------------------------
# Analytics - engagement stats across the Arena: how much each mode is
# actually getting played, tournament turnout, and who's most active.
# Same read-only spirit as Economy above.
# ---------------------------------------------------------------------------

@admin_bp.route("/analytics")
@admin_required
def analytics():
    from sqlalchemy import func

    total_users = User.query.filter_by(is_bot=False).count()
    week_ago = datetime.utcnow() - timedelta(days=7)
    new_this_week = User.query.filter(User.is_bot == False, User.created_at >= week_ago).count()  # noqa: E712

    battles_by_mode = (
        db.session.query(Battle.mode, func.count(Battle.id))
        .group_by(Battle.mode)
        .order_by(func.count(Battle.id).desc())
        .all()
    )
    total_battles = sum(count for _, count in battles_by_mode)
    finished_battles = Battle.query.filter_by(status="finished").count()

    attempts_by_mode = (
        db.session.query(BattleAttempt.mode, func.count(BattleAttempt.id),
                          func.sum(db.case((BattleAttempt.is_correct == True, 1), else_=0)))  # noqa: E712
        .group_by(BattleAttempt.mode)
        .all()
    )

    tournaments_by_status = (
        db.session.query(Tournament.status, func.count(Tournament.id))
        .group_by(Tournament.status)
        .all()
    )
    completed_tournaments = Tournament.query.filter_by(status="completed").count()
    avg_participants = None
    if completed_tournaments:
        total_participants = (
            db.session.query(func.count(TournamentParticipant.id))
            .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
            .filter(Tournament.status == "completed")
            .scalar()
        )
        avg_participants = round((total_participants or 0) / completed_tournaments, 1)

    most_active = (
        db.session.query(User, func.count(BattleParticipant.id).label("battle_count"))
        .join(BattleParticipant, BattleParticipant.user_id == User.id)
        .filter(User.is_bot == False)  # noqa: E712
        .group_by(User.id)
        .order_by(func.count(BattleParticipant.id).desc())
        .limit(10)
        .all()
    )

    return render_template(
        "admin_analytics.html", active_page="admin_analytics",
        total_users=total_users, new_this_week=new_this_week,
        battles_by_mode=battles_by_mode, total_battles=total_battles, finished_battles=finished_battles,
        attempts_by_mode=attempts_by_mode,
        tournaments_by_status=tournaments_by_status, completed_tournaments=completed_tournaments,
        avg_participants=avg_participants,
        most_active=most_active,
    )


# ---------------------------------------------------------------------------
# Users - list/search, ban/unban, grant/revoke admin, manual coin+XP
# grants, per-user battle/tournament history, and delete. Every action
# that changes something the affected user (or everyone) would see
# pushes a Socket.IO event so it's reflected live, without that person
# needing to reload - see battle.py's connect handler for the
# 'user_<id>' / 'site_broadcast' rooms every socket joins, which is what
# makes that targeting possible.
#
# The site owner's account (auth.OWNER_ADMIN_EMAILS) can never be
# banned, demoted, or deleted through this panel - not even by another
# admin - so there's always at least one way back in.
# ---------------------------------------------------------------------------

def _is_protected_owner(user: User) -> bool:
    return bool(user.email) and user.email.lower() in OWNER_ADMIN_EMAILS


@admin_bp.route("/users")
@admin_required
def users_list():
    q = request.args.get("q", "").strip()
    query = User.query.filter_by(is_bot=False)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.username.ilike(like), User.email.ilike(like)))
    users = query.order_by(User.created_at.desc()).limit(200).all()
    return render_template(
        "admin_users.html", active_page="admin_users", users=users, q=q,
        owner_emails=OWNER_ADMIN_EMAILS,
    )


@admin_bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@admin_required
def user_toggle_admin(user_id):
    user = User.query.get_or_404(user_id)
    if _is_protected_owner(user):
        flash("The site owner's admin access can't be changed here.", "warning")
    else:
        user.is_admin = not user.is_admin
        db.session.commit()
        flash(f"{user.username} is {'now' if user.is_admin else 'no longer'} an admin.", "success")
    return redirect(url_for("admin.users_list", q=request.form.get("q", "")))


@admin_bp.route("/users/<int:user_id>/toggle-ban", methods=["POST"])
@admin_required
def user_toggle_ban(user_id):
    user = User.query.get_or_404(user_id)
    if _is_protected_owner(user):
        flash("The site owner's account can't be banned.", "warning")
        return redirect(url_for("admin.users_list", q=request.form.get("q", "")))
    if user.id == current_user.id:
        flash("You can't ban your own account.", "warning")
        return redirect(url_for("admin.users_list", q=request.form.get("q", "")))

    if user.is_banned:
        user.is_banned = False
        user.ban_reason = None
        db.session.commit()
        flash(f"{user.username} has been unbanned.", "success")
    else:
        reason = request.form.get("reason", "").strip() or None
        user.is_banned = True
        user.ban_reason = reason
        db.session.commit()
        flash(f"{user.username} has been banned.", "success")
        # Kicks them out of any page/tab that's currently open - the
        # user_loader change alone only takes effect on their *next*
        # request, this makes it immediate for anyone already connected.
        socketio.emit(
            "account:banned", {"reason": reason},
            to=f"user_{user.id}", namespace="/",
        )
    return redirect(url_for("admin.users_list", q=request.form.get("q", "")))


@admin_bp.route("/users/<int:user_id>/grant", methods=["GET", "POST"])
@admin_required
def user_grant(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        errors = []
        if not reason:
            errors.append("A reason is required - it's recorded on the ledger.")

        try:
            coins = int(request.form.get("coins", "0") or "0")
        except ValueError:
            coins = None
            errors.append("Coins must be a whole number.")
        try:
            xp = int(request.form.get("xp", "0") or "0")
        except ValueError:
            xp = None
            errors.append("XP must be a whole number.")

        if not errors and coins == 0 and xp == 0:
            errors.append("Enter a non-zero amount for coins, XP, or both.")

        if errors:
            for message in errors:
                flash(message, "error")
            return redirect(url_for("admin.user_grant", user_id=user.id))

        if coins:
            if coins > 0:
                user.award_coins(coins, source="admin_grant")
            else:
                # award_coins() only credits - a negative admin grant is
                # a deduction, which is what spend_coins() is for. Not
                # gated on having "enough" the way a shop purchase is;
                # an admin correcting a balance can take it to 0.
                user.total_coins = max(0, user.total_coins + coins)
                db.session.add(CoinEvent(user_id=user.id, amount=coins, source="admin_grant"))
        if xp:
            user.award_xp(xp, source="admin_grant")
        db.session.commit()

        flash(f"Updated {user.username}: {coins:+d} coins, {xp:+d} XP ({reason}).", "success")

        # Live-updates the coin balance shown in that user's topbar (and
        # anywhere else listening) without them needing to reload - see
        # base_dashboard.html's socket listener for 'account:balance_updated'.
        socketio.emit(
            "account:balance_updated",
            {"total_coins": user.total_coins, "total_xp": user.total_xp, "level": user.level},
            to=f"user_{user.id}", namespace="/",
        )
        return redirect(url_for("admin.users_list"))

    return render_template("admin_user_grant.html", active_page="admin_users", user=user)


@admin_bp.route("/users/<int:user_id>/history")
@admin_required
def user_history(user_id):
    user = User.query.get_or_404(user_id)

    battles = (
        db.session.query(Battle, BattleParticipant)
        .join(BattleParticipant, BattleParticipant.battle_id == Battle.id)
        .filter(BattleParticipant.user_id == user.id)
        .order_by(Battle.created_at.desc())
        .limit(50)
        .all()
    )
    attempts = (
        BattleAttempt.query.filter_by(user_id=user.id)
        .order_by(BattleAttempt.submitted_at.desc())
        .limit(50)
        .all()
    )
    tournament_entries = (
        TournamentParticipant.query.filter_by(user_id=user.id)
        .order_by(TournamentParticipant.registered_at.desc())
        .all()
    )
    coin_events = (
        CoinEvent.query.filter_by(user_id=user.id).order_by(CoinEvent.created_at.desc()).limit(50).all()
    )

    return render_template(
        "admin_user_history.html", active_page="admin_users", user=user,
        battles=battles, attempts=attempts, tournament_entries=tournament_entries,
        coin_events=coin_events,
    )


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def user_delete(user_id):
    """Permanently deletes the account row. Rows in other tables that
    reference this user_id (CoinEvent, BattleParticipant, past
    tournament placements, etc.) are NOT cascade-deleted - they stay as
    history with a user_id that no longer resolves, which is exactly
    what the 'deleted user' fallback already used on the Economy page's
    recent-activity table (and here, on the users list) is for. An
    admin account must be demoted before it can be deleted, same
    "remove the higher permission first" pattern as the ban guard
    above."""
    user = User.query.get_or_404(user_id)
    if _is_protected_owner(user):
        flash("The site owner's account can't be deleted.", "warning")
    elif user.id == current_user.id:
        flash("You can't delete your own account.", "warning")
    elif user.is_admin:
        flash("Remove admin access before deleting this account.", "warning")
    else:
        username = user.username
        db.session.delete(user)
        db.session.commit()
        flash(f"Deleted account '{username}'.", "success")
    return redirect(url_for("admin.users_list"))


# ---------------------------------------------------------------------------
# Site Announcement - a single admin-editable banner shown on every page
# (see webserver.py's inject_announcement context processor for the
# first-load render, and base_dashboard.html's socket listener for how
# an edit reaches tabs that are already open).
# ---------------------------------------------------------------------------

def _get_announcement() -> Announcement:
    """The site only ever has one Announcement row (id=1) - get-or-create
    rather than a history table, since there's nothing to keep once a
    banner's been replaced or turned off."""
    row = Announcement.query.get(1)
    if row is None:
        row = Announcement(id=1, message="", is_active=False)
        db.session.add(row)
        db.session.commit()
    return row


@admin_bp.route("/announcement", methods=["GET", "POST"])
@admin_required
def announcement():
    row = _get_announcement()

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        is_active = bool(request.form.get("is_active"))
        if is_active and not message:
            flash("Enter a message before activating the announcement.", "error")
            return redirect(url_for("admin.announcement"))

        row.message = message
        row.is_active = is_active
        row.updated_by_id = current_user.id
        db.session.commit()
        flash("Announcement updated - live on every page now.", "success")

        socketio.emit(
            "site:announcement_updated",
            {"active": row.is_active, "message": row.message},
            to="site_broadcast", namespace="/",
        )
        return redirect(url_for("admin.announcement"))

    return render_template("admin_announcement.html", active_page="admin_users", announcement=row)
