import logging  # type: ignore
import asyncio
import aiohttp  # type: ignore
import json
import os
import hashlib
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, timedelta
from dotenv import load_dotenv  # type: ignore
from telegram import (  # type: ignore
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import (  # type: ignore
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    InlineQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.constants import ParseMode  # type: ignore

load_dotenv()

BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
ADMIN_PASS_HASH     = hashlib.sha256(os.getenv("ADMIN_PASSWORD", "pootilangaadi").encode()).hexdigest()
MAINT_PASS_HASH     = hashlib.sha256("pari".encode()).hexdigest()

TGOSINT_URL         = "https://tgosint.vercel.app/"
TGOSINT_KEY_DEFAULT = "drazeX"

DB_FILE = "db.json"

# ── States ────────────────────────────────────────────────────────────────────
AWAIT_INPUT         = 1
AWAIT_ADMIN_PW      = 2
AWAIT_BROADCAST     = 3
AWAIT_MAINT_PW      = 4
AWAIT_COOLDOWN_MIN  = 5
AWAIT_LIMIT_VAL     = 6
AWAIT_NOTE_TEXT     = 7
AWAIT_MSG_USER_TEXT = 8
AWAIT_API_KEY       = 9

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ── DB ────────────────────────────────────────────────────────────────────────

def loadDb():
    if not os.path.exists(DB_FILE):
        return {
            "users": {}, "lookups": [], "adminSessions": [],
            "maintenance": False, "adminIds": [],
            "apiKey": TGOSINT_KEY_DEFAULT,
        }
    with open(DB_FILE, "r") as f:
        data = json.load(f)
    for k, v in [("maintenance", False), ("adminIds", []), ("apiKey", TGOSINT_KEY_DEFAULT)]:
        if k not in data:
            data[k] = v
    return data

def saveDb(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

def getApiKey():
    return loadDb().get("apiKey", TGOSINT_KEY_DEFAULT)

def registerUser(userId, username, firstName):
    db  = loadDb()
    uid = str(userId)
    isNew = uid not in db["users"]
    if isNew:
        db["users"][uid] = {
            "userId": userId, "username": username or "",
            "firstName": firstName or "",
            "joinedAt": datetime.now().isoformat(),
            "totalLookups": 0,
            "lastSeen": datetime.now().isoformat(),
            "banned": False, "cooldownUntil": None,
            "dailyLimit": None, "notes": [], "lookupHistory": [],
        }
    else:
        db["users"][uid]["lastSeen"] = datetime.now().isoformat()
        if username:
            db["users"][uid]["username"] = username
        for k, v in [("banned", False), ("cooldownUntil", None),
                     ("dailyLimit", None), ("notes", []), ("lookupHistory", [])]:
            if k not in db["users"][uid]:
                db["users"][uid][k] = v
    saveDb(db)
    return isNew

def logLookup(userId, username, firstName, query, result, success):
    db  = loadDb()
    uid = str(userId)
    if uid in db["users"]:
        db["users"][uid]["totalLookups"] += 1
        db["users"][uid]["lastSeen"] = datetime.now().isoformat()
        db["users"][uid]["lookupHistory"].append({
            "ts": datetime.now().isoformat(), "query": query, "success": success,
        })
        if len(db["users"][uid]["lookupHistory"]) > 100:
            db["users"][uid]["lookupHistory"] = db["users"][uid]["lookupHistory"][-100:]
    db["lookups"].append({
        "ts": datetime.now().isoformat(),
        "userId": userId, "username": username or "",
        "firstName": firstName or "", "query": query, "success": success,
        "phone":   result.get("phone_info", {}).get("number", "") if result else "",
        "country": result.get("phone_info", {}).get("country", "") if result else "",
    })
    saveDb(db)

def getUserStats(userId):
    db  = loadDb()
    uid = str(userId)
    u   = db["users"].get(uid, {})
    ul  = [l for l in db["lookups"] if l["userId"] == userId]
    ts  = date.today().isoformat()
    return {
        "total":      u.get("totalLookups", 0),
        "today":      len([l for l in ul if l["ts"].startswith(ts)]),
        "successful": len([l for l in ul if l["success"]]),
        "joinedAt":   u.get("joinedAt", "N/A"),
        "lastSeen":   u.get("lastSeen", "N/A"),
    }

def getAdminStats():
    db   = loadDb()
    tot  = len(db["lookups"])
    ts   = date.today().isoformat()
    succ = [l for l in db["lookups"] if l["success"]]
    return {
        "totalUsers":   len(db["users"]),
        "totalLookups": tot,
        "todayLookups": len([l for l in db["lookups"] if l["ts"].startswith(ts)]),
        "successRate":  round(len(succ) / tot * 100, 1) if tot else 0,
        "bannedCount":  len([u for u in db["users"].values() if u.get("banned")]),
        "apiKey":       db.get("apiKey", TGOSINT_KEY_DEFAULT),
    }

def checkUserAccess(userId):
    db  = loadDb()
    uid = str(userId)
    u   = db["users"].get(uid)
    if not u:
        return True, ""
    if u.get("banned"):
        return False, "banned"
    cd = u.get("cooldownUntil")
    if cd:
        until = datetime.fromisoformat(cd)
        if datetime.now() < until:
            mins = int((until - datetime.now()).total_seconds() / 60)
            return False, f"cooldown:{mins}"
    lim = u.get("dailyLimit")
    if lim is not None:
        ts = date.today().isoformat()
        n  = len([l for l in db["lookups"] if l["userId"] == userId and l["ts"].startswith(ts)])
        if n >= lim:
            return False, f"limit:{lim}"
    return True, ""


# ── API ───────────────────────────────────────────────────────────────────────

async def fetchUserInfo(query):
    key   = getApiKey()
    isId  = str(query).lstrip("-").isdigit()
    param = query if isId else f"@{query}"
    url   = f"{TGOSINT_URL}?key={key}&q={param}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status != 200:
                    return None, f"HTTP {r.status}"
                return await r.json(), None
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        log.error("fetchUserInfo: %s", e)
        return None, "unreachable"


# ── Keyboards ─────────────────────────────────────────────────────────────────

def mainMenuKb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Tg  →  Num", callback_data="tgtonum")],
        [InlineKeyboardButton("My Stats",   callback_data="myStats")],
    ])

def afterResultKb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Look Up Another", callback_data="tgtonum")],
        [InlineKeyboardButton("My Stats",        callback_data="myStats")],
        [InlineKeyboardButton("Back to Menu",    callback_data="back_main")],
    ])

