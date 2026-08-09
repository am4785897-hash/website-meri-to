"""
Seeds the database with the 3 career paths and their roadmap steps, and
initializes a brand-new user's progress on a path the first time they
land on it.

This is what replaced the old hardcoded "placeholder" dict that used to
live directly inside webserver.py's /dashboard route. Run automatically
on every app boot (see create_app() in webserver.py) - it's fully
idempotent, so re-running it never duplicates rows.
"""

from datetime import date, datetime, timedelta, timezone
import json

from extensions import db
from models import (
    LearningPath, RoadmapStep, UserRoadmapProgress, RecommendedItem,
    Course, Lesson, UserLessonProgress,
    DailyChallenge, UserDailyChallengeProgress,
    Project, UserProjectLike,
    Badge, UserBadge,
    Bookmark,
    BattleQuestion,
    RewardSetting,
)

# (slug, title, subtitle, icon, [row1 step names], [row2 step names])
# Row1/row2 mirror the 2-row roadmap widget layout on the dashboard.
PATHS = [
    (
        "ethical-hacking",
        "Ethical Hacking",
        "Web Penetration Testing",
        "🕵️",
        ["Linux Basics", "Networking", "Python", "Web Technologies", "SQL Injection"],
        ["Burp Suite", "Nmap", "Wireshark", "CTF Practice", "Report Writing"],
    ),
    (
        "ai-ml",
        "AI & ML",
        "Machine Learning Basics",
        "🤖",
        ["Python & NumPy", "Pandas", "Statistics", "Machine Learning", "Deep Learning"],
        ["Neural Networks", "Computer Vision", "NLP", "LLMs & RAG", "Capstone Project"],
    ),
    (
        "app-development",
        "App Development",
        "Flutter & Firebase",
        "📱",
        ["UI/UX & Figma", "Dart Basics", "Flutter Widgets", "State Management", "APIs & Firebase"],
        ["Auth & Database", "Push Notifications", "Testing", "App Signing", "Play Store Launch"],
    ),
]


def seed_learning_paths() -> None:
    """Creates the 3 LearningPath rows + their RoadmapStep rows if missing."""
    for order_index, (slug, title, subtitle, icon, row1, row2) in enumerate(PATHS):
        path = LearningPath.query.filter_by(slug=slug).first()
        if path is None:
            path = LearningPath(
                slug=slug, title=title, subtitle=subtitle, icon=icon, order_index=order_index,
            )
            db.session.add(path)
            db.session.flush()  # get path.id before creating steps

        if path.steps.count() == 0:
            step_order = 0
            for name in row1:
                db.session.add(RoadmapStep(path_id=path.id, name=name, row=1, order_index=step_order))
                step_order += 1
            for name in row2:
                db.session.add(RoadmapStep(path_id=path.id, name=name, row=2, order_index=step_order))
                step_order += 1

    db.session.commit()


def ensure_progress_for_user(user, path: LearningPath) -> list[UserRoadmapProgress]:
    """
    Returns this user's UserRoadmapProgress rows for `path`, creating them
    on first visit: the very first step is "active", everything else
    "locked". No fake completed steps - progress is earned, not seeded.
    """
    steps = list(path.steps)
    existing = {
        p.step_id: p
        for p in UserRoadmapProgress.query.filter_by(user_id=user.id)
        .filter(UserRoadmapProgress.step_id.in_([s.id for s in steps]))
        .all()
    }

    created_any = False
    for i, step in enumerate(steps):
        if step.id not in existing:
            progress = UserRoadmapProgress(
                user_id=user.id,
                step_id=step.id,
                status="active" if i == 0 else "locked",
            )
            db.session.add(progress)
            existing[step.id] = progress
            created_any = True

    if created_any:
        db.session.commit()

    return [existing[step.id] for step in steps]


def path_completion_percent(user, path: LearningPath) -> int:
    """% of `path`'s steps this user has marked complete."""
    progress_rows = ensure_progress_for_user(user, path)
    if not progress_rows:
        return 0
    done = sum(1 for p in progress_rows if p.status == "complete")
    return round(done / len(progress_rows) * 100)


# (slug, title, subtitle, icon, category, difficulty, is_popular, href)
# `category` matches a LearningPath.slug - drives the AI/ML, Ethical
# Hacking, App Development filter pills. `is_popular` additionally
# surfaces a card under the "Popular" pill regardless of its category.
RECOMMENDED_ITEMS = [
    ("linux-basics", "Linux Basics", "Beginner", "🐧", "ethical-hacking", "Beginner", True, "#"),
    ("nmap-network-scanning", "Nmap Network Scanning", "Intermediate", "🔍", "ethical-hacking", "Intermediate", True, "#"),
    ("python-for-beginners", "Python for Beginners", "Beginner", "🐍", "ai-ml", "Beginner", True, "#"),
    ("chatbot-with-python", "Chatbot with Python", "Intermediate", "💬", "ai-ml", "Intermediate", True, "#"),
    ("flutter-todo-app", "Flutter Todo App", "Beginner", "🦋", "app-development", "Beginner", True, "#"),
    ("ai-image-classifier", "AI Image Classifier", "Intermediate", "✨", "ai-ml", "Intermediate", True, "#"),
    ("burp-suite-essentials", "Burp Suite Essentials", "Intermediate", "🛡️", "ethical-hacking", "Intermediate", False, "#"),
    ("pandas-data-wrangling", "Pandas Data Wrangling", "Beginner", "🐼", "ai-ml", "Beginner", False, "#"),
    ("firebase-auth-crash-course", "Firebase Auth Crash Course", "Beginner", "🔥", "app-development", "Beginner", False, "#"),
]


def seed_recommended_items() -> None:
    """Creates the RecommendedItem rows if missing. Idempotent."""
    for order_index, (slug, title, subtitle, icon, category, difficulty, is_popular, href) in enumerate(RECOMMENDED_ITEMS):
        if RecommendedItem.query.filter_by(slug=slug).first():
            continue
        db.session.add(RecommendedItem(
            slug=slug, title=title, subtitle=subtitle, icon=icon,
            category=category, difficulty=difficulty, is_popular=is_popular,
            href=href, order_index=order_index,
        ))
    db.session.commit()

    # One-time repair: the "Python for Beginners" card was originally
    # seeded with a placeholder href="#" (see RECOMMENDED_ITEMS above,
    # before the Python Mentor Course existed). seed_* functions only
    # INSERT missing rows, so an already-seeded row's href never gets
    # touched again - this targeted patch wires up just that one card
    # without altering anything else a real user's account might have.
    python_card = RecommendedItem.query.filter_by(slug="python-for-beginners").first()
    if python_card and python_card.href == "#":
        python_card.href = "/python-course"
        db.session.commit()


# ---------------------------------------------------------------------------
# Python Mentor Course
#
# One real, hand-written module ("Python Fundamentals") taught like a
# personal mentor: a plain-English explanation, a real-life analogy, an
# embedded video with clickable timestamps, short recap notes, a practice
# task, a Code Playground starter snippet, and a one-question quick-check
# quiz - all on a single Lesson row (see models.py).
#
# Swap VIDEO_URL below for your own recordings any time - nothing else
# needs to change, exactly like the old "individual html files/python
# course.html" video-link pattern this replaces.
# ---------------------------------------------------------------------------

VIDEO_URL = "https://youtu.be/UrsmFxEIp5k"  # TODO: replace with your own Python lesson recordings

