#!/usr/bin/env python3
"""
SessionsWithToby — Telegram LMS bot (v2: payment-gated, human-paced)
Loop: /start -> country -> name -> email -> lesson 1..3 (free)
     -> at lesson 3: upsell + Flutterwave link, stage=await_payment
     -> blocks further lessons until payment verified (webhook) or user replies PAID
     -> resume full course.
Secrets from env: TELEGRAM_BOT_TOKEN, FLUTTERWAVE_SECRET_KEY
Webhook: POST /flutterwave-webhook (Flutterwave calls this in prod)
"""
import os, json, uuid, asyncio, requests
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

BASE = os.path.dirname(os.path.abspath(__file__))
LESSONS = {l["id"]: l for l in json.load(open(os.path.join(BASE, "lessons.json")))}
COURSES = json.load(open(os.path.join(BASE, "courses.json")))
USERS = os.path.join(BASE, "users.json")
FLW_SECRET = os.environ["FLUTTERWAVE_SECRET_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
FLW_HASH = os.environ.get("FLUTTERWAVE_WEBHOOK_HASH", "")  # optional verify-hash

PRICE_TIERS = {
    "NG": {"currency": "NGN", "amount": 5000}, "GH": {"currency": "GHS", "amount": 80},
    "US": {"currency": "USD", "amount": 15}, "GB": {"currency": "GBP", "amount": 12},
    "CA": {"currency": "CAD", "amount": 20}, "KE": {"currency": "KES", "amount": 1500},
}
DEFAULT_TIER = {"currency": "USD", "amount": 12}
COUNTRY_MAP = {"nigeria": "NG", "ghana": "GH", "usa": "US", "united states": "US",
               "uk": "GB", "united kingdom": "GB", "england": "GB", "canada": "CA",
               "kenya": "KE", "south africa": "ZA"}
FREE_LESSONS = 3  # upsell after this many

def load_users(): return json.load(open(USERS)) if os.path.exists(USERS) else {}
def save_users(u): json.dump(u, open(USERS, "w"), indent=2)
def course_lessons(cid):
    c = next((c for c in COURSES if c["id"] == cid), COURSES[0]); return c["lessons"]
def course_title(cid): return next((c["title"] for c in COURSES if c["id"] == cid), "")
def price_for(cc): return PRICE_TIERS.get((cc or "").upper(), DEFAULT_TIER)

def lesson_text(lesson_id, pos, total):
    l = LESSONS.get(lesson_id, {})
    steps = [s for s in l.get("steps", [])][:3]
    body = f"🎤 Lesson {pos} of {total} — {l.get('title','')} ({l.get('durationMin','')} min)\n\n"
    for s in steps:
        t = s.get("title", ""); b = s.get("body", "")[:240]
        body += f"▸ {t}\n{b}\n\n"
    return body.strip()

def outcomes_text(lesson_id):
    l = LESSONS.get(lesson_id, {})
    outs = "\n".join(f"  ✓ {o}" for o in l.get("outcomes", []))
    return f"✅ By the end of this lesson:\n{outs}\n\nReply 'done' when you've finished — or 'repeat' to see it again."

def create_flutter_payment(email, name, tier, chat_id):
    ref = f"swt-{chat_id}-{uuid.uuid4().hex[:8]}"
    payload = {
        "tx_ref": ref, "amount": tier["amount"], "currency": tier["currency"],
        "redirect_url": "https://coachteesos.github.io/sessionswithtoby-/",
        "customer": {"email": email, "name": name or "Student"},
        "customizations": {"title": "SessionsWithToby", "description": "Full Vocal Course Upgrade"},
    }
    try:
        r = requests.post("https://api.flutterwave.com/v3/payments", json=payload,
                          headers={"Authorization": f"Bearer {FLW_SECRET}"}, timeout=15)
        return r.json().get("data", {}).get("link"), ref
    except Exception:
        return None, ref

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = load_users(); cid = str(update.effective_chat.id)
    u[cid] = {"stage": "country", "name": "", "email": "", "course": 1, "pos": 0,
              "country": None, "paid": False, "pay_ref": None, "upsold": False}
    save_users(u)
    await update.message.reply_text(
        "🎤 Welcome to SessionsWithToby — I'll coach your voice, one real lesson at a time.\n\n"
        "First, which country are you in? (e.g. Nigeria, USA, UK)")

async def msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u: return await start(update, ctx)
    user = u[cid]; text = update.message.text.strip().lower()

    if user["stage"] == "country":
        cc = COUNTRY_MAP.get(text)
        if not cc: return await update.message.reply_text("Reply with your country name (e.g. Nigeria, USA, UK):")
        user["country"] = cc; user["stage"] = "name"; save_users(u)
        return await update.message.reply_text("📍 Got it. What should I call you?")

    if user["stage"] == "name":
        user["name"] = update.message.text.strip(); user["stage"] = "email"; save_users(u)
        return await update.message.reply_text(f"Nice to meet you, {user['name'].split()[0]}! Drop your email so I can save your progress:")

    if user["stage"] == "email":
        if "@" not in text: return await update.message.reply_text("That's not an email — try again:")
        user["email"] = text; user["stage"] = "learning"; save_users(u)
        cl = course_lessons(user["course"])
        await update.message.reply_text(f"✅ You're in. Starting *{course_title(user['course'])}* — {len(cl)} lessons. First one coming up.")
        return await send_lesson(update, user)

    if user["stage"] == "await_payment":
        if text == "paid":
            user["paid"] = True; user["stage"] = "learning"; save_users(u)
            await update.message.reply_text("🎉 Payment received — unlocking the rest of your vocal journey!")
            return await send_lesson(update, user)
        if text == "done":
            await update.message.reply_text("You've finished your 3 free lessons. Complete the payment above to unlock lessons 4–38 + all 8 courses, AI mentor & certification. Reply 'paid' once you're done.")
            return
        return await update.message.reply_text("Reply 'paid' after completing payment, or tap the link above to unlock.")

    if user["stage"] == "learning":
        if text == "repeat": return await send_lesson(update, user)
        if text != "done":
            return await update.message.reply_text("Whenever you've finished this lesson, reply 'done' (or 'repeat' to see it again).")
        # advance
        cl = course_lessons(user["course"]); user["pos"] += 1; save_users(u)
        if user["pos"] == FREE_LESSONS and not user["upsold"]:
            user["upsold"] = True; save_users(u)
            tier = price_for(user["country"])
            link, ref = create_flutter_payment(user["email"], user["name"], tier, cid)
            user["pay_ref"] = ref; user["stage"] = "await_payment"; save_users(u)
            sym = {"NGN": "₦", "USD": "$", "GBP": "£", "GHS": "GH₵", "CAD": "CA$", "KES": "KSh"}[tier["currency"]]
            await update.message.reply_text(
                f"🔥 That's your 3rd lesson done — nice work, {user['name'].split()[0]}!\n\n"
                f"You've just tasted the method. The full *{course_title(user['course'])}* path "
                f"(plus all 8 courses, the AI mentor, and certification) is {sym}{tier['amount']} for your region.\n\n"
                + (f"Unlock everything here 👉 {link}" if link else "Payment link is temporarily down — message me later."))
            return
        if user["pos"] < len(cl):
            return await send_lesson(update, user)
        await update.message.reply_text("🏆 You finished the course! You've unlocked your voice. Message me 'certify' to get your credentials.")

async def send_lesson(update, user):
    cl = course_lessons(user["course"]); lid = cl[user["pos"]]
    await update.message.reply_text(lesson_text(lid, user["pos"] + 1, len(cl)))
    await update.message.reply_text(outcomes_text(lid))

async def flw_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad", status=400)
    if FLW_HASH and request.headers.get("verif-hash") != FLW_HASH:
        return web.Response(text="no", status=403)
    d = data.get("data", {})
    if data.get("event") in ("charge.completed", "transfer") and d.get("status") == "successful":
        tx_ref = d.get("tx_ref")
        users = load_users()
        for cid, u in users.items():
            if u.get("pay_ref") == tx_ref:
                u["paid"] = True; u["stage"] = "learning"; save_users(users)
                break
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