def adminDashboardKb():
    db  = loadDb()
    ml  = "Maintenance  ON" if db.get("maintenance") else "Maintenance  OFF"
    key = db.get("apiKey", TGOSINT_KEY_DEFAULT)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Users",          callback_data="adm_users_0"),
         InlineKeyboardButton("Lookups",        callback_data="adm_lookups_0")],
        [InlineKeyboardButton("Today",          callback_data="adm_today"),
         InlineKeyboardButton("Success Rate",   callback_data="adm_rate")],
        [InlineKeyboardButton("Recent Queries", callback_data="adm_recent"),
         InlineKeyboardButton("Broadcast",      callback_data="adm_broadcast")],
        [InlineKeyboardButton(f"API Key:  {key}", callback_data="adm_apikey")],
        [InlineKeyboardButton(ml,               callback_data="adm_maintenance")],
        [InlineKeyboardButton("Close",          callback_data="adm_close")],
    ])

def userManageKb(uid):
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    bl  = "Unban User" if u.get("banned") else "Ban User"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bl,                callback_data=f"usr_ban_{uid}"),
         InlineKeyboardButton("Set Cooldown",    callback_data=f"usr_cooldown_{uid}")],
        [InlineKeyboardButton("Set Daily Limit", callback_data=f"usr_limit_{uid}"),
         InlineKeyboardButton("Remove Limit",    callback_data=f"usr_rmlimit_{uid}")],
        [InlineKeyboardButton("Remove Cooldown", callback_data=f"usr_rmcooldown_{uid}"),
         InlineKeyboardButton("Add Note",        callback_data=f"usr_note_{uid}")],
        [InlineKeyboardButton("Lookup History",  callback_data=f"usr_history_{uid}_0"),
         InlineKeyboardButton("Message User",    callback_data=f"usr_msg_{uid}")],
        [InlineKeyboardButton("Back to Users",   callback_data="adm_users_0")],
    ])


# ── Formatters ────────────────────────────────────────────────────────────────

def sv(v):
    if v is None:
        return "null"
    s = str(v).strip()
    return s if s else "null"

def bf(v):
    if v is None:
        return "null"
    return "yes" if v else "no"

def buildResultMsg(data):
    p = data.get("phone_info", {}) or {}
    return (
        f"<b><u>USER PROFILE</u></b>\n"
        f"<code>{'━'*34}</code>\n"
        f"<b>Username</b>       <code>@{sv(data.get('username'))}</code>\n"
        f"<b>User ID</b>        <code>{sv(data.get('user_id'))}</code>\n"
        f"<b>First Name</b>     <code>{sv(data.get('first_name'))}</code>\n"
        f"<b>Last Name</b>      <code>{sv(data.get('last_name'))}</code>\n"
        f"<b>Full Name</b>      <code>{sv(data.get('full_name'))}</code>\n"
        f"<b>Bio</b>            <code>{sv(data.get('bio'))}</code>\n"
        f"<b>Status</b>         <code>{sv(data.get('status'))}</code>\n"
        f"<b>Last Online</b>    <code>{sv(data.get('was_online'))}</code>\n"
        f"<b>DC ID</b>          <code>{sv(data.get('dc_id'))}</code>\n"
        f"<b>Common Chats</b>   <code>{sv(data.get('common_chats_count'))}</code>\n"
        f"<b>Search Type</b>    <code>{sv(data.get('search_type'))}</code>\n"
        f"<b>Input Type</b>     <code>{sv(data.get('input_type'))}</code>\n"
        f"<b>Response Time</b>  <code>{sv(data.get('response_time'))}</code>\n"
        f"\n"
        f"<b><u>PHONE INFO</u></b>\n"
        f"<code>{'━'*34}</code>\n"
        f"<b>Number</b>         <tg-spoiler><code>{sv(p.get('number'))}</code></tg-spoiler>\n"
        f"<b>Country</b>        <code>{sv(p.get('country'))}</code>\n"
        f"<b>Country Code</b>   <code>{sv(p.get('country_code'))}</code>\n"
        f"\n"
        f"<b><u>ACCOUNT FLAGS</u></b>\n"
        f"<code>{'━'*34}</code>\n"
        f"<b>Bot</b>         <code>{bf(data.get('is_bot'))}</code>   "
        f"<b>Verified</b>    <code>{bf(data.get('is_verified'))}</code>\n"
        f"<b>Premium</b>     <code>{bf(data.get('is_premium'))}</code>   "
        f"<b>Scam</b>        <code>{bf(data.get('is_scam'))}</code>\n"
        f"<b>Fake</b>        <code>{bf(data.get('is_fake'))}</code>   "
        f"<b>Restricted</b>  <code>{bf(data.get('is_restricted'))}</code>\n"
        f"<b>Support</b>     <code>{bf(data.get('is_support'))}</code>   "
        f"<b>Contact</b>     <code>{bf(data.get('is_contact'))}</code>\n"
        f"<b>Mutual</b>      <code>{bf(data.get('is_mutual_contact'))}</code>\n"
        f"\n"
        f"<b><u>RESTRICTION REASON</u></b>\n"
        f"<code>{'━'*34}</code>\n"
        f"<code>{sv(data.get('restriction_reason'))}</code>\n"
        f"\n"
        f"<i>@drazeforce</i>"
    )