PYTHON_LESSONS = [
    dict(
        slug="variables-and-data-types",
        title="Variables & Data Types",
        mentor_intro=(
            "A variable is just a labeled box where you store a value so you can use it "
            "again later. In Python you don't have to say what TYPE of value goes in the "
            "box up front - you just write `name = value` and Python figures out the type "
            "for you (a number, a piece of text, True/False, and so on)."
        ),
        analogy=(
            "Think of variables like labeled jars in a kitchen. One jar is labeled \"sugar\", "
            "another \"flour\" - you don't need to relabel the jar every time you use it, you "
            "just reach for the jar by name. `age = 25` is exactly that: a jar labeled `age` "
            "with 25 inside it."
        ),
        video_timestamps=[
            {"time": "0:00", "label": "What is a variable?"},
            {"time": "3:20", "label": "Numbers, strings, booleans"},
            {"time": "7:45", "label": "Naming rules & best practices"},
        ],
        notes=(
            "A variable is created the moment you assign it a value: name = \"Alex\"\n"
            "Python has dynamic typing - the same variable name can hold different types over time.\n"
            "Common built-in types: int (25), float (3.14), str (\"hello\"), bool (True/False).\n"
            "Use type(x) to check what type a value currently is.\n"
            "Variable names are case-sensitive and can't start with a number."
        ),
        practice_task=(
            "Create three variables: your name (str), your age (int), and whether you're "
            "learning Python (bool). Print all three using an f-string, e.g. "
            "f\"{name} is {age} and learning: {learning}\"."
        ),
        starter_code=(
            "name = \"Alex\"\n"
            "age = 25\n"
            "is_learning_python = True\n\n"
            "print(f\"{name} is {age} years old.\")\n"
            "print(f\"Currently learning Python: {is_learning_python}\")\n"
            "print(type(age), type(name), type(is_learning_python))\n"
        ),
        quiz_question="What will type(age) print if age = 25?",
        quiz_options=["<class 'str'>", "<class 'int'>", "<class 'float'>", "<class 'bool'>"],
        quiz_correct_index=1,
        quiz_explanation="25 has no decimal point and isn't quoted, so Python stores it as an int.",
    ),
    dict(
        slug="operators",
        title="Operators",
        mentor_intro=(
            "Operators are the symbols that DO something to your values: + adds, - "
            "subtracts, == checks if two things are equal, and so on. Python groups them "
            "into arithmetic (+ - * / // % **), comparison (== != > < >= <=), and logical "
            "(and, or, not) operators."
        ),
        analogy=(
            "Operators are like verbs in a sentence. \"5 + 3\" is a full sentence: two nouns "
            "(5 and 3) joined by a verb (+) that tells Python what action to take on them. "
            "Comparison operators like == are asking a yes/no question - \"is this equal to "
            "that?\" - and the answer is always True or False."
        ),
        video_timestamps=[
            {"time": "0:00", "label": "Arithmetic operators"},
            {"time": "4:10", "label": "Comparison operators"},
            {"time": "8:30", "label": "and / or / not"},
        ],
        notes=(
            "// is floor (integer) division, % is the remainder (modulo).\n"
            "** is exponentiation: 2 ** 3 is 8.\n"
            "Comparison operators (==, !=, >, <, >=, <=) always return a bool.\n"
            "and / or / not combine multiple True/False conditions into one.\n"
            "Careful: = assigns a value, == compares two values - they are not the same thing."
        ),
        practice_task=(
            "Write code that takes two numbers, prints their sum, difference, product, "
            "floor division, and remainder, then prints whether the first number is "
            "greater than the second."
        ),
        starter_code=(
            "a = 17\n"
            "b = 5\n\n"
            "print(\"Sum:\", a + b)\n"
            "print(\"Difference:\", a - b)\n"
            "print(\"Product:\", a * b)\n"
            "print(\"Floor division:\", a // b)\n"
            "print(\"Remainder:\", a % b)\n"
            "print(\"Is a greater than b?\", a > b)\n"
        ),
        quiz_question="What does 17 % 5 evaluate to?",
        quiz_options=["3.4", "2", "3", "12"],
        quiz_correct_index=1,
        quiz_explanation="17 divided by 5 is 3 remainder 2 - % returns just the remainder, which is 2.",
    ),
    dict(
        slug="conditionals",
        title="Conditionals",
        mentor_intro=(
            "Conditionals let your program make decisions: \"IF this is true, do this - "
            "OTHERWISE do that.\" In Python that's written with if, elif (short for \"else "
            "if\"), and else, and indentation (4 spaces) is what tells Python which lines "
            "belong to which branch."
        ),
        analogy=(
            "Think of a conditional like a bouncer at a club: \"IF you're on the guest "
            "list, let them in. ELSE IF they have a ticket, let them in the side door. "
            "ELSE, turn them away.\" Only one of those branches ever runs for any given "
            "person - same with if/elif/else in Python."
        ),
        video_timestamps=[
            {"time": "0:00", "label": "if / else basics"},
            {"time": "3:50", "label": "elif chains"},
            {"time": "7:15", "label": "Nested conditionals"},
        ],
        notes=(
            "The colon (:) and indentation are not optional - they define the block.\n"
            "elif lets you check several conditions in order without nesting if-statements.\n"
            "Only the first matching branch runs; the rest are skipped.\n"
            "You can nest conditionals inside each other for more specific decisions.\n"
            "Truthy/falsy: 0, \"\", None, and empty containers all count as False in an if."
        ),
        practice_task=(
            "Write a program that takes an exam score (0-100) and prints a grade: "
            "90+ = 'A', 75-89 = 'B', 50-74 = 'C', below 50 = 'F'."
        ),
        starter_code=(
            "score = 82\n\n"
            "if score >= 90:\n"
            "    grade = \"A\"\n"
            "elif score >= 75:\n"
            "    grade = \"B\"\n"
            "elif score >= 50:\n"
            "    grade = \"C\"\n"
            "else:\n"
            "    grade = \"F\"\n\n"
            "print(f\"Score {score} -> Grade {grade}\")\n"
        ),
        quiz_question="If score = 60, which branch runs in the practice code above?",
        quiz_options=["grade = 'A'", "grade = 'B'", "grade = 'C'", "grade = 'F'"],
        quiz_correct_index=2,
        quiz_explanation="60 is not >= 90 or >= 75, but it IS >= 50, so the third branch (elif score >= 50) runs.",
    ),
    dict(
        slug="loops",
        title="Loops",
        mentor_intro=(
            "Loops let you repeat a block of code without copy-pasting it. A for loop "
            "repeats once per item in a sequence (like a range of numbers, or a list). "
            "A while loop repeats as long as a condition stays True."
        ),
        analogy=(
            "A for loop is like handing out flyers to every person in a line - you know "
            "exactly how many people there are, so you do one action per person until the "
            "line ends. A while loop is more like \"keep filling the water tank until it's "
            "full\" - you don't know exactly how many scoops it'll take, you just keep going "
            "until the condition (tank full) stops being true."
        ),
        video_timestamps=[
            {"time": "0:00", "label": "for loops + range()"},
            {"time": "4:30", "label": "while loops"},
            {"time": "9:00", "label": "break and continue"},
        ],
        notes=(
            "range(5) produces 0,1,2,3,4 - it stops BEFORE the number you give it.\n"
            "for loops are best when you know how many times to repeat, or are iterating a list.\n"
            "while loops repeat based on a condition - make sure something inside changes it,\n"
            "or you'll get an infinite loop.\n"
            "break exits a loop early; continue skips to the next iteration."
        ),
        practice_task=(
            "Write a loop that prints all numbers from 1 to 20, but skips multiples of 3 "
            "using continue, and stops completely once it reaches 18 using break."
        ),
        starter_code=(
            "for number in range(1, 21):\n"
            "    if number == 18:\n"
            "        break\n"
            "    if number % 3 == 0:\n"
            "        continue\n"
            "    print(number)\n"
        ),
        quiz_question="What does range(1, 21) produce?",
        quiz_options=[
            "Numbers 1 to 21, including 21",
            "Numbers 1 to 20, including 20",
            "Numbers 0 to 21",
            "Numbers 1 to 21, excluding 1",
        ],
        quiz_correct_index=1,
        quiz_explanation="range(start, stop) always stops BEFORE `stop`, so range(1, 21) gives 1 through 20.",
    ),
    dict(
        slug="functions",
        title="Functions",
        mentor_intro=(
            "A function is a reusable block of code you define once with def, and then "
            "call by name whenever you need it - optionally passing in different inputs "
            "(parameters) and getting a value back out (return)."
        ),
        analogy=(
            "A function is like a coffee machine: you press \"espresso\" (call the "
            "function), it takes water and coffee beans (the parameters), does the same "
            "steps every time inside the machine (the function body), and hands you a cup "
            "of coffee (the return value). You never need to know HOW it works inside to "
            "use it."
        ),
        video_timestamps=[
            {"time": "0:00", "label": "def and calling a function"},
            {"time": "3:40", "label": "Parameters vs arguments"},
            {"time": "6:50", "label": "return vs print"},
        ],
        notes=(
            "def name(parameters): starts a function definition, followed by an indented body.\n"
            "Parameters are placeholders; arguments are the actual values you pass in.\n"
            "return sends a value back to whoever called the function - it also ends the function.\n"
            "A function with no return statement returns None.\n"
            "Default parameter values (def greet(name=\"friend\")) make arguments optional."
        ),
        practice_task=(
            "Write a function `calculate_area(length, width)` that returns the area of a "
            "rectangle, then call it with two different sets of numbers and print both results."
        ),
        starter_code=(
            "def calculate_area(length, width):\n"
            "    return length * width\n\n"
            "print(\"Area 1:\", calculate_area(5, 3))\n"
            "print(\"Area 2:\", calculate_area(10, 2.5))\n"
        ),
        quiz_question="What does calculate_area(5, 3) return, based on the practice code?",
        quiz_options=["8", "15", "None", "'5 * 3'"],
        quiz_correct_index=1,
        quiz_explanation="return length * width evaluates to 5 * 3, which is 15.",
    ),
    dict(
        slug="lists-and-dictionaries",
        title="Lists & Dictionaries",
        mentor_intro=(
            "A list stores many values in order, accessed by position (index), starting at "
            "0. A dictionary stores values accessed by a meaningful KEY instead of a "
            "position, as key: value pairs."
        ),
        analogy=(
            "A list is like a numbered row of lockers - locker[0] is always the first "
            "locker, no matter what's inside. A dictionary is like an address book - you "
            "don't look someone up by \"the 3rd entry\", you look them up by NAME "
            "(contacts[\"Priya\"]), and it hands you back their number."
        ),
        video_timestamps=[
            {"time": "0:00", "label": "Creating & indexing lists"},
            {"time": "4:20", "label": "List methods: append, remove"},
            {"time": "8:10", "label": "Dictionaries: keys and values"},
        ],
        notes=(
            "Lists use square brackets: fruits = [\"apple\", \"banana\", \"cherry\"].\n"
            "Indexing starts at 0: fruits[0] is \"apple\"; fruits[-1] is the last item.\n"
            "fruits.append(x) adds to the end; fruits.remove(x) deletes the first match.\n"
            "Dictionaries use curly braces with key:value pairs: {\"name\": \"Alex\", \"age\": 25}.\n"
            "Access a dict value with student[\"name\"], and check existence with \"name\" in student."
        ),
        practice_task=(
            "Create a list of 3 favorite movies and print the second one. Then create a "
            "dictionary describing yourself with keys 'name', 'age', 'hobby' and print a "
            "sentence built from those values."
        ),
        starter_code=(
            "movies = [\"Inception\", \"Interstellar\", \"The Matrix\"]\n"
            "print(\"Second movie:\", movies[1])\n\n"
            "profile = {\"name\": \"Alex\", \"age\": 25, \"hobby\": \"coding\"}\n"
            "print(f\"{profile['name']} is {profile['age']} and loves {profile['hobby']}.\")\n"
        ),
        quiz_question="Given movies = [\"Inception\", \"Interstellar\", \"The Matrix\"], what is movies[1]?",
        quiz_options=["Inception", "Interstellar", "The Matrix", "IndexError"],
        quiz_correct_index=1,
        quiz_explanation="Indexing starts at 0, so movies[0] is 'Inception' and movies[1] is 'Interstellar'.",
    ),
]


def seed_python_course() -> None:
    """Creates the 'python-fundamentals' Course + its Lesson rows if missing. Idempotent."""
    course = Course.query.filter_by(slug="python-fundamentals").first()
    if course is None:
        course = Course(
            slug="python-fundamentals",
            title="Python Fundamentals",
            subtitle="Your first steps as a Python developer, taught mentor-style",
            icon="🐍",
            order_index=0,
        )
        db.session.add(course)
        db.session.flush()  # get course.id before creating lessons

    if course.lessons.count() == 0:
        for order_index, lesson_data in enumerate(PYTHON_LESSONS):
            db.session.add(Lesson(
                course_id=course.id,
                order_index=order_index,
                video_url=VIDEO_URL,
                xp_reward=20,
                quiz_xp_bonus=10,
                **lesson_data,
            ))

    db.session.commit()


