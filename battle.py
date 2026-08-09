"""
Battle blueprint for the Code Battle Arena.

This is a Flask Blueprint, registered onto the main app in webserver.py
(url_prefix="/battle"), same pattern as auth_bp and admin_bp.

Implements three modes end-to-end so far:

  - Output Prediction (solo): no code execution, exact-string judge.
  - Code Completion (solo): the player fills in a starter_code scaffold
    and submits it; the judge runs it once per hidden_tests row (feeding
    that row's "input" on stdin) and requires every row's stdout to
    match "expected". This is the first mode that runs untrusted code,
    so it brings its own sandboxed runner (_run_python_snippet) rather
    than importing webserver.run_code - webserver.py imports battle_bp
    at module load time (see its `from battle import battle_bp`), so a
    battle -> webserver import back would be circular. The limits here
    intentionally mirror /api/run-code's (same constants, same
    RLIMIT_NPROC=0 fork-bomb guard, same security caveats - read
    _run_python_snippet's docstring before relying on this anywhere
    that isn't already behind @login_required).
  - Speed Battle (1v1, live): the first head-to-head mode, so the first
    one that needs matchmaking and a live opponent - see the "SPEED
    BATTLE" section below for the matchmaking queue, Socket.IO event
    handlers (registered via register_battle_socketio, called from
    webserver.py once extensions.socketio is initialized - not at
    import time, for the same circular-import reason as above), and
    _finalize_battle's win/draw/participation payout logic.

The two solo modes follow the same shape: grade -> BattleAttempt row ->
reward if first attempt -> render result. Blind Coding (later) will
reuse _run_python_snippet as-is; only its template hides output until
the timer runs out.

Reward amounts are hardcoded here for now (matching the master spec's
coin table) rather than pulled from an admin-editable EconomyConfig
table, since that table doesn't exist yet - see the master plan's
Admin Rewards panel for where these move once it does.
"""

import json
import secrets
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, current_app
from flask_login import login_required, current_user
from flask_socketio import emit, join_room

from extensions import db, socketio
from models import (
    BattleQuestion, BattleAttempt, Battle, BattleParticipant, User, BATTLE_DIFFICULTIES,
    Tournament, TournamentParticipant, TournamentMatch, RewardSetting,
)

try:
    import resource  # POSIX only - see _limit_child_resources
except ImportError:
    resource = None

battle_bp = Blueprint("battle", __name__, template_folder="templates")

# These are fallback defaults only, used if a RewardSetting row is
# somehow missing (e.g. a fresh DB before seed_data.seed_reward_settings
# has run). The numbers that actually govern payouts live in the
# RewardSetting table, admin-editable from Admin > Rewards - see
# _reward_amount() below. Kept here (rather than deleted) purely so the
# fallback values are visible next to the constants they replace.
COINS_CORRECT = 100   # matches the spec's "Battle Win" amount
COINS_INCORRECT = 5   # matches the spec's "Participation" amount
COINS_DRAW = 40        # matches the spec's "Draw" amount (Speed Battle only)
XP_CORRECT = 20


def _reward_amount(key: str, default: int) -> int:
    """Admin-tunable coin amount for `key` (battle_win, participation,
    draw, tournament_champion, tournament_runner_up, tournament_third),
    read fresh from RewardSetting every call so an edit on the Admin >
    Rewards page takes effect on the very next match finalized - no
    restart, no cache to invalidate. Falls back to `default` if the row
    isn't there yet (fresh DB before seeding, or a key an admin never
    touched)."""
    setting = RewardSetting.query.get(key)
    return setting.amount if setting is not None else default

# ---------------------------------------------------------------------------
# Sandboxed runner for modes that execute submitted code (Code Completion,
# and later Blind Coding). Mirrors webserver.py's /api/run-code limits -
# see that route's docstring for the full security caveats (resource
# limiting, not a real sandbox; network is NOT restricted; RLIMIT_NPROC
# only works if the process itself isn't running as root).
# ---------------------------------------------------------------------------
TEST_TIMEOUT_SECONDS = 8
TEST_MAX_OUTPUT_CHARS = 4000
TEST_MAX_CODE_CHARS = 20000
TEST_MAX_MEMORY_BYTES = 256 * 1024 * 1024  # 256 MB
MAX_HIDDEN_TESTS_PER_QUESTION = 20  # caps how many subprocesses one submit can spawn


def _limit_child_resources():
    """Runs inside the child process, right before it execs python3 (POSIX
    only, guarded by the `resource` import above). Caps CPU time / memory /
    output file size and sets the process count limit to 0 so submitted
    code can't fork/spawn further processes."""
    resource.setrlimit(resource.RLIMIT_CPU, (TEST_TIMEOUT_SECONDS, TEST_TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (TEST_MAX_MEMORY_BYTES, TEST_MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))


def _run_python_snippet(code: str, stdin_data: str = "") -> dict:
    """Runs `code` as a standalone python3 -I subprocess with `stdin_data`
    piped in. Returns {"stdout", "stderr", "exit_code", "timed_out"}.
    Never raises - execution failures come back as a stderr string so
    callers can always finish grading and render a result."""
    if len(code) > TEST_MAX_CODE_CHARS:
        return {"stdout": "", "stderr": "Submission too long to run.", "exit_code": None, "timed_out": False}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            input=stdin_data or "",
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
            cwd=tempfile.gettempdir(),
            preexec_fn=_limit_child_resources if resource else None,
        )
        return {
            "stdout": proc.stdout[:TEST_MAX_OUTPUT_CHARS],
            "stderr": proc.stderr[:TEST_MAX_OUTPUT_CHARS],
            "exit_code": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Timed out after {TEST_TIMEOUT_SECONDS} seconds.",
            "exit_code": None,
            "timed_out": True,
        }
    except Exception as e:
        return {"stdout": "", "stderr": f"Execution failed: {e}", "exit_code": None, "timed_out": False}


def _run_hidden_tests(code: str, hidden_tests_json: str) -> list:
    """Parses a BattleQuestion.hidden_tests JSON string and runs `code`
    once per test case. Returns a list of per-test result dicts:
    {"input", "expected", "actual", "passed", "error"}. An unparsable or
    empty hidden_tests value returns an empty list - callers treat "no
    tests ran" as ungradeable, never as a free pass."""
    try:
        tests = json.loads(hidden_tests_json) if hidden_tests_json else []
        if not isinstance(tests, list):
            tests = []
    except (ValueError, TypeError):
        tests = []

    results = []
    for test in tests[:MAX_HIDDEN_TESTS_PER_QUESTION]:
        if not isinstance(test, dict):
            continue
        test_input = str(test.get("input", ""))
        expected = str(test.get("expected", ""))

        run = _run_python_snippet(code, stdin_data=test_input)
        actual = run["stdout"].strip()
        passed = run["exit_code"] == 0 and not run["stderr"] and actual == expected.strip()

        results.append({
            "input": test_input,
            "expected": expected,
            "actual": run["stdout"] if run["stdout"] else run["stderr"],
            "passed": passed,
        })

    return results


def _grade_output_prediction(question: BattleQuestion, submitted_answer: str) -> bool:
    """Exact-match judge (see master spec: AI referee is a tiebreak/
    explanation layer, never the primary judge where a deterministic
    check is possible). Normalizes only whitespace noise a player
    shouldn't be penalized for - trailing blank lines, \\r\\n vs \\n,
    and leading/trailing whitespace on the whole answer - not case or
    internal spacing, since output correctness is exact by definition."""
    def normalize(text: str) -> str:
        return text.replace("\r\n", "\n").strip()

    return normalize(submitted_answer) == normalize(question.expected_output or "")