def buildAdminUserCard(uid, apiData=None):
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    if not u:
        return "<b>User not found.</b>"

    banned  = u.get("banned", False)
    cd      = u.get("cooldownUntil")
    lim     = u.get("dailyLimit")
    notes   = u.get("notes", [])
    joined  = u.get("joinedAt", "N/A")[:16].replace("T", "  ")
    seen    = u.get("lastSeen",  "N/A")[:16].replace("T", "  ")
    total   = u.get("totalLookups", 0)

    cdStr = "none"
    if cd:
        until = datetime.fromisoformat(cd)
        if datetime.now() < until:
            mins  = int((until - datetime.now()).total_seconds() / 60)
            cdStr = f"{mins} min remaining"
        else:
            cdStr = "expired"

    limStr    = f"{lim} / day" if lim is not None else "unlimited"
    statusStr = "BANNED" if banned else (
        "ON COOLDOWN" if cdStr not in ("none", "expired") else "active"
    )

    # ── Bot record ────────────────────────────────────────────────────────────
    lines = [
        f"<b><u>USER RECORD</u></b>\n"
        f"<code>{'━'*34}</code>\n"
        f"<b>Name</b>          <code>{sv(u.get('firstName'))} {sv(u.get('lastName',''))}</code>\n"
        f"<b>Username</b>      <code>@{sv(u.get('username'))}</code>\n"
        f"<b>User ID</b>       <code>{uid}</code>\n"
        f"<b>Bot Status</b>    <code>{statusStr}</code>\n"
        f"<b>Joined Bot</b>    <code>{joined}</code>\n"
        f"<b>Last Seen</b>     <code>{seen}</code>\n"
        f"<b>Total Lookups</b> <code>{total}</code>\n"
        f"<b>Daily Limit</b>   <code>{limStr}</code>\n"
        f"<b>Cooldown</b>      <code>{cdStr}</code>"
    ]

    # ── Live Telegram data from API ───────────────────────────────────────────
    if apiData:
        p = apiData.get("phone_info", {}) or {}
        lines.append(
            f"\n\n<b><u>TELEGRAM PROFILE</u></b>\n"
            f"<code>{'━'*34}</code>\n"
            f"<b>Username</b>      <code>@{sv(apiData.get('username'))}</code>\n"
            f"<b>First Name</b>    <code>{sv(apiData.get('first_name'))}</code>\n"
            f"<b>Last Name</b>     <code>{sv(apiData.get('last_name'))}</code>\n"
            f"<b>Full Name</b>     <code>{sv(apiData.get('full_name'))}</code>\n"
            f"<b>Bio</b>           <code>{sv(apiData.get('bio'))}</code>\n"
            f"<b>Status</b>        <code>{sv(apiData.get('status'))}</code>\n"
            f"<b>Last Online</b>   <code>{sv(apiData.get('was_online'))}</code>\n"
            f"<b>DC ID</b>         <code>{sv(apiData.get('dc_id'))}</code>\n"
            f"<b>Common Chats</b>  <code>{sv(apiData.get('common_chats_count'))}</code>\n"
            f"<b>Search Type</b>   <code>{sv(apiData.get('search_type'))}</code>\n"
            f"<b>Input Type</b>    <code>{sv(apiData.get('input_type'))}</code>\n"
            f"<b>Response Time</b> <code>{sv(apiData.get('response_time'))}</code>\n"
            f"\n"
            f"<b><u>PHONE INFO</u></b>\n"
            f"<code>{'━'*34}</code>\n"
            f"<b>Number</b>        <tg-spoiler><code>{sv(p.get('number'))}</code></tg-spoiler>\n"
            f"<b>Country</b>       <code>{sv(p.get('country'))}</code>\n"
            f"<b>Country Code</b>  <code>{sv(p.get('country_code'))}</code>\n"
            f"\n"
            f"<b><u>ACCOUNT FLAGS</u></b>\n"
            f"<code>{'━'*34}</code>\n"
            f"<b>Bot</b>        <code>{bf(apiData.get('is_bot'))}</code>   "
            f"<b>Verified</b>   <code>{bf(apiData.get('is_verified'))}</code>\n"
            f"<b>Premium</b>    <code>{bf(apiData.get('is_premium'))}</code>   "
            f"<b>Scam</b>       <code>{bf(apiData.get('is_scam'))}</code>\n"
            f"<b>Fake</b>       <code>{bf(apiData.get('is_fake'))}</code>   "
            f"<b>Restricted</b> <code>{bf(apiData.get('is_restricted'))}</code>\n"
            f"<b>Support</b>    <code>{bf(apiData.get('is_support'))}</code>   "
            f"<b>Contact</b>    <code>{bf(apiData.get('is_contact'))}</code>\n"
            f"<b>Mutual</b>     <code>{bf(apiData.get('is_mutual_contact'))}</code>\n"
            f"\n"
            f"<b><u>RESTRICTION REASON</u></b>\n"
            f"<code>{'━'*34}</code>\n"
            f"<code>{sv(apiData.get('restriction_reason'))}</code>"
        )
    else:
        lines.append(
            f"\n\n<b><u>TELEGRAM PROFILE</u></b>\n"
            f"<code>{'━'*34}</code>\n"
            f"<i>API returned no data for this user ID.</i>"
        )

    # ── Notes ─────────────────────────────────────────────────────────────────
    if notes:
        lines.append(f"\n\n<b><u>ADMIN NOTES</u></b>\n<code>{'━'*34}</code>")
        for i, n in enumerate(notes[-5:], 1):
            ts = n.get("ts","")[:10]
            lines.append(f"<code>{i}.</code>  <i>{n.get('text','')}</i>  <code>[{ts}]</code>")

    return "\n".join(lines)


# ── /start ────────────────────────────────────────────────────────────────────

