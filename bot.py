#!/usr/bin/env python3
"""
SessionsWithToby — Telegram LMS bot (v3: dynamic, behavior-adaptive, payment-gated)
Flow:
  /start -> country -> name -> email -> 3 free lessons
  -> upsell (geo-priced Flutterwave) -> await_payment (gated)
  -> ON PAY: skill-level quiz (behavioral) -> places tier -> dynamic menu
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
FREE_LESSONS = 3
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
TIERS = {  # tier -> ordered lesson windows (course, slice) for adaptive serving
    "Beginner": [("Technique", slice(0, 10)), ("Hear Anything, Repeat Anything", slice(0, 6))],
    "Intermediate": [("Technique", slice(8, 20)), ("Perform Like You Mean It", slice(0, 8))],
    "Advanced": [("Think Like A Career Vocalist", slice(0, 4)), ("Get Paid To Sing", slice(0, 10)), ("Make Records, Not excuses", slice(0, 10))],
}
TIER_FROM_SCORE = lambda s: "Beginner" if s <= 6 else "Intermediate" if s <= 9 else "Advanced"

_USER_LOCK = threading.Lock()
_USERS_CACHE = {}  # last-good copy; survives transient storage read failures so we never wipe everyone

def load_users():
    with _USER_LOCK:
        if SHEET_ID:
            data = sheet_read()
            if data is not None:
                _USERS_CACHE.clear(); _USERS_CACHE.update(data)
            return dict(_USERS_CACHE) or {}
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
        if SHEET_ID:
            sheet_write(u)
            return
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

# --- Google Sheet backend (live: token at /data/google_token.json) ---
def _sheets_svc():
    import google.oauth2.credentials as oc
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = oc.Credentials.from_authorized_user_file(
        os.environ.get("GOOGLE_TOKEN_PATH", "/data/google_token.json"), ["https://www.googleapis.com/auth/spreadsheets"])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("sheets", "v4", credentials=creds), creds
def sheet_read():
    try:
        svc, _ = _sheets_svc()
        res = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Users!A:Z").execute()
        rows = res.get("values", [])
        return {r[0]: json.loads(r[1]) for r in rows[1:] if len(r) >= 2}
    except Exception as e:
        print("sheet_read err:", e); return None
def sheet_write(u):
    try:
        svc, _ = _sheets_svc()
        vals = [["chat_id", "data_json"]] + [[k, json.dumps(v)] for k, v in u.items()]
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range="Users!A:Z",
            valueInputOption="RAW", body={"values": vals}).execute()
    except Exception as e:
        print("sheet_write err:", e)

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
def lesson_text(lid, pos, total):
    l = LESSONS.get(lid, {})
    steps = [s for s in l.get("steps", [])][:3]
    b = f"🎤 Lesson {pos} of {total} — {l.get('title','')} ({l.get('durationMin','')} min)\n\n"
    for s in steps: b += f"▸ {s.get('title','')}\n{s.get('body','')[:240]}\n\n"
    return b.strip()
def outcomes_text(lid):
    l = LESSONS.get(lid, {})
    return "✅ By the end:\n" + "\n".join(f"  ✓ {o}" for o in l.get("outcomes", [])) + \
           "\n\nReply 'done' (or 'repeat'). After payment: use 'next', 'topics', 'search <kw>'."

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

async def start(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    # returning paid user: never wipe progress
    if cid in u and u[cid].get("paid"):
        return await update.message.reply_text(
            f"\U0001F44B Welcome back, {u[cid]['name'].split()[0]}! Use `next`, `profile`, or `topics`.")
    # parse referral deep-link: /start <REFCODE>
    args = getattr(ctx, "args", None) or []
    payload = (args[0] if args else "").strip().upper()
    referred_by = None
    if payload:
        for oid, ou in u.items():
            if ou.get("ref_code") == payload and oid != cid:
                referred_by = oid; break
    ref_code = (u.get(cid, {}) or {}).get("ref_code") or make_ref_code(cid)
    u[cid] = {"stage": "country", "name": "", "email": "", "course": 1, "pos": 0,
              "country": None, "paid": False, "pay_ref": None, "upsold": False,
              "tier": None, "assess_q": 0, "assess_score": 0, "path": [], "path_i": 0,
              "ref_code": ref_code, "referred_by": referred_by, "referrals": [],
              "referrals_paid": 0, "referral_earnings": 0, "lessons_done": 0,
              "joined": datetime.now(timezone.utc).isoformat(), "pending_reward_msg": ""}
    if referred_by and referred_by in u:
        u[referred_by].setdefault("referrals", []).append(cid)
    save_users(u)
    extra = (" \U0001F49B You joined through a friend's link \u2014 they'll earn when you unlock. Welcome!"
             if referred_by else
             " Finish 3 free lessons, then share your profile card to earn when friends join.")
    await update.message.reply_text(
        "\U0001F3A4 Welcome to SessionsWithToby \u2014 I coach your voice, one real lesson at a time." + extra +
        "\n\nWhich country are you in? (e.g. Nigeria, USA, UK)")

async def msg(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u: return await start(update, ctx)
    user = u[cid]; text = update.message.text.strip()
    low = text.lower()

    # ----- referral reward alert (fires on any inbound message) -----
    if user.get("pending_reward_msg"):
        await update.message.reply_text(user.pop("pending_reward_msg")); save_users(u)

    # ----- assessment (post-pay) -----
    if user["stage"] == "assess":
        if low not in ASSESS[user["assess_q"]][1]:
            return await update.message.reply_text("Reply with a number: " + " / ".join(ASSESS[user["assess_q"]][1]))
        user["assess_score"] += int(low); user["assess_q"] += 1; save_users(u)
        if user["assess_q"] < len(ASSESS):
            return await update.message.reply_text(f"Q{user['assess_q']+1}. {ASSESS[user['assess_q']][0]}")
        tier = TIER_FROM_SCORE(user["assess_score"]); user["tier"] = tier
        user["path"] = [lid for ctitle, sl in TIERS[tier] for lid in course_lessons(ctitle)[sl]]
        user["path_i"] = 0; user["stage"] = "menu"; save_users(u)
        await update.message.reply_text(f"\U0001F9ED Assessment done. Your level: *{tier}*.\nI've built a {len(user['path'])}-lesson path for you.\n\nCommands now:\n• `next` — your adaptive lesson\n• `topics` — browse all 8 courses\n• `search <keyword>` — find any lesson\n• `level` — re-assess\n• `profile` — your shareable Vocal Profile Card")
        return await send_path_lesson(update, user, cid)

    # ----- paid menu -----
    if user["stage"] == "menu":
        if low in ("profile", "card", "refer"): return await profile(update, ctx)
        if low == "next": return await send_path_lesson(update, user, cid)
        if low == "topics":
            lines = "\n".join(f"  {i+1}. {c['title']} ({len(c['lessons'])} lessons)" for i, c in enumerate(COURSES))
            return await update.message.reply_text("📚 All courses — reply the number to dive in:\n" + lines)
        if low == "level":
            user["stage"] = "assess"; user["assess_q"] = 0; user["assess_score"] = 0; save_users(u)
            return await update.message.reply_text("Re-assessing. Q1. " + ASSESS[0][0])
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
            lines = "\n".join(f"  {i+1}. {c['title']} ({len(c['lessons'])} lessons)" for i, c in enumerate(COURSES))
            return await update.message.reply_text("📚 All courses — reply the number to dive in:\n" + lines)
        if low.isdigit():
            c = course_by_title(user.get("browse_course", "")); n = int(low)
            if c and 1 <= n <= len(c["lessons"]):
                lid = c["lessons"][n-1]; await update.message.reply_text(lesson_text(lid, n, len(c["lessons"])))
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
        user["name"] = text; user["stage"] = "email"; save_users(u)
        return await update.message.reply_text(f"Nice, {text.split()[0]}! Drop your email so I can save your progress:")
    if user["stage"] == "email":
        if "@" not in low: return await update.message.reply_text("That's not an email — try again:")
        user["email"] = text; user["stage"] = "learning"; save_users(u)
        cl = course_lessons("Sing Without Limits")
        await update.message.reply_text(f"✅ In. Starting *Sing Without Limits* ({len(cl)} lessons). First up:")
        return await send_free_lesson(update, user)
    if user["stage"] == "await_payment":
        if low != "paid":
            return await update.message.reply_text("Reply 'paid' after completing the payment link above to unlock everything.")
        # do NOT trust the user — re-verify with Flutterwave using the stored tx_ref
        if verify_payment(user.get("pay_ref")):
            user["paid"] = True; user["stage"] = "assess"; user["assess_q"] = 0; user["assess_score"] = 0
            credit_referral(u, cid); save_users(u)
            return await update.message.reply_text("🎉 Payment confirmed! Quick assessment so I serve you right.\n\nQ1. " + ASSESS[0][0])
        return await update.message.reply_text("🔍 I checked with Flutterwave and this payment isn't confirmed yet. Finish the payment link, then reply 'paid' again. If you already paid, wait a minute and try once more.")

    # ----- free lessons -----
    if user["stage"] == "learning":
        if low == "repeat": return await send_free_lesson(update, user)
        if low != "done":
            return await update.message.reply_text("When you've finished this lesson, reply 'done' (or 'repeat').")
        cl = course_lessons("Sing Without Limits"); user["pos"] += 1
        user["lessons_done"] = user.get("lessons_done", 0) + 1; save_users(u)
        if user["pos"] == FREE_LESSONS and not user["upsold"]:
            user["upsold"] = True; save_users(u)
            tier = price_for(user["country"]); link, ref = create_flutter_payment(user["email"], user["name"], tier, cid)
            user["pay_ref"] = ref; user["stage"] = "await_payment"; save_users(u)
            sym = CCY[tier["currency"]]
            await update.message.reply_text(
                f"🔥 3 free lessons done — nice, {user['name'].split()[0]}!\n\n"
                f"Full access (all 8 courses, AI mentor, certification) is {sym}{tier['amount']} for your region.\n\n"
                + (f"Unlock 👉 {link}" if link else "Payment link temporarily down — message me later."))
            return
        if user["pos"] < len(cl): return await send_free_lesson(update, user)
        return await update.message.reply_text("🏆 Free path complete! Pay above to unlock the full journey.")

async def send_free_lesson(update, user):
    cl = course_lessons("Sing Without Limits"); lid = cl[user["pos"]]
    await update.message.reply_text(lesson_text(lid, user["pos"] + 1, len(cl)))
    await update.message.reply_text(outcomes_text(lid))
async def send_path_lesson(update, user, cid=None):
    if user["path_i"] >= len(user["path"]):
        return await update.message.reply_text("🏆 You've completed your adaptive path! Use `topics` or `search` to keep going.")
    lid = user["path"][user["path_i"]]; user["path_i"] += 1
    user["lessons_done"] = user.get("lessons_done", 0) + 1
    # persist WITHOUT clobbering other users (save_users expects the full dict)
    allu = load_users(); allu[cid] = user; save_users(allu)
    # path-relative numbering (course lookup is best-effort, never a hard dependency)
    pos = user["path_i"]; total = len(user["path"])
    await update.message.reply_text(lesson_text(lid, pos, total))
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

async def profile(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u: return await start(update, ctx)
    user = u[cid]
    ccy = price_for(user.get("country") or "US")["currency"]; reward = REFERRAL_REWARD.get(user.get("country") or "US", 3)
    link = f"https://t.me/{BOT_USERNAME}?start={user.get('ref_code','')}"
    await update.message.reply_text(render_profile(user))
    await update.message.reply_text(f"\U0001F517 Your invite link (tap to share):\n{link}\n\nRefer a friend, earn {CCY[ccy]}{reward} each time they unlock \U0001F4B0")
    await update.message.reply_text("\U0001F4E4 Share this card on your status/Story — every friend who joins via your link earns you a reward when they unlock.")

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

async def healthz(request):
    return web.Response(text="ok")

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("card", profile))
    app.add_handler(CommandHandler("refer", profile))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))
    await app.initialize(); await app.start()
    asyncio.create_task(app.updater.start_polling())
    web_app = web.Application(); web_app.router.add_post("/flutterwave-webhook", flw_webhook)
    web_app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(web_app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8000))).start()
    print("Bot + webhook listening")
    while True: await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