@battle_bp.route("/output-prediction")
@login_required
def output_prediction_list():
    questions = (
        BattleQuestion.query
        .filter_by(mode="output_prediction", is_active=True)
        .order_by(BattleQuestion.created_at.desc())
        .all()
    )

    # One query for all of this user's attempts, instead of N+1 inside
    # the template loop.
    solved_ids = {
        attempt.question_id
        for attempt in BattleAttempt.query.filter_by(user_id=current_user.id, is_correct=True).all()
    }

    return render_template(
        "battle_output_prediction_list.html",
        active_page="battle_arena",
        questions=questions,
        solved_ids=solved_ids,
    )


@battle_bp.route("/output-prediction/<int:question_id>", methods=["GET", "POST"])
@login_required
def output_prediction_question(question_id):
    question = BattleQuestion.query.get_or_404(question_id)
    if question.mode != "output_prediction" or not question.is_active:
        abort(404)

    result = None

    if request.method == "POST":
        submitted_answer = request.form.get("submitted_answer", "")
        is_correct = _grade_output_prediction(question, submitted_answer)

        # Anti-farming: only a user's first-ever attempt at this question
        # pays out. Checked before inserting this attempt's own row, so
        # it never counts itself.
        is_first_attempt = (
            BattleAttempt.query
            .filter_by(user_id=current_user.id, question_id=question.id)
            .count() == 0
        )

        coins_awarded = 0
        xp_awarded = 0
        if is_first_attempt:
            if is_correct:
                coins_awarded = _reward_amount("battle_win", COINS_CORRECT)
                xp_awarded = XP_CORRECT
            else:
                coins_awarded = _reward_amount("participation", COINS_INCORRECT)

        attempt = BattleAttempt(
            user_id=current_user.id,
            question_id=question.id,
            mode="output_prediction",
            submitted_answer=submitted_answer,
            is_correct=is_correct,
            coins_awarded=coins_awarded,
            xp_awarded=xp_awarded,
            submitted_at=datetime.utcnow(),
        )
        db.session.add(attempt)

        if coins_awarded:
            current_user.award_coins(coins_awarded, source="battle_win" if is_correct else "participation")
        if xp_awarded:
            current_user.award_xp(xp_awarded, source="battle")

        db.session.commit()

        result = {
            "is_correct": is_correct,
            "is_first_attempt": is_first_attempt,
            "coins_awarded": coins_awarded,
            "xp_awarded": xp_awarded,
            "submitted_answer": submitted_answer,
        }

    return render_template(
        "battle_output_prediction_question.html",
        active_page="battle_arena",
        question=question,
        result=result,
    )


@battle_bp.route("/code-completion")
@login_required
def code_completion_list():
    questions = (
        BattleQuestion.query
        .filter_by(mode="code_completion", is_active=True)
        .order_by(BattleQuestion.created_at.desc())
        .all()
    )

    solved_ids = {
        attempt.question_id
        for attempt in BattleAttempt.query.filter_by(user_id=current_user.id, is_correct=True).all()
    }

    return render_template(
        "battle_code_completion_list.html",
        active_page="battle_arena",
        questions=questions,
        solved_ids=solved_ids,
    )


@battle_bp.route("/code-completion/<int:question_id>", methods=["GET", "POST"])
@login_required
def code_completion_question(question_id):
    question = BattleQuestion.query.get_or_404(question_id)
    if question.mode != "code_completion" or not question.is_active:
        abort(404)

    result = None

    if request.method == "POST":
        submitted_code = request.form.get("submitted_code", "")
        test_results = _run_hidden_tests(submitted_code, question.hidden_tests)

        # No tests ran (missing/malformed hidden_tests on this question) -
        # can't grade it. Don't record an attempt or pay out; tell the
        # player instead of silently marking it wrong or free-passing it.
        if not test_results:
            flash("This question has no grading tests configured yet - it can't be judged. Try another question.", "warning")
            return render_template(
                "battle_code_completion_question.html",
                active_page="battle_arena",
                question=question,
                result=None,
            )

        is_correct = all(t["passed"] for t in test_results)

        # Anti-farming: only a user's first-ever attempt at this question
        # pays out, same policy as Output Prediction.
        is_first_attempt = (
            BattleAttempt.query
            .filter_by(user_id=current_user.id, question_id=question.id)
            .count() == 0
        )

        coins_awarded = 0
        xp_awarded = 0
        if is_first_attempt:
            if is_correct:
                coins_awarded = _reward_amount("battle_win", COINS_CORRECT)
                xp_awarded = XP_CORRECT
            else:
                coins_awarded = _reward_amount("participation", COINS_INCORRECT)

        attempt = BattleAttempt(
            user_id=current_user.id,
            question_id=question.id,
            mode="code_completion",
            submitted_answer=submitted_code,
            is_correct=is_correct,
            coins_awarded=coins_awarded,
            xp_awarded=xp_awarded,
            submitted_at=datetime.utcnow(),
        )
        db.session.add(attempt)

        if coins_awarded:
            current_user.award_coins(coins_awarded, source="battle_win" if is_correct else "participation")
        if xp_awarded:
            current_user.award_xp(xp_awarded, source="battle")

        db.session.commit()

        result = {
            "is_correct": is_correct,
            "is_first_attempt": is_first_attempt,
            "coins_awarded": coins_awarded,
            "xp_awarded": xp_awarded,
            "submitted_code": submitted_code,
            "test_results": test_results,
            "passed_count": sum(1 for t in test_results if t["passed"]),
            "total_count": len(test_results),
        }

    return render_template(
        "battle_code_completion_question.html",
        active_page="battle_arena",
        question=question,
        result=result,
    )


# =============================================================================
# SPEED BATTLE (1v1, live matchmaking via Socket.IO)
# =============================================================================
#
# In-memory matchmaking state. Deliberately NOT a database table: a
# waiting player is a transient thing (they're staring at a "Searching
# for opponent..." spinner with a live socket connection open), not a
# durable record anything else needs to query or that should survive a
# restart. This means the queue is scoped to a single process - fine for
# the single dev-server process this app runs as (see extensions.py's
# socketio comment); sharding across multiple worker processes would
# need this moved into something shared (e.g. Redis) instead.
#
# Modes that share the matchmaking queue below (as opposed to AI Battle,
# which has no queue - see its own section further down - or Tournament,
# which uses its own bracket-scheduling instead of live matchmaking).
MATCHMAKING_MODES = ("speed_battle", "blind_coding")

# _queues:      (mode, difficulty) -> list of {"user_id", "sid"} waiting,
#               in arrival order (front of the list = been waiting
#               longest). Keyed by mode too (not just difficulty) so a
#               Speed Battle queuer and a Blind Coding queuer never get
#               matched with each other even at the same difficulty.
# _sid_users:   socket id -> user_id, so the disconnect handler (which
#               only gets a sid, not a User) can find who left.
# _lock:        guards both dicts above - matchmaking runs the classic
#               "check queue, then mutate it" sequence, which needs to
#               be atomic across concurrent connect/find_match calls or
#               two players could both read an empty queue and neither
#               ever gets matched with the other.
_queues = {(mode, difficulty): [] for mode in MATCHMAKING_MODES for difficulty in BATTLE_DIFFICULTIES}
_sid_users = {}
_lock = threading.Lock()


def _battle_room(battle_id: int) -> str:
    return f"battle_{battle_id}"


def _active_battle_participant(user_id: int):
    """Returns this user's BattleParticipant row for their current active
    Battle, or None. Used both to stop a user from queuing for a second
    match while already in one, and to route a page load straight to
    their in-progress room."""
    return (
        BattleParticipant.query
        .join(Battle, BattleParticipant.battle_id == Battle.id)
        .filter(BattleParticipant.user_id == user_id, Battle.status == "active")
        .first()
    )


