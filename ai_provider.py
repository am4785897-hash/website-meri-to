"""
AI Tutor provider - the ONLY file that should ever talk to an external AI
API. Everything else (webserver.py, the frontend) calls generate_tutor_reply()
below and never knows or cares whether that's backed by a real model or
the built-in mock - so plugging in ChatGPT/Claude/Hugging Face/any
provider later means editing THIS file only.

--- CURRENT STATE ---
generate_tutor_reply() checks Config.HUGGINGFACE_API_KEY first (uses
_call_huggingface()), then Config.AI_PROVIDER_API_KEY (uses _call_openai())
if that's set instead, and falls back to _call_mock() - a small
rule-based tutor - whenever neither key is set, or if the real call
fails for any reason (bad key, network issue, rate limit, package not
installed, model still loading). That fallback is what keeps the AI
Tutor page from ever crashing, key or no key.

--- TO SWITCH TO A DIFFERENT PROVIDER LATER ---
Write a _call_<provider>(messages, mode) function below with the same
signature as the existing ones - takes the running chat history as a
list of {"role": "user"|"assistant", "content": str} dicts plus the
active mode, returns a plain string reply - then wire it into
generate_tutor_reply() at the bottom. Nothing in webserver.py, models.py,
or ai-tutor.html needs to change.
"""
import random
import re

from config import Config

# Human-readable label for each quick-action mode - used to steer the
# mock tutor's tone, and to build the system prompt for a real provider.
MODE_LABELS = {
    "doubt": "Doubt Solving",
    "code_review": "Code Review",
    "bug": "Bug Explanation",
    "quiz": "Quiz Generation",
    "project": "Project Guidance",
    "interview": "Interview Preparation",
    "career": "Career Guidance",
    "image_gen": "Image Generation",
    "achievement_summary": "Achievement Summary",
    "general": "General Chat",
}

# System prompt per mode - only used by a real provider call (the mock
# below doesn't need one). Kept short and on-topic to Coder Enchanté Academy's
# three learning paths.
_MODE_SYSTEM_PROMPTS = {
    "doubt": "You are a patient coding tutor for Coder Enchanté Academy. Explain concepts simply, with a short real-life analogy where it helps.",
    "code_review": "You are a senior engineer reviewing a learner's code. Point out readability, edge cases, and style issues constructively and concisely.",
    "bug": "You are debugging alongside a learner. Ask for the exact error if missing, then reason step by step to the likely cause.",
    "quiz": "You generate one multiple-choice quiz question at a time on the learner's topic, then explain the answer once they respond.",
    "project": "You give practical, scoped project guidance - break big project ideas into small, buildable steps.",
    "interview": "You are a technical interviewer. Ask one realistic interview question at a time and give constructive feedback on answers.",
    "career": "You give grounded, specific career advice for someone learning Ethical Hacking, AI/ML, or App Development.",
    "achievement_summary": "You write short, warm, specific certificate achievement summaries (3-4 sentences) for a learner who just completed a course or path, ending with a couple of key-strength bullet points.",
    "general": "You are a helpful, encouraging tutor for Coder Enchanté Academy, covering Ethical Hacking, AI/ML, and App Development.",
}

_CODE_HINTS = ("def ", "class ", "import ", "{", "}", "print(", "function ", "const ", "let ", "=>", "```")


def _looks_like_code(text: str) -> bool:
    lowered = text.lower()
    return any(hint in text or hint in lowered for hint in _CODE_HINTS)


# Model served via Hugging Face's Inference Providers. Swap for any other
# chat-instruct model id available there if you'd prefer a different one.
# Model served via Hugging Face Inference Providers. Deliberately an
# OPEN model - some HF models (Llama, Gemma, etc.) are "gated" and
# require manually requesting access on the model's HF page first;
# using one of those here would silently fail every call and fall back
# to the mock, with nothing telling you why. Swap this only for another
# model you've confirmed you can access.
_HUGGINGFACE_MODEL = "Qwen/Qwen2.5-7B-Instruct"


def _call_huggingface(messages: list[dict], mode: str) -> str:
    """
    Real provider call - Hugging Face Inference Providers, via the
    official huggingface_hub client's OpenAI-style chat_completion().
    Only reached when Config.HUGGINGFACE_API_KEY is set (see
    generate_tutor_reply() below). Raises on any failure (missing
    package, bad token, network error, model cold-starting, etc.) so the
    caller's try/except can fall back to the mock tutor instead of ever
    showing a broken page.
    """
    from huggingface_hub import InferenceClient  # imported lazily so the app still boots fine without the package installed

    client = InferenceClient(api_key=Config.HUGGINGFACE_API_KEY)

    system_prompt = _MODE_SYSTEM_PROMPTS.get(mode, _MODE_SYSTEM_PROMPTS["general"])
    api_messages = [{"role": "system", "content": system_prompt}] + [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]

    response = client.chat_completion(
        model=_HUGGINGFACE_MODEL,
        messages=api_messages,
        max_tokens=600,
        temperature=0.6,
    )
    return response.choices[0].message.content.strip()