async def cmdStart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u     = update.effective_user
    isNew = registerUser(u.id, u.username, u.first_name)
    db    = loadDb()

    if isNew:
        for aid in db.get("adminIds", []):
            try:
                await ctx.bot.send_message(
                    chat_id=aid,
                    text=(
                        f"<b><u>NEW USER JOINED</u></b>\n"
                        f"<code>{'━'*28}</code>\n"
                        f"<b>Name</b>      <code>{u.first_name or 'null'}</code>\n"
                        f"<b>Username</b>  <code>@{u.username or 'null'}</code>\n"
                        f"<b>User ID</b>   <code>{u.id}</code>\n"
                        f"<b>Time</b>      <code>{datetime.now().strftime('%Y-%m-%d  %H:%M')}</code>"
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    if db.get("maintenance"):
        await update.message.reply_text(
            "<b>Under Maintenance</b>\n\n<i>We will be back shortly.</i>",
            parse_mode=ParseMode.HTML
        )
        return

    name = u.first_name or "there"
    await update.message.reply_text(
        f"<b>Welcome, {name}</b>\n\n"
        f"<code>Telegram Username  →  Phone Lookup</code>\n\n"
        f"Retrieve detailed profile info and phone numbers\n"
        f"linked to any Telegram username.\n\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML, reply_markup=mainMenuKb()
    )


# ── /stats ────────────────────────────────────────────────────────────────────

async def cmdStats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    registerUser(u.id, u.username, u.first_name)
    s  = getUserStats(u.id)
    j  = s["joinedAt"][:10]  if s["joinedAt"]  != "N/A" else "N/A"
    ls = s["lastSeen"][:10]  if s["lastSeen"]  != "N/A" else "N/A"
    await update.message.reply_text(
        f"<b><u>YOUR STATS</u></b>\n"
        f"<code>{'━'*28}</code>\n"
        f"<b>Total Lookups</b>   <code>{s['total']}</code>\n"
        f"<b>Today</b>           <code>{s['today']}</code>\n"
        f"<b>Successful</b>      <code>{s['successful']}</code>\n"
        f"<b>Member Since</b>    <code>{j}</code>\n"
        f"<b>Last Active</b>     <code>{ls}</code>\n"
        f"<code>{'━'*28}</code>\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML, reply_markup=mainMenuKb()
    )


# ── /admin ────────────────────────────────────────────────────────────────────

async def cmdAdmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db  = loadDb()
    if uid in db.get("adminIds", []):
        stats = getAdminStats()
        await update.message.reply_text(
            _dashboardText(stats), parse_mode=ParseMode.HTML,
            reply_markup=adminDashboardKb()
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "<b>Admin Access</b>\n\n<i>Enter the admin password.</i>",
        parse_mode=ParseMode.HTML
    )
    return AWAIT_ADMIN_PW

def _dashboardText(stats):
    return (
        f"<b><u>ADMIN DASHBOARD</u></b>\n"
        f"<code>{'━'*28}</code>\n\n"
        f"<b>Users</b>          <code>{stats['totalUsers']}</code>   "
        f"<b>Banned</b>  <code>{stats['bannedCount']}</code>\n"
        f"<b>Total Lookups</b>  <code>{stats['totalLookups']}</code>\n"
        f"<b>Today</b>          <code>{stats['todayLookups']}</code>\n"
        f"<b>Success Rate</b>   <code>{stats['successRate']}%</code>\n\n"
        f"<code>{'━'*28}</code>"
    )

async def receiveAdminPw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    h       = hashlib.sha256(entered.encode()).hexdigest()
    try:
        await update.message.delete()
    except Exception:
        pass
    if h != ADMIN_PASS_HASH:
        await update.message.reply_text(
            "<b>Incorrect password.</b>\n<i>Access denied.</i>",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    uid = update.effective_user.id
    db  = loadDb()
    if uid not in db["adminIds"]:
        db["adminIds"].append(uid)
    db["adminSessions"].append({"userId": uid, "ts": datetime.now().isoformat()})
    saveDb(db)
    stats = getAdminStats()
    await update.message.reply_text(
        _dashboardText(stats), parse_mode=ParseMode.HTML,
        reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


# ── Dashboard callback ────────────────────────────────────────────────────────

async def cbAdminDashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    stats = getAdminStats()
    await q.edit_message_text(
        _dashboardText(stats), parse_mode=ParseMode.HTML,
        reply_markup=adminDashboardKb()
    )

async def cbAdminClose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.delete_message()


# ── API Key ───────────────────────────────────────────────────────────────────

async def cbAdminApiKey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    cur = getApiKey()
    await q.edit_message_text(
        f"<b><u>CHANGE API KEY</u></b>\n"
        f"<code>{'━'*28}</code>\n\n"
        f"<b>Current Key</b>  <code>{cur}</code>\n\n"
        f"<i>Send the new key as a plain message.\n"
        f"Bot will immediately use it for all future requests.</i>\n\n"
        f"<code>https://tgosint.vercel.app/?key=NEWKEY&amp;q=@user</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="adm_dashboard")]
        ])
    )
    return AWAIT_API_KEY

async def receiveApiKey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    newKey = update.message.text.strip()
    if not newKey or len(newKey) < 2:
        await update.message.reply_text(
            "<b>Invalid key.</b>  Try again.", parse_mode=ParseMode.HTML
        )
        return AWAIT_API_KEY
    db     = loadDb()
    oldKey = db.get("apiKey", TGOSINT_KEY_DEFAULT)
    db["apiKey"] = newKey
    saveDb(db)
    await update.message.reply_text(
        f"<b><u>API KEY UPDATED</u></b>\n"
        f"<code>{'━'*28}</code>\n\n"
        f"<b>Old</b>  <code>{oldKey}</code>\n"
        f"<b>New</b>  <code>{newKey}</code>\n\n"
        f"<i>All future requests use the new key.</i>",
        parse_mode=ParseMode.HTML, reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


# ── Users list ────────────────────────────────────────────────────────────────

async def cbAdminUsers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    page = int(q.data.split("_")[-1]) if q.data.split("_")[-1].isdigit() else 0
    db   = loadDb()
    all_ = sorted(db["users"].values(), key=lambda u: u.get("lastSeen",""), reverse=True)
    pp   = 5
    tot  = len(all_)
    chunk = all_[page*pp : page*pp+pp]

    lines = [
        f"<b><u>ALL USERS</u></b>  "
        f"<i>page {page+1} / {max(1,(tot+pp-1)//pp)}</i>\n"
        f"<code>{'━'*28}</code>"
    ]
    kb = []
    for u in chunk:
        j   = u.get("joinedAt","")[:10]
        tag = "  BANNED" if u.get("banned") else ""
        cd  = u.get("cooldownUntil")
        if cd and datetime.now() < datetime.fromisoformat(cd):
            tag = "  COOLDOWN"
        lines.append(
            f"\n<b>{sv(u.get('firstName'))}</b>  "
            f"<code>@{sv(u.get('username'))}</code>{tag}\n"
            f"<code>{u.get('userId','?')}</code>  |  "
            f"Lookups: <code>{u.get('totalLookups',0)}</code>  |  "
            f"Joined: <code>{j}</code>"
        )
        kb.append([InlineKeyboardButton(
            f"{sv(u.get('firstName'))}  (@{sv(u.get('username'))})",
            callback_data=f"usr_view_{u.get('userId')}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"adm_users_{page-1}"))
    if (page+1)*pp < tot:
        nav.append(InlineKeyboardButton("Next", callback_data=f"adm_users_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("Back", callback_data="adm_dashboard")])

    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ── User card ─────────────────────────────────────────────────────────────────

async def cbUserView(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    await q.edit_message_text(
        "<i>Fetching live Telegram profile...</i>", parse_mode=ParseMode.HTML
    )
    apiData, _ = await fetchUserInfo(str(uid))
    card       = buildAdminUserCard(uid, apiData=apiData)
    await q.message.edit_text(
        card, parse_mode=ParseMode.HTML, reply_markup=userManageKb(uid)
    )


# ── Ban ───────────────────────────────────────────────────────────────────────

async def cbUserBan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    u["banned"] = not u.get("banned", False)
    db["users"][str(uid)] = u
    saveDb(db)
    action = "BANNED" if u["banned"] else "UNBANNED"
    try:
        await ctx.bot.send_message(
            chat_id=uid,
            text=(
                "<b>Account Update</b>\n\n"
                f"<i>Your access has been "
                f"{'suspended' if u['banned'] else 'reinstated'}.</i>"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    await q.edit_message_text(
        f"<b>User {action}</b>\n<code>{uid}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
        ])
    )


# ── Cooldown ──────────────────────────────────────────────────────────────────

async def cbUserCooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    ctx.user_data["cooldownTarget"] = uid
    await q.edit_message_text(
        f"<b>Set Cooldown</b>\n\nUser ID: <code>{uid}</code>\n\n"
        f"<i>Enter duration in minutes.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data=f"usr_view_{uid}")]
        ])
    )
    return AWAIT_COOLDOWN_MIN

async def receiveCooldownMin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    uid = ctx.user_data.get("cooldownTarget")
    if not uid:
        return ConversationHandler.END
    try:
        mins = int(raw)
        if mins <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "<b>Invalid.</b>  Enter a positive number of minutes.",
            parse_mode=ParseMode.HTML
        )
        return AWAIT_COOLDOWN_MIN
    until = datetime.now() + timedelta(minutes=mins)
    db    = loadDb()
    db["users"][str(uid)]["cooldownUntil"] = until.isoformat()
    saveDb(db)
    try:
        await ctx.bot.send_message(
            chat_id=uid,
            text=(
                f"<b>Cooldown Applied</b>\n\n"
                f"<i>You are on cooldown for <b>{mins} minutes</b>.\n"
                f"Lookups paused until then.</i>"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"<b>Cooldown Set</b>\n\n<code>{uid}</code>  →  <code>{mins} minutes</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
        ])
    )
    return ConversationHandler.END

async def cbUserRemoveCooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    db  = loadDb()
    if str(uid) in db["users"]:
        db["users"][str(uid)]["cooldownUntil"] = None
        saveDb(db)
    await q.edit_message_text(
        f"<b>Cooldown Removed</b>\n<code>{uid}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
        ])
    )


# ── Daily limit ───────────────────────────────────────────────────────────────

async def cbUserSetLimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    ctx.user_data["limitTarget"] = uid
    await q.edit_message_text(
        f"<b>Set Daily Limit</b>\n\nUser ID: <code>{uid}</code>\n\n"
        f"<i>Enter max lookups allowed per day.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data=f"usr_view_{uid}")]
        ])
    )
    return AWAIT_LIMIT_VAL

async def receiveLimitVal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    uid = ctx.user_data.get("limitTarget")
    if not uid:
        return ConversationHandler.END
    try:
        lim = int(raw)
        if lim <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "<b>Invalid.</b>  Enter a positive number.", parse_mode=ParseMode.HTML
        )
        return AWAIT_LIMIT_VAL
    db = loadDb()
    db["users"][str(uid)]["dailyLimit"] = lim
    saveDb(db)
    await update.message.reply_text(
        f"<b>Daily Limit Set</b>\n\n<code>{uid}</code>  →  <code>{lim} / day</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
        ])
    )
    return ConversationHandler.END

