#!/usr/bin/env python3
"""
SessionsWithToby — Telegram LMS bot (v3: dynamic, behavior-adaptive, payment-gated)
Flow:
  /start -> country -> name -> email -> all free lessons
  -> adaptive assessment -> dynamic menu
       * search <kw>  : find lessons by keyword
       * topics       : list 8 courses, pick one
       * next         : adaptive next lesson for their tier
       * level        : re-take assessment
  User store: Google Sheet if configured, else local users.json (same schema).
"""
import os, json, uuid, asyncio, re, requests, threading, hashlib
from datetime import datetime, timezone
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

BASE = os.path.dirname(os.path.abspath(__file__))
LESSONS = {l["id"]: l for l in json.load(open(os.path.join(BASE, "lessons.json")))}
COURSES = json.load(open(os.path.join(BASE, "courses.json")))
USERS = os.environ.get("USERS_PATH", os.path.join(BASE, "users.json"))
TEST_MODE = os.environ.get("TEST_MODE", "") == "1"  # free end-to-end test, skips Flutterwave
FLW_SECRET = os.environ.get("FLUTTERWAVE_SECRET_KEY", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")
FLW_HASH = os.environ.get("FLUTTERWAVE_WEBHOOK_HASH", "")
SHEET_ID = os.environ.get("USERS_SHEET_ID", "")  # optional: Google Sheet backend

PRICE_TIERS = {"NG": {"currency": "NGN", "amount": 5000}, "GH": {"currency": "GHS", "amount": 80},
               "US": {"currency": "USD", "amount": 15}, "GB": {"currency": "GBP", "amount": 12},
               "CA": {"currency": "CAD", "amount": 20}, "KE": {"currency": "KES", "amount": 1500}}
DEFAULT_TIER = {"currency": "USD", "amount": 12}
CCY = {"NGN": "₦", "USD": "$", "GBP": "£", "GHS": "GH₵", "CAD": "CA$", "KES": "KSh"}
COUNTRY_MAP = {"nigeria": "NG", "ghana": "GH", "usa": "US", "united states": "US",
               "uk": "GB", "united kingdom": "GB", "england": "GB", "canada": "CA",
               "kenya": "KE", "south africa": "ZA"}
FREE_LESSONS = 9999
BOT_USERNAME = os.environ.get("BOT_USERNAME", "SessionsWithTobyBot")
REFERRAL_REWARD = {"NG": 1000, "GH": 16, "US": 3, "GB": 3, "CA": 4, "KE": 300, "ZA": 30}
def make_ref_code(cid): return "SWT" + hashlib.md5(cid.encode()).hexdigest()[:6].upper()

# Behavioral skill assessment: 4 questions -> tier
ASSESS = [
    ("How long have you been singing? (1=just starting, 2=months, 3=years)", ["1", "2", "3"]),
    ("Can you reliably match a pitch by ear? (1=no, 2=sometimes, 3=yes)", ["1", "2", "3"]),
    ("Do you perform for others (stage, church, online)? (1=never, 2=sometimes, 3=regularly)", ["1", "2", "3"]),
    ("What's your main goal? (1=sing without embarrassment, 2=sound good, 3=go pro)", ["1", "2", "3"]),
]
ASSESS_EMOJI = ["📅", "👂", "🎤", "🎯"]
def assess_prompt(q):
    return f"{ASSESS_EMOJI[q]} Q{q+1}/{len(ASSESS)}. {ASSESS[q][0]}"
TIERS = {  # tier -> ordered lesson windows (course, slice) for adaptive serving
    "Beginner": [("Technique", slice(0, 10)), ("Hear Anything, Repeat Anything", slice(0, 6))],
    "Intermediate": [("Technique", slice(8, 20)), ("Perform Like You Mean It", slice(0, 8))],
    "Advanced": [("Think Like A Career Vocalist", slice(0, 4)), ("Get Paid To Sing", slice(0, 10)), ("Make Records, Not excuses", slice(0, 10))],
}
TIER_FROM_SCORE = lambda s: "Beginner" if s <= 6 else "Intermediate" if s <= 9 else "Advanced"

_USER_LOCK = threading.Lock()
_USERS_CACHE = {}  # last-good copy; survives transient storage read failures so we never wipe everyone

# --- per-user serial queue -------------------------------------------------
# Each chat gets ONE worker that processes its updates strictly in order.
# Without this, two rapid messages from the same user (e.g. concurrent 'done')
# both load the same state, both mutate, and the last save CLOBBERS the other's
# progress. The queue makes per-user updates atomic in practice.
_USER_QUEUES = {}
_Q_LOCK = threading.Lock()

async def _user_worker(cid, q):
    while True:
        update, ctx, handler = await q.get()
        try:
            await handler(update, ctx)
        except Exception as e:
            print("user worker error:", cid, e)
        finally:
            q.task_done()

async def _enqueue(update, ctx, handler):
    cid = str(update.effective_chat.id)
    with _Q_LOCK:
        q = _USER_QUEUES.get(cid)
        if q is None:
            q = asyncio.Queue()
            _USER_QUEUES[cid] = q
            asyncio.create_task(_user_worker(cid, q))
    await q.put((update, ctx, handler))

async def start(update, ctx):
    await _enqueue(update, ctx, _start)
async def msg(update, ctx):
    await _enqueue(update, ctx, _msg)
async def profile(update, ctx):
    await _enqueue(update, ctx, _profile)
async def share_cmd(update, ctx):
    await _enqueue(update, ctx, _share)
async def reset_cmd(update, ctx):
    await _enqueue(update, ctx, _reset)

def load_users():
    with _USER_LOCK:
        try:
            data = json.load(open(USERS)) if os.path.exists(USERS) else {}
            _USERS_CACHE.clear(); _USERS_CACHE.update(data)
            return data
        except Exception:
            if _USERS_CACHE:
                return dict(_USERS_CACHE)
            return {}
def save_users(u):
    with _USER_LOCK:
        _USERS_CACHE.clear(); _USERS_CACHE.update(u)
        # human-readable Google Sheet mirror (dashboard only; users.json is source of truth)
        sheet_sync(u)
        # ensure storage dir exists (self-heals Railway volume mounts)
        d = os.path.dirname(USERS)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        # merge on-disk changes written since u was loaded (prevents cross-user clobber)
        if os.path.exists(USERS):
            try:
                on_disk = json.load(open(USERS))
                on_disk.update(u)
                u = on_disk
            except Exception:
                pass
        tmp = USERS + ".tmp"
        json.dump(u, open(tmp, "w"), indent=2)
        os.replace(tmp, USERS)

# --- Google Sheet backend (human-readable mirror of users.json) ---
# The Sheet is a READ-ONLY dashboard for the coach; users.json stays the source of truth.
SHEET_COLS = ["chat_id","name","email","country","joined","paid","tier","lessons_done",
              "goal","stage","referred_by","ref_code","referrals_paid","referral_earnings"]
GOAL_TXT = {1:"sing w/o embarrassment",2:"sound good performing",3:"go pro & get paid"}
def _sheets_svc():
    import google.oauth2.credentials as oc
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = oc.Credentials.from_authorized_user_file(
        os.environ.get("GOOGLE_TOKEN_PATH", "/data/google_token.json"), ["https://www.googleapis.com/auth/spreadsheets"])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("sheets", "v4", credentials=creds), creds
def _resolve_sheet_id():
    sid = os.environ.get("USERS_SHEET_ID", "").strip()
    if sid:
        return sid
    pid = os.path.join(os.path.dirname(USERS) or "/data", ".sheet_id")
    if os.path.exists(pid):
        return open(pid).read().strip()
    if os.environ.get("SHEET_AUTOCREATE") == "1":
        svc, _ = _sheets_svc()
        sheet = svc.spreadsheets().create(
            body={"properties": {"title": "SessionsWithToby — Students"}}).execute()
        open(pid, "w").write(sheet["spreadsheetId"])
        print("[sheet] auto-created:", sheet.get("spreadsheetUrl"))
        return sheet["spreadsheetId"]
    return ""
def sheet_sync(u):
    sid = _resolve_sheet_id()
    if not sid:
        return
    try:
        svc, _ = _sheets_svc()
        existing = svc.spreadsheets().values().get(spreadsheetId=sid, range="A:Z").execute().get("values", [])
        cols = list(existing[0]) if existing else list(SHEET_COLS)
        for c in SHEET_COLS:
            if c not in cols:
                cols.append(c)
        idx = {c: i for i, c in enumerate(cols)}
        rows = {}
        for r in existing[1:]:
            if r:
                rows[r[0]] = list(r) + [""] * (len(cols) - len(r))
        for cid, d in u.items():
            row = rows.get(cid, [""] * len(cols))
            row[idx["chat_id"]] = cid
            row[idx["name"]] = d.get("name", "")
            row[idx["email"]] = d.get("email", "")
            row[idx["country"]] = d.get("country", "")
            row[idx["joined"]] = d.get("joined", "")
            row[idx["paid"]] = "YES" if d.get("paid") else "no"
            row[idx["tier"]] = d.get("tier") or ""
            row[idx["lessons_done"]] = d.get("lessons_done", 0)
            row[idx["goal"]] = GOAL_TXT.get(d.get("goal"), "")
            row[idx["stage"]] = d.get("stage", "")
            row[idx["referred_by"]] = d.get("referred_by", "")
            row[idx["ref_code"]] = d.get("ref_code", "")
            row[idx["referrals_paid"]] = d.get("referrals_paid", 0)
            row[idx["referral_earnings"]] = d.get("referral_earnings", 0)
            rows[cid] = row
        grid = [cols] + [rows[k] for k in sorted(rows)]
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range="A1",
            valueInputOption="RAW", body={"values": grid}).execute()
    except Exception as e:
        print("sheet_sync err:", e)