def _remove_from_queues(user_id: int) -> None:
    """Drops every queue entry for this user, across all (mode,
    difficulty) queues. Called before adding them to a (possibly
    different) queue and on disconnect, so a user can never be waiting
    in two places, and a closed tab doesn't sit in the queue forever."""
    for key in _queues:
        _queues[key] = [entry for entry in _queues[key] if entry["user_id"] != user_id]


def _finalize_battle(battle: Battle, winner_user_id: int = None, is_draw: bool = False) -> None:
    """Pays out and closes a Battle exactly once. Called either the
    instant a correct submission wins it, or by the timeout watcher if
    time runs out with nobody having won.

    Reward policy (matches the master spec's coin table): the winner
    gets the Battle Win amount + XP; a draw pays the smaller Draw amount
    to both players and no XP; the loser of a decided battle gets the
    Participation amount and no XP - same "coins for showing up, XP only
    for actually solving it" split as the solo modes. Every branch here
    sets Battle.status="finished" up front, so a caller that raced to
    this function a moment too late (see the atomic UPDATE guard in the
    'battle:submit' handler) can't double-pay it.
    """
    battle.status = "finished"
    battle.is_draw = is_draw
    battle.winner_id = winner_user_id if not is_draw else None
    battle.finished_at = datetime.utcnow()

    for participant in battle.participants:
        if is_draw:
            participant.result = "draw"
            participant.coins_awarded = _reward_amount("draw", COINS_DRAW)
            participant.xp_awarded = 0
        elif participant.user_id == winner_user_id:
            participant.result = "win"
            participant.coins_awarded = _reward_amount("battle_win", COINS_CORRECT)
            participant.xp_awarded = XP_CORRECT
        else:
            participant.result = "lose"
            participant.coins_awarded = _reward_amount("participation", COINS_INCORRECT)
            participant.xp_awarded = 0

        # The AI Battle opponent is a system account (see
        # _get_or_create_ai_bot) - it plays the role of a second
        # participant so the rest of this pipeline (matchmaking-free
        # though it is) doesn't need special-casing, but it never
        # actually earns Student Coins/XP. result stays set above so
        # templates can still show "AI won" / "You won"; only the
        # ledger-writing is skipped here.
        if participant.user.is_bot:
            participant.coins_awarded = 0
            participant.xp_awarded = 0
            continue

        if participant.coins_awarded:
            participant.user.award_coins(
                participant.coins_awarded,
                source="draw" if is_draw else ("battle_win" if participant.result == "win" else "participation"),
                battle_id=battle.id,
            )
        if participant.xp_awarded:
            participant.user.award_xp(participant.xp_awarded, source="battle")

    db.session.commit()

    # A tournament match is a normal Battle underneath (same rewards as
    # any other battle - see the coin table's plain "Battle Win" line;
    # only the eventual Champion/Runner-up/Third-place bonuses are
    # tournament-specific, paid out separately once the bracket finishes -
    # see _maybe_complete_tournament), but it also has to report its
    # result back to the bracket so the next round can be built. Kept as
    # a call-out rather than inlined above so Speed Battle/AI Battle's
    # payout logic above stays exactly as it was before Tournament
    # existed - nothing here changes for a non-tournament battle.
    if battle.mode == "tournament":
        _advance_tournament_after_match(battle)


def _battle_timeout_watcher(app, battle_id: int, delay_seconds: int) -> None:
    """Runs in a background thread (started right after a match is made -
    see 'battle:find_match'). Sleeps for the question's time limit, then
    checks whether the battle is still undecided; if so, nobody solved
    it in time, so it's a draw. If the battle already finished (someone
    won, or another watcher/thread already drew it), this is a no-op -
    _finalize_battle is only ever reached once per battle because the
    query below is scoped to status="active"."""
    import time
    time.sleep(delay_seconds)

    with app.app_context():
        with _lock:
            battle = Battle.query.get(battle_id)
            if battle is None or battle.status != "active":
                return
            _finalize_battle(battle, is_draw=True)
        # socketio.emit() (the instance method), NOT the bare emit()
        # imported above - this thread has no active client request to
        # piggyback on (bare emit() relies on one), so it needs to be
        # told explicitly which room/namespace to publish to.
        socketio.emit(
            "battle:finished",
            _serialize_battle_result(battle),
            to=_battle_room(battle_id),
            namespace="/",
        )


# =============================================================================
# BLIND CODING (1v1, live matchmaking - like Speed Battle, but neither
# player sees test results, their own or their opponent's, until BOTH
# have submitted or time runs out)
# =============================================================================
#
# Shares Speed Battle's matchmaking queue (see MATCHMAKING_MODES above),
# Battle/BattleParticipant rows, room-joining, and _finalize_battle
# payout logic - the only genuinely different piece is the submit path:
# 'battle:submit' grades and reveals immediately with unlimited retries,
# which is the opposite of "blind", so Blind Coding gets its own
# 'blind:submit' handler (single shot, silent) and its own timeout
# watcher (has to reason about partial submissions instead of just
# declaring a draw) further below.

def _decide_blind_winner(battle: Battle):
    """Decides a finished-or-timed-out Blind Coding battle from
    whatever's actually been submitted: whoever's correct wins; if both
    are correct, whoever submitted first wins (still a race, just a
    silent one); if neither submitted a correct answer - including a
    player who never submitted at all before time ran out - it's a
    draw, same as Speed Battle's "nobody solved it in time" case.
    Returns (winner_user_id_or_None, is_draw)."""
    p1, p2 = battle.participants[0], battle.participants[1]
    if p1.is_correct and p2.is_correct:
        t1 = p1.submitted_at or datetime.max
        t2 = p2.submitted_at or datetime.max
        return (p1.user_id if t1 <= t2 else p2.user_id), False
    if p1.is_correct:
        return p1.user_id, False
    if p2.is_correct:
        return p2.user_id, False
    return None, True


def _blind_timeout_watcher(app, battle_id: int, delay_seconds: int) -> None:
    """Blind Coding's version of _battle_timeout_watcher. Can't just
    draw on timeout the way Speed Battle does: one player may have
    already submitted a correct answer silently and be waiting on an
    opponent who never submits at all - that's still a win, not a draw,
    so this defers to _decide_blind_winner instead of assuming nobody
    solved it."""
    import time
    time.sleep(delay_seconds)

    with app.app_context():
        with _lock:
            battle = Battle.query.get(battle_id)
            if battle is None or battle.status != "active":
                return
            winner_id, is_draw = _decide_blind_winner(battle)
            _finalize_battle(battle, winner_user_id=winner_id, is_draw=is_draw)
        socketio.emit(
            "battle:finished",
            _serialize_battle_result(battle),
            to=_battle_room(battle_id),
            namespace="/",
        )


def _serialize_battle_result(battle: Battle) -> dict:
    return {
        "battle_id": battle.id,
        "is_draw": battle.is_draw,
        "winner_id": battle.winner_id,
        "participants": [
            {
                "user_id": p.user_id,
                "username": p.user.username,
                "is_correct": p.is_correct,
                "result": p.result,
                "coins_awarded": p.coins_awarded,
                "xp_awarded": p.xp_awarded,
            }
            for p in battle.participants
        ],
    }