async def cbUserRemoveLimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    db  = loadDb()
    if str(uid) in db["users"]:
        db["users"][str(uid)]["dailyLimit"] = None
        saveDb(db)
    await q.edit_message_text(
        f"<b>Daily Limit Removed</b>\n<code>{uid}</code>  →  unlimited",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
        ])
    )


# ── Notes ─────────────────────────────────────────────────────────────────────

async def cbUserNote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    ctx.user_data["noteTarget"] = uid
    await q.edit_message_text(
        f"<b>Add Note</b>\n\nUser ID: <code>{uid}</code>\n\n"
        f"<i>Type your private note.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data=f"usr_view_{uid}")]
        ])
    )
    return AWAIT_NOTE_TEXT

async def receiveNoteText(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = ctx.user_data.get("noteTarget")
    if not uid:
        return ConversationHandler.END
    text = update.message.text.strip()
    db   = loadDb()
    if str(uid) in db["users"]:
        db["users"][str(uid)]["notes"].append({
            "text": text, "ts": datetime.now().isoformat(),
            "by":   update.effective_user.id,
        })
        saveDb(db)
    await update.message.reply_text(
        f"<b>Note Saved</b>\n\n<i>{text}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
        ])
    )
    return ConversationHandler.END


# ── History ───────────────────────────────────────────────────────────────────

async def cbUserHistory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    uid   = int(parts[3])
    page  = int(parts[4]) if len(parts) > 4 else 0

    db   = loadDb()
    u    = db["users"].get(str(uid), {})
    hist = list(reversed(u.get("lookupHistory", [])))
    pp   = 8
    tot  = len(hist)
    chunk = hist[page*pp : page*pp+pp]

    lines = [
        f"<b><u>LOOKUP HISTORY</u></b>  <code>{uid}</code>\n"
        f"<i>page {page+1} / {max(1,(tot+pp-1)//pp)}</i>\n"
        f"<code>{'━'*28}</code>"
    ]
    if not chunk:
        lines.append("\n<i>No history yet.</i>")
    for e in chunk:
        ts = e.get("ts","")[:16].replace("T","  ")
        ok = "ok" if e.get("success") else "fail"
        lines.append(
            f"\n<code>{ts}</code>  <b>@{sv(e.get('query'))}</b>  <code>[{ok}]</code>"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"usr_history_{uid}_{page-1}"))
    if (page+1)*pp < tot:
        nav.append(InlineKeyboardButton("Next", callback_data=f"usr_history_{uid}_{page+1}"))
    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")])
    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ── Message user ──────────────────────────────────────────────────────────────

async def cbUserMsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    ctx.user_data["msgTarget"] = uid
    await q.edit_message_text(
        f"<b>Message User</b>\n\n<code>{uid}</code>\n\n"
        f"<i>Type the message to send privately.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data=f"usr_view_{uid}")]
        ])
    )
    return AWAIT_MSG_USER_TEXT

