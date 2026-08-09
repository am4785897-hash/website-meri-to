"""
Database models.

User               - used by both the auth blueprint (auth.py) for
                     signup/login/OAuth, and by webserver.py's
                     Flask-Login user_loader. Also carries the real
                     gamification counters (XP / streak) shown on the
                     dashboard - no more hardcoded placeholder numbers.
LearningPath       - one of the 3 career paths (Ethical Hacking, AI/ML,
                     App Development).
RoadmapStep        - a single node in a path's roadmap widget.
UserRoadmapProgress - per-user, per-step status (locked/active/complete).
                     Replaces the old hardcoded demo roadmap dict.

See seed_data.py for how LearningPath/RoadmapStep rows get populated and
how a brand-new user's UserRoadmapProgress rows get initialized.
"""

from datetime import datetime, timedelta

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db

# XP required per level. Level is derived from total_xp, never stored
# directly, so it can never drift out of sync with the XP total.
XP_PER_LEVEL = 200


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)

    # Null for OAuth-only accounts (Google/GitHub) that never set a
    # site password.
    password_hash = db.Column(db.String(255), nullable=True)

    # Set when the account was created/linked via "Continue with Google"
    # or "Continue with GitHub" - see auth.py's _login_or_create_oauth_user.
    oauth_provider = db.Column(db.String(20), nullable=True)
    oauth_id = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- Gamification (real, per-user - not dashboard placeholders) ---
    total_xp = db.Column(db.Integer, nullable=False, default=0)
    streak_days = db.Column(db.Integer, nullable=False, default=0)
    last_active_date = db.Column(db.Date, nullable=True)

    # Student Coins - the Code Battle Arena economy. Separate counter from
    # total_xp on purpose: XP tracks learning progress and never goes down,
    # coins are a spendable currency (battle rewards in, shop purchases
    # out), so they need their own ledger rather than reusing XPEvent.
    total_coins = db.Column(db.Integer, nullable=False, default=0)

    # Gates access to the Code Battle Arena admin panel (Question Bank,
    # Tournaments, Rewards, Economy, Analytics). Not exposed on signup -
    # only ever flipped directly in the DB by whoever runs the site.
    is_admin = db.Column(db.Boolean, nullable=False, default=False)

    # True only for the single system account battle.py's AI Battle mode
    # plays as the "opponent" (see battle.py's _get_or_create_ai_bot -
    # username "ai_opponent"). Lets award_coins/award_xp calls be
    # skipped for that account everywhere a Battle is finalized, and
    # lets the Leaderboard query filter it out, without hardcoding a
    # username check in either place.
    is_bot = db.Column(db.Boolean, nullable=False, default=False)

    # Admin > Users moderation. A banned account is treated as logged
    # out everywhere: webserver.py's user_loader returns None for a
    # banned id (so any HTTP request they make bounces to login same as
    # a session that never existed), and battle.py's socket 'connect'
    # handler refuses the websocket outright - see both for why this is
    # checked in two places instead of one.
    is_banned = db.Column(db.Boolean, nullable=False, default=False)
    ban_reason = db.Column(db.String(200), nullable=True)

    roadmap_progress = db.relationship(
        "UserRoadmapProgress", backref="user", cascade="all, delete-orphan", lazy="dynamic"
    )

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        if not self.password_hash:
            # OAuth-only account - no password set, so password login
            # always fails for it.
            return False
        return check_password_hash(self.password_hash, raw_password)

    @property
    def level(self) -> int:
        """Derived from total_xp so it's always consistent - never set directly."""
        return max(1, (self.total_xp // XP_PER_LEVEL) + 1)

    @property
    def xp_into_current_level(self) -> int:
        return self.total_xp % XP_PER_LEVEL

    def award_xp(self, amount: int, source: str = "other") -> None:
        self.total_xp = max(0, self.total_xp + amount)
        if amount:
            # Ledger entry so the Leaderboard's "This Week" / "This Month"
            # tabs can sum real XP earned in a window, instead of faking
            # a period breakdown out of the single running total_xp
            # counter (which only ever tells you the all-time sum).
            db.session.add(XPEvent(user_id=self.id, amount=amount, source=source))

    def award_coins(self, amount: int, source: str = "other", battle_id: int = None) -> None:
        """Credit coins (battle wins, participation, tournament placement).
        For debits use spend_coins() instead, which enforces a sufficient
        balance - never subtract by passing a negative amount here."""
        if amount < 0:
            raise ValueError("award_coins() only credits; use spend_coins() to debit")
        self.total_coins += amount
        if amount:
            db.session.add(CoinEvent(user_id=self.id, amount=amount, source=source, battle_id=battle_id))

    def spend_coins(self, amount: int, source: str = "shop_purchase") -> bool:
        """Debit coins for a shop purchase. Returns False (and changes
        nothing) if the balance is insufficient, so callers can show an
        error instead of ever letting total_coins go negative."""
        if amount <= 0:
            raise ValueError("spend_coins() amount must be positive")
        if self.total_coins < amount:
            return False
        self.total_coins -= amount
        db.session.add(CoinEvent(user_id=self.id, amount=-amount, source=source))
        return True

    def register_activity(self) -> None:
        """
        Call once per "session" of dashboard use to keep the daily streak
        honest. Idempotent for repeat visits on the same day.
        """
        today = datetime.utcnow().date()
        if self.last_active_date == today:
            return
        if self.last_active_date == today - timedelta(days=1):
            self.streak_days += 1
        else:
            # First-ever visit, or the streak was broken by a gap.
            self.streak_days = 1
        self.last_active_date = today

    def __repr__(self):
        return f"<User {self.username}>"


class RewardSetting(db.Model):
    """Admin-tunable coin amounts for the Code Battle Arena economy -
    lets an admin retune Battle Win/Participation/Draw/Tournament
    placement payouts from the Admin > Rewards page without a code
    deploy. Seeded once with the master spec's default numbers (see
    seed_data.py's seed_reward_settings); battle.py reads through
    battle._reward_amount() instead of hardcoding these as module
    constants, so an admin edit here takes effect on the very next
    battle finalized - no restart needed."""
    __tablename__ = "reward_settings"

    key = db.Column(db.String(40), primary_key=True)
    label = db.Column(db.String(80), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(200), nullable=True)

    def __repr__(self):
        return f"<RewardSetting {self.key}={self.amount}>"


class Announcement(db.Model):
    """Site-wide banner, admin-editable from Admin > Users (Announcement
    panel). Deliberately a single row (id is always 1 - see admin.py's
    get_announcement()) rather than a history table: there's only ever
    one "current" banner, and admin.py's realtime push (site:announcement_updated,
    to the 'site_broadcast' room every connected socket joins - see
    battle.py's connect handler) is what makes an edit appear on every
    open tab without a reload, so there's no need to keep old ones
    around."""
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(500), nullable=False, default="")
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    def __repr__(self):
        return f"<Announcement active={self.is_active} '{self.message[:30]}'>"


class XPEvent(db.Model):
    """One XP award, timestamped - the ledger behind User.total_xp.
    total_xp alone can't answer "how much XP did this user earn this
    week/month", so every award_xp() call appends a row here too. Rows
    only start accumulating once this feature ships, so a user's very
    first week/month total reflects XP earned from here on, not a
    backfilled history that doesn't exist."""
    __tablename__ = "xp_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    source = db.Column(db.String(30), nullable=False, default="other")  # lesson | quiz | challenge | other
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<XPEvent user={self.user_id} +{self.amount} ({self.source})>"


class CoinEvent(db.Model):
    """One Student Coins award or spend, timestamped - the ledger behind
    User.total_coins. Every award_coins()/spend_coins() call appends a row
    here, same reasoning as XPEvent: total_coins alone can't answer "how
    many coins came from battles vs. tournaments" or feed the admin
    Economy dashboard's mint-vs-spend view, and a ledger is what makes a
    balance auditable instead of just trusted.

    amount is positive for a credit (battle_win, participation, draw,
    tournament_champion, tournament_runner_up, tournament_third) and
    negative for a debit (shop_purchase) - sum(amount) for a user must
    always equal their total_coins, which is what makes farming/double-
    award bugs detectable later instead of silently drifting the balance.
    """
    __tablename__ = "coin_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    source = db.Column(db.String(30), nullable=False, default="other")
    # Set when the event came from a specific match (battle_win, draw,
    # participation). Left nullable/unset for tournament placement and
    # shop purchases, which aren't tied to one battle. No FK constraint
    # yet since the Battle table doesn't exist - added once Battle ships.
    battle_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        sign = "+" if self.amount >= 0 else ""
        return f"<CoinEvent user={self.user_id} {sign}{self.amount} ({self.source})>"


# The 8 Code Battle Arena modes, in the order they appear in the arena UI.
# Kept as a plain tuple (not a DB-level enum) so adding a 9th mode later
# is a one-line change here, not a migration.
BATTLE_MODES = (
    "speed_battle",
    "bug_hunt",
    "output_prediction",
    "code_completion",
    "blind_coding",
    "ai_battle",
    "team_battle",
    "tournament",
)

BATTLE_DIFFICULTIES = ("easy", "medium", "hard")


class BattleQuestion(db.Model):
    """One question in the Code Battle Arena's question bank - admin-
    authored, mode-specific. A single row covers every mode's needs even
    though most fields are only used by some modes, rather than one
    subtable per mode, since the admin form and judging pipeline both
    want "load one row, branch on mode" rather than a join per mode.

    hidden_tests is stored as a JSON string (not a separate table) since
    SQLite has no native array/JSON column type here and a test list is
    always read/written as a whole, never queried by individual test -
    same reasoning as how AITutorMessage stores attachment metadata as
    plain columns rather than a related table.
    """
    __tablename__ = "battle_questions"

    id = db.Column(db.Integer, primary_key=True)
    mode = db.Column(db.String(30), nullable=False)
    difficulty = db.Column(db.String(10), nullable=False, default="medium")
    title = db.Column(db.String(150), nullable=False)

    # The problem statement shown to the player.
    prompt = db.Column(db.Text, nullable=False)

    # Speed Battle / Code Completion / Blind Coding: code scaffold shown
    # before the player starts typing. Nullable - not every mode needs one.
    starter_code = db.Column(db.Text, nullable=True)

    # Bug Hunt only: the broken code the player has to find/fix.
    buggy_code = db.Column(db.Text, nullable=True)

    # Output Prediction only: the code the player reads (no editor shown).
    predict_code = db.Column(db.Text, nullable=True)

    # Output Prediction's correct answer, matched as an exact string by
    # the (non-AI) judge - see the master spec's AI-referee-as-tiebreak-
    # only design.
    expected_output = db.Column(db.Text, nullable=True)

    # JSON-encoded list of {"input": ..., "expected": ...} pairs, run
    # against submitted code for every mode except Output Prediction.
    hidden_tests = db.Column(db.Text, nullable=True)

    time_limit_seconds = db.Column(db.Integer, nullable=False, default=300)

    # Lets admins retire a question without deleting it (and losing the
    # BattleParticipant rows that reference it once battles exist).
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<BattleQuestion {self.mode}/{self.difficulty} '{self.title}'>"


class BattleAttempt(db.Model):
    """One player's attempt at one BattleQuestion - currently used by the
    solo modes (Output Prediction first, Code Completion/Blind Coding
    later) that don't need a live opponent or a Battle/BattleParticipant
    row. Head-to-head modes (Speed Battle, Bug Hunt, AI Battle, Team
    Battle, Tournament) will get their own Battle/BattleParticipant
    tables once SocketIO matchmaking exists - this table is deliberately
    scoped to "one player vs. one question", not "match between players".

    Reward policy (anti-farming, matches the master spec's coin table):
    only the FIRST attempt at a given question by a given user ever pays
    out - correct pays the battle_win coin/XP amount, incorrect pays the
    smaller participation amount. Every attempt after that is practice
    only (coins_awarded/xp_awarded stay 0), so replaying a solved
    question can't be used to mint coins. See battle.py's grading logic
    for where that check happens.
    """
    __tablename__ = "battle_attempts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("battle_questions.id"), nullable=False)

    # Denormalized from the question at attempt time (same reasoning as
    # XPEvent.source) so querying "this user's output_prediction history"
    # doesn't require a join, and stays correct even if a question's mode
    # is ever edited later.
    mode = db.Column(db.String(30), nullable=False)

    submitted_answer = db.Column(db.Text, nullable=False)
    is_correct = db.Column(db.Boolean, nullable=False, default=False)

    coins_awarded = db.Column(db.Integer, nullable=False, default=0)
    xp_awarded = db.Column(db.Integer, nullable=False, default=0)

    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

    question = db.relationship("BattleQuestion")

    def __repr__(self):
        status = "correct" if self.is_correct else "incorrect"
        return f"<BattleAttempt user={self.user_id} question={self.question_id} {status}>"


# Lifecycle of a live (matchmade) Battle row. "active" -> exactly one of
# "finished" (a winner or draw was decided) / "abandoned" (a participant
# disconnected before either happened - see battle.py's disconnect
# handler). Kept as a plain tuple for the same reason as BATTLE_MODES.
BATTLE_STATUSES = ("active", "finished", "abandoned")


class Battle(db.Model):
    """One live, matchmade head-to-head match - first mode is Speed
    Battle (1v1, both players race the same BattleQuestion). Distinct
    from BattleAttempt (solo, no opponent, no matchmaking): this table
    only exists once two players are actually paired, via battle.py's
    in-memory matchmaking queue.

    winner_id is null while status="active", and stays null forever if
    is_draw ends up true - a finished non-draw battle always has exactly
    one of (winner_id set) or (is_draw=True), never both.
    """
    __tablename__ = "battles"

    id = db.Column(db.Integer, primary_key=True)
    mode = db.Column(db.String(30), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("battle_questions.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active")

    winner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    is_draw = db.Column(db.Boolean, nullable=False, default=False)

    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    question = db.relationship("BattleQuestion")
    winner = db.relationship("User", foreign_keys=[winner_id])
    participants = db.relationship(
        "BattleParticipant", backref="battle", cascade="all, delete-orphan", lazy="joined"
    )

    def __repr__(self):
        return f"<Battle {self.id} {self.mode} status={self.status}>"


class BattleParticipant(db.Model):
    """One player's seat in one live Battle - exactly 2 rows per Battle
    for Speed Battle (1v1). Holds that player's own submission/result,
    same "denormalize what's queried without a join" reasoning as
    BattleAttempt.mode.

    Rewards are credited exactly once, at battle-finalize time (not per
    submission - see battle.py's _finalize_battle), so a player can
    resubmit after a wrong answer without it paying out twice.
    """
    __tablename__ = "battle_participants"
    __table_args__ = (db.UniqueConstraint("battle_id", "user_id", name="uq_battle_participant_user"),)

    id = db.Column(db.Integer, primary_key=True)
    battle_id = db.Column(db.Integer, db.ForeignKey("battles.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    submitted_code = db.Column(db.Text, nullable=True)
    is_correct = db.Column(db.Boolean, nullable=False, default=False)
    submitted_at = db.Column(db.DateTime, nullable=True)

    # "win" | "lose" | "draw" | null (battle still active). Denormalized
    # alongside Battle.winner_id so a participant's own result renders
    # without comparing IDs in every template.
    result = db.Column(db.String(10), nullable=True)

    coins_awarded = db.Column(db.Integer, nullable=False, default=0)
    xp_awarded = db.Column(db.Integer, nullable=False, default=0)

    user = db.relationship("User")

    def __repr__(self):
        return f"<BattleParticipant battle={self.battle_id} user={self.user_id} result={self.result}>"


# ---------------------------------------------------------------------------
# Tournament (single-elimination bracket across the Battle infrastructure
# above - see battle.py's "TOURNAMENT" section for the bracket-generation
# and advancement logic). A Tournament doesn't grade anything itself: each
# live pairing gets its own ordinary Battle/BattleParticipant pair (same
# rows Speed Battle and AI Battle use), so the existing 'battle:submit'
# Socket.IO handler judges tournament matches unmodified - a
# TournamentMatch just remembers which bracket slot a Battle belongs to
# and what to do with its winner once it finishes.
# ---------------------------------------------------------------------------

TOURNAMENT_STATUSES = ("registration", "active", "completed", "cancelled")


class Tournament(db.Model):
    """One scheduled bracket. Stays in 'registration' until either its
    starts_at time passes or an admin starts it early (battle.py's
    start_tournament), at which point the registered field is shuffled,
    seeded, and locked in - no joining or dropping out after that.

    max_participants is the bracket's target size and must be a power of
    two (4/8/16/32 - enforced in admin.py's tournament form, not here)
    so single elimination divides evenly. The bracket actually generated
    at start time is sized to the smallest power of two that fits however
    many people actually registered (see start_tournament) - could be
    smaller than max_participants if registration didn't fill up, never
    larger. total_rounds is only known once that happens, hence nullable.
    """
    __tablename__ = "tournaments"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    difficulty = db.Column(db.String(10), nullable=False, default="medium")
    max_participants = db.Column(db.Integer, nullable=False, default=8)
    status = db.Column(db.String(20), nullable=False, default="registration")
    starts_at = db.Column(db.DateTime, nullable=False)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    # Set once by _maybe_complete_tournament and never touched again -
    # see that function's docstring for why third_place_id can stay null
    # even after completion (a 2-player bracket has no consolation match).
    champion_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    runner_up_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    third_place_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    total_rounds = db.Column(db.Integer, nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    champion = db.relationship("User", foreign_keys=[champion_id])
    runner_up = db.relationship("User", foreign_keys=[runner_up_id])
    third_place = db.relationship("User", foreign_keys=[third_place_id])

    participants = db.relationship(
        "TournamentParticipant", backref="tournament", cascade="all, delete-orphan", lazy="dynamic"
    )
    matches = db.relationship(
        "TournamentMatch", backref="tournament", cascade="all, delete-orphan", lazy="dynamic"
    )

    def __repr__(self):
        return f"<Tournament {self.id} '{self.title}' status={self.status}>"


class TournamentParticipant(db.Model):
    """One player's registration for one Tournament. seed is assigned
    once (at start_tournament time, from a random shuffle - there's no
    skill rating in this app to seed by) and never changes afterward.
    eliminated_round records the round they went out in, for the bracket
    UI ("eliminated round 2") and profile history; stays null for a
    player who's still alive, and also for the eventual champion (they
    were never eliminated - check Tournament.champion_id for that)."""
    __tablename__ = "tournament_participants"
    __table_args__ = (
        db.UniqueConstraint("tournament_id", "user_id", name="uq_tournament_participant"),
    )

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    seed = db.Column(db.Integer, nullable=True)
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)
    eliminated_round = db.Column(db.Integer, nullable=True)

    user = db.relationship("User")

    def __repr__(self):
        return f"<TournamentParticipant tournament={self.tournament_id} user={self.user_id}>"


TOURNAMENT_MATCH_STATUSES = ("pending", "active", "finished", "bye")


class TournamentMatch(db.Model):
    """One bracket slot. round_number starts at 1; slot_index is the
    match's position within its round (0-based) and is what determines
    which round-(n+1) match its winner feeds into - see battle.py's
    _generate_next_round for that pairing arithmetic.

    player2_id null (with player1_id set) means this slot is a bye -
    player1 advances automatically with status='bye' and no Battle row.
    Both null should never happen past round 1 (see start_tournament's
    docstring for why the bye count can never exceed half a round).

    battle_id stays null until both players are known, at which point a
    normal Battle/BattleParticipant pair is created for it (same shape
    Speed Battle uses) and this column is set to it - that Battle is the
    actual judge; this row just tracks bracket position and mirrors the
    Battle's winner back once it finishes.

    is_third_place_match marks the one-off consolation match between the
    two semifinal losers, played in the same round_number as the final
    (round_number == tournament.total_rounds) but kept out of the normal
    per-round win/loss bracket math via this flag - see
    _generate_next_round.
    """
    __tablename__ = "tournament_matches"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    slot_index = db.Column(db.Integer, nullable=False)
    is_third_place_match = db.Column(db.Boolean, nullable=False, default=False)

    player1_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    player2_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    battle_id = db.Column(db.Integer, db.ForeignKey("battles.id"), nullable=True)
    winner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")

    player1 = db.relationship("User", foreign_keys=[player1_id])
    player2 = db.relationship("User", foreign_keys=[player2_id])
    winner = db.relationship("User", foreign_keys=[winner_id])
    battle = db.relationship("Battle")

    def __repr__(self):
        return f"<TournamentMatch tournament={self.tournament_id} r{self.round_number}#{self.slot_index} status={self.status}>"


class LearningPath(db.Model):
    """One of the 3 career paths shown as a "Continue Learning" card."""
    __tablename__ = "learning_paths"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(40), unique=True, nullable=False)
    title = db.Column(db.String(120), nullable=False)
    subtitle = db.Column(db.String(200), nullable=False, default="")
    icon = db.Column(db.String(8), nullable=False, default="📘")
    order_index = db.Column(db.Integer, nullable=False, default=0)

    steps = db.relationship(
        "RoadmapStep", backref="path", cascade="all, delete-orphan",
        order_by="RoadmapStep.order_index", lazy="dynamic",
    )

    def __repr__(self):
        return f"<LearningPath {self.slug}>"


class RoadmapStep(db.Model):
    """A single node in a path's roadmap widget (e.g. "Linux Basics")."""
    __tablename__ = "roadmap_steps"

    id = db.Column(db.Integer, primary_key=True)
    path_id = db.Column(db.Integer, db.ForeignKey("learning_paths.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    row = db.Column(db.Integer, nullable=False, default=1)  # 1 or 2, matches the 2-row widget layout
    order_index = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<RoadmapStep {self.name}>"


class UserRoadmapProgress(db.Model):
    """Per-user status of a single RoadmapStep: locked / active / complete."""
    __tablename__ = "user_roadmap_progress"
    __table_args__ = (db.UniqueConstraint("user_id", "step_id", name="uq_user_step"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    step_id = db.Column(db.Integer, db.ForeignKey("roadmap_steps.id"), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="locked")  # locked | active | complete
    completed_at = db.Column(db.DateTime, nullable=True)

    step = db.relationship("RoadmapStep")

    def __repr__(self):
        return f"<UserRoadmapProgress user={self.user_id} step={self.step_id} {self.status}>"


class RecommendedItem(db.Model):
    """
    A single card in the "Recommended for You" grid (e.g. "Linux Basics").
    `category` matches a LearningPath.slug so the filter pills can select
    by path; `is_popular` lets a card also show up under the "Popular"
    pill regardless of its category, mirroring the reference design.
    """
    __tablename__ = "recommended_items"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False)
    title = db.Column(db.String(120), nullable=False)
    subtitle = db.Column(db.String(120), nullable=False, default="")
    icon = db.Column(db.String(8), nullable=False, default="📘")
    category = db.Column(db.String(40), nullable=False)  # matches LearningPath.slug
    difficulty = db.Column(db.String(20), nullable=False, default="Beginner")
    is_popular = db.Column(db.Boolean, nullable=False, default=False)
    href = db.Column(db.String(255), nullable=False, default="#")
    order_index = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<RecommendedItem {self.slug}>"


# ---------------------------------------------------------------------------
# Python Mentor Course
#
# A self-contained "course -> lessons -> per-user progress" structure.
# Modeled as a generic Course/Lesson pair (not "PythonLesson") so the same
# tables can hold future courses (AI/ML, App Dev) without new tables later -
# see seed_data.py's seed_python_course() for how the first course's rows
# are populated.
# ---------------------------------------------------------------------------

class Course(db.Model):
    """A single mentor-style course (e.g. 'Python Fundamentals')."""
    __tablename__ = "courses"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False)
    title = db.Column(db.String(120), nullable=False)
    subtitle = db.Column(db.String(200), nullable=False, default="")
    icon = db.Column(db.String(8), nullable=False, default="🐍")
    order_index = db.Column(db.Integer, nullable=False, default=0)

    lessons = db.relationship(
        "Lesson", backref="course", cascade="all, delete-orphan",
        order_by="Lesson.order_index", lazy="dynamic",
    )

    def __repr__(self):
        return f"<Course {self.slug}>"


class Lesson(db.Model):
    """
    One mentor-style lesson: simple explanation + real-life analogy +
    an embedded video (with timestamps) + notes + a practice task + a
    starter snippet for the Code Playground + a single quick-check quiz
    question. Everything a lesson needs lives on this one row so the
    lesson page can render top-to-bottom from a single query.
    """
    __tablename__ = "lessons"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)

    title = db.Column(db.String(160), nullable=False)
    mentor_intro = db.Column(db.Text, nullable=False, default="")   # plain-English "here's the idea" explanation
    analogy = db.Column(db.Text, nullable=False, default="")        # real-life comparison

    video_url = db.Column(db.String(300), nullable=True)            # YouTube link - swap anytime, no code changes
    # List of {"time": "0:45", "label": "Why we need variables"} dicts, shown as
    # clickable timestamp chips under the video.
    video_timestamps = db.Column(db.JSON, nullable=False, default=list)

    notes = db.Column(db.Text, nullable=False, default="")          # short written recap, one point per line
    practice_task = db.Column(db.Text, nullable=False, default="")  # "try this yourself" prompt

    starter_code = db.Column(db.Text, nullable=False, default="")   # preloaded into the Code Playground

    # Single quick-check question. Options stored as a JSON list of strings;
    # correct_index is only ever read server-side (see /api/lesson-quiz-check
    # in webserver.py) so it never reaches the browser.
    quiz_question = db.Column(db.String(300), nullable=True)
    quiz_options = db.Column(db.JSON, nullable=False, default=list)
    quiz_correct_index = db.Column(db.Integer, nullable=True)
    quiz_explanation = db.Column(db.String(400), nullable=True)

    xp_reward = db.Column(db.Integer, nullable=False, default=20)
    quiz_xp_bonus = db.Column(db.Integer, nullable=False, default=10)

    def __repr__(self):
        return f"<Lesson {self.slug}>"


class UserLessonProgress(db.Model):
    """Per-user status of a single Lesson: locked / active / complete."""
    __tablename__ = "user_lesson_progress"
    __table_args__ = (db.UniqueConstraint("user_id", "lesson_id", name="uq_user_lesson"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="locked")  # locked | active | complete
    quiz_correct = db.Column(db.Boolean, nullable=False, default=False)  # has this user's quiz XP bonus been paid out
    completed_at = db.Column(db.DateTime, nullable=True)

    lesson = db.relationship("Lesson")

    def __repr__(self):
        return f"<UserLessonProgress user={self.user_id} lesson={self.lesson_id} {self.status}>"


# ---------------------------------------------------------------------------
# Daily Challenges
#
# A small pool of challenges rotates one-per-day (see seed_data.py's
# get_todays_challenge() - deterministic by calendar date, so every user
# sees the same challenge on the same day and it flips automatically at
# UTC midnight with no cron job needed).
# ---------------------------------------------------------------------------

class DailyChallenge(db.Model):
    """One challenge in the rotation pool."""
    __tablename__ = "daily_challenges"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    category = db.Column(db.String(40), nullable=False, default="ethical-hacking")
    reward_xp = db.Column(db.Integer, nullable=False, default=100)
    order_index = db.Column(db.Integer, nullable=False, default=0)  # position in the rotation

    def __repr__(self):
        return f"<DailyChallenge {self.slug}>"


class UserDailyChallengeProgress(db.Model):
    """
    Per-user, per-calendar-day attempt at that day's challenge.
    One row per (user, date) - a fresh row is created the first time a
    user is served a given day's challenge, starting at "not_started".
    """
    __tablename__ = "user_daily_challenge_progress"
    __table_args__ = (db.UniqueConstraint("user_id", "challenge_date", name="uq_user_challenge_date"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("daily_challenges.id"), nullable=False)
    challenge_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(12), nullable=False, default="not_started")  # not_started | started | complete
    completed_at = db.Column(db.DateTime, nullable=True)

    challenge = db.relationship("DailyChallenge")

    def __repr__(self):
        return f"<UserDailyChallengeProgress user={self.user_id} date={self.challenge_date} {self.status}>"


# ---------------------------------------------------------------------------
# Project Gallery
# ---------------------------------------------------------------------------

class Project(db.Model):
    """A single card in the Project Gallery (e.g. 'AI Chatbot')."""
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    icon = db.Column(db.String(8), nullable=False, default="🧩")
    tech_tag = db.Column(db.String(40), nullable=False, default="Python")
    category = db.Column(db.String(40), nullable=False, default="ethical-hacking")  # matches LearningPath.slug
    difficulty = db.Column(db.String(20), nullable=False, default="Beginner")
    base_likes = db.Column(db.Integer, nullable=False, default=0)  # starting count, before real user likes stack on top
    href = db.Column(db.String(255), nullable=False, default="#")
    order_index = db.Column(db.Integer, nullable=False, default=0)

    def likes_count(self) -> int:
        return self.base_likes + UserProjectLike.query.filter_by(project_id=self.id).count()

    def __repr__(self):
        return f"<Project {self.slug}>"


class UserProjectLike(db.Model):
    """A single user's ❤ on a single project - toggled on/off, never duplicated."""
    __tablename__ = "user_project_likes"
    __table_args__ = (db.UniqueConstraint("user_id", "project_id", name="uq_user_project_like"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    liked_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<UserProjectLike user={self.user_id} project={self.project_id}>"


# ---------------------------------------------------------------------------
# AI Tutor
#
# Plain chat history storage - completely provider-agnostic. See
# ai_provider.py for where the actual reply text comes from (mock today,
# a real AI API later); these two tables just persist whatever came back.
# ---------------------------------------------------------------------------

class AITutorConversation(db.Model):
    """One chat thread. A user can have many over time (each 'New Chat')."""
    __tablename__ = "ai_tutor_conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False, default="New Chat")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship(
        "AITutorMessage", backref="conversation", cascade="all, delete-orphan",
        order_by="AITutorMessage.created_at", lazy="dynamic",
    )

    def __repr__(self):
        return f"<AITutorConversation {self.id} user={self.user_id}>"


class AITutorMessage(db.Model):
    """A single message in a conversation - either the learner or the tutor."""
    __tablename__ = "ai_tutor_messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("ai_tutor_conversations.id"), nullable=False)
    role = db.Column(db.String(12), nullable=False)  # "user" | "assistant"
    mode = db.Column(db.String(20), nullable=False, default="general")  # doubt/code_review/bug/quiz/project/interview/career/image_gen
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- Attachments (files sent by the learner, or images generated by the tutor) ---
    attachment_url = db.Column(db.String(255), nullable=True)   # relative URL to serve it from
    attachment_name = db.Column(db.String(255), nullable=True)  # original filename / display name
    attachment_kind = db.Column(db.String(10), nullable=True)   # "file" | "image"
    # Extracted text content of an uploaded text/code file - fed to the AI
    # as context, but deliberately kept OUT of `content` so the chat bubble
    # stays short and readable instead of dumping the whole file inline.
    attachment_text = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<AITutorMessage {self.id} conv={self.conversation_id} {self.role}>"


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

class Certificate(db.Model):
    """
    One earned certificate - either for finishing a whole LearningPath's
    roadmap (source_type='path') or a Course's lessons (source_type='course').
    Issued automatically the moment completion hits 100% (see
    certificates.py's ensure_certificates_for_user()) - never duplicated,
    thanks to the unique constraint below.
    """
    __tablename__ = "certificates"
    __table_args__ = (
        db.UniqueConstraint("user_id", "source_type", "source_slug", name="uq_user_certificate_source"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    certificate_uid = db.Column(db.String(40), unique=True, nullable=False)  # e.g. TVA-EH-2026-9F3K21
    source_type = db.Column(db.String(10), nullable=False)  # "path" | "course"
    source_slug = db.Column(db.String(60), nullable=False)  # e.g. "ethical-hacking" or "python-fundamentals"
    title = db.Column(db.String(160), nullable=False)       # display title, e.g. "Ethical Hacking - Web Penetration Testing"
    issued_at = db.Column(db.DateTime, default=datetime.utcnow)
    # AI-written blurb (see ai_provider.py's generate_achievement_summary) -
    # generated once at issue time and cached here, not regenerated on
    # every page view.
    ai_summary = db.Column(db.Text, nullable=True)

    user = db.relationship("User")

    def __repr__(self):
        return f"<Certificate {self.certificate_uid}>"


# ---------------------------------------------------------------------------
# Badges
#
# A badge is a fixed, seeded definition (see seed_data.py's BADGES list);
# a UserBadge row is only created the moment a user actually earns it -
# see seed_data.py's check_and_award_badges(), called after every
# XP-awarding action (lesson complete, quiz correct, daily challenge
# complete). Nothing here computes progress live - it's evaluated against
# the same total_xp / level / lesson / streak data everything else uses.
# ---------------------------------------------------------------------------

class Badge(db.Model):
    __tablename__ = "badges"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(200), nullable=False, default="")
    icon = db.Column(db.String(8), nullable=False, default="🏅")
    category = db.Column(db.String(20), nullable=False, default="xp")  # xp | level | lessons | quiz | streak | challenge
    order_index = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<Badge {self.slug}>"


class UserBadge(db.Model):
    __tablename__ = "user_badges"
    __table_args__ = (db.UniqueConstraint("user_id", "badge_id", name="uq_user_badge"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    badge_id = db.Column(db.Integer, db.ForeignKey("badges.id"), nullable=False)
    earned_at = db.Column(db.DateTime, default=datetime.utcnow)

    badge = db.relationship("Badge")

    def __repr__(self):
        return f"<UserBadge user={self.user_id} badge={self.badge_id}>"


# ---------------------------------------------------------------------------
# Notes
#
# Freeform personal notes - not tied to any specific lesson, so they work
# for jotting down anything (a Notes-nav-item feature, not per-lesson
# annotations). Optionally tagged with a course_slug so a note can still
# say "this is about the Ethical Hacking course" without a hard FK.
# ---------------------------------------------------------------------------

class Note(db.Model):
    __tablename__ = "notes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False, default="Untitled note")
    content = db.Column(db.Text, nullable=False, default="")
    course_slug = db.Column(db.String(60), nullable=True)  # optional tag, e.g. "python-fundamentals"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Note {self.id} user={self.user_id}>"


# ---------------------------------------------------------------------------
# Bookmarks
#
# Lightweight polymorphic bookmark: target_type + target_id instead of two
# nullable FK columns, so adding a third bookmarkable thing later (e.g.
# courses) needs no schema change - just a new target_type string and a
# lookup branch in seed_data.py's bookmarks_view().
# ---------------------------------------------------------------------------

class Bookmark(db.Model):
    __tablename__ = "bookmarks"
    __table_args__ = (
        db.UniqueConstraint("user_id", "target_type", "target_id", name="uq_user_bookmark_target"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    target_type = db.Column(db.String(12), nullable=False)  # "lesson" | "project"
    target_id = db.Column(db.Integer, nullable=False)       # Lesson.id or Project.id, depending on target_type
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Bookmark user={self.user_id} {self.target_type}:{self.target_id}>"