@battle_bp.route("/speed-battle")
@login_required
def speed_battle_lobby():
    existing = _active_battle_participant(current_user.id)
    if existing:
        return redirect(url_for("battle.speed_battle_room", battle_id=existing.battle_id))

    counts = {
        difficulty: BattleQuestion.query.filter_by(mode="speed_battle", difficulty=difficulty, is_active=True).count()
        for difficulty in BATTLE_DIFFICULTIES
    }
    return render_template(
        "battle_speed_battle_lobby.html",
        active_page="battle_arena",
        difficulty_counts=counts,
    )


@battle_bp.route("/speed-battle/room/<int:battle_id>")
@login_required
def speed_battle_room(battle_id):
    battle = Battle.query.get_or_404(battle_id)
    participant = next((p for p in battle.participants if p.user_id == current_user.id), None)
    if participant is None:
        # Not one of this battle's two players - not theirs to watch.
        abort(403)

    opponent = next((p for p in battle.participants if p.user_id != current_user.id), None)

    return render_template(
        "battle_speed_battle_room.html",
        active_page="battle_arena",
        battle=battle,
        question=battle.question,
        participant=participant,
        opponent=opponent,
    )


# =============================================================================
# BLIND CODING routes (matchmaking + socket handling live in the
# BLIND CODING section further below, alongside Speed Battle's socket
# handlers - see that section's docstring for why they share the queue)
# =============================================================================

@battle_bp.route("/blind-coding")
@login_required
def blind_coding_lobby():
    existing = _active_battle_participant(current_user.id)
    if existing and existing.battle.mode == "blind_coding":
        return redirect(url_for("battle.blind_coding_room", battle_id=existing.battle_id))

    counts = {
        difficulty: BattleQuestion.query.filter_by(mode="blind_coding", difficulty=difficulty, is_active=True).count()
        for difficulty in BATTLE_DIFFICULTIES
    }
    return render_template(
        "battle_blind_coding_lobby.html",
        active_page="battle_arena",
        difficulty_counts=counts,
    )


@battle_bp.route("/blind-coding/room/<int:battle_id>")
@login_required
def blind_coding_room(battle_id):
    battle = Battle.query.get_or_404(battle_id)
    if battle.mode != "blind_coding":
        abort(404)
    participant = next((p for p in battle.participants if p.user_id == current_user.id), None)
    if participant is None:
        abort(403)  # not one of this battle's two players - not theirs to watch

    opponent = next((p for p in battle.participants if p.user_id != current_user.id), None)

    return render_template(
        "battle_blind_coding_room.html",
        active_page="battle_arena",
        battle=battle,
        question=battle.question,
        participant=participant,
        opponent=opponent,
    )


# =============================================================================
# AI BATTLE (1v1 vs. a bot opponent - no matchmaking wait)
# =============================================================================
#
# Reuses Speed Battle's Battle/BattleParticipant tables and its
# 'battle:submit' / 'battle:join_room' Socket.IO handlers as-is (they
# only ever look at battle.participants generically - they have no idea
# one of the two rows is a bot, and don't need to). The only genuinely
# new pieces are: a system "AI opponent" User account to be that second
# participant, a start route that skips the matchmaking queue entirely
# (no need to wait for anyone), and _ai_solve_watcher standing in for a
# human opponent's own 'battle:submit' call after a simulated delay.

AI_BOT_USERNAME = "ai_opponent"

# Simulated "thinking + typing" time before the bot submits its (always
# correct) solution, per difficulty. Deliberately wide and randomized so
# it doesn't feel like a fixed countdown, and capped against the
# question's own time_limit_seconds when scheduled below so the bot
# always resolves the match before the timeout-draw watcher would.
AI_SOLVE_DELAY_RANGE_SECONDS = {
    "easy": (10, 20),
    "medium": (18, 32),
    "hard": (28, 48),
}


def _get_or_create_ai_bot() -> User:
    """Returns the single system account AI Battle plays as, creating it
    on first use. Its password is a random value nobody is ever told -
    this account is never meant to log in, only to sit on the other end
    of a BattleParticipant row (see is_bot's docstring in models.py for
    why it needs to be a real User row at all: BattleParticipant.user_id
    is a foreign key, and duplicating the whole reward/finalize pipeline
    just to avoid one bot row was worse than reusing it with a flag)."""
    bot = User.query.filter_by(username=AI_BOT_USERNAME).first()
    if bot is not None:
        return bot
    bot = User(username=AI_BOT_USERNAME, email="ai-opponent@codebattle.internal", is_bot=True)
    bot.set_password(secrets.token_hex(32))
    db.session.add(bot)
    db.session.commit()
    return bot


def _ai_solve_watcher(app, battle_id: int, ai_user_id: int, delay_seconds: float) -> None:
    """Runs in a background thread, started right when an AI Battle
    begins. Stands in for the bot's own 'battle:submit' call: sleeps
    `delay_seconds` to simulate it "solving" the problem, then - if the
    player hasn't already won by then - marks the bot's participant row
    correct and finalizes the battle as a bot win. Guarded by the same
    _lock as a real 'battle:submit', so a player's genuine last-second
    win can't be clobbered by this firing at the same moment."""
    import time
    time.sleep(delay_seconds)

    with app.app_context():
        with _lock:
            battle = Battle.query.get(battle_id)
            if battle is None or battle.status != "active":
                return
            bot_participant = next((p for p in battle.participants if p.user_id == ai_user_id), None)
            if bot_participant is None:
                return
            bot_participant.is_correct = True
            bot_participant.submitted_at = datetime.utcnow()
            _finalize_battle(battle, winner_user_id=ai_user_id)
        socketio.emit(
            "battle:finished",
            _serialize_battle_result(battle),
            to=_battle_room(battle_id),
            namespace="/",
        )


@battle_bp.route("/ai-battle")
@login_required
def ai_battle_lobby():
    existing = _active_battle_participant(current_user.id)
    if existing and existing.battle.mode == "ai_battle":
        return redirect(url_for("battle.ai_battle_room", battle_id=existing.battle_id))
    elif existing:
        # Already mid-match in a different live mode (e.g. Speed
        # Battle) - finish that one first rather than juggling two.
        flash("Finish your current battle before starting another.", "warning")
        return redirect(url_for("battle.speed_battle_room", battle_id=existing.battle_id))

    counts = {
        difficulty: BattleQuestion.query.filter_by(mode="ai_battle", difficulty=difficulty, is_active=True).count()
        for difficulty in BATTLE_DIFFICULTIES
    }
    return render_template(
        "battle_ai_battle_lobby.html",
        active_page="battle_arena",
        difficulty_counts=counts,
    )


@battle_bp.route("/ai-battle/start", methods=["POST"])
@login_required
def ai_battle_start():
    difficulty = request.form.get("difficulty")
    if difficulty not in BATTLE_DIFFICULTIES:
        flash("Pick a valid difficulty first.", "warning")
        return redirect(url_for("battle.ai_battle_lobby"))

    existing = _active_battle_participant(current_user.id)
    if existing:
        return redirect(url_for("battle.speed_battle_room" if existing.battle.mode != "ai_battle" else "battle.ai_battle_room", battle_id=existing.battle_id))

    question = (
        BattleQuestion.query
        .filter_by(mode="ai_battle", difficulty=difficulty, is_active=True)
        .order_by(db.func.random())
        .first()
    )
    if question is None:
        flash(f"No {difficulty} AI Battle questions available right now.", "warning")
        return redirect(url_for("battle.ai_battle_lobby"))

    bot = _get_or_create_ai_bot()

    battle = Battle(mode="ai_battle", question_id=question.id, status="active")
    db.session.add(battle)
    db.session.flush()
    db.session.add(BattleParticipant(battle_id=battle.id, user_id=current_user.id))
    db.session.add(BattleParticipant(battle_id=battle.id, user_id=bot.id))
    db.session.commit()

    import random
    low, high = AI_SOLVE_DELAY_RANGE_SECONDS[difficulty]
    delay = random.uniform(low, high)
    # Never let the simulated bot outlast the question's own time limit -
    # otherwise the timeout-draw watcher (reused below, unmodified)
    # would beat it to finalizing the battle.
    delay = min(delay, max(question.time_limit_seconds - 3, 3))

    app = current_app._get_current_object()
    socketio.start_background_task(_ai_solve_watcher, app, battle.id, bot.id, delay)
    socketio.start_background_task(_battle_timeout_watcher, app, battle.id, question.time_limit_seconds)

    return redirect(url_for("battle.ai_battle_room", battle_id=battle.id))