def ensure_lesson_progress_for_user(user, course: Course) -> list[UserLessonProgress]:
    """
    Returns this user's UserLessonProgress rows for every lesson in `course`,
    creating them on first visit: the first lesson is "active", the rest
    "locked" - mirrors ensure_progress_for_user() above for roadmap steps.
    """
    lessons = list(course.lessons)
    existing = {
        p.lesson_id: p
        for p in UserLessonProgress.query.filter_by(user_id=user.id)
        .filter(UserLessonProgress.lesson_id.in_([l.id for l in lessons]))
        .all()
    }

    created_any = False
    for i, lesson in enumerate(lessons):
        if lesson.id not in existing:
            progress = UserLessonProgress(
                user_id=user.id,
                lesson_id=lesson.id,
                status="active" if i == 0 else "locked",
            )
            db.session.add(progress)
            existing[lesson.id] = progress
            created_any = True

    if created_any:
        db.session.commit()

    return [existing[lesson.id] for lesson in lessons]


def course_completion_percent(user, course: Course) -> int:
    """% of `course`'s lessons this user has marked complete."""
    progress_rows = ensure_lesson_progress_for_user(user, course)
    if not progress_rows:
        return 0
    done = sum(1 for p in progress_rows if p.status == "complete")
    return round(done / len(progress_rows) * 100)


# ---------------------------------------------------------------------------
# Daily Challenges
# ---------------------------------------------------------------------------

# (slug, title, description, category, reward_xp)
DAILY_CHALLENGES = [
    (
        "port-scan-sweep",
        "Scan your local network and find all open ports in range 1-1000.",
        "Fire up the Code Playground, point a socket scanner at your own "
        "machine (127.0.0.1) or router, and log every open port you find "
        "between 1 and 1000.",
        "ethical-hacking",
        150,
    ),
    (
        "list-comprehension-drill",
        "Rewrite 5 everyday for-loops as one-line list comprehensions.",
        "Take five small loops you'd normally write with `for`, and "
        "convert each into a Pythonic list comprehension in the Playground.",
        "ai-ml",
        100,
    ),
    (
        "widget-tree-sketch",
        "Sketch a Flutter widget tree for a login screen from memory.",
        "No looking it up - draft the widget hierarchy (Scaffold down to "
        "the submit button) for a simple login screen, then compare it "
        "against the App Development roadmap notes.",
        "app-development",
        100,
    ),
    (
        "sql-injection-payloads",
        "Craft 3 SQL injection test payloads for a login form.",
        "Using what you learned in the SQL Injection lesson, write three "
        "different payloads you'd use to test a login form for the "
        "vulnerability - then explain in one line why each one works.",
        "ethical-hacking",
        150,
    ),
    (
        "pandas-cleanup-sprint",
        "Clean a messy dataset: drop nulls, fix types, rename columns.",
        "Grab any small CSV (or fake one up), load it with pandas in the "
        "Playground, and get it to a clean, well-typed DataFrame end to end.",
        "ai-ml",
        120,
    ),
    (
        "api-request-mini-app",
        "Build a 10-line script that hits a public API and prints one field.",
        "Pick any free public API, make a request with Python, and pull "
        "out a single field from the JSON response to print - the smallest "
        "possible end-to-end API integration.",
        "app-development",
        100,
    ),
    (
        "password-strength-checker",
        "Write a function that scores password strength from 0-100.",
        "In the Playground, write a function that takes a password string "
        "and returns a 0-100 strength score based on length, character "
        "variety, and common-pattern checks.",
        "ethical-hacking",
        130,
    ),
]


def seed_daily_challenges() -> None:
    """Creates the DailyChallenge rotation pool rows if missing. Idempotent."""
    for order_index, (slug, title, description, category, reward_xp) in enumerate(DAILY_CHALLENGES):
        if DailyChallenge.query.filter_by(slug=slug).first() is None:
            db.session.add(DailyChallenge(
                slug=slug, title=title, description=description,
                category=category, reward_xp=reward_xp, order_index=order_index,
            ))
    db.session.commit()


def get_todays_challenge() -> DailyChallenge | None:
    """
    Deterministic "challenge of the day": every user sees the same one on
    the same calendar date (UTC), and it rotates automatically at
    midnight - no scheduler/cron needed, it's just today's ordinal mod
    the pool size.
    """
    challenges = DailyChallenge.query.order_by(DailyChallenge.order_index).all()
    if not challenges:
        return None
    today = datetime.now(timezone.utc).date()
    return challenges[today.toordinal() % len(challenges)]


def next_utc_midnight_iso() -> str:
    """ISO timestamp of the next UTC midnight, for the client-side countdown."""
    today = datetime.now(timezone.utc).date()
    tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return tomorrow.isoformat()


def ensure_today_progress_for_user(user, challenge: DailyChallenge) -> UserDailyChallengeProgress:
    """Gets (or creates, starting at 'not_started') this user's row for
    today's challenge. Yesterday's row is simply left as-is in history."""
    today = datetime.now(timezone.utc).date()
    progress = UserDailyChallengeProgress.query.filter_by(user_id=user.id, challenge_date=today).first()
    if progress is None:
        progress = UserDailyChallengeProgress(
            user_id=user.id, challenge_id=challenge.id, challenge_date=today, status="not_started",
        )
        db.session.add(progress)
        db.session.commit()
    return progress


# ---------------------------------------------------------------------------
# Project Gallery
# ---------------------------------------------------------------------------

# (slug, title, description, icon, tech_tag, category, difficulty, base_likes, href)
PROJECTS = [
    (
        "ai-chatbot", "AI Chatbot",
        "A conversational chatbot you build and extend in the Playground - "
        "swap in any provider later without touching the UI.",
        "💬", "Python", "ai-ml", "Intermediate", 124, "#",
    ),
    (
        "expense-tracker-app", "Expense Tracker App",
        "A Flutter + Firebase mobile app for logging expenses, with charts "
        "and category budgets.",
        "📱", "Flutter", "app-development", "Beginner", 98, "#",
    ),
    (
        "network-scanner", "Network Scanner",
        "A Python CLI tool that sweeps a subnet, reports live hosts, and "
        "flags open ports worth a closer look.",
        "🛰️", "Python", "ethical-hacking", "Intermediate", 158, "#",
    ),
    (
        "fake-news-detector", "Fake News Detector",
        "An ML text-classification project that scores how likely an "
        "article is to be misleading, trained on a labeled headline set.",
        "📰", "ML", "ai-ml", "Advanced", 112, "#",
    ),
]


def seed_projects() -> None:
    """Creates the Project Gallery rows if missing. Idempotent."""
    for order_index, (slug, title, description, icon, tech_tag, category, difficulty, base_likes, href) in enumerate(PROJECTS):
        if Project.query.filter_by(slug=slug).first() is None:
            db.session.add(Project(
                slug=slug, title=title, description=description, icon=icon,
                tech_tag=tech_tag, category=category, difficulty=difficulty,
                base_likes=base_likes, href=href, order_index=order_index,
            ))
    db.session.commit()


def project_gallery_view(user) -> list[dict]:
    """Every project as a plain dict, with this user's like state baked in -
    exactly what dashboard.html's Project Gallery card needs to render
    (plus category/difficulty, used by the standalone Projects page)."""
    liked_project_ids = {
        row.project_id for row in UserProjectLike.query.filter_by(user_id=user.id).all()
    }
    projects = Project.query.order_by(Project.order_index).all()
    return [{
        "slug": p.slug,
        "title": p.title,
        "description": p.description,
        "icon": p.icon,
        "tech_tag": p.tech_tag,
        "category": p.category,
        "difficulty": p.difficulty,
        "likes_count": p.likes_count(),
        "liked": p.id in liked_project_ids,
        "href": p.href,
    } for p in projects]


# ---------------------------------------------------------------------------
# Ethical Hacking, AI/ML, and App Development courses
#
# Same lesson-dict shape as PYTHON_LESSONS above, seeded through the
# generic _seed_course() helper below instead of duplicating
# seed_python_course()'s logic three more times.
# ---------------------------------------------------------------------------