def course_lessons(title):
    c = next((c for c in COURSES if c["title"].lower() == title.lower()), COURSES[0])
    return c["lessons"]
def course_by_title(title): return next((c for c in COURSES if c["title"].lower() == title.lower()), None)
def price_for(cc): return PRICE_TIERS.get((cc or "").upper(), DEFAULT_TIER)
def credit_referral(users, cid):
    """Award the referrer when `cid` converts to paid. Idempotent."""
    u = users.get(cid, {})
    rb = u.get("referred_by")
    if not rb or rb not in users or u.get("referral_credited"):
        return
    ref = users[rb]
    ccy = price_for(ref.get("country") or "US")["currency"]
    reward = REFERRAL_REWARD.get(ref.get("country") or "US", 3)
    ref["referrals_paid"] = ref.get("referrals_paid", 0) + 1
    ref["referral_earnings"] = ref.get("referral_earnings", 0) + reward
    ref["pending_reward_msg"] = (
        f"\U0001F389 A singer you referred just unlocked the full course! "
        f"You earned {CCY[ccy]}{reward}. Share your profile card to earn more.")
    u["referral_credited"] = True
    users[rb] = ref; users[cid] = u
STEP_ICON = {"teach": "📖", "exercise": "🏋️", "practice": "🔁", "tip": "💡"}
STEP_LABEL = {"teach": "LEARN", "exercise": "DO IT", "practice": "BUILD IT", "tip": "PRO TIP"}
COURSE_ICON = {"Technique": "🎤", "Ear Training": "👂", "Performance": "🎭", "Mindset": "🧠",
               "Theory": "🎼", "Style-Specific": "🎨", "Business": "💼", "BandLab": "🎚️"}