@battle_bp.route("/ai-battle/room/<int:battle_id>")
@login_required
def ai_battle_room(battle_id):
    battle = Battle.query.get_or_404(battle_id)
    if battle.mode != "ai_battle":
        abort(404)
    participant = next((p for p in battle.participants if p.user_id == current_user.id), None)
    if participant is None:
        abort(403)

    return render_template(
        "battle_ai_battle_room.html",
        active_page="battle_arena",
        battle=battle,
        question=battle.question,
        participant=participant,
    )


def register_battle_socketio(socketio) -> None:
    """Registers Speed Battle's Socket.IO event handlers onto the given
    SocketIO instance. Called from webserver.py's create_app(), after
    extensions.socketio.init_app(flask_app) - not at module import time,
    the same reason battle.py doesn't import from webserver.py: at
    import time webserver.py hasn't finished defining/initializing
    things yet (it's mid-`from battle import battle_bp`), so a decorator
    like `@socketio.on(...)` running at battle.py's own import time
    would need an already-initialized socketio that doesn't exist yet.
    Wrapping registration in this function, called later once
    everything's ready, avoids that ordering problem entirely.
    """

    @socketio.on("connect")
    def handle_connect():
        if not current_user.is_authenticated:
            return False  # rejects the connection
        if current_user.is_banned:
            return False  # belt-and-suspenders alongside the user_loader check - a
            # banned user's Flask-Login session is already invalidated
            # server-side, but this covers the edge case of a socket
            # opened from an already-loaded page before that took effect.
        # Every connected socket joins these two rooms so admin.py's
        # realtime pushes (Admin > Users: ban/coin-grant/announcement)
        # can reach a specific user or everyone without a page reload -
        # see admin.py's grant_coins/toggle_ban/announcement routes for
        # what gets emitted into them.
        join_room(f"user_{current_user.id}")
        join_room("site_broadcast")

    @socketio.on("disconnect")
    def handle_disconnect():
        user_id = _sid_users.pop(request.sid, None)
        if user_id is None:
            return
        with _lock:
            _remove_from_queues(user_id)

        # If they were mid-battle, don't award anything either way (an
        # abandon-to-avoid-a-loss and an abandon-that-hands-the-opponent-
        # a-free-win are both a farming vector) - just close the battle
        # out so the opponent isn't left waiting on someone who's gone.
        with _lock:
            participant = _active_battle_participant(user_id)
            if participant is not None:
                battle = participant.battle
                if battle.status == "active":
                    battle.status = "abandoned"
                    battle.finished_at = datetime.utcnow()
                    db.session.commit()
                else:
                    battle = None
        if participant is not None and battle is not None:
            emit(
                "battle:opponent_left",
                {"battle_id": battle.id},
                to=_battle_room(battle.id),
                namespace="/",
            )

    @socketio.on("battle:find_match")
    def handle_find_match(data):
        if not current_user.is_authenticated:
            return
        data = data or {}
        difficulty = data.get("difficulty")
        mode = data.get("mode", "speed_battle")
        if mode not in MATCHMAKING_MODES:
            emit("battle:error", {"message": "Unknown battle mode."})
            return
        if difficulty not in BATTLE_DIFFICULTIES:
            emit("battle:error", {"message": "Pick a valid difficulty first."})
            return

        existing = _active_battle_participant(current_user.id)
        if existing:
            emit("battle:match_found", {"battle_id": existing.battle_id})
            return

        question = (
            BattleQuestion.query
            .filter_by(mode=mode, difficulty=difficulty, is_active=True)
            .order_by(db.func.random())
            .first()
        )
        if question is None:
            mode_label = "Blind Coding" if mode == "blind_coding" else "Speed Battle"
            emit("battle:error", {"message": f"No {difficulty} {mode_label} questions available right now."})
            return

        user_id = current_user.id
        sid = request.sid
        _sid_users[sid] = user_id
        queue_key = (mode, difficulty)

        with _lock:
            _remove_from_queues(user_id)
            opponent_entry = next(
                (entry for entry in _queues[queue_key] if entry["user_id"] != user_id), None
            )
            if opponent_entry:
                _queues[queue_key].remove(opponent_entry)
            else:
                _queues[queue_key].append({"user_id": user_id, "sid": sid})

        if not opponent_entry:
            emit("battle:searching", {"difficulty": difficulty, "mode": mode})
            return

        # Matched - create the Battle + both BattleParticipant rows and
        # tell both sockets. Runs inside this handler's own request/app
        # context, so it's fine to touch the DB directly here.
        battle = Battle(mode=mode, question_id=question.id, status="active")
        db.session.add(battle)
        db.session.flush()  # need battle.id before creating participants
        db.session.add(BattleParticipant(battle_id=battle.id, user_id=opponent_entry["user_id"]))
        db.session.add(BattleParticipant(battle_id=battle.id, user_id=user_id))
        db.session.commit()

        join_room(_battle_room(battle.id), sid=sid)
        join_room(_battle_room(battle.id), sid=opponent_entry["sid"])

        emit("battle:match_found", {"battle_id": battle.id}, to=sid)
        emit("battle:match_found", {"battle_id": battle.id}, to=opponent_entry["sid"])

        from flask import current_app
        app = current_app._get_current_object()
        # Blind Coding can't just draw on timeout the way Speed Battle
        # does - a player may have already submitted a correct answer
        # silently and be waiting on their opponent, so its watcher has
        # to look at whatever's actually been submitted instead of
        # assuming nobody solved it. See _blind_timeout_watcher.
        if mode == "blind_coding":
            socketio.start_background_task(
                _blind_timeout_watcher, app, battle.id, question.time_limit_seconds
            )
        else:
            socketio.start_background_task(
                _battle_timeout_watcher, app, battle.id, question.time_limit_seconds
            )

    @socketio.on("battle:cancel_match")
    def handle_cancel_match():
        if not current_user.is_authenticated:
            return
        with _lock:
            _remove_from_queues(current_user.id)
        emit("battle:cancelled", {})

    @socketio.on("battle:join_room")
    def handle_join_room(data):
        if not current_user.is_authenticated:
            return
        data = data or {}
        battle_id = data.get("battle_id")
        battle = Battle.query.get(battle_id)
        if battle is None:
            return
        is_participant = any(p.user_id == current_user.id for p in battle.participants)
        if not is_participant:
            return  # not theirs to watch - see the HTTP room route's same check
        join_room(_battle_room(battle_id))
        if battle.status == "finished":
            emit("battle:finished", _serialize_battle_result(battle))

    @socketio.on("tournament:join_room")
    def handle_tournament_join_room(data):
        """Lets a tournament bracket page listen for 'tournament:bracket_updated'
        / 'tournament:completed' pushes (see the TOURNAMENT section below)
        so it can refresh itself the moment a match finishes and the next
        round is generated, instead of the player having to reload
        manually. No membership check needed the way battle rooms have
        one - a bracket is public to any logged-in viewer, same as the
        tournament detail page itself."""
        if not current_user.is_authenticated:
            return
        data = data or {}
        tournament_id = data.get("tournament_id")
        if tournament_id:
            join_room(_tournament_room(tournament_id))

    @socketio.on("battle:submit")
    def handle_submit(data):
        if not current_user.is_authenticated:
            return
        data = data or {}
        battle_id = data.get("battle_id")
        code = data.get("code", "")

        battle = Battle.query.get(battle_id)
        if battle is None or battle.status != "active":
            emit("battle:error", {"message": "This battle isn't active anymore."})
            return

        participant = next((p for p in battle.participants if p.user_id == current_user.id), None)
        if participant is None:
            return  # not one of this battle's two players

        test_results = _run_hidden_tests(code, battle.question.hidden_tests)
        is_correct = bool(test_results) and all(t["passed"] for t in test_results)

        participant.submitted_code = code
        participant.is_correct = is_correct
        participant.submitted_at = datetime.utcnow()
        db.session.commit()

        # Always tell the submitter their own test results.
        emit("battle:your_result", {
            "is_correct": is_correct,
            "test_results": test_results,
            "passed_count": sum(1 for t in test_results if t["passed"]),
            "total_count": len(test_results),
        }, to=request.sid)

        if not is_correct:
            # Let the opponent know someone's attempting, without
            # revealing pass/fail detail or code - Speed Battle is a
            # race, not a spoiler.
            emit(
                "battle:opponent_status",
                {"status": "submitted_incorrect"},
                to=_battle_room(battle_id),
                include_self=False,
            )
            return

        # Correct - try to claim the win. Guarded by the same process-
        # wide _lock the matchmaking queue uses (this app runs as a
        # single process - see extensions.py's socketio comment), so if
        # both players' correct submissions land at nearly the same
        # time, only the thread that gets the lock first sees
        # status=="active" and finalizes; the other sees "finished" and
        # falls through without double-paying anyone.
        with _lock:
            db.session.refresh(battle)
            already_decided = battle.status != "active"
            if not already_decided:
                _finalize_battle(battle, winner_user_id=current_user.id)

        if not already_decided:
            emit(
                "battle:finished",
                _serialize_battle_result(battle),
                to=_battle_room(battle_id),
            )

    @socketio.on("blind:submit")
    def handle_blind_submit(data):
        if not current_user.is_authenticated:
            return
        data = data or {}
        battle_id = data.get("battle_id")
        code = data.get("code", "")

        battle = Battle.query.get(battle_id)
        if battle is None or battle.status != "active" or battle.mode != "blind_coding":
            emit("battle:error", {"message": "This battle isn't active anymore."})
            return

        participant = next((p for p in battle.participants if p.user_id == current_user.id), None)
        if participant is None:
            return  # not one of this battle's two players

        if participant.submitted_at is not None:
            # One shot only - that's the whole point of "blind". A
            # retry button would leak information (getting to try again
            # implies the first attempt was wrong), so this is a hard
            # stop rather than "keep trying" like Speed Battle allows.
            emit("blind:already_submitted", {}, to=request.sid)
            return

        test_results = _run_hidden_tests(code, battle.question.hidden_tests)
        is_correct = bool(test_results) and all(t["passed"] for t in test_results)

        participant.submitted_code = code
        participant.is_correct = is_correct
        participant.submitted_at = datetime.utcnow()
        db.session.commit()

        # Deliberately NOT sending pass/fail or test_results here (unlike
        # 'battle:submit') - just an acknowledgment that the submission
        # was received. Neither player learns anything about either
        # submission until the reveal below.
        emit("blind:submitted", {}, to=request.sid)
        emit(
            "blind:opponent_submitted", {},
            to=_battle_room(battle_id), include_self=False,
        )

        with _lock:
            db.session.refresh(battle)
            still_active = battle.status == "active"
            both_submitted = all(p.submitted_at is not None for p in battle.participants)
            if still_active and both_submitted:
                winner_id, is_draw = _decide_blind_winner(battle)
                _finalize_battle(battle, winner_user_id=winner_id, is_draw=is_draw)

        if both_submitted:
            emit(
                "battle:finished",
                _serialize_battle_result(battle),
                to=_battle_room(battle_id),
            )