WEB_PENTESTING_LESSONS = [
    dict(
        slug="how-sql-queries-work",
        title="How SQL Queries Work",
        mentor_intro=(
            "Before you can understand SQL injection, you need to see a normal SQL query "
            "doing its job. A login form usually runs something like "
            "SELECT * FROM users WHERE username = 'alex' AND password = 'hunter2' behind "
            "the scenes - if a row comes back, you're logged in. SQL injection is what "
            "happens when an attacker can change the shape of that query just by typing "
            "into the username or password box."
        ),
        analogy=(
            "A SQL query is like a fill-in-the-blank form the database reads out loud: "
            "\"find me the user named ___ with password ___.\" Normally you only fill in "
            "the blanks. Injection is what happens when you write something in the blank "
            "that changes the sentence itself, not just the answer."
        ),
        notes=(
            "Most login checks run one query: SELECT * FROM users WHERE username=... AND password=...\n"
            "If any row is returned, the app treats the login as successful.\n"
            "The query is normally built by directly pasting user input into a string.\n"
            "That direct string-pasting is the root cause of SQL injection.\n"
            "Parameterized queries (covered later) fix this at the source."
        ),
        practice_task=(
            "In the Playground, write a Python string that builds a fake SQL query the "
            "unsafe way: f\"SELECT * FROM users WHERE username = '{username}'\" — then "
            "print it with username set to a normal name, and print it again with "
            "username set to something with a single quote in it. Notice how the quote "
            "breaks the structure."
        ),
        starter_code=(
            "def build_query(username):\n"
            "    return f\"SELECT * FROM users WHERE username = '{username}'\"\n\n"
            "print(build_query(\"alex\"))\n"
            "print(build_query(\"alex' OR '1'='1\"))\n"
        ),
        quiz_question="What makes a web app vulnerable to SQL injection in the first place?",
        quiz_options=[
            "Using HTTPS instead of HTTP",
            "Directly pasting user input into a SQL query string",
            "Storing passwords in a database",
            "Using a slow database server",
        ],
        quiz_correct_index=1,
        quiz_explanation="Injection happens when user input is pasted directly into the query text instead of being treated as pure data.",
    ),
    dict(
        slug="what-is-sql-injection",
        title="What is SQL Injection",
        mentor_intro=(
            "SQL injection (SQLi) is a technique where an attacker types SQL syntax into a "
            "normal input field so the database executes something the developer never "
            "intended - like turning a login check into 'always true', or pulling extra "
            "columns out of a completely different table."
        ),
        analogy=(
            "Imagine handing someone a form that says \"Name: ___\" and they write "
            "\"Alex, and also give me the keys to the building\" - if whoever's reading "
            "the form isn't careful, they might actually follow BOTH instructions. That's "
            "exactly what an injected SQL payload tries to do to a database."
        ),
        notes=(
            "SQL injection is consistently in the OWASP Top 10 web vulnerabilities.\n"
            "It can lead to authentication bypass, data theft, or full data destruction.\n"
            "It affects any query built by string-concatenating user input.\n"
            "It's not limited to login forms - search boxes, filters, and URLs are common targets too."
        ),
        practice_task=(
            "Write down three different places in a typical website (besides the login "
            "form) where user input might end up inside a SQL query."
        ),
        starter_code=(
            "# Common injection entry points on a typical site:\n"
            "entry_points = [\n"
            "    \"Login form (username/password)\",\n"
            "    \"Search bar\",\n"
            "    \"URL query parameters like ?id=5\",\n"
            "]\n"
            "for point in entry_points:\n"
            "    print(\"-\", point)\n"
        ),
        quiz_question="SQL injection is best described as...",
        quiz_options=[
            "A virus that infects databases",
            "Malicious SQL syntax smuggled in through normal user input",
            "A type of DDoS attack",
            "A database backup technique",
        ],
        quiz_correct_index=1,
        quiz_explanation="It's specifically about getting the database to run attacker-controlled SQL by hiding it inside ordinary input.",
    ),
    dict(
        slug="types-of-sql-injection",
        title="Types of SQL Injection",
        mentor_intro=(
            "SQL injection isn't one single technique - it comes in a few flavors "
            "depending on how the attacker gets feedback back from the database: "
            "In-band (the result shows up directly, via UNION or errors), Blind "
            "(nothing shows up, so you ask true/false questions), and Out-of-band "
            "(the database sends data somewhere else entirely, like DNS)."
        ),
        analogy=(
            "It's like three different ways to figure out what's behind a locked door: "
            "In-band is opening it and looking straight in. Blind is knocking and only "
            "getting a yes/no through the door. Out-of-band is slipping a note under a "
            "completely different door down the hall."
        ),
        notes=(
            "In-band SQLi: Union-based and Error-based - results appear directly.\n"
            "Blind SQLi: Boolean-based and Time-based - inferred from true/false behavior.\n"
            "Out-of-band SQLi: relies on the DB making an external network request.\n"
            "The next few lessons dig into Union-based, Error-based, and Blind in detail."
        ),
        practice_task=(
            "Match each type to its 'signal': you get the data directly / you only get "
            "true-or-false / the data leaves through a different channel entirely."
        ),
        starter_code=(
            "types_and_signals = {\n"
            "    \"In-band (Union/Error)\": \"Data shown directly in the response\",\n"
            "    \"Blind (Boolean/Time)\": \"Only true/false or delay differences\",\n"
            "    \"Out-of-band\": \"Data exfiltrated via a separate channel (e.g. DNS)\",\n"
            "}\n"
            "for kind, signal in types_and_signals.items():\n"
            "    print(f\"{kind}: {signal}\")\n"
        ),
        quiz_question="Which type of SQL injection relies on asking the database true/false questions with no visible data returned?",
        quiz_options=["Union-based", "Error-based", "Blind", "Out-of-band"],
        quiz_correct_index=2,
        quiz_explanation="Blind SQLi gets no direct output - the attacker infers data one true/false answer at a time.",
    ),
    dict(
        slug="union-based-injection",
        title="Union-Based Injection",
        mentor_intro=(
            "UNION lets you combine the results of two SELECT queries into one result "
            "set. Union-based injection abuses this: if an attacker can append "
            "' UNION SELECT ... to a query, and the column count/types line up, the "
            "database happily returns rows from a completely different table alongside "
            "the original results."
        ),
        analogy=(
            "It's like photocopying a second, unrelated document onto the back of an "
            "approved report before handing it in - to the person collecting the report, "
            "it all looks like one document, but you smuggled in extra pages."
        ),
        notes=(
            "UNION SELECT requires the same number of columns as the original query.\n"
            "Attackers first probe the column count using ORDER BY or UNION SELECT NULL,NULL,...\n"
            "Once the count matches, real data (like usernames/passwords) can be pulled into the columns.\n"
            "This only works when the app actually displays query results on the page."
        ),
        practice_task=(
            "Write a comment explaining, in your own words, why the number of columns "
            "in a UNION SELECT payload has to match the original query's column count."
        ),
        starter_code=(
            "# A UNION requires matching column counts on both sides.\n"
            "original_columns = 3\n"
            "injected_columns = [\"NULL\", \"NULL\", \"NULL\"]  # probing for the right count\n"
            "print(f\"Original query selects {original_columns} columns\")\n"
            "print(f\"Injected UNION SELECT needs exactly {len(injected_columns)} columns to match\")\n"
        ),
        quiz_question="For a UNION-based injection to work, the attacker's SELECT must...",
        quiz_options=[
            "Use a different table only",
            "Match the original query's column count",
            "Always target the admin table",
            "Be written in a different SQL dialect",
        ],
        quiz_correct_index=1,
        quiz_explanation="UNION only works when both SELECTs return the same number of columns.",
    ),
    dict(
        slug="error-based-injection",
        title="Error-Based Injection",
        mentor_intro=(
            "Error-based injection deliberately triggers a database error that leaks "
            "information in the error MESSAGE itself - for example, forcing a type "
            "conversion error that prints the current database version or a table name "
            "right there in the page's error output."
        ),
        analogy=(
            "It's like asking a question that the person can't answer directly, but the "
            "way they stumble over the answer while apologizing accidentally reveals the "
            "answer anyway."
        ),
        notes=(
            "Relies on the app displaying raw database error messages to the user.\n"
            "Common technique: force a type-cast error (e.g. converting text to a number).\n"
            "The leaked error text can reveal DB version, table names, or column names.\n"
            "Fix: never show raw database errors to end users - log them instead, server-side."
        ),
        practice_task=(
            "Explain why showing a site's raw internal error messages to visitors is a "
            "security risk, even beyond SQL injection specifically."
        ),
        starter_code=(
            "# Simulating what NOT to do: showing a raw error to the user\n"
            "def unsafe_error_handler(user_input):\n"
            "    try:\n"
            "        return int(user_input)\n"
            "    except ValueError as e:\n"
            "        return f\"Error: {e}\"  # leaks internal detail - don't do this in production\n\n"
            "print(unsafe_error_handler(\"not_a_number\"))\n"
        ),
        quiz_question="Error-based SQL injection works by...",
        quiz_options=[
            "Crashing the whole server",
            "Leaking data through a triggered error message",
            "Deleting the error logs",
            "Bypassing HTTPS encryption",
        ],
        quiz_correct_index=1,
        quiz_explanation="The technique deliberately forces an error whose message text reveals database information.",
    ),
    dict(
        slug="blind-sql-injection",
        title="Blind SQL Injection",
        mentor_intro=(
            "When an app shows no data and no errors, attackers can still extract "
            "information one bit at a time using Blind SQLi: Boolean-based (does the "
            "page look different for a TRUE vs FALSE condition?) or Time-based (does the "
            "response take 5 seconds longer when a condition is TRUE, using something "
            "like SLEEP(5)?)."
        ),
        analogy=(
            "It's like playing 20 Questions with someone who can only say 'yes' or 'no' - "
            "slow, but if you ask enough well-chosen questions, you can eventually figure "
            "out the whole answer."
        ),
        notes=(
            "Boolean-based: compares page behavior for TRUE vs FALSE injected conditions.\n"
            "Time-based: uses a deliberate delay (e.g. SLEEP(5)) as the TRUE signal instead of visible output.\n"
            "Both are slower than in-band techniques - often automated with tools like sqlmap.\n"
            "Blind SQLi proves that 'no visible errors' does NOT mean 'not vulnerable'."
        ),
        practice_task=(
            "In plain English, describe how you'd extract a single character of a secret "
            "value using ONLY true/false answers (hint: think binary search)."
        ),
        starter_code=(
            "# Simplified idea behind Boolean-based blind extraction (binary search over an ASCII range)\n"
            "def guess_char_range(secret_char_code, low=32, high=126):\n"
            "    guesses = 0\n"
            "    while low < high:\n"
            "        guesses += 1\n"
            "        mid = (low + high) // 2\n"
            "        if secret_char_code > mid:   # this comparison is what the TRUE/FALSE payload checks\n"
            "            low = mid + 1\n"
            "        else:\n"
            "            high = mid\n"
            "    return low, guesses\n\n"
            "print(guess_char_range(ord(\"A\")))  # (character code found, number of yes/no questions needed)\n"
        ),
        quiz_question="Time-based blind SQL injection detects a TRUE condition by...",
        quiz_options=[
            "Reading the response body directly",
            "A deliberate delay in the response time",
            "An HTTP redirect",
            "A change in the page's HTTP status code only",
        ],
        quiz_correct_index=1,
        quiz_explanation="Time-based blind SQLi uses a forced delay (like SLEEP(5)) as the only observable signal for TRUE.",
    ),
    dict(
        slug="preventing-sql-injection",
        title="Preventing SQL Injection",
        mentor_intro=(
            "The real fix for SQL injection isn't 'filter out quote characters' - it's "
            "parameterized queries (a.k.a. prepared statements), where user input is sent "
            "to the database SEPARATELY from the query structure, so it can never be "
            "interpreted as SQL syntax no matter what characters it contains."
        ),
        analogy=(
            "It's the difference between mailing someone a letter where they might "
            "misread your instructions as part of the address, versus filling out a "
            "structured form with clearly separate boxes - the form's structure can't be "
            "changed by what you write inside a box."
        ),
        notes=(
            "Parameterized queries: cursor.execute('SELECT * FROM users WHERE username=%s', (username,))\n"
            "Never build SQL with f-strings or + concatenation from user input.\n"
            "Least-privilege database accounts limit the damage even if injection occurs.\n"
            "Web application firewalls (WAFs) help but are a second layer, not a substitute for fixing the code.\n"
            "This is the same principle any of this app's own database code follows for anything user-input-related."
        ),
        practice_task=(
            "Rewrite the unsafe query builder from the first lesson "
            "(f\"SELECT * FROM users WHERE username = '{username}'\") as a comment showing "
            "the parameterized version instead."
        ),
        starter_code=(
            "# UNSAFE (don't do this):\n"
            "# query = f\"SELECT * FROM users WHERE username = '{username}'\"\n\n"
            "# SAFE (parameterized - input is data, never SQL syntax):\n"
            "query = \"SELECT * FROM users WHERE username = %s\"\n"
            "params = (\"alex\",)\n"
            "print(query)\n"
            "print(params)\n"
        ),
        quiz_question="What's the actual fix for SQL injection, not just a band-aid?",
        quiz_options=[
            "Blocking the single-quote character",
            "Parameterized queries / prepared statements",
            "Turning off error messages only",
            "Using a longer password policy",
        ],
        quiz_correct_index=1,
        quiz_explanation="Parameterized queries separate user input from query structure entirely, closing off injection at the root cause.",
    ),
]