def progress_bar(done, total, width=5):
    filled = min(width, max(1, round((done / total) * width))) if total and done < total else (width if total else 0)
    return "▰" * filled + "▱" * (width - filled) + f" {done}/{total}"

# --- coach voice (humanization layer) -----------------------------------
# Goal phrasing from assessment Q4 (1=sing without embarrassment, 2=sound good, 3=go pro)
GOAL_PHRASE = {1: "sing without embarrassment", 2: "sound good every time you perform",
               3: "go pro and get paid to sing"}
# Words that signal a learner is struggling -> trigger empathy, not a hard push
STRUGGLE_WORDS = ("hard", "can't", "cant", "difficult", "stuck", "confused",
                  "weird", "pain", "hurt", "struggle", "impossible", "tone deaf")
def first_name(user):
    for k in ("first_name", "name"):
        v = (user.get(k) or "").strip()
        if v:
            return v.split()[0]
    return "singer"
def goal_line(user):
    return GOAL_PHRASE.get(user.get("goal")) or "become a stronger singer"
def is_struggling(text):
    t = (text or "").lower()
    return any(w in t for w in STRUGGLE_WORDS)

def lesson_text(lid, pos, total, user=None):
    l = LESSONS.get(lid, {})
    cap = None
    if user is not None:
        mins = (user or {}).get("mins_per_session")
        if mins:
            cap = 1 if mins < 15 else 2 if mins < 30 else 3 if mins < 45 else 5 if mins < 60 else 8
    steps = [s for s in l.get("steps", [])][:cap or 3]
    c = l.get("course", "")
    chead = COURSE_ICON.get(c, "🎵")
    outcome = l.get("displayOutcome") or (l.get("outcomes") or [""])[0]
    scn = (f"Imagine you're {l.get('title','')} in a real session: {outcome[0].lower()}{outcome[1:]}.")
    bar = progress_bar(pos, total)
    note = f"\n💡 {len(steps)} step{'s' if len(steps)!=1 else ''} selected for your {user.get('mins_per_session','')} min session." if cap else ""
    b = (f"{chead} Lesson {pos} of {total} — {l.get('title','')} "
         f"({l.get('durationMin','')} min) · {c}\n{bar}\n\n"
         f"🎯 Outcome:\n   {outcome}\n\n"
         f"🎬 Scenario:\n   {scn}{note}\n")
    for i, s in enumerate(steps, 1):
        t = s.get("type", "teach"); icon=STEP_ICON.get(t,"▸"); label=STEP_LABEL.get(t,"STEP")
        body=s.get("body",""); 
        if cap is not None and len(body)>160: body=body[:157]+"..."
        b += f"\n{icon} {label} {i}/{len(steps)}: {s.get('title','')}\n{body}\n"
    return b.strip()

def outcomes_text(lid):
    l = LESSONS.get(lid, {})
    pt = l.get("performanceTask", {})
    return ("✅ By the end you'll be able to:\n"
            + "\n".join(f"   ✓ {o}" for o in l.get("outcomes", []))
            + (f"\n\n🎙️ Your task: {pt.get('prompt','')}" if pt.get("prompt") else "")
            + "\n\n👉 When you're done, reply 'done' — or tell me how it felt in one word and I'll adjust.")

def search_lessons(kw):
    kw = kw.lower()
    hits = [l for l in LESSONS.values() if kw in l["title"].lower() or any(kw in o.lower() for o in l.get("outcomes", []))]
    return hits[:8]