# =============================================================================
# TOURNAMENT (scheduled, bracketed single-elimination)
# =============================================================================
#
# Unlike Speed Battle/AI Battle, a tournament's bracket has to survive
# across many separate matches over time, so - unlike the matchmaking
# queue above - it's real database state (Tournament / TournamentParticipant
# / TournamentMatch, in models.py), not an in-memory structure.
#
# Each individual pairing still reuses the exact same Battle/BattleParticipant
# rows and 'battle:submit' judging path as Speed Battle - a TournamentMatch
# just points at the Battle it spawned and remembers where its winner
# feeds into the bracket. That reuse is why _finalize_battle above ends
# with a single `if battle.mode == "tournament": _advance_tournament_after_match(battle)`
# rather than a parallel judging pipeline living here.
#
# Lifecycle: registration -> active -> completed (or cancelled, if fewer
# than 2 people ever registered). The registration -> active transition
# is lazy - checked whenever the lobby or a tournament's detail page
# loads and starts_at has passed (see _auto_start_due_tournaments) -
# rather than needing a cron job/scheduler this app doesn't have.
# start_tournament is also exposed for admin.py's "Start Now" action.

COINS_TOURNAMENT_CHAMPION = 1000
COINS_TOURNAMENT_RUNNER_UP = 500
COINS_TOURNAMENT_THIRD = 250

# Bracket sizes an admin can choose when creating a tournament - each is
# a power of two so single elimination divides evenly. Enforced in
# admin.py's tournament form; kept here too since battle.py's bracket
# math (start_tournament) also assumes it.
TOURNAMENT_SIZES = (4, 8, 16, 32)


def _tournament_room(tournament_id: int) -> str:
    return f"tournament_{tournament_id}"


def _match_loser(match: TournamentMatch):
    """The user_id that lost `match`, or None if it was a bye (nobody
    to have lost) or it hasn't finished yet. Used to seed the two
    semifinal losers into the third-place playoff."""
    if match.status != "finished" or match.winner_id is None:
        return None
    return match.player2_id if match.winner_id == match.player1_id else match.player1_id


def _create_tournament_match(
    tournament: Tournament, round_number: int, slot_index: int,
    player1_id, player2_id, is_third_place: bool, app,
) -> None:
    """Creates one TournamentMatch row for the given bracket slot and,
    if it has two real players, the live Battle/BattleParticipant pair
    that actually judges it (same shape as Speed Battle's) plus its
    timeout watcher. A slot with only one real player is a bye - it
    resolves immediately with no Battle at all. A slot with neither
    (both None) is skipped entirely; that should only be reachable if
    the third-place playoff loses BOTH its would-be players to byes in
    the same semifinal round, which start_tournament's bye-count
    guarantee (see its docstring) rules out for the main bracket, but
    this function stays defensive about it anyway since it's cheap to."""
    if player1_id is None and player2_id is None:
        return

    if player1_id is None or player2_id is None:
        winner_id = player1_id if player1_id is not None else player2_id
        db.session.add(TournamentMatch(
            tournament_id=tournament.id, round_number=round_number, slot_index=slot_index,
            is_third_place_match=is_third_place, player1_id=player1_id, player2_id=player2_id,
            winner_id=winner_id, status="bye",
        ))
        return

    match = TournamentMatch(
        tournament_id=tournament.id, round_number=round_number, slot_index=slot_index,
        is_third_place_match=is_third_place, player1_id=player1_id, player2_id=player2_id,
        status="active",
    )
    db.session.add(match)
    db.session.flush()  # need match.id before it could be looked up mid-transaction

    question = (
        BattleQuestion.query
        .filter_by(mode="tournament", difficulty=tournament.difficulty, is_active=True)
        .order_by(db.func.random())
        .first()
    )
    if question is None:
        # No tournament question bank at this difficulty - leave the
        # match "active" with no battle_id rather than failing the
        # whole bracket transition. The match room route flashes a
        # "check back shortly" message for this case; an admin adding
        # a tournament-mode question at this difficulty doesn't
        # automatically unstick it (nothing re-polls), but it stops the
        # rest of the bracket that doesn't depend on this slot from
        # being blocked.
        return

    battle = Battle(mode="tournament", question_id=question.id, status="active")
    db.session.add(battle)
    db.session.flush()
    db.session.add(BattleParticipant(battle_id=battle.id, user_id=player1_id))
    db.session.add(BattleParticipant(battle_id=battle.id, user_id=player2_id))
    match.battle_id = battle.id

    socketio.start_background_task(_tournament_timeout_watcher, app, battle.id, question.time_limit_seconds)
    socketio.emit(
        "tournament:bracket_updated", {"tournament_id": tournament.id},
        to=_tournament_room(tournament.id), namespace="/",
    )