async def receiveMsgUserText(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = ctx.user_data.get("msgTarget")
    if not uid:
        return ConversationHandler.END
    text = update.message.text.strip()
    try:
        await ctx.bot.send_message(
            chat_id=uid,
            text=(
                f"<b>Message from Admin</b>\n"
                f"<code>{'━'*28}</code>\n\n"
                f"{text}\n\n<i>@drazeforce</i>"
            ),
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(
            f"<b>Delivered</b>\n<code>{uid}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
            ])
        )
    except Exception as e:
        await update.message.reply_text(
            f"<b>Failed</b>\n<code>{e}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
            ])
        )
    return ConversationHandler.END


# ── Lookups list ──────────────────────────────────────────────────────────────

async def cbAdminLookups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    page = int(q.data.split("_")[-1]) if q.data.split("_")[-1].isdigit() else 0
    db   = loadDb()
    all_ = list(reversed(db["lookups"]))
    pp   = 5
    tot  = len(all_)
    chunk = all_[page*pp : page*pp+pp]

    lines = [
        f"<b><u>ALL LOOKUPS</u></b>  "
        f"<i>page {page+1} / {max(1,(tot+pp-1)//pp)}</i>\n"
        f"<code>{'━'*28}</code>"
    ]
    for l in chunk:
        ts = l.get("ts","")[:16].replace("T","  ")
        ok = "ok" if l.get("success") else "fail"
        lines.append(
            f"\n<b>@{sv(l.get('query'))}</b>  <code>[{ok}]</code>\n"
            f"<code>{sv(l.get('firstName'))}</code>  <code>@{sv(l.get('username'))}</code>\n"
            f"Phone: <tg-spoiler><code>{sv(l.get('phone'))}</code></tg-spoiler>  "
            f"Country: <code>{sv(l.get('country'))}</code>\n"
            f"<code>{ts}</code>"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"adm_lookups_{page-1}"))
    if (page+1)*pp < tot:
        nav.append(InlineKeyboardButton("Next", callback_data=f"adm_lookups_{page+1}"))
    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("Back", callback_data="adm_dashboard")])
    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ── Today ─────────────────────────────────────────────────────────────────────

async def cbAdminToday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    db = loadDb()
    ts = date.today().isoformat()
    tl = [l for l in db["lookups"] if l["ts"].startswith(ts)]
    sc = len([l for l in tl if l["success"]])
    uq = len(set(l["userId"] for l in tl))

    lines = [
        f"<b><u>TODAY</u></b>\n<code>{'━'*28}</code>\n",
        f"<b>Total</b>         <code>{len(tl)}</code>",
        f"<b>Unique Users</b>  <code>{uq}</code>",
        f"<b>Successful</b>    <code>{sc}</code>",
        f"<b>Failed</b>        <code>{len(tl)-sc}</code>\n",
        f"<code>{'━'*28}</code>",
    ]
    for l in list(reversed(tl))[:10]:
        t  = l.get("ts","")[11:16]
        ok = "ok" if l.get("success") else "fail"
        lines.append(
            f"\n<code>{t}</code>  <b>@{sv(l.get('query'))}</b>  "
            f"<code>[{ok}]</code>\n<i>{sv(l.get('firstName'))}</i>"
        )
    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="adm_dashboard")]
        ])
    )


# ── Rate ──────────────────────────────────────────────────────────────────────

async def cbAdminRate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    db = loadDb()
    tot  = len(db["lookups"])
    succ = len([l for l in db["lookups"] if l["success"]])
    rate = round(succ / tot * 100, 1) if tot else 0

    topU = {}
    for l in db["lookups"]:
        uid = l.get("userId")
        topU[uid] = topU.get(uid, 0) + 1
    top5 = sorted(topU.items(), key=lambda x: x[1], reverse=True)[:5]

    lines = [
        f"<b><u>SUCCESS RATE</u></b>\n<code>{'━'*28}</code>\n",
        f"<b>Total</b>    <code>{tot}</code>",
        f"<b>Success</b>  <code>{succ}</code>",
        f"<b>Failed</b>   <code>{tot-succ}</code>",
        f"<b>Rate</b>     <code>{rate}%</code>\n",
        f"<code>{'━'*28}</code>\n<b>Top Users</b>",
    ]
    for uid, count in top5:
        info = db["users"].get(str(uid), {})
        lines.append(
            f"<code>{sv(info.get('firstName'))}</code>  "
            f"<code>@{sv(info.get('username'))}</code>  "
            f"<code>{count} lookups</code>"
        )
    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="adm_dashboard")]
        ])
    )


# ── Recent ────────────────────────────────────────────────────────────────────

async def cbAdminRecent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    db     = loadDb()
    recent = list(reversed(db["lookups"][-20:]))
    lines  = [f"<b><u>RECENT QUERIES</u></b>\n<code>{'━'*28}</code>"]
    for l in recent:
        ts = l.get("ts","")[:16].replace("T","  ")
        ok = "ok" if l.get("success") else "fail"
        lines.append(
            f"\n<code>{ts}</code>\n"
            f"<b>@{sv(l.get('query'))}</b>  <code>[{ok}]</code>\n"
            f"<i>{sv(l.get('firstName'))}</i>  <code>@{sv(l.get('username'))}</code>"
        )
    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="adm_dashboard")]
        ])
    )


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def cbAdminBroadcastPrompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["awaitingBroadcast"] = True
    await q.edit_message_text(
        "<b>Broadcast</b>\n\n<i>Send the message to push to all users.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="adm_dashboard")]
        ])
    )
    return AWAIT_BROADCAST

async def receiveBroadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaitingBroadcast"):
        return ConversationHandler.END
    ctx.user_data["awaitingBroadcast"] = False
    db   = loadDb()
    text = update.message.text
    sent = failed = 0
    for uid in db["users"]:
        try:
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=(
                    f"<b>Announcement</b>\n"
                    f"<code>{'━'*28}</code>\n\n"
                    f"{text}\n\n<i>@drazeforce</i>"
                ),
                parse_mode=ParseMode.HTML
            )
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"<b>Broadcast Complete</b>\n\n"
        f"<b>Sent</b>    <code>{sent}</code>\n"
        f"<b>Failed</b>  <code>{failed}</code>",
        parse_mode=ParseMode.HTML, reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


# ── Maintenance ───────────────────────────────────────────────────────────────

