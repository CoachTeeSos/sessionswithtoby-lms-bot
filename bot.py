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
import os, json, uuid, asyncio, re, requests
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

BASE = os.path.dirname(os.path.abspath(__file__))
LESSONS = {l["id"]: l for l in json.load(open(os.path.join(BASE, "lessons.json")))}
COURSES = json.load(open(os.path.join(BASE, "courses.json")))
USERS = os.path.join(BASE, "users.json")
FLW_SECRET = os.environ["FLUTTERWAVE_SECRET_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
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

def load_users():
    if SHEET_ID:
        return sheet_read() or {}
    return json.load(open(USERS)) if os.path.exists(USERS) else {}
def save_users(u):
    if SHEET_ID: sheet_write(u)
    else: json.dump(u, open(USERS, "w"), indent=2)

# --- Google Sheet backend (latent until SHEET_ID + creds present) ---
def sheet_read():
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("google_api", os.path.join(BASE, "google_api.py"))
        ga = importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)
        rows = ga.sheets_get(SHEET_ID, "Users!A:Z")
        return {r[0]: json.loads(r[1]) for r in rows[1:] if r}
    except Exception: return None
def sheet_write(u):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("google_api", os.path.join(BASE, "google_api.py"))
        ga = importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)
        vals = [["chat_id", "data"]] + [[k, json.dumps(v)] for k, v in u.items()]
        ga.sheets_update(SHEET_ID, "Users!A:Z", vals)
    except Exception: pass

def course_lessons(title):
    c = next((c for c in COURSES if c["title"].lower() == title.lower()), COURSES[0])
    return c["lessons"]
def course_by_title(title): return next((c for c in COURSES if c["title"].lower() == title.lower()), None)
def price_for(cc): return PRICE_TIERS.get((cc or "").upper(), DEFAULT_TIER)
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
    try:
        r = requests.post("https://api.flutterwave.com/v3/payments", json=payload,
                          headers={"Authorization": f"Bearer {FLW_SECRET}"}, timeout=15)
        return r.json().get("data", {}).get("link"), ref
    except Exception: return None, ref

async def start(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    u[cid] = {"stage": "country", "name": "", "email": "", "course": 1, "pos": 0,
              "country": None, "paid": False, "pay_ref": None, "upsold": False,
              "tier": None, "assess_q": 0, "assess_score": 0, "path": [], "path_i": 0}
    save_users(u)
    await update.message.reply_text("🎤 Welcome to SessionsWithToby — I coach your voice, one real lesson at a time.\n\nWhich country are you in? (e.g. Nigeria, USA, UK)")

async def msg(update, ctx):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u: return await start(update, ctx)
    user = u[cid]; text = update.message.text.strip()
    low = text.lower()

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
        await update.message.reply_text(f"🧭 Assessment done. Your level: *{tier}*.\nI've built a {len(user['path'])}-lesson path for you.\n\nCommands now:\n• `next` — your adaptive lesson\n• `topics` — browse all 8 courses\n• `search <keyword>` — find any lesson\n• `level` — re-assess")
        return await send_path_lesson(update, user)

    # ----- paid menu -----
    if user["stage"] == "menu":
        if low == "next": return await send_path_lesson(update, user)
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
            return await update.message.reply_text(f"📖 {c['title']} — reply a lesson number 1–{len(c['lessons'])} to open it.")
        if low.isdigit() and user.get("last_course"):
            c = course_by_title(user["last_course"]); n = int(low)
            if c and 1 <= n <= len(c["lessons"]):
                lid = c["lessons"][n-1]; await update.message.reply_text(lesson_text(lid, n, len(c["lessons"])))
                return await update.message.reply_text(outcomes_text(lid))
        return await update.message.reply_text("Use: `next`, `topics`, `search <kw>`, `level`.")

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
        if low == "paid":
            user["paid"] = True; user["stage"] = "assess"; user["assess_q"] = 0; user["assess_score"] = 0; save_users(u)
            return await update.message.reply_text("🎉 Payment received! Quick assessment so I serve you right.\n\nQ1. " + ASSESS[0][0])
        return await update.message.reply_text("Reply 'paid' after completing the payment link above to unlock everything.")

    # ----- free lessons -----
    if user["stage"] == "learning":
        if low == "repeat": return await send_free_lesson(update, user)
        if low != "done":
            return await update.message.reply_text("When you've finished this lesson, reply 'done' (or 'repeat').")
        cl = course_lessons("Sing Without Limits"); user["pos"] += 1; save_users(u)
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
async def send_path_lesson(update, user):
    if user["path_i"] >= len(user["path"]):
        return await update.message.reply_text("🏆 You've completed your adaptive path! Use `topics` or `search` to keep going.")
    lid = user["path"][user["path_i"]]; user["path_i"] += 1; save_users(user)
    # find course for numbering
    ctitle = LESSONS[lid]["course"]; c = course_by_title(ctitle); n = c["lessons"].index(lid) + 1
    await update.message.reply_text(lesson_text(lid, n, len(c["lessons"])))
    await update.message.reply_text(outcomes_text(lid) + "\n\n(type 'next' for your next adaptive lesson)")

async def flw_webhook(request):
    try: data = await request.json()
    except Exception: return web.Response(text="bad", status=400)
    if FLW_HASH and request.headers.get("verif-hash") != FLW_HASH: return web.Response(text="no", status=403)
    d = data.get("data", {})
    if data.get("event") in ("charge.completed",) and d.get("status") == "successful":
        tx = d.get("tx_ref"); users = load_users()
        for cid, u in users.items():
            if u.get("pay_ref") == tx:
                u["paid"] = True; u["stage"] = "assess"; u["assess_q"] = 0; u["assess_score"] = 0; save_users(users); break
    return web.Response(text="ok")

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))
    await app.initialize(); await app.start()
    asyncio.create_task(app.updater.start_polling())
    web_app = web.Application(); web_app.router.add_post("/flutterwave-webhook", flw_webhook)
    runner = web.AppRunner(web_app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8000))).start()
    print("Bot + webhook listening")
    while True: await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