MACHINE_LEARNING_LESSONS = [
    dict(
        slug="what-is-machine-learning",
        title="What is Machine Learning",
        mentor_intro=(
            "Traditional programming is rules-in, answers-out: you write the logic, the "
            "computer follows it. Machine learning flips that - you feed in examples "
            "(data) and answers, and the computer works out the rules (the model) itself, "
            "which it can then apply to new, unseen examples."
        ),
        analogy=(
            "It's like teaching a kid to recognize dogs by showing them hundreds of photos "
            "labeled 'dog' or 'not dog', instead of trying to write down an exact "
            "definition of what a dog looks like. Eventually they can spot a dog they've "
            "never seen before."
        ),
        notes=(
            "Traditional programming: rules + data -> answers.\n"
            "Machine learning: data + answers -> rules (the model).\n"
            "A 'model' is just the learned rules, ready to make predictions on new data.\n"
            "More/better quality data usually means a better model."
        ),
        practice_task=(
            "List two everyday apps you use that likely rely on machine learning behind "
            "the scenes, and guess what data they were probably trained on."
        ),
        starter_code=(
            "examples = {\n"
            "    \"Spam filter\": \"Trained on emails labeled spam / not spam\",\n"
            "    \"Movie recommendations\": \"Trained on what similar users watched and liked\",\n"
            "}\n"
            "for app, training_data in examples.items():\n"
            "    print(f\"{app}: {training_data}\")\n"
        ),
        quiz_question="What's the key difference between traditional programming and machine learning?",
        quiz_options=[
            "ML doesn't need any code at all",
            "ML learns rules from data instead of being explicitly programmed with rules",
            "Traditional programming is always faster",
            "There is no real difference",
        ],
        quiz_correct_index=1,
        quiz_explanation="ML derives its own rules (the model) from labeled examples, rather than following rules a human wrote out.",
    ),
    dict(
        slug="types-of-machine-learning",
        title="Types of Machine Learning",
        mentor_intro=(
            "ML splits into three broad categories: Supervised learning (you have "
            "labeled examples, like emails marked spam/not-spam), Unsupervised learning "
            "(no labels - the model finds patterns/groups on its own), and Reinforcement "
            "learning (an agent learns by trial and error, getting rewards or penalties)."
        ),
        analogy=(
            "Supervised is like studying with an answer key. Unsupervised is like sorting "
            "a messy box of photos into piles based on similarity, with no one telling you "
            "the categories. Reinforcement is like learning to ride a bike - you fall, "
            "adjust, and get better through trial and error."
        ),
        notes=(
            "Supervised learning: input + correct output pairs (e.g. spam detection, price prediction).\n"
            "Unsupervised learning: no labels, model finds structure (e.g. customer segmentation).\n"
            "Reinforcement learning: reward-driven, trial-and-error (e.g. game-playing AI).\n"
            "Most beginner ML courses (and this one) start with supervised learning."
        ),
        practice_task=(
            "For each: spam detection, grouping customers by shopping habits, and a robot "
            "learning to walk - label which type of ML it is."
        ),
        starter_code=(
            "scenarios = {\n"
            "    \"Spam detection\": \"Supervised\",\n"
            "    \"Grouping customers by shopping habits\": \"Unsupervised\",\n"
            "    \"Robot learning to walk\": \"Reinforcement\",\n"
            "}\n"
            "for scenario, ml_type in scenarios.items():\n"
            "    print(f\"{scenario} -> {ml_type}\")\n"
        ),
        quiz_question="A model that groups customers into segments with NO pre-labeled categories is using...",
        quiz_options=["Supervised learning", "Unsupervised learning", "Reinforcement learning", "Rule-based programming"],
        quiz_correct_index=1,
        quiz_explanation="No labels + finding structure/groups on its own is the definition of unsupervised learning.",
    ),
    dict(
        slug="intro-to-numpy-pandas",
        title="Intro to NumPy & pandas",
        mentor_intro=(
            "Almost every ML project starts with NumPy (fast number arrays) and pandas "
            "(spreadsheet-like DataFrames) for loading, cleaning, and exploring data "
            "before any model ever sees it. A DataFrame is basically a table you can "
            "filter, sort, and compute on with Python code."
        ),
        analogy=(
            "pandas is like Excel, but you control it with code instead of clicking - "
            "which means whatever you did to clean the data can be re-run instantly on a "
            "new dataset."
        ),
        notes=(
            "import pandas as pd - the standard alias, almost universal in ML code.\n"
            "A DataFrame is rows + named columns, like a spreadsheet.\n"
            "df.head() shows the first few rows - the first thing you run on any new dataset.\n"
            "df['column_name'] pulls out a single column as a Series."
        ),
        practice_task=(
            "Without pandas installed, simulate a tiny 'DataFrame' using a list of "
            "dictionaries, then print just the 'name' field from each row."
        ),
        starter_code=(
            "rows = [\n"
            "    {\"name\": \"Alex\", \"score\": 88},\n"
            "    {\"name\": \"Sam\", \"score\": 95},\n"
            "    {\"name\": \"Priya\", \"score\": 72},\n"
            "]\n\n"
            "for row in rows:\n"
            "    print(row[\"name\"])\n"
        ),
        quiz_question="A pandas DataFrame is best described as...",
        quiz_options=[
            "A single number",
            "A table with rows and named columns",
            "A type of neural network",
            "A web framework",
        ],
        quiz_correct_index=1,
        quiz_explanation="A DataFrame is a 2D, spreadsheet-like structure - rows of data under named columns.",
    ),
    dict(
        slug="linear-regression-basics",
        title="Linear Regression Basics",
        mentor_intro=(
            "Linear regression is the simplest supervised model: it draws the best "
            "straight line through your data points so it can predict a NUMBER (like a "
            "house price) from one or more input features (like square footage)."
        ),
        analogy=(
            "Picture a scatter plot of house size vs. price. Linear regression is the "
            "single straight line you'd draw by eye to best represent the trend - the "
            "model just finds that line mathematically instead of by eye."
        ),
        notes=(
            "Predicts a continuous number, not a category (that's classification - next lesson).\n"
            "The 'line' is really: prediction = (weight * input) + bias.\n"
            "Training = adjusting weight and bias until the line fits the data as closely as possible.\n"
            "Works best when the relationship between input and output is roughly linear."
        ),
        practice_task=(
            "Given the line prediction = 2*x + 3, calculate the prediction for x = 5 by "
            "hand, then verify it in the Playground."
        ),
        starter_code=(
            "def predict(x, weight=2, bias=3):\n"
            "    return weight * x + bias\n\n"
            "for x in [0, 1, 5, 10]:\n"
            "    print(f\"x={x} -> prediction={predict(x)}\")\n"
        ),
        quiz_question="Linear regression is used to predict...",
        quiz_options=["A yes/no category", "A continuous number", "An image label", "A text sentiment"],
        quiz_correct_index=1,
        quiz_explanation="Linear regression outputs a continuous numeric value, like a price or temperature.",
    ),
    dict(
        slug="classification-basics",
        title="Classification Basics",
        mentor_intro=(
            "Classification predicts a CATEGORY instead of a number - spam vs. not spam, "
            "cat vs. dog, or which of several classes an input belongs to. It's the other "
            "half of supervised learning, alongside regression."
        ),
        analogy=(
            "If regression is drawing a line through points, classification is drawing a "
            "boundary that separates one group of points from another - like drawing a "
            "fence between the 'cats' side of a field and the 'dogs' side."
        ),
        notes=(
            "Binary classification: 2 possible classes (spam / not spam).\n"
            "Multi-class classification: 3+ possible classes (cat / dog / bird).\n"
            "A classifier's raw output is often a probability (e.g. 0.87 = 87% likely spam).\n"
            "A threshold (often 0.5) turns that probability into a final class decision."
        ),
        practice_task=(
            "Given a spam probability of 0.72 and a threshold of 0.5, decide whether the "
            "email should be classified as spam, and write the logic in code."
        ),
        starter_code=(
            "def classify(probability, threshold=0.5):\n"
            "    return \"spam\" if probability >= threshold else \"not spam\"\n\n"
            "print(classify(0.72))\n"
            "print(classify(0.31))\n"
        ),
        quiz_question="A model that predicts 'spam' or 'not spam' is doing...",
        quiz_options=["Regression", "Binary classification", "Unsupervised clustering", "Reinforcement learning"],
        quiz_correct_index=1,
        quiz_explanation="Two possible output categories = binary classification.",
    ),
    dict(
        slug="training-testing-splits",
        title="Training & Testing Splits",
        mentor_intro=(
            "You never train and evaluate a model on the exact same data - that's like "
            "grading a student using the same questions they memorized answers to. "
            "Instead, data gets split: a training set the model learns from, and a "
            "separate test set used only to check how well it generalizes."
        ),
        analogy=(
            "It's the difference between practicing with sample exam questions (training "
            "set) and then taking the REAL exam with different questions (test set) - "
            "that's the only fair way to know if you actually learned the material."
        ),
        notes=(
            "A common split is 80% training / 20% testing.\n"
            "The model must NEVER see test data during training - that would leak the answers.\n"
            "A model that does great on training data but poorly on test data is 'overfitting'.\n"
            "Overfitting means it memorized the training data instead of learning general patterns."
        ),
        practice_task=(
            "Given 1000 data rows and an 80/20 split, calculate exactly how many rows go "
            "into training vs. testing."
        ),
        starter_code=(
            "total_rows = 1000\n"
            "train_ratio = 0.8\n\n"
            "train_size = int(total_rows * train_ratio)\n"
            "test_size = total_rows - train_size\n"
            "print(f\"Training rows: {train_size}\")\n"
            "print(f\"Testing rows: {test_size}\")\n"
        ),
        quiz_question="Why do we test a model on DIFFERENT data than it was trained on?",
        quiz_options=[
            "It's faster that way",
            "To check whether it generalizes instead of just memorizing",
            "Training data is always corrupted",
            "It's not actually necessary",
        ],
        quiz_correct_index=1,
        quiz_explanation="Testing on unseen data is the only way to know if the model learned general patterns rather than memorizing.",
    ),
    dict(
        slug="evaluating-model-accuracy",
        title="Evaluating Model Accuracy",
        mentor_intro=(
            "Accuracy (percent of predictions the model got right) is the simplest metric, "
            "but it can be misleading - if 95% of emails aren't spam, a model that just "
            "always guesses 'not spam' gets 95% accuracy while being completely useless. "
            "That's why precision and recall exist alongside accuracy."
        ),
        analogy=(
            "Imagine a smoke detector that never goes off - it'll be 'right' almost every "
            "day (no fire, no alarm), but it's useless the one day it actually matters. "
            "Accuracy alone hides exactly this kind of failure."
        ),
        notes=(
            "Accuracy = correct predictions / total predictions.\n"
            "Accuracy alone is misleading on imbalanced data (e.g. rare fraud detection).\n"
            "Precision: of the things predicted positive, how many actually were?\n"
            "Recall: of the things that were actually positive, how many did the model catch?"
        ),
        practice_task=(
            "A model makes 100 predictions and gets 90 right. Calculate its accuracy as a "
            "percentage in the Playground."
        ),
        starter_code=(
            "correct = 90\n"
            "total = 100\n\n"
            "accuracy = (correct / total) * 100\n"
            "print(f\"Accuracy: {accuracy}%\")\n"
        ),
        quiz_question="Why can accuracy alone be a misleading metric?",
        quiz_options=[
            "It's too hard to calculate",
            "It can look high even when the model ignores the rare, important cases",
            "It only works for regression",
            "It's not a real metric",
        ],
        quiz_correct_index=1,
        quiz_explanation="On imbalanced data, always predicting the majority class gives high accuracy while missing what actually matters.",
    ),
]