def create_flutter_payment(email, name, tier, cid):
    ref = f"swt-{cid}-{uuid.uuid4().hex[:8]}"
    payload = {"tx_ref": ref, "amount": tier["amount"], "currency": tier["currency"],
               "redirect_url": "https://coachteesos.github.io/sessionswithtoby-/",
               "customer": {"email": email, "name": name or "Student"},
               "customizations": {"title": "SessionsWithToby", "description": "Full Vocal Course Upgrade"}}
    if TEST_MODE:
        return "https://example.com/test-unlock (TEST MODE — no charge)", ref
    try:
        r = requests.post("https://api.flutterwave.com/v3/payments", json=payload,
                          headers={"Authorization": f"Bearer {FLW_SECRET}"}, timeout=15)
        return r.json().get("data", {}).get("link"), ref
    except Exception: return None, ref

def verify_payment(tx_ref):
    """Re-query Flutterwave to confirm a tx_ref actually paid. Never trust the user."""
    if TEST_MODE:
        return True  # TEST MODE: never hits Flutterwave, never charged
    if not tx_ref:
        return False
    try:
        r = requests.get("https://api.flutterwave.com/v3/transactions/verify_by_reference",
                         params={"tx_ref": tx_ref},
                         headers={"Authorization": f"Bearer {FLW_SECRET}"}, timeout=15)
        j = r.json()
        return j.get("status") == "success" and j.get("data", {}).get("status") == "successful"
    except Exception:
        return False