def _tournament_timeout_watcher(app, battle_id: int, delay_seconds: int) -> None:
    """Tournament's version of _battle_timeout_watcher. A bracket match
    can't resolve as a draw the way Speed Battle can - the bracket needs
    exactly one winner to advance - so if nobody's solved it by the
    deadline, this settles it with an unweighted coin flip between the
    two participants rather than leaving the whole tournament stuck.
    This is a last-resort tiebreak for a stalled match, not the primary
    judge (that's still each player's own hidden-test submission, same
    as everywhere else) - it only ever fires when NEITHER player
    solved it in time, so nobody can game it by stalling: you'd have
    the same 50/50 odds just guessing an answer, which the correctness
    judge already forecloses as a strategy."""
    import time
    time.sleep(delay_seconds)

    with app.app_context():
        with _lock:
            battle = Battle.query.get(battle_id)
            if battle is None or battle.status != "active":
                return
            participant_ids = [p.user_id for p in battle.participants]
            winner_id = secrets.choice(participant_ids)
            _finalize_battle(battle, winner_user_id=winner_id)
        socketio.emit(
            "battle:finished",
            _serialize_battle_result(battle),
            to=_battle_room(battle_id),
            namespace="/",
        )


def _advance_tournament_after_match(battle: Battle) -> None:
    """Called by _finalize_battle for every battle.mode=='tournament'
    battle right after it's judged and paid out. Records the result on
    the TournamentMatch it belongs to, marks the loser eliminated, and -
    for a normal bracket match - either advances to placement (if it was
    the final) or checks whether its whole round is now done and, if so,
    builds the next one."""
    match = TournamentMatch.query.filter_by(battle_id=battle.id).first()
    if match is None:
        return  # not one of ours (shouldn't happen - every tournament Battle has a match)
    tournament = match.tournament

    match.status = "finished"
    match.winner_id = battle.winner_id
    loser_id = next((p.user_id for p in battle.participants if p.user_id != battle.winner_id), None)
    if loser_id is not None:
        tp = TournamentParticipant.query.filter_by(tournament_id=tournament.id, user_id=loser_id).first()
        if tp is not None and tp.eliminated_round is None:
            tp.eliminated_round = match.round_number
    db.session.commit()

    # An admin cancelling a tournament mid-bracket (see admin.py) stops
    # it from progressing any further from here, even though whichever
    # match was already live still gets judged and recorded above.
    if tournament.status != "active":
        return

    if match.is_third_place_match:
        _maybe_complete_tournament(tournament)
        return

    if match.round_number == tournament.total_rounds:
        # The championship final itself.
        _maybe_complete_tournament(tournament)
        return

    round_matches = (
        TournamentMatch.query
        .filter_by(tournament_id=tournament.id, round_number=match.round_number, is_third_place_match=False)
        .order_by(TournamentMatch.slot_index)
        .all()
    )
    if not all(m.status in ("finished", "bye") for m in round_matches):
        return  # still waiting on another match in this round

    _generate_next_round(tournament, match.round_number, round_matches)