APP_DEVELOPMENT_LESSONS = [
    dict(
        slug="what-is-flutter",
        title="What is Flutter",
        mentor_intro=(
            "Flutter is Google's toolkit for building apps from ONE codebase that runs on "
            "iOS, Android, web, and desktop. Instead of writing separate native apps, you "
            "write Dart code once and Flutter draws every pixel itself, so the app looks "
            "and behaves identically everywhere."
        ),
        analogy=(
            "It's like writing one recipe that automatically comes out perfectly whether "
            "you're cooking on a gas stove, electric stove, or campfire - one set of "
            "instructions, consistent result regardless of the 'platform' you're cooking on."
        ),
        notes=(
            "Flutter apps are written in Dart (Google's programming language).\n"
            "One codebase targets iOS, Android, web, and desktop.\n"
            "Flutter renders its own UI rather than using each platform's native components.\n"
            "This is why a Flutter app looks the same on an iPhone and an Android phone."
        ),
        practice_task=(
            "List two advantages and one tradeoff of writing one Flutter codebase instead "
            "of separate native iOS and Android apps."
        ),
        starter_code=(
            "pros = [\"One codebase for multiple platforms\", \"Consistent look across devices\"]\n"
            "tradeoffs = [\"App size can be larger than a fully native app\"]\n\n"
            "print(\"Pros:\", pros)\n"
            "print(\"Tradeoffs:\", tradeoffs)\n"
        ),
        quiz_question="What is Flutter's biggest selling point?",
        quiz_options=[
            "It only works on Android",
            "One codebase runs on multiple platforms",
            "It replaces the need for a backend",
            "It's a database system",
        ],
        quiz_correct_index=1,
        quiz_explanation="Flutter's core value is writing UI once in Dart and deploying it across iOS, Android, web, and desktop.",
    ),
    dict(
        slug="widgets-and-widget-tree",
        title="Widgets & the Widget Tree",
        mentor_intro=(
            "In Flutter, EVERYTHING is a widget - text, buttons, padding, even the screen "
            "layout itself. Widgets nest inside each other to form a widget tree, and "
            "Flutter redraws exactly the parts of that tree that change."
        ),
        analogy=(
            "Think of the widget tree like a set of nested boxes - a big box (the screen) "
            "contains a medium box (a card), which contains smaller boxes (an icon and "
            "some text). Move or restyle a box, and only that box (and what's inside it) "
            "needs to be redrawn."
        ),
        notes=(
            "Everything visible in Flutter is a widget, including invisible layout helpers like Padding.\n"
            "Widgets nest to form a tree - a Column can contain a Row, which contains a Text.\n"
            "Common widgets: Text, Container, Row, Column, Scaffold, ElevatedButton.\n"
            "Understanding the tree is essential before state management makes sense."
        ),
        practice_task=(
            "Sketch (in a comment) a simple widget tree for a login screen: Scaffold at "
            "the top, down to two Text widgets and a button."
        ),
        starter_code=(
            "# A simplified widget tree, described as nested Python dicts:\n"
            "widget_tree = {\n"
            "    \"Scaffold\": {\n"
            "        \"Column\": [\"Text: Username\", \"Text: Password\", \"ElevatedButton: Login\"]\n"
            "    }\n"
            "}\n"
            "print(widget_tree)\n"
        ),
        quiz_question="In Flutter, what is a Text widget, a Column, and even padding all examples of?",
        quiz_options=["Plugins", "Widgets", "Databases", "APIs"],
        quiz_correct_index=1,
        quiz_explanation="Flutter's core principle is that everything on screen - visible or structural - is a widget.",
    ),
    dict(
        slug="stateless-vs-stateful-widgets",
        title="Stateless vs. Stateful Widgets",
        mentor_intro=(
            "A StatelessWidget never changes once built - like a label that always shows "
            "the same text. A StatefulWidget can redraw itself when its internal data "
            "changes, like a counter that updates every time you tap a button."
        ),
        analogy=(
            "A StatelessWidget is like a printed sign - what it says is fixed. A "
            "StatefulWidget is like a digital scoreboard - the numbers on it can update "
            "any time the game state changes."
        ),
        notes=(
            "StatelessWidget: no internal data that changes over time.\n"
            "StatefulWidget: has a State object that can call setState() to trigger a redraw.\n"
            "setState() tells Flutter 'something changed, please redraw this part of the tree'.\n"
            "Choosing the wrong one is a very common beginner mistake - if it needs to change, it's Stateful."
        ),
        practice_task=(
            "For a static 'About' screen and a Todo list with checkboxes you can tick, "
            "decide which one is Stateless and which is Stateful, and why."
        ),
        starter_code=(
            "screens = {\n"
            "    \"About screen (fixed text)\": \"Stateless\",\n"
            "    \"Todo list with tickable checkboxes\": \"Stateful\",\n"
            "}\n"
            "for screen, widget_type in screens.items():\n"
            "    print(f\"{screen} -> {widget_type}\")\n"
        ),
        quiz_question="A widget that needs to redraw itself whenever the user interacts with it should be...",
        quiz_options=["A StatelessWidget", "A StatefulWidget", "A Scaffold only", "An Image widget"],
        quiz_correct_index=1,
        quiz_explanation="Anything that changes over time (via setState) needs to be a StatefulWidget.",
    ),
    dict(
        slug="building-a-todo-list-ui",
        title="Building a Todo List UI",
        mentor_intro=(
            "A todo list app is the classic first Flutter project because it touches all "
            "the fundamentals at once: a list of items (ListView), each item as a widget "
            "(a Row with a checkbox and text), and a way to add new ones (a text field + "
            "button)."
        ),
        analogy=(
            "Building the UI first, before wiring up any real logic, is like building a "
            "stage set before the actors arrive - you want the layout right before you "
            "worry about what happens when someone interacts with it."
        ),
        notes=(
            "ListView.builder() efficiently renders a scrollable list of items.\n"
            "Each todo item is typically a Row: a Checkbox + Expanded(Text(...)).\n"
            "A TextField + IconButton at the top or bottom lets the user type + submit a new item.\n"
            "At this stage the list can just be a hardcoded/dummy list - real state comes next lesson."
        ),
        practice_task=(
            "List, in order, the 3 widgets you'd nest to build a single todo row: from "
            "outermost to innermost."
        ),
        starter_code=(
            "todo_row_structure = [\"Row\", \"Checkbox\", \"Expanded(Text)\"]\n"
            "for i, widget in enumerate(todo_row_structure, start=1):\n"
            "    print(f\"{i}. {widget}\")\n"
        ),
        quiz_question="Which widget is best suited for efficiently rendering a scrollable list of todo items?",
        quiz_options=["Text", "ListView.builder()", "Padding", "Scaffold"],
        quiz_correct_index=1,
        quiz_explanation="ListView.builder() is designed specifically for efficient, scrollable, potentially long lists.",
    ),
    dict(
        slug="state-management-basics",
        title="State Management Basics",
        mentor_intro=(
            "Once your todo list UI exists, you need actual STATE - the list of todos "
            "that lives in memory and changes as the user adds/removes/checks items. For "
            "a small app, Flutter's built-in setState() inside a StatefulWidget is enough; "
            "bigger apps later reach for Provider, Riverpod, or Bloc."
        ),
        analogy=(
            "State is like the actual to-do list on your fridge whiteboard - the UI is "
            "just the whiteboard's current display. Every time you erase and rewrite an "
            "item, that's setState() telling the widget 'redraw yourself with this new data'."
        ),
        notes=(
            "State = the data that can change and affects what's on screen.\n"
            "setState(() { ... }) is the simplest way to trigger a rebuild in a StatefulWidget.\n"
            "For simple apps (like a todo list), setState() alone is genuinely enough.\n"
            "Larger apps use dedicated state management packages to avoid deeply nested setState calls."
        ),
        practice_task=(
            "Simulate adding an item to a todo list and 'triggering a rebuild' by printing "
            "the list before and after the change."
        ),
        starter_code=(
            "todos = [\"Buy milk\", \"Walk the dog\"]\n"
            "print(\"Before:\", todos)\n\n"
            "def add_todo(item):\n"
            "    todos.append(item)  # in real Flutter, this line would be inside setState()\n\n"
            "add_todo(\"Finish Flutter lesson\")\n"
            "print(\"After:\", todos)\n"
        ),
        quiz_question="For a small app, what's the simplest way to trigger a UI rebuild when data changes?",
        quiz_options=["Restarting the app", "setState()", "Deleting the widget tree", "Rewriting the whole app in a new language"],
        quiz_correct_index=1,
        quiz_explanation="setState() is Flutter's built-in mechanism to say 'this data changed, please redraw'.",
    ),
    dict(
        slug="connecting-firebase",
        title="Connecting Firebase",
        mentor_intro=(
            "Firebase gives a Flutter app a real backend without writing your own server: "
            "Firestore for storing data (like todos, per user), Firebase Auth for "
            "login/signup, and hosting/notifications if you need them. Your todo list can "
            "go from 'stored only in memory' to 'saved permanently and synced across devices'."
        ),
        analogy=(
            "It's like the difference between writing on a whiteboard that gets erased "
            "when you leave the room, versus writing in a shared notebook stored safely "
            "somewhere else - Firebase is that safely-stored, always-accessible notebook."
        ),
        notes=(
            "Firestore: a cloud NoSQL database - stores your todos as documents in a collection.\n"
            "Firebase Auth: handles signup/login so you don't build your own auth system in the app.\n"
            "Data syncs in real time across devices signed into the same account.\n"
            "Connecting Firebase to Flutter needs a project setup step on firebase.google.com plus a config file in the app."
        ),
        practice_task=(
            "Write out, in order, the 3 setup steps you'd need before your Flutter todo "
            "app can save data to Firestore (hint: create project, register app, add config)."
        ),
        starter_code=(
            "setup_steps = [\n"
            "    \"1. Create a project at firebase.google.com\",\n"
            "    \"2. Register your Flutter app inside that project\",\n"
            "    \"3. Add the generated config file to your Flutter project\",\n"
            "]\n"
            "for step in setup_steps:\n"
            "    print(step)\n"
        ),
        quiz_question="What does Firestore provide for a Flutter app?",
        quiz_options=["A UI design system", "A cloud database for storing and syncing data", "A programming language", "A code compiler"],
        quiz_correct_index=1,
        quiz_explanation="Firestore is Firebase's cloud NoSQL database - it's what persists and syncs your app's data.",
    ),
    dict(
        slug="running-your-first-app",
        title="Running Your First App",
        mentor_intro=(
            "With the UI built, state wired up, and Firebase connected, the last step is "
            "actually running the app - on an emulator, a real device, or as a web build - "
            "and seeing your todo list persist data for real. This lesson is where "
            "everything from the course comes together into one working project."
        ),
        analogy=(
            "This is opening night - every rehearsal (lesson) up to now was building "
            "toward this one moment where the whole thing runs live, end to end, in front "
            "of a real audience (you)."
        ),
        notes=(
            "flutter run launches the app on a connected device/emulator or in a browser.\n"
            "Hot reload lets you see UI changes almost instantly without a full restart.\n"
            "flutter build creates a release version ready to publish.\n"
            "This project (Expense Tracker / Todo app) is exactly the kind you'll see in the Project Gallery."
        ),
        practice_task=(
            "Write a short checklist (3-5 items) of everything you'd verify works before "
            "considering your first Flutter app 'done': e.g. add item, check item, data persists after restart."
        ),
        starter_code=(
            "checklist = [\n"
            "    \"Can add a new todo item\",\n"
            "    \"Can check/uncheck an item\",\n"
            "    \"List still shows saved items after restarting the app\",\n"
            "]\n"
            "for item in checklist:\n"
            "    print(\"[ ]\", item)\n"
        ),
        quiz_question="What does 'hot reload' let you do while developing a Flutter app?",
        quiz_options=[
            "Deploy directly to the app store",
            "See UI code changes almost instantly without a full restart",
            "Automatically write your widget tree for you",
            "Connect to Firebase automatically",
        ],
        quiz_correct_index=1,
        quiz_explanation="Hot reload injects updated code into the running app, showing changes in about a second, without losing app state.",
    ),
]