async def cbMaintenanceToggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    db  = loadDb()
    cur = db.get("maintenance", False)
    ctx.user_data["pendingMaintenance"] = not cur
    act = "disable" if cur else "enable"
    await q.edit_message_text(
        f"<b>Maintenance Mode</b>\n\n"
        f"About to <b>{act}</b> maintenance.\n\n"
        f"<i>Enter the maintenance password to confirm.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="adm_dashboard")]
        ])
    )
    return AWAIT_MAINT_PW

async def receiveMaintPw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    h       = hashlib.sha256(entered.encode()).hexdigest()
    try:
        await update.message.delete()
    except Exception:
        pass
    if h != MAINT_PASS_HASH:
        await update.message.reply_text(
            "<b>Incorrect password.</b>\n<i>State unchanged.</i>",
            parse_mode=ParseMode.HTML, reply_markup=adminDashboardKb()
        )
        return ConversationHandler.END
    newState = ctx.user_data.pop("pendingMaintenance", False)
    db = loadDb()
    db["maintenance"] = newState
    saveDb(db)
    label = "ENABLED" if newState else "DISABLED"
    await update.message.reply_text(
        f"<b>Maintenance {label}</b>\n\n"
        f"<i>{'Users will see a maintenance message.' if newState else 'Bot is back online.'}</i>",
        parse_mode=ParseMode.HTML, reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


# ── User-facing callbacks ─────────────────────────────────────────────────────

async def cbBackMain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    name = update.effective_user.first_name or "there"
    await q.edit_message_text(
        f"<b>Welcome, {name}</b>\n\n"
        f"<code>Telegram Username  →  Phone Lookup</code>\n\n"
        f"Retrieve detailed profile info and phone numbers\n"
        f"linked to any Telegram username.\n\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML, reply_markup=mainMenuKb()
    )
    return ConversationHandler.END

async def cbTgToNum(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "<b>Lookup</b>\n\n"
        "Send a username  <i>or</i>  forward a message from the target.\n\n"
        "<i>Example:  drazeforce  or  @drazeforce</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="back_main")]
        ])
    )
    return AWAIT_INPUT

async def cbMyStats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    u  = update.effective_user
    s  = getUserStats(u.id)
    j  = s["joinedAt"][:10] if s["joinedAt"] != "N/A" else "N/A"
    ls = s["lastSeen"][:10] if s["lastSeen"] != "N/A" else "N/A"
    await q.edit_message_text(
        f"<b><u>YOUR STATS</u></b>\n"
        f"<code>{'━'*28}</code>\n"
        f"<b>Total Lookups</b>   <code>{s['total']}</code>\n"
        f"<b>Today</b>           <code>{s['today']}</code>\n"
        f"<b>Successful</b>      <code>{s['successful']}</code>\n"
        f"<b>Member Since</b>    <code>{j}</code>\n"
        f"<b>Last Active</b>     <code>{ls}</code>\n"
        f"<code>{'━'*28}</code>\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Menu", callback_data="back_main")]
        ])
    )


# ── Lookup flow ───────────────────────────────────────────────────────────────

async def receiveInput(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    msg = update.message
    q   = None

    db = loadDb()
    if db.get("maintenance"):
        await msg.reply_text(
            "<b>Under Maintenance</b>\n\n<i>Please check back shortly.</i>",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    allowed, reason = checkUserAccess(u.id)
    if not allowed:
        if reason == "banned":
            await msg.reply_text(
                "<b>Access Denied</b>\n\n"
                "<i>Your account has been suspended.\n"
                "Contact @drazeforce if you believe this is an error.</i>",
                parse_mode=ParseMode.HTML
            )
        elif reason.startswith("cooldown:"):
            mins = reason.split(":")[1]
            await msg.reply_text(
                f"<b>Cooldown Active</b>\n\n"
                f"<i><b>{mins} minutes</b> remaining before your next lookup.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Menu", callback_data="back_main")]
                ])
            )
        elif reason.startswith("limit:"):
            lim = reason.split(":")[1]
            await msg.reply_text(
                f"<b>Daily Limit Reached</b>\n\n"
                f"<i>Your limit of <b>{lim} lookups</b> per day has been reached.\n"
                f"Resets at midnight.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Menu", callback_data="back_main")]
                ])
            )
        return ConversationHandler.END

    if getattr(msg, "forward_origin", None):
        fwd = msg.forward_origin
        if hasattr(fwd, "sender_user") and fwd.sender_user:
            q = fwd.sender_user.username or str(fwd.sender_user.id)
        elif hasattr(fwd, "chat") and fwd.chat:
            q = fwd.chat.username or str(fwd.chat.id)
        if not q:
            await msg.reply_text(
                "<b>Could Not Extract Identity</b>\n\n"
                "<i>This user has hidden their identity in forwards.\n"
                "Enter their username or user ID manually.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Cancel", callback_data="back_main")]
                ])
            )
            return AWAIT_INPUT
    else:
        raw  = msg.text.strip().lstrip("@") if msg.text else ""
        isN  = raw.lstrip("-").isdigit()
        if isN:
            q = raw
        else:
            if not raw or len(raw) < 3 or len(raw) > 32 or \
               not all(c.isalnum() or c == "_" for c in raw):
                await msg.reply_text(
                    "<b>Invalid Input</b>\n\n"
                    "<i>Send a username (3–32 chars) or a numeric user ID.</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Cancel", callback_data="back_main")]
                    ])
                )
                return AWAIT_INPUT
            q = raw

    # ── Protected usernames — block lookup silently ───────────────────────────
    PROTECTED = {"drazeforce", "drazeX"}
    if str(q).lower() in {p.lower() for p in PROTECTED}:
        await msg.reply_text(
            "<b>Lookup Blocked</b>\n\n"
            "<i>This username is protected and cannot be looked up.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=afterResultKb()
        )
        return ConversationHandler.END
    # ─────────────────────────────────────────────────────────────────────────

    dq   = f"@{q}" if not str(q).lstrip("-").isdigit() else q
    wait = await msg.reply_text(
        f"<b>Looking up</b>  <code>{dq}</code>\n<i>Please wait...</i>",
        parse_mode=ParseMode.HTML
    )
    data, err = await fetchUserInfo(q)
    await wait.delete()

    if err == "timeout":
        await msg.reply_text(
            "<b>Request Timed Out</b>\n\n<i>Try again in a moment.</i>",
            parse_mode=ParseMode.HTML, reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, q, None, False)
        return ConversationHandler.END

    if err or data is None:
        await msg.reply_text(
            "<b>Service Unavailable</b>\n\n<i>The lookup API is unreachable.</i>",
            parse_mode=ParseMode.HTML, reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, q, None, False)
        return ConversationHandler.END

    pi = data.get("phone_info", {})
    if not pi.get("success"):
        reason = pi.get("message", "Could not fetch details")
        await msg.reply_text(
            f"<b>Lookup Failed</b>\n\n<code>{reason}</code>\n\n"
            f"<i>{dq} may not exist or has no linked number.</i>",
            parse_mode=ParseMode.HTML, reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, q, data, False)
        return ConversationHandler.END

    text = buildResultMsg(data)
    pfp  = data.get("profile_pic")
    if pfp:
        try:
            pm = await msg.reply_photo(photo=pfp)
            await pm.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=afterResultKb())
        except Exception:
            await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=afterResultKb())
    else:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=afterResultKb())

    logLookup(u.id, u.username, u.first_name, q, data, True)
    return ConversationHandler.END


