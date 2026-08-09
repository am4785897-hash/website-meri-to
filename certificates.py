"""
Certificate issuance logic - auto-issues a Certificate row the moment a
learner hits 100% completion on a LearningPath's roadmap or a Course's
lessons. Kept in its own module (same pattern as seed_data.py /
ai_provider.py) so webserver.py stays focused on routing.
"""
import secrets
from datetime import datetime

from extensions import db
from models import Certificate, LearningPath, Course
from seed_data import path_completion_percent, course_completion_percent
from ai_provider import generate_achievement_summary


def _abbreviate(slug: str) -> str:
    """'ethical-hacking' -> 'EH', 'python-fundamentals' -> 'PF', 'ai-ml' -> 'AM'."""
    parts = [p for p in slug.replace("_", "-").split("-") if p]
    letters = "".join(p[0] for p in parts[:3]).upper()
    return letters or "TV"


def _generate_certificate_uid(slug: str) -> str:
    """
    e.g. 'TVA-EH-2026-9F3K21'. Loops on the (astronomically unlikely)
    chance of a collision rather than trusting randomness alone, since
    certificate_uid is a real unique DB constraint.
    """
    prefix = f"TVA-{_abbreviate(slug)}-{datetime.utcnow().year}"
    while True:
        candidate = f"{prefix}-{secrets.token_hex(3).upper()}"
        if Certificate.query.filter_by(certificate_uid=candidate).first() is None:
            return candidate


def _issue_if_missing(user, source_type: str, source_slug: str, title: str) -> None:
    already = Certificate.query.filter_by(
        user_id=user.id, source_type=source_type, source_slug=source_slug,
    ).first()
    if already is not None:
        return

    summary = generate_achievement_summary(user.username, title)
    db.session.add(Certificate(
        user_id=user.id,
        certificate_uid=_generate_certificate_uid(source_slug),
        source_type=source_type,
        source_slug=source_slug,
        title=title,
        ai_summary=summary,
    ))
    db.session.commit()


def ensure_certificates_for_user(user) -> None:
    """
    Call this whenever the learner might have just crossed 100% on
    something (the /certificates page calls it on every visit, cheaply -
    it's a no-op for anything already issued). Checks all 3 learning
    paths and all courses; issues any newly-earned certificates.
    """
    for path in LearningPath.query.order_by(LearningPath.order_index).all():
        if path_completion_percent(user, path) >= 100:
            _issue_if_missing(user, "path", path.slug, f"{path.title} - {path.subtitle}")

    for course in Course.query.order_by(Course.order_index).all():
        if course_completion_percent(user, course) >= 100:
            _issue_if_missing(user, "course", course.slug, course.title)


def certificates_in_progress(user) -> list[dict]:
    """
    Everything the learner HASN'T earned yet, with their current %, so
    the Certificates page can show a locked/in-progress state instead of
    just the earned ones (matches the reference UI's 'All Certificates'
    tab). Only includes items below 100% (100%+ already has a real
    Certificate row from ensure_certificates_for_user above).
    """
    earned_keys = {
        (c.source_type, c.source_slug)
        for c in Certificate.query.filter_by(user_id=user.id).all()
    }
    in_progress = []

    for path in LearningPath.query.order_by(LearningPath.order_index).all():
        if ("path", path.slug) in earned_keys:
            continue
        pct = path_completion_percent(user, path)
        in_progress.append({
            "title": f"{path.title} - {path.subtitle}", "icon": path.icon, "percent": pct,
        })

    for course in Course.query.order_by(Course.order_index).all():
        if ("course", course.slug) in earned_keys:
            continue
        pct = course_completion_percent(user, course)
        in_progress.append({
            "title": course.title, "icon": course.icon, "percent": pct,
        })

    return in_progress