def _call_openai(messages: list[dict], mode: str) -> str:
    """
    Real provider call - OpenAI's Chat Completions API. Only reached when
    Config.AI_PROVIDER_API_KEY is set (see generate_tutor_reply() below).
    Raises on any failure (missing package, bad key, network error, etc.)
    so the caller's try/except can fall back to the mock tutor instead of
    ever showing a broken page.
    """
    from openai import OpenAI  # imported lazily so the app still boots fine without the package installed

    client = OpenAI(api_key=Config.AI_PROVIDER_API_KEY)

    system_prompt = _MODE_SYSTEM_PROMPTS.get(mode, _MODE_SYSTEM_PROMPTS["general"])
    api_messages = [{"role": "system", "content": system_prompt}] + [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]

    response = client.chat.completions.create(
        model="gpt-4o-mini",  # swap for any chat-completions-compatible model
        messages=api_messages,
        max_tokens=600,
        temperature=0.6,
    )
    return response.choices[0].message.content.strip()


def _call_mock(messages: list[dict], mode: str) -> str:
    """
    Rule-based stand-in tutor - deterministic-ish, mode-aware, and reacts
    a little to what was actually typed, so the AI Tutor UI has real
    content to show without any external API. Replace PROVIDER below with
    a real model call whenever you're ready; nothing else changes.
    """
    last_user_msg = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    ).strip()
    topic = last_user_msg.rstrip("?.! ") or "that"
    if len(topic) > 80:
        topic = topic[:77] + "..."

    if mode == "code_review":
        if _looks_like_code(last_user_msg):
            return (
                f"Reviewing what you pasted:\n\n"
                f"• **Readability** - naming and structure look reasonable at a glance; "
                f"consider adding a short docstring so future-you remembers the intent.\n"
                f"• **Edge cases** - check what happens with empty input, `None`, or unexpected types.\n"
                f"• **Style** - keep functions focused on one job; if this one does more than "
                f"one thing, it's a good split candidate.\n\n"
                f"Paste a specific function and I'll go line-by-line once a real model is wired in here."
            )
        return (
            "Paste the code you'd like reviewed (or drop it into the Code Playground and "
            "share it here) and I'll walk through readability, edge cases, and style."
        )

    if mode == "bug":
        if "traceback" in last_user_msg.lower() or "error" in last_user_msg.lower():
            return (
                f"Let's debug it. Based on \"{topic}\":\n\n"
                f"1. Read the **last line** of the traceback first - that's the actual error type.\n"
                f"2. Find the **line number** it points to in your file - that's where it broke, "
                f"not necessarily where the bug is.\n"
                f"3. Add a `print()` right before that line to check the values going in.\n\n"
                f"Share the exact error text and the code around it for a precise fix."
            )
        return (
            "Tell me the exact error message (or paste the traceback) plus the few lines of "
            "code around where it happens, and I'll help track it down."
        )

    if mode == "quiz":
        return (
            f"Quick check on **{topic}**:\n\n"
            f"**Q:** Which of these best describes {topic}?\n"
            f"A) A syntax rule\n"
            f"B) A core concept you'll use repeatedly in this path\n"
            f"C) A deprecated feature\n"
            f"D) Unrelated to this course\n\n"
            f"Reply with A, B, C, or D and I'll explain why."
        )

    if mode == "project":
        return (
            f"For a project around **{topic}**, a solid path is:\n\n"
            f"1. **Scope it small** - one core feature working end-to-end beats five half-built ones.\n"
            f"2. **Sketch the data** - what do you read/store/output? Write it down before coding.\n"
            f"3. **Build the ugly version first** - hardcode inputs, get the logic working, style later.\n"
            f"4. **Add one polish pass** - error handling, then UI, then docs.\n\n"
            f"Want me to break this into a day-by-day plan?"
        )

    if mode == "interview":
        return (
            f"Interview-style question on **{topic}**:\n\n"
            f"\"Walk me through how you'd approach {topic} in a real project, and what could "
            f"go wrong?\"\n\n"
            f"Try answering out loud in under 90 seconds - that's the real constraint you'll "
            f"face live. I'll give feedback on structure and gaps once you reply."
        )

    if mode == "career":
        return (
            "A few things that consistently move the needle: ship small public projects "
            "(even simple ones) so there's something to point to, write a short README for "
            "each one explaining *why* not just *what*, and pick one path (Ethical Hacking, "
            "AI/ML, or App Development) to go deep on before spreading thin across all three. "
            "What's your current focus?"
        )

    if mode == "achievement_summary":
        # Pull the quoted title back out of the prompt built by
        # generate_achievement_summary() below, rather than using the
        # generic `topic` (which would show the whole prompt sentence).
        quoted = re.search(r'"([^"]+)"', last_user_msg)
        title = quoted.group(1) if quoted else topic
        strengths = random.sample(
            ["Consistent practice", "Strong problem-solving", "Clean, readable code",
             "Fast concept pickup", "Solid project execution"],
            k=3,
        )
        return (
            f"Demonstrated excellent dedication and understanding throughout {title}. "
            f"Completed all lessons, quizzes, and hands-on exercises with strong, "
            f"consistent effort from start to finish.\n\n"
            f"Key strengths: {strengths[0]}, {strengths[1]}, {strengths[2]}.\n\n"
            f"Keep learning and keep building!"
        )

    if mode == "image_gen":
        return (
            "I can generate that as an image too - use the image generation "
            "action instead of the text chat for this one, and I'll render "
            "it rather than describe it."
        )

    # mode == "doubt" or "general" / fallback
    return (
        f"Good question about **{topic}**. Here's the short version: break it into "
        f"the smallest piece that confuses you and we'll work outward from there - "
        f"concepts usually click once you see one concrete example. Want a real-life "
        f"analogy, or a code example?"
    )