def _seed_course(slug, title, subtitle, icon, order_index, lesson_dicts, video_url=None) -> None:
    """
    Generic version of seed_python_course()'s logic - creates a Course +
    its Lesson rows if missing. video_url (optional) is applied to every
    lesson that doesn't already set its own - same one-line-swap pattern
    as seed_python_course()'s VIDEO_URL.
    """
    course = Course.query.filter_by(slug=slug).first()
    if course is None:
        course = Course(slug=slug, title=title, subtitle=subtitle, icon=icon, order_index=order_index)
        db.session.add(course)
        db.session.flush()  # get course.id before creating lessons

    if course.lessons.count() == 0:
        for order_index_, lesson_data in enumerate(lesson_dicts):
            lesson_data.setdefault("video_url", video_url)
            db.session.add(Lesson(
                course_id=course.id,
                order_index=order_index_,
                xp_reward=20,
                quiz_xp_bonus=10,
                **lesson_data,
            ))
    db.session.commit()


def seed_extra_courses() -> None:
    """Creates the Ethical Hacking, AI/ML, and App Development courses. Idempotent."""
    _seed_course(
        slug="web-penetration-testing", title="Web Penetration Testing",
        subtitle="SQL injection, from how queries work to how to prevent it",
        icon="🛡️", order_index=1, lesson_dicts=WEB_PENTESTING_LESSONS,
        video_url="https://youtu.be/2eLJNBroFrg",  # TODO: swap for your own recordings, same as VIDEO_URL in seed_python_course()
    )
    _seed_course(
        slug="machine-learning-basics", title="Machine Learning Basics",
        subtitle="Core ML concepts - from what a model is to evaluating one",
        icon="🤖", order_index=2, lesson_dicts=MACHINE_LEARNING_LESSONS,
    )
    _seed_course(
        slug="flutter-app-development", title="Flutter App Development",
        subtitle="Build a real todo app - widgets, state, and Firebase",
        icon="📱", order_index=3, lesson_dicts=APP_DEVELOPMENT_LESSONS,
    )


# ---------------------------------------------------------------------------
# Badges
#
# BADGES holds each badge's display fields (seeded into the Badge table).
# BADGE_CONDITIONS holds the matching pass/fail rule for each slug, checked
# against a fresh stats snapshot (see check_and_award_badges) - kept as a
# separate dict instead of DB columns since a condition is code, not data.
# ---------------------------------------------------------------------------

BADGES = [
    dict(slug="first-lesson", title="First Steps", description="Complete your first lesson", icon="🎯", category="lessons", order_index=0),
    dict(slug="five-lessons", title="Quick Learner", description="Complete 5 lessons", icon="📘", category="lessons", order_index=1),
    dict(slug="course-champion", title="Course Champion", description="Finish every lesson in one course", icon="🏆", category="lessons", order_index=2),
    dict(slug="quiz-whiz", title="Quiz Whiz", description="Answer 5 quick-check quizzes correctly", icon="🧠", category="quiz", order_index=3),
    dict(slug="xp-rookie", title="XP Rookie", description="Earn 100 total XP", icon="⭐", category="xp", order_index=4),
    dict(slug="xp-grinder", title="XP Grinder", description="Earn 500 total XP", icon="🌟", category="xp", order_index=5),
    dict(slug="xp-legend", title="XP Legend", description="Earn 1,500 total XP", icon="💫", category="xp", order_index=6),
    dict(slug="level-5", title="Rising Star", description="Reach Level 5", icon="🚀", category="level", order_index=7),
    dict(slug="level-10", title="Elite Learner", description="Reach Level 10", icon="👑", category="level", order_index=8),
    dict(slug="challenge-starter", title="Challenge Accepted", description="Complete your first Daily Challenge", icon="🔥", category="challenge", order_index=9),
    dict(slug="challenge-streak-7", title="Consistency Wins", description="Complete 7 Daily Challenges", icon="📅", category="challenge", order_index=10),
    dict(slug="streak-7", title="On Fire", description="Reach a 7-day learning streak", icon="🔥", category="streak", order_index=11),
    dict(slug="streak-30", title="Unstoppable", description="Reach a 30-day learning streak", icon="⚡", category="streak", order_index=12),
]

BADGE_CONDITIONS = {
    "first-lesson": lambda s: s["lessons_completed"] >= 1,
    "five-lessons": lambda s: s["lessons_completed"] >= 5,
    "course-champion": lambda s: s["courses_completed"] >= 1,
    "quiz-whiz": lambda s: s["quizzes_correct"] >= 5,
    "xp-rookie": lambda s: s["total_xp"] >= 100,
    "xp-grinder": lambda s: s["total_xp"] >= 500,
    "xp-legend": lambda s: s["total_xp"] >= 1500,
    "level-5": lambda s: s["level"] >= 5,
    "level-10": lambda s: s["level"] >= 10,
    "challenge-starter": lambda s: s["challenges_completed"] >= 1,
    "challenge-streak-7": lambda s: s["challenges_completed"] >= 7,
    "streak-7": lambda s: s["streak_days"] >= 7,
    "streak-30": lambda s: s["streak_days"] >= 30,
}


def seed_badges() -> None:
    """Inserts every row in BADGES that doesn't exist yet. Idempotent."""
    existing_slugs = {b.slug for b in Badge.query.all()}
    for badge_data in BADGES:
        if badge_data["slug"] not in existing_slugs:
            db.session.add(Badge(**badge_data))
    db.session.commit()


def _completed_course_count(user) -> int:
    """How many courses this user has 100% completed (every lesson done)."""
    completed_lesson_ids = {
        p.lesson_id for p in
        UserLessonProgress.query.filter_by(user_id=user.id, status="complete").all()
    }
    count = 0
    for course in Course.query.all():
        lessons = list(course.lessons)
        if lessons and all(l.id in completed_lesson_ids for l in lessons):
            count += 1
    return count


def _badge_stats(user) -> dict:
    return {
        "lessons_completed": UserLessonProgress.query.filter_by(user_id=user.id, status="complete").count(),
        "quizzes_correct": UserLessonProgress.query.filter_by(user_id=user.id, quiz_correct=True).count(),
        "courses_completed": _completed_course_count(user),
        "total_xp": user.total_xp,
        "level": user.level,
        "challenges_completed": UserDailyChallengeProgress.query.filter_by(user_id=user.id, status="complete").count(),
        "streak_days": user.streak_days,
    }


def check_and_award_badges(user) -> list[Badge]:
    """
    Call this right after anything that could move the needle on a badge -
    a lesson completing, a quiz answered correctly, a daily challenge
    finishing, XP being awarded. Cheap to call often: it's a handful of
    COUNT queries plus a dict of in-Python comparisons, and it only writes
    to the DB when a badge is actually newly earned.

    Returns the list of Badge rows newly earned this call (empty most of
    the time) - callers use this to show a "Badge unlocked!" toast.
    """
    stats = _badge_stats(user)
    earned_badge_ids = {ub.badge_id for ub in UserBadge.query.filter_by(user_id=user.id).all()}

    newly_earned = []
    for badge in Badge.query.all():
        if badge.id in earned_badge_ids:
            continue
        condition = BADGE_CONDITIONS.get(badge.slug)
        if condition and condition(stats):
            db.session.add(UserBadge(user_id=user.id, badge_id=badge.id))
            newly_earned.append(badge)

    if newly_earned:
        db.session.commit()
    return newly_earned


