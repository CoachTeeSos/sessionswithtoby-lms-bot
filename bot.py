#!/usr/bin/env python3
"""
SessionsWithToby — Telegram LMS bot (minimal proving loop)
Loop: /start -> capture name+email -> deliver lesson 1 of course 1
     -> "done" advances + tracks progress
     -> at lesson 3: geo-price + Flutterwave payment link (upgrade to full course)
Secrets from env: TELEGRAM_BOT_TOKEN, FLUTTERWAVE_SECRET_KEY
Users stored locally in users.json (Google Sheet later).
"""
import os, json, time, uuid, requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

BASE = os.path.dirname(os.path.abspath(__file__))
LESSONS = {l["id"]: l for l in json.load(open(os.path.join(BASE, "lessons.json")))}
COURSES = json.load(open(os.path.join(BASE, "courses.json")))
USERS = os.path.join(BASE, "users.json")
FLW_SECRET = os.environ["FLUTTERWAVE_SECRET_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# geo price tiers by Telegram country_code (ISO-2). Default catches the rest.
PRICE_TIERS = {
    "NG": {"currency": "NGN", "amount": 5000, "label": "₦5,000"},
    "GH": {"currency": "GHS", "amount": 80,  "label": "GHS 80"},
    "US": {"currency": "USD", "amount": 15,  "label": "$15"},
    "GB": {"currency": "GBP", "amount": 12,  "label": "£12"},
    "CA": {"currency": "CAD", "amount": 20,  "label": "CA$20"},
    "KE": {"currency": "KES", "amount": 1500,"label": "KES 1,500"},
}
DEFAULT_TIER = {"currency": "USD", "amount": 12, "label": "$12"}

COUNTRY_MAP = {
    "nigeria": "NG", "ghana": "GH", "usa": "US", "united states": "US",
    "uk": "GB", "united kingdom": "GB", "england": "GB", "canada": "CA",
    "kenya": "KE", "south africa": "ZA",
}

def load_users():
    return json.load(open(USERS)) if os.path.exists(USERS) else {}

def save_users(u):
    json.dump(u, open(USERS, "w"), indent=2)

def course_lessons(course_id):
    c = next((c for c in COURSES if c["id"] == course_id), COURSES[0])
    return c["lessons"]

def lesson_block(lesson_id):
    l = LESSONS.get(lesson_id, {})
    steps = "\n".join(f"• {s.get('title','')}: {s.get('body','')[:300]}" for s in l.get("steps", []))
    outcomes = "\n".join(f"  ✓ {o}" for o in l.get("outcomes", []))
    return f"🎤 *{l.get('title','')}* ({l.get('durationMin','')} min)\n\n{steps}\n\nOutcomes:\n{outcomes}\n\nReply 'done' when finished."

def price_for(country_code):
    return PRICE_TIERS.get((country_code or "").upper(), DEFAULT_TIER)

def create_flutter_payment(email, name, tier, chat_id):
    ref = f"swt-{chat_id}-{uuid.uuid4().hex[:8]}"
    payload = {
        "tx_ref": ref,
        "amount": tier["amount"],
        "currency": tier["currency"],
        "redirect_url": "https://coachteesos.github.io/sessionswithtoby-/",
        "customer": {"email": email, "name": name or "Student"},
        "customizations": {"title": "SessionsWithToby", "description": "Full Vocal Course Upgrade"},
    }
    try:
        r = requests.post("https://api.flutterwave.com/v3/payments",
                          json=payload, headers={"Authorization": f"Bearer {FLW_SECRET}"}, timeout=15)
        j = r.json()
        return j.get("data", {}).get("link"), ref
    except Exception as e:
        return None, ref

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = load_users(); cid = str(update.effective_chat.id)
    u.setdefault(cid, {"stage": "country", "name": "", "email": "", "course": 1, "pos": 0, "country": None})
    save_users(u)
    await update.message.reply_text(
        "🎤 Welcome to SessionsWithToby. I'll coach your voice, one lesson at a time.\n\n"
        "First — which country are you in? (reply with the name, e.g. Nigeria, USA, UK)")

async def msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = load_users(); cid = str(update.effective_chat.id)
    if cid not in u:
        await start(update, ctx); return
    user = u[cid]; text = update.message.text.strip()

    if user["stage"] == "country":
        cc = COUNTRY_MAP.get(text.strip().lower())
        if not cc:
            await update.message.reply_text("Not sure I caught that. Reply with your country name (e.g. Nigeria, USA, UK):")
            return
        user["country"] = cc; user["stage"] = "name"; save_users(u)
        await update.message.reply_text("📍 Noted. What's your name?")
        return

    if user["stage"] == "name":
        user["name"] = text; user["stage"] = "email"
        save_users(u); await update.message.reply_text(f"Nice to meet you, {text.split()[0]}! What's your email?")
        return
    if user["stage"] == "email":
        if "@" not in text:
            await update.message.reply_text("That doesn't look like an email. Try again:"); return
        user["email"] = text; user["stage"] = "learning"
        save_users(u)
        cl = course_lessons(user["course"])
        await update.message.reply_text(f"✅ You're in. Starting *{next(c['title'] for c in COURSES if c['id']==user['course'])}*.")
        await update.message.reply_text(lesson_block(cl[0]), parse_mode="Markdown")
        return

    if user["stage"] == "learning":
        if text.lower() == "done":
            cl = course_lessons(user["course"]); user["pos"] += 1; save_users(u)
            if user["pos"] < len(cl):
                # upsell at lesson 3
                if user["pos"] == 2:
                    await update.message.reply_text("🔥 You're 2 lessons in. Want the FULL course (all 8 paths, AI mentor, certification)?")
                    link, ref = create_flutter_payment(user["email"], user["name"], price_for(user.get("country")), cid)
                    user["pay_ref"] = ref; save_users(u)
                    if link:
                        await update.message.reply_text(f"Unlock everything here: {link}\n(Price auto-set for your region.)")
                    else:
                        await update.message.reply_text("Payment link temporarily unavailable — message @SessionswithCoachToby_LMSbot later.")
                await update.message.reply_text(lesson_block(cl[user["pos"]]), parse_mode="Markdown")
            else:
                await update.message.reply_text("🏆 Course complete! You unlocked your voice. Message us to certify.")
        else:
            await update.message.reply_text("Reply 'done' when you've finished this lesson.")

async def set_country(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # helper: store country_code when bot receives it (or from chat member update)
    pass

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))
    print("Bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