PROVIDER = _call_mock


class ImageGenerationUnavailable(Exception):
    """Raised when image generation is requested but no provider is
    configured - the caller shows this as a plain message instead of a
    broken image."""


# Text-to-image model served via Hugging Face Inference Providers. Swap
# for any other text-to-image model id available there if you'd prefer.
_HUGGINGFACE_IMAGE_MODEL = "black-forest-labs/FLUX.1-schnell"


def generate_tutor_image(prompt: str):
    """
    Generates an image from a text prompt and returns a PIL.Image.
    Only Hugging Face is wired up for image generation right now (OpenAI
    would need a separate _call_openai_image() following the same shape
    if you want that path too - same one-function-to-add pattern as
    everywhere else in this file).

    Raises ImageGenerationUnavailable if no image-capable key is set, or
    the underlying call fails - the caller (webserver.py) turns that into
    a plain chat message rather than a broken <img> tag.
    """
    if not Config.HUGGINGFACE_API_KEY:
        raise ImageGenerationUnavailable(
            "Image generation needs a Hugging Face API key. Set the "
            "HUGGINGFACE_API_KEY environment variable to enable it."
        )

    from huggingface_hub import InferenceClient  # imported lazily so the app still boots fine without the package installed

    client = InferenceClient(api_key=Config.HUGGINGFACE_API_KEY)
    try:
        return client.text_to_image(prompt, model=_HUGGINGFACE_IMAGE_MODEL)
    except Exception as e:
        raise ImageGenerationUnavailable(f"Image generation failed: {e}") from e


def generate_achievement_summary(learner_name: str, title: str) -> str:
    """
    Public entry point for the Certificates module. Goes through the exact
    same provider chain as the AI Tutor (mock -> Hugging Face -> OpenAI,
    see generate_tutor_reply() below) so wiring up a real provider for
    chat also upgrades certificate summaries for free - no separate key
    or code path needed.
    """
    prompt = (
        f"Write a short achievement summary for {learner_name}, who just "
        f"completed \"{title}\" on Coder Enchanté Academy."
    )
    return generate_tutor_reply([{"role": "user", "content": prompt}], mode="achievement_summary")


def generate_tutor_reply(messages: list[dict], mode: str = "general") -> str:
    """
    The one function the rest of the app calls. Always returns a plain
    string reply to the learner - the AI Tutor page never crashes or
    shows an error, key or no key.

    Order: Hugging Face (if HUGGINGFACE_API_KEY is set) -> OpenAI (if
    AI_PROVIDER_API_KEY is set) -> built-in mock. On ANY failure at a
    real-provider step (bad key, network issue, rate limit, package
    missing, gated-model access, model loading), the exception is
    printed to the terminal (so you can actually see WHY it fell back)
    and then falls through to the next option.
    """
    mode = mode if mode in MODE_LABELS else "general"

    if Config.HUGGINGFACE_API_KEY:
        try:
            return _call_huggingface(messages, mode)
        except Exception as e:
            print(f"[ai_provider] Hugging Face call failed, falling back: {e}")

    if Config.AI_PROVIDER_API_KEY:
        try:
            return _call_openai(messages, mode)
        except Exception as e:
            print(f"[ai_provider] OpenAI call failed, falling back: {e}")

    return PROVIDER(messages, mode)