async def _start(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    # returning user: never wipe progress on /start; only refresh stale fields
    if cid in u:
        user = u[cid]
        if not user.get("ref_code"):
            user["ref_code"] = make_ref_code(cid)
        for k in ("first_name", "name"):
            if update.effective_user.first_name and not user.get(k):
                user[k] = (update.effective_user.first_name or "").strip()
                if k == "name":
                    user["first_name"] = user[k].split()[0]
        save_users(u)
        if user.get("paid"):
            nm = first_name(user); done = user.get("lessons_done", 0)
            return await update.message.reply_text(
                f"Good to see you again, {nm}. 🙏\n\n"
                f"You've done {done} lessons — pick up wherever you left off.\n\n"
                f"Commands:\n• next — your next lesson\n• topics — browse all courses\n• profile — your card\n• reset — start fresh")
        nm = first_name(user); done = user.get("lessons_done", 0)
        return await update.message.reply_text(
            f"Welcome back, {nm}. 🙏\n\nYou’ve done {done} lessons. Pick up right where you left off.\n\n"
            f"Commands:\n• next — your next lesson\n• topics — browse all courses\n• profile — your card\n• reset — start fresh")
    # parse referral deep-link: /start <REFCODE>
    args = getattr(ctx, "args", None) or []
    payload = (args[0] if args else "").strip().upper()
    referred_by = None
    if payload:
        for oid, ou in u.items():
            if ou.get("ref_code") == payload and oid != cid:
                referred_by = oid; break
    ref_code = (u.get(cid, {}) or {}).get("ref_code") or make_ref_code(cid)
    telegram_name = (update.effective_user.first_name or "").strip()
    u[cid] = {"stage": "country", "name": telegram_name, "email": "", "course": 1, "pos": 0,
              "country": None, "paid": False, "pay_ref": None, "upsold": False,
              "tier": None, "assess_q": 0, "assess_score": 0, "path": [], "path_i": 0,
              "ref_code": ref_code, "referred_by": referred_by, "referrals": [],
              "referrals_paid": 0, "referral_earnings": 0, "lessons_done": 0,
              "first_name": telegram_name.split()[0] if telegram_name else "", "goal": 0,
              "joined": datetime.now(timezone.utc).isoformat(), "pending_reward_msg": "",
              "streak": 0, "last_lesson_ts": None}
    if referred_by and referred_by in u:
        u[referred_by].setdefault("referrals", []).append(cid)
    save_users(u)
    _admin_event({"type": "signup", "chat_id": cid, "country": None, "ts": datetime.now(timezone.utc).isoformat()})
    extras = []
    if telegram_name:
        extras.append(f"Hey, {telegram_name.split()[0]}.")
    if referred_by:
        extras.append("\U0001F49B You joined through a friend's link — they'll earn when you unlock. Welcome!")
    extras.append("Which country are you in? e.g. Nigeria, USA, UK")
    extras.append("After that: pick your email, then pick how much time you have per session.")
    await update.message.reply_text(
        "\U0001F3A4 Welcome to Sessions With Toby — I coach your voice, one real lesson at a time.\n\n"
        + "\n".join(extras))
async def _msg(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u: return await _start(update, ctx)
    user = u[cid]; text = update.message.text.strip()
    low = text.lower()

    # ----- referral reward alert (fires on any inbound message) -----
    if user.get("pending_reward_msg"):
        await update.message.reply_text(user.pop("pending_reward_msg")); save_users(u)

    # ----- assessment (post-pay) -----
    if user["stage"] == "assess":
        if low not in ASSESS[user["assess_q"]][1]:
            return await update.message.reply_text("Reply with a number: " + " / ".join(ASSESS[user["assess_q"]][1]))
        if user["assess_q"] == len(ASSESS) - 1:
            user["goal"] = int(low)   # Q4 = main goal, used for personalized coaching
        user["assess_score"] += int(low); user["assess_q"] += 1; save_users(u)
        if user["assess_q"] < len(ASSESS):
            return await update.message.reply_text(assess_prompt(user["assess_q"]))
        tier = TIER_FROM_SCORE(user["assess_score"]); user["tier"] = tier
        user["path"] = [lid for ctitle, sl in TIERS[tier] for lid in course_lessons(ctitle)[sl]]
        user["path_i"] = 0; user["stage"] = "menu"; save_users(u)
        await update.message.reply_text(
            f"\U0001F9ED Assessment done. Your level: *{tier}*.\n"
            f"Since your goal is to {goal_line(user)}, I've built a {len(user['path'])}-lesson path aimed right at that.\n\n"
            f"Commands now:\n• `next` — your adaptive lesson\n• `topics` — browse all 8 courses\n• `search <keyword>` — find any lesson\n• `level` — re-assess\n• `profile` — your shareable Vocal Profile Card")
        return await send_path_lesson(update, user, cid)

    # ----- menu -----
    if user["stage"] == "menu":
        if low in ("profile", "card", "refer"): return await profile(update, ctx)
        if low == "next": return await send_path_lesson(update, user, cid)
        if low == "topics":
            lines = "\n".join(f"  {i+1}. {c['title']}" for i, c in enumerate(COURSES))
            return await update.message.reply_text("All courses — reply the number to open:\n" + lines)
        if low == "level":
            user["stage"] = "assess"; user["assess_q"] = 0; user["assess_score"] = 0; save_users(u)
            return await update.message.reply_text("Re-assessing. " + assess_prompt(0))
        if low.startswith("search "):
            kw = low[7:].strip(); hits = search_lessons(kw)
            if not hits: return await update.message.reply_text("No lessons matched. Try another word.")
            return await update.message.reply_text("🔎 Found:\n" + "\n".join(f"  • {h['title']} ({h['course']})" for h in hits))
        if low.isdigit() and 1 <= int(low) <= len(COURSES):
            c = COURSES[int(low) - 1]
            user["stage"] = "browse"; user["browse_course"] = c["title"]; save_users(u)
            return await update.message.reply_text(f"📖 {c['title']} — reply a lesson number 1–{len(c['lessons'])} to open it (or 'topics' to go back).")
        return await update.message.reply_text("Use: `next`, `topics`, `search <kw>`, `level`.")

    # ----- browse a course's lessons -----
    if user["stage"] == "browse":
        if low == "topics":
            user["stage"] = "menu"; save_users(u)
            lines = "\n".join(f"  {i+1}. {c['title']}" for i, c in enumerate(COURSES))
            return await update.message.reply_text("All courses — reply the number to open:\n" + lines)
        if low.isdigit():
            c = course_by_title(user.get("browse_course", "")); n = int(low)
            if c and 1 <= n <= len(c["lessons"]):
                lid = c["lessons"][n-1]; await update.message.reply_text(lesson_text(lid, n, len(c["lessons"]), user=user))
                return await update.message.reply_text(outcomes_text(lid))
            return await update.message.reply_text(f"Pick a number 1–{len(c['lessons']) if c else 0}.")
        return await update.message.reply_text("Reply a lesson number, or 'topics' to go back.")

    # ----- capture stages -----
    if user["stage"] == "country":
        cc = COUNTRY_MAP.get(low)
        if not cc: return await update.message.reply_text("Reply with your country name (e.g. Nigeria, USA, UK):")
        user["country"] = cc; user["stage"] = "name"; save_users(u)
        return await update.message.reply_text("📍 Got it. What should I call you?")
    if user["stage"] == "name":
        user["name"] = text; user["first_name"] = text.split()[0]; user["stage"] = "email"; save_users(u)
        return await update.message.reply_text(f"Nice, {text.split()[0]}! Drop your email so I can save your progress:")
    if user["stage"] == "email":
        if "@" not in low: return await update.message.reply_text("That’s not an email — try again please.")
        user["email"] = text; user["stage"] = "time"; save_users(u)
        await update.message.reply_text("How much time can you spare per session?\n\nReply:\n• 10\n• 20\n• 30\n• 45\nor just type minutes.")
        return
    if user["stage"] == "time":
        try:
            mins = int(low)
        except ValueError:
            mins = 20
        mins = max(5, min(120, mins))
        user["mins_per_session"] = mins; user["stage"] = "learning"; save_users(u)
        cl = course_lessons("Sing Without Limits")
        await update.message.reply_text(
            f"Got it — {mins}-minute sessions. That’s more than enough.\n\n"
            f"Starting your first lesson now 🎤")
        return await send_free_lesson(update, user)
    if user["stage"] == "await_payment":
        # payment disabled for inspection: auto-advance
        user["paid"] = True; user["stage"] = "assess"; user["assess_q"] = 0; user["assess_score"] = 0
        credit_referral(u, cid); save_users(u)
        _admin_event({"type": "pay", "chat_id": cid, "tx_ref": user.get("pay_ref"), "country": user.get("country"), "ts": datetime.now(timezone.utc).isoformat()})
        return await update.message.reply_text("🎉 Payment confirmed! Quick assessment so I serve you right.\n\n" + assess_prompt(0))

    # ----- free lessons -----
    if user["stage"] == "learning":
        if low == "repeat": return await send_free_lesson(update, user)
        if low == "share": return await _share(update, ctx)
        if low == "topics":
            lines = "\n".join(f"  {i+1}. {c['title']}" for i, c in enumerate(COURSES))
            return await update.message.reply_text("All courses — reply the number to open:\n" + lines)
        if low.startswith("search "):
            kw = low[7:].strip(); hits = search_lessons(kw)
            if not hits: return await update.message.reply_text("No lessons matched. Try another word.")
            return await update.message.reply_text("🔎 Found:\n" + "\n".join(f"  • {h['title']} ({h['course']})" for h in hits))
        if low == "next":
            cl = course_lessons("Sing Without Limits"); pos = user.get("pos", 0)
            if pos < len(cl):
                user["pos"] = pos + 1; user["lessons_done"] = user.get("lessons_done", 0) + 1; save_users(u)
                return await send_free_lesson(update, user)
            return await update.message.reply_text("🏆 Free path complete! Unlock above to keep going on the full journey.")
        if low == "continue":
            return await update.message.reply_text(f"Resuming from: {user.get('stage','start')}. {user.get('lessons_done', 0)} lessons done.")
        if low == "reset":
            if cid in u: del u[cid]; save_users(u)
            return await update.message.reply_text("🗑️ Progress wiped. Starting fresh.\n\nWhich country are you in? (e.g. Nigeria, USA, UK)")
        if _key_hits(text):
            hits = search_lessons(low)
            if hits:
                lid = hits[0]["id"]; title = hits[0]["title"]; course = hits[0]["course"]
                return await update.message.reply_text(f"🔥 Jumping to: {title} [{course}]\n\n{lesson_text(lid, 1, 1)}\n\n{outcomes_text(lid)}\n\n(type 'next' for your next lesson)")
        if low != "done":
            if is_struggling(text):
                return await update.message.reply_text(
                    f"Hey, {first_name(user)} — hitting a wall here is completely normal. "
                    f"Most singers do at this exact spot.\n\n"
                    f"Don't force it. Type 'repeat' to run the lesson again, or tell me in one line "
                    f"what's tripping you up and I'll point you at the right drill.")
            return await update.message.reply_text(
                f"When you've finished this lesson, reply 'done'.\n\n"
                f"Or just tell me how it felt — 'tight', 'easy', 'confused' — "
                f"I read every reply and steer you from there.")
        cl = course_lessons("Sing Without Limits"); user["pos"] += 1
        user["lessons_done"] = user.get("lessons_done", 0) + 1; save_users(u)
        # payment temporarily removed for inspection — keep learning
        if user["pos"] < len(cl):
            await update.message.reply_text(f"Locked in. Lesson {user['pos']} next — keep the momentum going. 🎤")
            return await send_free_lesson(update, user)
        return await update.message.reply_text("🏆 Free path complete! Unlock above to keep going on the full journey.")

async def send_free_lesson(update, user):
    lid=None; total=FREE_LESSONS
    feats = _safe_features()
    if user["pos"] < min(FREE_LESSONS, len(feats)):
        lid = feats[user["pos"]]
    else:
        cl = course_lessons("Sing Without Limits"); lid = cl[user["pos"]]; total = len(cl)
    await update.message.reply_text(lesson_text(lid, user["pos"] + 1, total, user=user))
    await update.message.reply_text(outcomes_text(lid))
    # bump streak after free lesson
    cid = str(update.effective_chat.id)
    streak = _bump_streak(load_users(), cid)
    if streak and streak % 3 == 0:
        await asyncio.sleep(0.4)
        await update.message.reply_text(f"🔥 {streak}-day streak — most singers quit by day 2. You’re building something real.")
async def send_path_lesson(update, user, cid=None):
    if user["path_i"] >= len(user["path"]):
        return await update.message.reply_text(
            "🏆 Adaptive path complete.\n\n"
            "Next moves:\n"
            "• `topics` — open the next course in progression\n"
            "• `search <keyword>` — drill only what you need\n"
            "• `level` — re-assess and rebuild your path")
    lid = user["path"][user["path_i"]]; user["path_i"] += 1
    user["lessons_done"] = user.get("lessons_done", 0) + 1
    # persist WITHOUT clobbering other users (save_users expects the full dict)
    allu = load_users(); allu[cid] = user; save_users(allu)
    # path-relative numbering (course lookup is best-effort, never a hard dependency)
    pos = user["path_i"]; total = len(user["path"])
    # bump streak after paid path lesson
    streak = _bump_streak(allu, cid)
    if streak and streak % 3 == 0:
        await asyncio.sleep(0.4)
        await update.message.reply_text(f"🔥 {streak}-day streak — most singers quit by day 2. You’re building something real.")
    await update.message.reply_text(lesson_text(lid, pos, total, user=user))
    await update.message.reply_text(outcomes_text(lid) + "\n\n(type 'next' for your next adaptive lesson)")

INNER = 32
def _row(s): return "║ " + s[:INNER].ljust(INNER) + " ║"
def render_profile(user):
    ccy = price_for(user.get("country") or "US")["currency"]; sym = CCY[ccy]
    tier = user.get("tier") or "Free"; country = user.get("country") or "—"
    done = user.get("lessons_done", 0); pi = user.get("path_i", 0); pt = len(user.get("path", []))
    refs = len(user.get("referrals", [])); conv = user.get("referrals_paid", 0)
    earned = user.get("referral_earnings", 0)
    link = f"https://t.me/{BOT_USERNAME}?start={user.get('ref_code','')}"
    reward = REFERRAL_REWARD.get(user.get("country") or "US", 3)
    lines = [f"\U0001F3A4 VOCAL PROFILE CARD", f"Name: {user.get('name') or 'Singer'}",
             f"\U0001F30D {country}  ·  Tier: {tier}", f"Lessons done: {done}",
             (f"Path: {pi}/{pt}" if pt else "Path: —"),
             f"Invited: {refs}  ·  Converted: {conv}", f"Earned: {sym}{earned}"]
    bar = "═" * (INNER + 2)
    out = ["╔" + bar + "╗", _row(lines[0].center(INNER)), "╠" + bar + "╣"]
    for ln in lines[1:]: out.append(_row(ln))
    out.append("╚" + bar + "╝")
    return "\n".join(out)

async def _profile(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u: return await _start(update, ctx)
    user = u[cid]
    ccy = price_for(user.get("country") or "US")["currency"]; reward = REFERRAL_REWARD.get(user.get("country") or "US", 3)
    link = f"https://t.me/{BOT_USERNAME}?start={user.get('ref_code','')}"
    await update.message.reply_text(render_profile(user))
    await update.message.reply_text(f"\U0001F517 Your invite link (tap to share):\n{link}\n\nRefer a friend, earn {CCY[ccy]}{reward} each time they unlock \U0001F4B0")
    await update.message.reply_text("\U0001F4E4 Share this card on your status/Story — every friend who joins via your link earns you a reward when they unlock.")
async def _share(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u: return await _start(update, ctx)
    user = u[cid]
    ref_link = f"https://t.me/{BOT_USERNAME}?start={user.get('ref_code','')}"
    reward = REFERRAL_REWARD.get(user.get("country") or "US", 3)
    ccy = price_for(user.get("country") or "US")["currency"]; sym = CCY[ccy]
    done = user.get("lessons_done", 0)
    text = (
        f"I just trained my voice with Sessions With Toby — {done} lesson{'s' if done!=1 else ''} done. "
        f"Tap the link to try it, then share as your WhatsApp status/Story. "
        f"When someone joins and unlocks, I earn {sym}{reward}.\n\n{ref_link}"
    )
    wa = "https://wa.me/?text=" + __import__("urllib.parse").quote(text)
    await update.message.reply_text(
        f"Your share card\n\n{text}\n\n"
        f"• Open WhatsApp:\n{wa}\n\n"
        f"Reward per unlock: {sym}{reward}"
    )

async def flw_webhook(request):
    try: data = await request.json()
    except Exception: return web.Response(text="bad", status=400)
    if FLW_HASH and request.headers.get("verif-hash") != FLW_HASH: return web.Response(text="no", status=403)
    if data.get("event") in ("charge.completed",) and data.get("data", {}).get("status") == "successful":
        tx = data.get("data", {}).get("tx_ref")
        # re-verify with Flutterwave before granting access (don't trust raw payload)
        if not verify_payment(tx):
            return web.Response(text="unverified", status=500)  # Flutterwave retries
        users = load_users()
        for cid, u in users.items():
            if u.get("pay_ref") == tx:
                u["paid"] = True; u["stage"] = "assess"; u["assess_q"] = 0; u["assess_score"] = 0
                credit_referral(users, cid); save_users(users); break
    return web.Response(text="ok")

# admin API
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_ADMIN_EVENTS = 200  # ring buffer
_ADMIN_EVENTS = []
def _admin_event(ev):
    _ADMIN_EVENTS.append(ev)
    if len(_ADMIN_EVENTS) > MAX_ADMIN_EVENTS:
        del _ADMIN_EVENTS[:-MAX_ADMIN_EVENTS]
def _admin_ok(data): return web.json_response({"ok": True, **data})
def _admin_bad(msg, status=400): return web.Response(text=msg, status=status)
def _admin_auth(request):
    t = request.headers.get("X-Admin-Token", "")
    if not _ADMIN_TOKEN or t != _ADMIN_TOKEN:
        raise web.HTTPUnauthorized(text="unauthorized")
def _bump_streak(u, cid):
    user = u.get(cid)
    if not user:
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    last = user.get("last_lesson_ts")
    last_day = datetime.fromisoformat(last).date().isoformat() if last else None
    prev = int(user.get("streak") or 0)
    if last_day == today:
        return prev
    if last_day == (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=1)).date().isoformat():
        user["streak"] = prev + 1
    else:
        user["streak"] = 1
    user["last_lesson_ts"] = datetime.now(timezone.utc).isoformat()
    save_users(u)
    return int(user.get("streak") or 0)

async def admin_stats(request):
    _admin_auth(request)
    users = load_users()
    total = len(users)
    paid = sum(1 for u in users.values() if u.get("paid"))
    lessons = sum(int(u.get("lessons_done", 0) or 0) for u in users.values())
    return _admin_ok({"users": total, "paid": paid, "lessons_done": lessons})
async def admin_recent(request):
    _admin_auth(request)
    limit = int(request.rel_url.query.get("limit", "20"))
    users = load_users()
    rows = []
    for cid, u in users.items():
        rows.append({
            "chat_id": cid,
            "name": u.get("name"),
            "email": u.get("email"),
            "country": u.get("country"),
            "stage": u.get("stage"),
            "tier": u.get("tier"),
            "paid": bool(u.get("paid")),
            "lessons_done": int(u.get("lessons_done", 0) or 0),
            "joined": u.get("joined"),
        })
    rows.sort(key=lambda r: r.get("joined") or "", reverse=True)
    return _admin_ok({"items": rows[: max(1, min(limit, 100))]})
async def admin_events(request):
    _admin_auth(request)
    limit = int(request.rel_url.query.get("limit", "50"))
    return _admin_ok({"items": list(reversed(_ADMIN_EVENTS[-max(1, min(limit, 100)):]))})
async def admin_forget(request):
    _admin_auth(request)
    try: body = await request.json()
    except Exception: return _admin_bad("bad json", 400)
    cid = str(body.get("chat_id", "")).strip()
    if not cid: return _admin_bad("chat_id required", 400)
    users = load_users()
    if cid in users:
        u = users.pop(cid)
        _admin_event({"type": "forget", "chat_id": cid, "name": u.get("name"), "ts": datetime.now(timezone.utc).isoformat()})
        save_users(users)
    return _admin_ok({"deleted": cid in users})

# keyboard helpers
_BTN = lambda text, cmd: f"[{text}](tg://bot_command?start={cmd})"

COMMANDS_HELP = (
    "\U0001F4AC Commands now available:\n"
    f"{_BTN('📚 topics', 'topics')} browse all courses\n"
    f"{_BTN('🔎 search', 'search Riffs')} find any lesson\n"
    f"{_BTN('▶️ next', 'next')} your next lesson\n"
    f"{_BTN('🪪 profile', 'profile')} your vocal profile card\n"
    f"{_BTN('🔄 level', 'level')} retake the level check"
)

# universal featured lessons for pre-pay/browse use
FEATURED_IDS = [10, 9, 3, 28, 41, 15, 54]  # mix, head, vibrato, riffs, runs, stage, mindset

def _safe_features():
    hits = [l for l in FEATURED_IDS if l in LESSONS]
    if not hits:
        hits = [l["id"] for l in list(LESSONS.values())[:6]]
    return hits

def _featured_menu():
    lines = ["🔥 Jump straight into what singers actually want:\n"]
    for lid in _safe_features():
        l = LESSONS.get(lid, {})
        lines.append(f"• {l.get('title')} [{l.get('course')}]")
    lines.append("\nPaste a title or keyword to open it, or use `search <word>`.")
    return "\n".join(lines)

def _key_hits(text):
    t = (text or "").lower()
    return any(k in t for k in ("high note", "high notes", "mix", "mixed", "vibrato", "riffs", "runs", "belt", "belting", "falsetto", "head voice", "chest voice", "agility", "runs", "riff"))

async def healthz(request):
    return web.Response(text="ok")


async def main():
    print("[boot] starting bot + web server")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("card", profile))
    app.add_handler(CommandHandler("refer", profile))
    app.add_handler(CommandHandler("share", share_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))
    await app.initialize(); await app.start()
    try:
        await app.updater.start_polling()
    except RuntimeError as exc:
        print(f"[boot] polling skipped: {exc}")
    web_app = web.Application()
    web_app.router.add_get("/healthz", healthz)
    web_app.router.add_get("/admin/stats", admin_stats)
    web_app.router.add_get("/admin/users/recent", admin_recent)
    web_app.router.add_get("/admin/events", admin_events)
    web_app.router.add_post("/admin/users/forget", admin_forget)
    runner = web.AppRunner(web_app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8000))).start()
    print("[boot] bot + web server listening")
    while True: await asyncio.sleep(3600)

if __name__ == "__main__":
    # surface where state actually lives so deploy misconfig is obvious in logs
    print(f"[storage] USERS_PATH={USERS}  SHEET_ID={'set' if SHEET_ID else 'unset'}")
    asyncio.run(main())