# ── Inline ────────────────────────────────────────────────────────────────────

async def inlineQuery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query.query.strip().lstrip("@")
    if not q or len(q) < 3:
        await update.inline_query.answer([], cache_time=0)
        return
    await update.inline_query.answer([
        InlineQueryResultArticle(
            id=q, title=f"Look up @{q}",
            description="Tap to fetch profile and phone info",
            input_message_content=InputTextMessageContent(
                f"<b>Lookup initiated for</b> <code>@{q}</code>\n\n"
                f"<i>Open the bot to see the result.</i>",
                parse_mode=ParseMode.HTML
            ),
        )
    ], cache_time=0)


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<i>Use the menu to navigate.</i>",
        parse_mode=ParseMode.HTML, reply_markup=mainMenuKb()
    )

async def errorHandler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Update %s caused error: %s", update, ctx.error)


# ── Keep-alive ────────────────────────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, fmt, *args):
        pass

def startHealthServer():
    port   = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    log.info("Health server on port %d", port)
    server.serve_forever()

def startSelfPing():
    time.sleep(15)
    url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not url:
        return
    log.info("Self-ping → %s", url)
    while True:
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception as e:
            log.warning("Self-ping failed: %s", e)
        time.sleep(300)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    adminConv = ConversationHandler(
        entry_points=[CommandHandler("admin", cmdAdmin)],
        states={
            AWAIT_ADMIN_PW:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveAdminPw)],
            AWAIT_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveBroadcast)],
            AWAIT_MAINT_PW:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveMaintPw)],
        },
        fallbacks=[CommandHandler("start", cmdStart)],
        allow_reentry=True, per_message=False,
    )
    lookupConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbTgToNum, pattern="^tgtonum$")],
        states={
            AWAIT_INPUT: [MessageHandler(
                (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, receiveInput
            )],
        },
        fallbacks=[CallbackQueryHandler(cbBackMain, pattern="^back_main$")],
        allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    maintConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbMaintenanceToggle, pattern="^adm_maintenance$")],
        states={
            AWAIT_MAINT_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveMaintPw)],
        },
        fallbacks=[CallbackQueryHandler(cbAdminDashboard, pattern="^adm_dashboard$")],
        allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    apiKeyConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbAdminApiKey, pattern="^adm_apikey$")],
        states={
            AWAIT_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveApiKey)],
        },
        fallbacks=[CallbackQueryHandler(cbAdminDashboard, pattern="^adm_dashboard$")],
        allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    cooldownConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbUserCooldown, pattern="^usr_cooldown_")],
        states={
            AWAIT_COOLDOWN_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveCooldownMin)],
        },
        fallbacks=[], allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    limitConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbUserSetLimit, pattern="^usr_limit_")],
        states={
            AWAIT_LIMIT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveLimitVal)],
        },
        fallbacks=[], allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    noteConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbUserNote, pattern="^usr_note_")],
        states={
            AWAIT_NOTE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveNoteText)],
        },
        fallbacks=[], allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    msgConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbUserMsg, pattern="^usr_msg_")],
        states={
            AWAIT_MSG_USER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveMsgUserText)],
        },
        fallbacks=[], allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )

    app.add_error_handler(errorHandler)

    for conv in [adminConv, lookupConv, maintConv, apiKeyConv,
                 cooldownConv, limitConv, noteConv, msgConv]:
        app.add_handler(conv)

    app.add_handler(CommandHandler("start", cmdStart))
    app.add_handler(CommandHandler("stats", cmdStats))
    app.add_handler(CommandHandler("admin", cmdAdmin))
    app.add_handler(InlineQueryHandler(inlineQuery))

    app.add_handler(CallbackQueryHandler(cbBackMain,             pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cbMyStats,              pattern="^myStats$"))
    app.add_handler(CallbackQueryHandler(cbAdminDashboard,       pattern="^adm_dashboard$"))
    app.add_handler(CallbackQueryHandler(cbAdminUsers,           pattern="^adm_users_"))
    app.add_handler(CallbackQueryHandler(cbAdminLookups,         pattern="^adm_lookups_"))
    app.add_handler(CallbackQueryHandler(cbAdminToday,           pattern="^adm_today$"))
    app.add_handler(CallbackQueryHandler(cbAdminRate,            pattern="^adm_rate$"))
    app.add_handler(CallbackQueryHandler(cbAdminRecent,          pattern="^adm_recent$"))
    app.add_handler(CallbackQueryHandler(cbAdminBroadcastPrompt, pattern="^adm_broadcast$"))
    app.add_handler(CallbackQueryHandler(cbAdminClose,           pattern="^adm_close$"))
    app.add_handler(CallbackQueryHandler(cbUserView,             pattern="^usr_view_"))
    app.add_handler(CallbackQueryHandler(cbUserBan,              pattern="^usr_ban_"))
    app.add_handler(CallbackQueryHandler(cbUserRemoveCooldown,   pattern="^usr_rmcooldown_"))
    app.add_handler(CallbackQueryHandler(cbUserRemoveLimit,      pattern="^usr_rmlimit_"))
    app.add_handler(CallbackQueryHandler(cbUserHistory,          pattern="^usr_history_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    threading.Thread(target=startHealthServer, daemon=True).start()
    threading.Thread(target=startSelfPing,     daemon=True).start()

    log.info("Bot running")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()