def _generate_next_round(tournament: Tournament, round_number: int, round_matches: list) -> None:
    """Builds round_number+1 from round_number's (now all finished/bye)
    matches. If round_number+1 is the final (always exactly 2 matches
    feeding it - a round with N matches always has exactly 2 matches
    the round before the one it halves down to 1), this also spins up
    the third-place playoff between that round's two losers alongside
    the final itself, so both matches run at the same time instead of
    the bronze match waiting on the gold match to finish first."""
    from flask import current_app
    app = current_app._get_current_object()

    winners = [m.winner_id for m in round_matches]
    next_round = round_number + 1

    if next_round == tournament.total_rounds:
        _create_tournament_match(tournament, next_round, 0, winners[0], winners[1], False, app)
        loser0, loser1 = _match_loser(round_matches[0]), _match_loser(round_matches[1])
        _create_tournament_match(tournament, next_round, 1, loser0, loser1, True, app)
    else:
        for i in range(0, len(winners), 2):
            _create_tournament_match(tournament, next_round, i // 2, winners[i], winners[i + 1], False, app)

    db.session.commit()
    # Covers the (unusual but possible) case where every match just
    # created above resolved instantly as a bye - without this, a
    # bracket that somehow reaches its final round entirely via byes
    # would never get placed.
    _maybe_complete_tournament(tournament)


def _maybe_complete_tournament(tournament: Tournament) -> None:
    """Completes and pays out `tournament` once its championship final
    (and third-place playoff, if this bracket has one) are both done -
    a no-op otherwise, and a no-op again if called twice, so callers can
    invoke it opportunistically from more than one place without
    worrying about double-paying a champion.

    A 2-player tournament (total_rounds == 1) has no semifinal round and
    therefore no third-place match at all - third_place_id simply stays
    null for those, and no Third-place coins are paid."""
    if tournament.status != "active" or tournament.total_rounds is None:
        return

    final_match = TournamentMatch.query.filter_by(
        tournament_id=tournament.id, round_number=tournament.total_rounds, is_third_place_match=False
    ).first()
    if final_match is None or final_match.status not in ("finished", "bye"):
        return

    third_match = TournamentMatch.query.filter_by(
        tournament_id=tournament.id, round_number=tournament.total_rounds, is_third_place_match=True
    ).first()
    if third_match is not None and third_match.status not in ("finished", "bye"):
        return

    tournament.status = "completed"
    tournament.finished_at = datetime.utcnow()
    tournament.champion_id = final_match.winner_id
    tournament.runner_up_id = (
        final_match.player2_id if final_match.winner_id == final_match.player1_id else final_match.player1_id
    )
    if third_match is not None:
        tournament.third_place_id = third_match.winner_id
    db.session.commit()

    # Placement bonuses on top of whatever each player already earned
    # per-round via the ordinary battle_win/participation payouts in
    # _finalize_battle - matches the master spec's coin table, where
    # Champion/Runner-up/Third are listed separately from Battle Win.
    for user_id, amount, source in (
        (tournament.champion_id, _reward_amount("tournament_champion", COINS_TOURNAMENT_CHAMPION), "tournament_champion"),
        (tournament.runner_up_id, _reward_amount("tournament_runner_up", COINS_TOURNAMENT_RUNNER_UP), "tournament_runner_up"),
        (tournament.third_place_id, _reward_amount("tournament_third", COINS_TOURNAMENT_THIRD), "tournament_third"),
    ):
        if user_id is None:
            continue
        user = User.query.get(user_id)
        if user is not None and not user.is_bot:
            user.award_coins(amount, source=source)
    db.session.commit()

    socketio.emit(
        "tournament:completed", {"tournament_id": tournament.id},
        to=_tournament_room(tournament.id), namespace="/",
    )


def start_tournament(tournament: Tournament) -> bool:
    """Shuffles/seeds whoever registered and generates round 1, flipping
    the tournament to 'active'. Returns False (and cancels the
    tournament instead) if fewer than 2 people ever registered - can't
    bracket a field of 0 or 1. Called lazily by the lobby/detail routes
    once starts_at has passed, and directly by admin.py's "Start Now"
    action. Guarded by the same process-wide _lock as matchmaking so two
    requests racing into this at the same moment (e.g. two people
    loading the detail page right as starts_at ticks over) can't both
    generate round 1.

    Byes: the bracket is padded to `bracket_size`, the smallest power of
    two >= however many people actually registered - never padded up to
    tournament.max_participants if registration didn't fill. Because
    bracket_size is the *smallest* such power of two, byes (bracket_size
    - participant_count) always number fewer than half of round 1's
    slots, which guarantees every round-1 match has at least one real
    player and round 1 always has at least one real (non-bye) match to
    judge - see _create_tournament_match's docstring for where that
    guarantee gets relied on.
    """
    import random
    from flask import current_app

    with _lock:
        if tournament.status != "registration":
            return False

        participants = tournament.participants.all()
        if len(participants) < 2:
            tournament.status = "cancelled"
            db.session.commit()
            return False

        random.shuffle(participants)
        for i, tp in enumerate(participants):
            tp.seed = i + 1

        bracket_size = 2
        while bracket_size < len(participants):
            bracket_size *= 2
        total_rounds = bracket_size.bit_length() - 1

        tournament.status = "active"
        tournament.started_at = datetime.utcnow()
        tournament.total_rounds = total_rounds
        db.session.flush()

        slots = [tp.user_id for tp in participants] + [None] * (bracket_size - len(participants))
        app = current_app._get_current_object()
        for slot in range(bracket_size // 2):
            _create_tournament_match(tournament, 1, slot, slots[2 * slot], slots[2 * slot + 1], False, app)
        db.session.commit()

    round1_matches = (
        TournamentMatch.query
        .filter_by(tournament_id=tournament.id, round_number=1, is_third_place_match=False)
        .order_by(TournamentMatch.slot_index)
        .all()
    )
    if all(m.status in ("finished", "bye") for m in round1_matches):
        _generate_next_round(tournament, 1, round1_matches)

    return True


def _auto_start_due_tournaments() -> None:
    """Lazily starts any tournament whose scheduled starts_at has
    already passed. Called at the top of the lobby/detail routes rather
    than by a background scheduler this app doesn't have - the cost is
    that a tournament won't actually flip to 'active' until the next
    time someone loads one of those pages, which for a low-traffic arena
    is an acceptable trade for not needing a cron job."""
    due = Tournament.query.filter(
        Tournament.status == "registration", Tournament.starts_at <= datetime.utcnow()
    ).all()
    for tournament in due:
        start_tournament(tournament)


@battle_bp.route("/tournament")
@login_required
def tournament_lobby():
    _auto_start_due_tournaments()
    tournaments = Tournament.query.order_by(Tournament.starts_at.desc()).all()
    my_registrations = {
        tp.tournament_id for tp in TournamentParticipant.query.filter_by(user_id=current_user.id).all()
    }
    participant_counts = {
        t.id: t.participants.count() for t in tournaments
    }
    return render_template(
        "battle_tournament_lobby.html",
        active_page="battle_arena",
        tournaments=tournaments,
        my_registrations=my_registrations,
        participant_counts=participant_counts,
    )


@battle_bp.route("/tournament/<int:tournament_id>")
@login_required
def tournament_detail(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    if tournament.status == "registration" and tournament.starts_at <= datetime.utcnow():
        start_tournament(tournament)
        db.session.refresh(tournament)

    participants = tournament.participants.order_by(TournamentParticipant.seed).all()
    is_registered = any(tp.user_id == current_user.id for tp in participants)

    rounds = {}
    if tournament.total_rounds:
        for r in range(1, tournament.total_rounds + 1):
            rounds[r] = (
                TournamentMatch.query
                .filter_by(tournament_id=tournament.id, round_number=r, is_third_place_match=False)
                .order_by(TournamentMatch.slot_index)
                .all()
            )
    third_place_match = (
        TournamentMatch.query.filter_by(tournament_id=tournament.id, is_third_place_match=True).first()
        if tournament.total_rounds else None
    )

    my_active_match = None
    if is_registered and tournament.status == "active":
        my_active_match = (
            TournamentMatch.query
            .filter_by(tournament_id=tournament.id, status="active")
            .filter(db.or_(TournamentMatch.player1_id == current_user.id, TournamentMatch.player2_id == current_user.id))
            .first()
        )

    return render_template(
        "battle_tournament_detail.html",
        active_page="battle_arena",
        tournament=tournament,
        participants=participants,
        is_registered=is_registered,
        rounds=rounds,
        third_place_match=third_place_match,
        my_active_match=my_active_match,
        spots_left=max(tournament.max_participants - len(participants), 0),
    )


@battle_bp.route("/tournament/<int:tournament_id>/register", methods=["POST"])
@login_required
def tournament_register(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    if tournament.status != "registration":
        flash("Registration for this tournament has closed.", "warning")
    elif TournamentParticipant.query.filter_by(tournament_id=tournament.id, user_id=current_user.id).first():
        flash("You're already registered for this tournament.", "info")
    elif tournament.participants.count() >= tournament.max_participants:
        flash("This tournament is full.", "warning")
    else:
        db.session.add(TournamentParticipant(tournament_id=tournament.id, user_id=current_user.id))
        db.session.commit()
        flash(f"You're in! Registered for \u201c{tournament.title}\u201d.", "success")

    return redirect(url_for("battle.tournament_detail", tournament_id=tournament.id))


@battle_bp.route("/tournament/<int:tournament_id>/unregister", methods=["POST"])
@login_required
def tournament_unregister(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    if tournament.status != "registration":
        flash("You can only drop out while registration is still open.", "warning")
        return redirect(url_for("battle.tournament_detail", tournament_id=tournament.id))

    tp = TournamentParticipant.query.filter_by(tournament_id=tournament.id, user_id=current_user.id).first()
    if tp is not None:
        db.session.delete(tp)
        db.session.commit()
        flash("You've dropped out of the tournament.", "info")

    return redirect(url_for("battle.tournament_detail", tournament_id=tournament.id))


@battle_bp.route("/tournament/<int:tournament_id>/match/<int:match_id>")
@login_required
def tournament_match_room(tournament_id, match_id):
    match = TournamentMatch.query.get_or_404(match_id)
    if match.tournament_id != tournament_id:
        abort(404)
    if match.battle_id is None:
        flash("This match hasn't been paired with a question yet - check back shortly.", "warning")
        return redirect(url_for("battle.tournament_detail", tournament_id=tournament_id))

    battle = Battle.query.get_or_404(match.battle_id)
    participant = next((p for p in battle.participants if p.user_id == current_user.id), None)
    if participant is None:
        abort(403)  # not one of this match's two players - not theirs to play
    opponent = next((p for p in battle.participants if p.user_id != current_user.id), None)

    return render_template(
        "battle_tournament_match_room.html",
        active_page="battle_arena",
        tournament=match.tournament,
        match=match,
        battle=battle,
        question=battle.question,
        participant=participant,
        opponent=opponent,
    )