def badges_view(user) -> list[dict]:
    """Every badge, earned or not, in display order - for the /badges page."""
    earned_by_badge_id = {ub.badge_id: ub for ub in UserBadge.query.filter_by(user_id=user.id).all()}
    return [{
        "badge": badge,
        "earned": badge.id in earned_by_badge_id,
        "earned_at": earned_by_badge_id[badge.id].earned_at if badge.id in earned_by_badge_id else None,
    } for badge in Badge.query.order_by(Badge.order_index).all()]


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

def bookmarks_view(user) -> list[dict]:
    """
    Every bookmark this user has, resolved into something a template can
    render directly (title, subtitle, href, icon) - newest first. Skips
    silently over any bookmark whose target got deleted since it was saved.
    """
    rows = Bookmark.query.filter_by(user_id=user.id).order_by(Bookmark.created_at.desc()).all()
    view = []
    for row in rows:
        if row.target_type == "lesson":
            lesson = Lesson.query.get(row.target_id)
            if lesson is None:
                continue
            course = Course.query.get(lesson.course_id)
            view.append({
                "bookmark_id": row.id,
                "target_type": "lesson",
                "target_id": lesson.id,
                "icon": course.icon if course else "📘",
                "title": lesson.title,
                "subtitle": course.title if course else "Lesson",
                "href": f"/course/{course.slug}/{lesson.slug}" if course else "#",
                "created_at": row.created_at,
            })
        elif row.target_type == "project":
            project = Project.query.get(row.target_id)
            if project is None:
                continue
            view.append({
                "bookmark_id": row.id,
                "target_type": "project",
                "target_id": project.id,
                "icon": project.icon,
                "title": project.title,
                "subtitle": project.tech_tag,
                "href": project.href,
                "created_at": row.created_at,
            })
    return view


# ---------------------------------------------------------------------------
# Code Battle Arena - starter Question Bank
# ---------------------------------------------------------------------------
#
# Without this, every mode's list page shows "No questions yet" and every
# Speed/AI Battle difficulty shows 0 in the picker - the grading pipeline
# has always worked (see battle.py), there was just never any data to
# grade against. This seeds a handful of questions per difficulty for
# every mode that's actually implemented so far; admins can add more (or
# retire these) from the Question Bank in the admin panel same as any
# other question - nothing here is special-cased.

# (difficulty, title, prompt, predict_code, expected_output)
OUTPUT_PREDICTION_QUESTIONS = [
    (
        "easy", "Order of Operations",
        "Read the code below. What does it print?",
        "print(2 + 3 * 4)",
        "14",
    ),
    (
        "medium", "String Multiplication",
        "Read the code below. What does it print?",
        "word = 'ab'\nprint(word * 3)",
        "ababab",
    ),
    (
        "hard", "Mutable Default Argument",
        "Read the code below. What does it print? (Careful - this is a classic Python gotcha.)",
        "def add_item(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n\nadd_item('a')\nprint(add_item('b'))",
        "['a', 'b']",
    ),
]

# (mode, difficulty, title, prompt, starter_code, hidden_tests)
# Shared across code_completion / speed_battle / ai_battle - all three
# grade the same way (battle.py's _run_hidden_tests), so a "coding task"
# here is just assigned to a mode + given a starter_code scaffold that
# doesn't already pass, forcing an actual submission either way.
CODE_EXEC_QUESTIONS = [
    (
        "code_completion", "easy", "Double It",
        "Read an integer and print double its value.",
        "n = int(input())\n# TODO: print n doubled",
        [{"input": "3", "expected": "6"}, {"input": "10", "expected": "20"}],
    ),
    (
        "code_completion", "medium", "Is Palindrome",
        "Read a word and print True if it reads the same backwards, else False.",
        "word = input()\n# TODO: print True or False",
        [{"input": "level", "expected": "True"}, {"input": "python", "expected": "False"}],
    ),
    (
        "code_completion", "hard", "Nth Fibonacci",
        "Read an integer n (0-indexed) and print the nth Fibonacci number (0, 1, 1, 2, 3, 5, ...).",
        "n = int(input())\n# TODO: print the nth Fibonacci number",
        [{"input": "0", "expected": "0"}, {"input": "1", "expected": "1"}, {"input": "7", "expected": "13"}],
    ),
    (
        "speed_battle", "easy", "Square It",
        "Read an integer and print its square.",
        "n = int(input())\n# your code here",
        [{"input": "4", "expected": "16"}, {"input": "9", "expected": "81"}],
    ),
    (
        "speed_battle", "medium", "Sum of Digits",
        "Read an integer and print the sum of its digits.",
        "n = input()\n# your code here",
        [{"input": "1234", "expected": "10"}, {"input": "9", "expected": "9"}],
    ),
    (
        "speed_battle", "hard", "Is Prime",
        "Read an integer and print True if it's prime, else False.",
        "n = int(input())\n# your code here",
        [{"input": "7", "expected": "True"}, {"input": "8", "expected": "False"}, {"input": "1", "expected": "False"}],
    ),
    (
        "ai_battle", "easy", "Reverse a Word",
        "Read a word and print it reversed.",
        "word = input()\n# your code here",
        [{"input": "python", "expected": "nohtyp"}],
    ),
    (
        "ai_battle", "medium", "Count Vowels",
        "Read a word and print how many vowels (a, e, i, o, u) it contains.",
        "word = input()\n# your code here",
        [{"input": "hello", "expected": "2"}, {"input": "sky", "expected": "0"}],
    ),
    (
        "ai_battle", "hard", "FizzBuzz Sum",
        "Read an integer n. Sum every number from 1 to n, replacing multiples of 3 with 0 and multiples of 5 with 0 in the sum (i.e. skip them), and print the total.",
        "n = int(input())\n# your code here",
        [{"input": "10", "expected": "22"}],  # 1+2+4+7+8 = 22 (skips 3,5,6,9,10)
    ),
    (
        "tournament", "easy", "Max of Two",
        "Read two integers on one line, separated by a space, and print the larger one.",
        "a, b = map(int, input().split())\n# your code here",
        [{"input": "3 7", "expected": "7"}, {"input": "9 2", "expected": "9"}],
    ),
    (
        "tournament", "medium", "Count Words",
        "Read a line of text and print how many words it contains (split on whitespace).",
        "line = input()\n# your code here",
        [{"input": "the quick brown fox", "expected": "4"}, {"input": "hello", "expected": "1"}],
    ),
    (
        "tournament", "hard", "Anagram Check",
        "Read two words on one line, separated by a space, and print True if they're anagrams of each other, else False.",
        "a, b = input().split()\n# your code here",
        [{"input": "listen silent", "expected": "True"}, {"input": "hello world", "expected": "False"}],
    ),
    (
        "blind_coding", "easy", "Sum of Digits",
        "Read an integer and print the sum of its digits (treat a leading minus sign as ignorable - use its absolute value).",
        "n = input()\n# your code here",
        [{"input": "1234", "expected": "10"}, {"input": "-27", "expected": "9"}],
    ),
    (
        "blind_coding", "medium", "Reverse Words",
        "Read a line of text and print the words in reverse order, still space-separated.",
        "line = input()\n# your code here",
        [{"input": "one two three", "expected": "three two one"}, {"input": "hello world", "expected": "world hello"}],
    ),
    (
        "blind_coding", "hard", "Balanced Brackets",
        "Read a string of only ( ) [ ] { } characters and print True if the brackets are balanced and properly nested, else False.",
        "s = input()\n# your code here",
        [{"input": "([]{})", "expected": "True"}, {"input": "([)]", "expected": "False"}],
    ),
]


def seed_battle_questions() -> None:
    """Creates the Code Battle Arena's starter question bank if missing,
    one row per (mode, title) - idempotent, same pattern as
    seed_daily_challenges(). created_by_id is left null (system-seeded,
    not authored by any particular admin)."""
    for difficulty, title, prompt, predict_code, expected_output in OUTPUT_PREDICTION_QUESTIONS:
        if BattleQuestion.query.filter_by(mode="output_prediction", title=title).first() is None:
            db.session.add(BattleQuestion(
                mode="output_prediction", difficulty=difficulty, title=title, prompt=prompt,
                predict_code=predict_code, expected_output=expected_output,
            ))

    for mode, difficulty, title, prompt, starter_code, hidden_tests in CODE_EXEC_QUESTIONS:
        if BattleQuestion.query.filter_by(mode=mode, title=title).first() is None:
            db.session.add(BattleQuestion(
                mode=mode, difficulty=difficulty, title=title, prompt=prompt,
                starter_code=starter_code, hidden_tests=json.dumps(hidden_tests),
            ))

    db.session.commit()

# Default coin amounts - matches the master spec's economy table exactly
# (Battle Win +100, Participation +5, Draw +40, Champion +1000,
# Runner-up +500, Third +250). Only used the very first time the app
# starts against a fresh DB; after that, whatever an admin has set on
# the Admin > Rewards page wins, since seed_reward_settings() below
# never touches a row that already exists.
DEFAULT_REWARD_SETTINGS = [
    ("battle_win", "Battle Win", 100, "Awarded to the winner of any Speed Battle, AI Battle, or tournament-round match."),
    ("participation", "Participation", 5, "Awarded to the loser of a match, or a partial-credit submission, just for playing."),
    ("draw", "Draw", 40, "Awarded to both players when a match ends in a draw (Speed Battle only - tournament matches can't draw)."),
    ("tournament_champion", "Tournament Champion", 1000, "One-time bonus paid when a tournament bracket completes."),
    ("tournament_runner_up", "Tournament Runner-up", 500, "One-time bonus paid to the tournament's runner-up."),
    ("tournament_third", "Tournament Third Place", 250, "One-time bonus paid to the tournament's third-place finisher."),
]


def seed_reward_settings() -> None:
    """Creates the default RewardSetting rows if they don't exist yet -
    idempotent, and (unlike seed_battle_questions) intentionally never
    updates a row that's already there, since an admin may have already
    retuned it from the default and a re-seed on every app start
    shouldn't stomp that."""
    for key, label, amount, description in DEFAULT_REWARD_SETTINGS:
        if RewardSetting.query.get(key) is None:
            db.session.add(RewardSetting(key=key, label=label, amount=amount, description=description))
    db.session.commit()
