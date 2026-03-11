import logging
import asyncio
import aiohttp #type: ignore
import json
import os
import hashlib
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, timedelta
from dotenv import load_dotenv #type: ignore
from telegram import ( #type: ignore
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import ( #type: ignore
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    InlineQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.constants import ParseMode #type: ignore

load_dotenv()

BOT_TOKEN            = os.getenv("BOT_TOKEN", "")
ADMIN_PASS_HASH      = hashlib.sha256(os.getenv("ADMIN_PASSWORD", "pootilangaadi").encode()).hexdigest()
MAINT_PASS_HASH      = hashlib.sha256("pari".encode()).hexdigest()
TGOSINT_URL          = "https://tgosint.vercel.app/"
TGOSINT_KEY_DEFAULT  = "shit"
HARDCODED_ADMIN_ID   = 961369378
DB_FILE              = "db.json"

# ── States ────────────────────────────────────────────────────────────────────
AWAIT_INPUT          = 1
AWAIT_ADMIN_PW       = 2
AWAIT_BROADCAST      = 3
AWAIT_MAINT_PW       = 4
AWAIT_COOLDOWN_MIN   = 5
AWAIT_LIMIT_VAL      = 6
AWAIT_NOTE_TEXT      = 7
AWAIT_API_KEY        = 8
AWAIT_CONTACT_MSG    = 9
AWAIT_ADMIN_REPLY    = 10
AWAIT_USER_ID_INPUT  = 11

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ── DB ────────────────────────────────────────────────────────────────────────

def loadDb():
    if not os.path.exists(DB_FILE):
        return {
            "users": {}, "lookups": [], "adminSessions": [],
            "maintenance": False, "adminIds": [HARDCODED_ADMIN_ID],
            "apiKey": TGOSINT_KEY_DEFAULT, "inbox": [],
        }
    with open(DB_FILE, "r") as f:
        data = json.load(f)
    defaults = {
        "maintenance": False,
        "adminIds": [HARDCODED_ADMIN_ID],
        "apiKey": TGOSINT_KEY_DEFAULT,
        "inbox": [],
    }
    for k, v in defaults.items():
        if k not in data:
            data[k] = v
    if HARDCODED_ADMIN_ID not in data["adminIds"]:
        data["adminIds"].append(HARDCODED_ADMIN_ID)
    return data

def saveDb(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

def getApiKey():
    return loadDb().get("apiKey", TGOSINT_KEY_DEFAULT)

def isAdmin(userId):
    db = loadDb()
    return userId in db.get("adminIds", [HARDCODED_ADMIN_ID])

def registerUser(userId, username, firstName):
    db    = loadDb()
    uid   = str(userId)
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
            "chatSession": False,
        }
    else:
        db["users"][uid]["lastSeen"] = datetime.now().isoformat()
        if username:
            db["users"][uid]["username"] = username
        for k, v in [
            ("banned", False), ("cooldownUntil", None),
            ("dailyLimit", None), ("notes", []),
            ("lookupHistory", []), ("chatSession", False),
        ]:
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
    unread = len([m for m in db.get("inbox", []) if not m.get("read")])
    return {
        "totalUsers":   len(db["users"]),
        "totalLookups": tot,
        "todayLookups": len([l for l in db["lookups"] if l["ts"].startswith(ts)]),
        "successRate":  round(len(succ) / tot * 100, 1) if tot else 0,
        "bannedCount":  len([u for u in db["users"].values() if u.get("banned")]),
        "apiKey":       db.get("apiKey", TGOSINT_KEY_DEFAULT),
        "unreadInbox":  unread,
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

# ── Inbox helpers ─────────────────────────────────────────────────────────────

def addInboxMessage(fromId, fromName, fromUsername, text, msgType="message"):
    db  = loadDb()
    mid = f"{fromId}_{int(time.time())}"
    db["inbox"].append({
        "id":          mid,
        "fromId":      fromId,
        "fromName":    fromName,
        "fromUsername": fromUsername or "",
        "text":        text,
        "type":        msgType,
        "read":        False,
        "ts":          datetime.now().isoformat(),
        "replies":     [],
    })
    saveDb(db)
    return mid

def getInboxMessage(mid):
    db = loadDb()
    for m in db.get("inbox", []):
        if m["id"] == mid:
            return m
    return None

def markInboxRead(mid):
    db = loadDb()
    for m in db["inbox"]:
        if m["id"] == mid:
            m["read"] = True
    saveDb(db)

def deleteInboxMessage(mid):
    db = loadDb()
    db["inbox"] = [m for m in db["inbox"] if m["id"] != mid]
    saveDb(db)

def addInboxReply(mid, text, fromAdmin=True):
    db = loadDb()
    for m in db["inbox"]:
        if m["id"] == mid:
            m["replies"].append({
                "text":      text,
                "fromAdmin": fromAdmin,
                "ts":        datetime.now().isoformat(),
            })
    saveDb(db)


# ── API ───────────────────────────────────────────────────────────────────────

async def fetchUserInfo(query):
    key   = getApiKey()
    param = f"@{query}" if not str(query).lstrip("-").isdigit() else query
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

def timeAgo(isoStr):
    try:
        dt   = datetime.fromisoformat(isoStr)
        diff = datetime.now() - dt
        s    = int(diff.total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s//60} min ago"
        if s < 86400:
            return f"{s//3600} hr ago"
        return f"{s//86400} days ago"
    except Exception:
        return "unknown"

def buildResultMsg(data):
    p = data.get("phone_info", {}) or {}
    return (
        f"<b>USER PROFILE</b>\n"
        f"<code>────────────────────────</code>\n"
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
        f"<b>PHONE INFO</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Number</b>         <tg-spoiler><code>{sv(p.get('number'))}</code></tg-spoiler>\n"
        f"<b>Country</b>        <code>{sv(p.get('country'))}</code>\n"
        f"<b>Country Code</b>   <code>{sv(p.get('country_code'))}</code>\n"
        f"\n"
        f"<b>FLAGS</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Premium</b>     <code>{bf(data.get('is_premium'))}</code>\n"
        f"<b>Verified</b>    <code>{bf(data.get('is_verified'))}</code>\n"
        f"<b>Scam</b>        <code>{bf(data.get('is_scam'))}</code>\n"
        f"<b>Fake</b>        <code>{bf(data.get('is_fake'))}</code>\n"
        f"<b>Bot</b>         <code>{bf(data.get('is_bot'))}</code>\n"
        f"<b>Restricted</b>  <code>{bf(data.get('is_restricted'))}</code>\n"
        f"<b>Support</b>     <code>{bf(data.get('is_support'))}</code>\n"
        f"<b>Contact</b>     <code>{bf(data.get('is_contact'))}</code>\n"
        f"<b>Mutual</b>      <code>{bf(data.get('is_mutual_contact'))}</code>\n"
        f"\n"
        f"<b>RESTRICTION</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<code>{sv(data.get('restriction_reason'))}</code>\n"
        f"\n"
        f"<code>────────────────────────</code>\n"
        f"<i>@drazeforce</i>"
    )

def buildAdminUserCard(uid, apiData=None):
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    if not u:
        return "<b>User not found.</b>"

    banned = u.get("banned", False)
    cd     = u.get("cooldownUntil")
    lim    = u.get("dailyLimit")
    notes  = u.get("notes", [])
    joined = u.get("joinedAt", "N/A")[:16].replace("T", "  ")
    seen   = u.get("lastSeen",  "N/A")[:16].replace("T", "  ")
    total  = u.get("totalLookups", 0)

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

    lines = [
        f"<b>{sv(u.get('firstName'))} {sv(u.get('lastName',''))}</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Username</b>      <code>@{sv(u.get('username'))}</code>\n"
        f"<b>User ID</b>       <code>{uid}</code>\n"
        f"<b>Status</b>        <code>{statusStr}</code>\n"
        f"<b>Joined Bot</b>    <code>{joined}</code>\n"
        f"<b>Last Seen</b>     <code>{seen}</code>\n"
        f"<b>Total Lookups</b> <code>{total}</code>\n"
        f"<b>Daily Limit</b>   <code>{limStr}</code>\n"
        f"<b>Cooldown</b>      <code>{cdStr}</code>"
    ]

    if apiData:
        p = apiData.get("phone_info", {}) or {}
        lines.append(
            f"\n\n<b>TELEGRAM PROFILE</b>\n"
            f"<code>────────────────────────</code>\n"
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
            f"<b>PHONE INFO</b>\n"
            f"<code>────────────────────────</code>\n"
            f"<b>Number</b>        <tg-spoiler><code>{sv(p.get('number'))}</code></tg-spoiler>\n"
            f"<b>Country</b>       <code>{sv(p.get('country'))}</code>\n"
            f"<b>Country Code</b>  <code>{sv(p.get('country_code'))}</code>\n"
            f"\n"
            f"<b>FLAGS</b>\n"
            f"<code>────────────────────────</code>\n"
            f"<b>Premium</b>     <code>{bf(apiData.get('is_premium'))}</code>\n"
            f"<b>Verified</b>    <code>{bf(apiData.get('is_verified'))}</code>\n"
            f"<b>Scam</b>        <code>{bf(apiData.get('is_scam'))}</code>\n"
            f"<b>Fake</b>        <code>{bf(apiData.get('is_fake'))}</code>\n"
            f"<b>Bot</b>         <code>{bf(apiData.get('is_bot'))}</code>\n"
            f"<b>Restricted</b>  <code>{bf(apiData.get('is_restricted'))}</code>\n"
            f"<b>Support</b>     <code>{bf(apiData.get('is_support'))}</code>\n"
            f"<b>Contact</b>     <code>{bf(apiData.get('is_contact'))}</code>\n"
            f"<b>Mutual</b>      <code>{bf(apiData.get('is_mutual_contact'))}</code>\n"
            f"\n"
            f"<b>RESTRICTION</b>\n"
            f"<code>────────────────────────</code>\n"
            f"<code>{sv(apiData.get('restriction_reason'))}</code>"
        )
    else:
        lines.append(
            f"\n\n<b>TELEGRAM PROFILE</b>\n"
            f"<code>────────────────────────</code>\n"
            f"<i>No API data available for this user ID.</i>"
        )

    if notes:
        lines.append(f"\n\n<b>ADMIN NOTES</b>\n<code>────────────────────────</code>")
        for i, n in enumerate(notes[-5:], 1):
            ts = n.get("ts", "")[:10]
            lines.append(f"<code>{i}.</code>  <i>{n.get('text','')}</i>  <code>[{ts}]</code>")

    return "\n".join(lines)

async def notifyUser(bot, userId, text):
    try:
        await bot.send_message(chat_id=userId, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ── Keyboards ─────────────────────────────────────────────────────────────────

def mainMenuKb(userId=None):
    rows = [
        [InlineKeyboardButton("Lookup", callback_data="tgtonum"),
         InlineKeyboardButton("Stats",  callback_data="myStats")],
        [InlineKeyboardButton("Get User ID",    callback_data="getuserid")],
        [InlineKeyboardButton("Contact Admin",  callback_data="contact_admin")],
    ]
    if userId and isAdmin(userId):
        rows.append([InlineKeyboardButton("Admin Panel", callback_data="adm_dashboard")])
        rows.append([InlineKeyboardButton("Inbox",       callback_data="adm_inbox_0")])
    return InlineKeyboardMarkup(rows)

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
    unread = len([m for m in db.get("inbox", []) if not m.get("read")])
    inboxLabel = f"Inbox  ({unread} new)" if unread else "Inbox"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Users",       callback_data="adm_users_0"),
         InlineKeyboardButton("Live Feed",   callback_data="adm_lookups_0")],
        [InlineKeyboardButton("Broadcast",   callback_data="adm_broadcast"),
         InlineKeyboardButton("Stats",       callback_data="adm_rate")],
        [InlineKeyboardButton(ml,            callback_data="adm_maintenance"),
         InlineKeyboardButton(f"API Key:  {key}", callback_data="adm_apikey")],
        [InlineKeyboardButton(inboxLabel,    callback_data="adm_inbox_0")],
        [InlineKeyboardButton("Close",       callback_data="adm_close")],
    ])

def userManageKb(uid):
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    bl  = "Unban" if u.get("banned") else "Ban"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bl,          callback_data=f"usr_ban_{uid}"),
         InlineKeyboardButton("Cooldown",  callback_data=f"usr_cooldown_{uid}")],
        [InlineKeyboardButton("Limit",     callback_data=f"usr_limit_{uid}"),
         InlineKeyboardButton("Message",   callback_data=f"usr_msg_{uid}")],
        [InlineKeyboardButton("History",   callback_data=f"usr_history_{uid}_0"),
         InlineKeyboardButton("Note",      callback_data=f"usr_note_{uid}")],
        [InlineKeyboardButton("Back",      callback_data="adm_users_0")],
    ])

def getUserIdKb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("My User ID",      callback_data="uid_self")],
        [InlineKeyboardButton("Get Someone's ID", callback_data="uid_other")],
        [InlineKeyboardButton("Back",             callback_data="back_main")],
    ])


# ── Dashboard text ─────────────────────────────────────────────────────────────

def _dashboardText(stats):
    return (
        f"<b>ADMIN DASHBOARD</b>\n"
        f"<code>────────────────────────</code>\n\n"
        f"<b>Users</b>         <code>{stats['totalUsers']}</code>   "
        f"<b>Banned</b>  <code>{stats['bannedCount']}</code>\n"
        f"<b>Lookups</b>       <code>{stats['totalLookups']}</code>\n"
        f"<b>Today</b>         <code>{stats['todayLookups']}</code>\n"
        f"<b>Success Rate</b>  <code>{stats['successRate']}%</code>\n"
        f"<b>Inbox</b>         <code>{stats['unreadInbox']} unread</code>\n\n"
        f"<code>────────────────────────</code>"
    )


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
                        f"<b>NEW USER JOINED</b>\n"
                        f"<code>────────────────────────</code>\n"
                        f"<b>Name</b>      <code>{u.first_name or 'null'}</code>\n"
                        f"<b>Username</b>  <code>@{u.username or 'null'}</code>\n"
                        f"<b>User ID</b>   <code>{u.id}</code>\n"
                        f"<b>Time</b>      <code>{datetime.now().strftime('%Y-%m-%d  %H:%M')}</code>"
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    if db.get("maintenance") and not isAdmin(u.id):
        await update.message.reply_text(
            "<b>Under Maintenance</b>\n\n"
            "The service is temporarily unavailable.\n"
            "Please check back shortly.",
            parse_mode=ParseMode.HTML
        )
        return

    name = u.first_name or "there"
    await update.message.reply_text(
        f"<b>Welcome, {name}</b>\n\n"
        f"<code>────────────────────────</code>\n"
        f"Telegram username to phone lookup.\n"
        f"Get detailed profile info on any user.\n"
        f"<code>────────────────────────</code>\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=mainMenuKb(u.id)
    )


# ── /stats ────────────────────────────────────────────────────────────────────

async def cmdStats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u  = update.effective_user
    registerUser(u.id, u.username, u.first_name)
    s  = getUserStats(u.id)
    j  = s["joinedAt"][:10] if s["joinedAt"] != "N/A" else "N/A"
    ls = s["lastSeen"][:10] if s["lastSeen"] != "N/A" else "N/A"
    await update.message.reply_text(
        f"<b>YOUR STATS</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Total</b>        <code>{s['total']}</code>\n"
        f"<b>Today</b>        <code>{s['today']}</code>\n"
        f"<b>Successful</b>   <code>{s['successful']}</code>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Since</b>        <code>{j}</code>\n"
        f"<b>Last</b>         <code>{ls}</code>\n"
        f"<code>────────────────────────</code>\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=mainMenuKb(u.id)
    )


# ── /admin ────────────────────────────────────────────────────────────────────

async def cmdAdmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if isAdmin(uid):
        stats = getAdminStats()
        await update.message.reply_text(
            _dashboardText(stats), parse_mode=ParseMode.HTML,
            reply_markup=adminDashboardKb()
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "<b>Admin Access</b>\n\n"
        "Enter the admin password.",
        parse_mode=ParseMode.HTML
    )
    return AWAIT_ADMIN_PW

async def receiveAdminPw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    h       = hashlib.sha256(entered.encode()).hexdigest()
    try:
        await update.message.delete()
    except Exception:
        pass
    if h != ADMIN_PASS_HASH:
        await update.message.reply_text(
            "<b>Incorrect password.</b>\n\nAccess denied.",
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
        f"<b>CHANGE API KEY</b>\n"
        f"<code>────────────────────────</code>\n\n"
        f"<b>Current Key</b>  <code>{cur}</code>\n\n"
        f"Send the new key as a plain message.",
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
            "<b>Invalid key.</b>  Try again.",
            parse_mode=ParseMode.HTML
        )
        return AWAIT_API_KEY
    db     = loadDb()
    oldKey = db.get("apiKey", TGOSINT_KEY_DEFAULT)
    db["apiKey"] = newKey
    saveDb(db)
    await update.message.reply_text(
        f"<b>API KEY UPDATED</b>\n"
        f"<code>────────────────────────</code>\n\n"
        f"<b>Old</b>  <code>{oldKey}</code>\n"
        f"<b>New</b>  <code>{newKey}</code>\n\n"
        f"All future requests use the new key.",
        parse_mode=ParseMode.HTML,
        reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


# ── Users list ────────────────────────────────────────────────────────────────

async def cbAdminUsers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    page = int(q.data.split("_")[-1]) if q.data.split("_")[-1].isdigit() else 0
    db   = loadDb()
    all_ = sorted(db["users"].values(), key=lambda u: u.get("lastSeen", ""), reverse=True)
    pp   = 8
    tot  = len(all_)
    chunk = all_[page*pp : page*pp+pp]

    def statusIcon(u):
        if u.get("banned"):
            return "X"
        cd = u.get("cooldownUntil")
        if cd:
            try:
                if datetime.now() < datetime.fromisoformat(cd):
                    return "~"
            except Exception:
                pass
        return "."

    lines = [
        f"<b>USERS</b>   <i>{page*pp+1}–{min((page+1)*pp,tot)} of {tot}</i>\n"
        f"<code>X</code> banned   <code>~</code> cooldown   <code>.</code> active"
    ]
    kb = []
    for i, u in enumerate(chunk, start=page*pp+1):
        icon = statusIcon(u)
        name = sv(u.get("firstName"))[:18]
        kb.append([InlineKeyboardButton(
            f"{i:02d}  {icon}  {name}",
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
        "<i>Fetching live profile...</i>", parse_mode=ParseMode.HTML
    )
    apiData, _ = await fetchUserInfo(str(uid))
    card       = buildAdminUserCard(uid, apiData=apiData)
    await q.message.edit_text(
        card, parse_mode=ParseMode.HTML,
        reply_markup=userManageKb(uid)
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

    if u["banned"]:
        await notifyUser(
            ctx.bot, uid,
            f"<b>Account Suspended</b>\n\n"
            f"Your account has been banned.\n"
            f"If you believe this is an error, you can submit an appeal."
        )
        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text="Tap below to submit an appeal to the admin.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Submit Appeal", callback_data="appeal_ban")]
                ])
            )
        except Exception:
            pass
    else:
        await notifyUser(
            ctx.bot, uid,
            "<b>Account Reinstated</b>\n\n"
            "Your account has been unbanned. You can use the bot again."
        )

    await q.edit_message_text(
        f"<b>User {action}</b>\n<code>{uid}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to User", callback_data=f"usr_view_{uid}")]
        ])
    )


# ── Appeal flow ───────────────────────────────────────────────────────────────

async def cbAppealBan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    if not u.get("banned"):
        await q.edit_message_text(
            "<b>Your account is not banned.</b>",
            parse_mode=ParseMode.HTML
        )
        return
    ctx.user_data["awaitingAppeal"] = True
    await q.edit_message_text(
        "<b>Submit Appeal</b>\n\n"
        "Explain why your account should be unbanned.\n"
        "Send your message below.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="back_main")]
        ])
    )
    return AWAIT_CONTACT_MSG

async def receiveAppeal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaitingAppeal"):
        return ConversationHandler.END
    ctx.user_data["awaitingAppeal"] = False
    u    = update.effective_user
    text = update.message.text.strip()
    mid  = addInboxMessage(u.id, u.first_name or "Unknown", u.username, text, msgType="appeal")

    db = loadDb()
    for aid in db.get("adminIds", []):
        try:
            await ctx.bot.send_message(
                chat_id=aid,
                text=(
                    f"<b>NEW APPEAL</b>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<b>From</b>  <code>{u.first_name or 'null'}</code>  "
                    f"<code>@{u.username or 'null'}</code>\n"
                    f"<b>ID</b>    <code>{u.id}</code>\n\n"
                    f"Tap Inbox to review."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Open Inbox", callback_data="adm_inbox_0")]
                ])
            )
        except Exception:
            pass

    await update.message.reply_text(
        "<b>Appeal Submitted</b>\n\n"
        "Your appeal has been sent to the admin.\n"
        "You will be notified of the decision.",
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END


# ── Cooldown ──────────────────────────────────────────────────────────────────

async def cbUserCooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    ctx.user_data["cooldownTarget"] = uid
    await q.edit_message_text(
        f"<b>Set Cooldown</b>\n\n"
        f"User ID: <code>{uid}</code>\n\n"
        f"Enter duration in minutes.",
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
    await notifyUser(
        ctx.bot, uid,
        f"<b>Cooldown Applied</b>\n\n"
        f"You have been placed on cooldown for <b>{mins} minutes</b>.\n"
        f"Lookups are paused until the cooldown expires."
    )
    await update.message.reply_text(
        f"<b>Cooldown Set</b>\n\n"
        f"<code>{uid}</code>  —  <code>{mins} minutes</code>",
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
    await notifyUser(
        ctx.bot, uid,
        "<b>Cooldown Removed</b>\n\nYour cooldown has been lifted. You can make lookups again."
    )
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
        f"<b>Set Daily Limit</b>\n\n"
        f"User ID: <code>{uid}</code>\n\n"
        f"Enter max lookups allowed per day.",
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
            "<b>Invalid.</b>  Enter a positive number.",
            parse_mode=ParseMode.HTML
        )
        return AWAIT_LIMIT_VAL
    db = loadDb()
    db["users"][str(uid)]["dailyLimit"] = lim
    saveDb(db)
    await notifyUser(
        ctx.bot, uid,
        f"<b>Daily Limit Set</b>\n\n"
        f"Your daily lookup limit has been set to <b>{lim} lookups per day</b>."
    )
    await update.message.reply_text(
        f"<b>Daily Limit Set</b>\n\n"
        f"<code>{uid}</code>  —  <code>{lim} / day</code>",
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
    await notifyUser(
        ctx.bot, uid,
        "<b>Daily Limit Removed</b>\n\nYour daily lookup limit has been removed. You now have unlimited lookups."
    )
    await q.edit_message_text(
        f"<b>Daily Limit Removed</b>\n<code>{uid}</code>  —  unlimited",
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
        f"<b>Add Note</b>\n\n"
        f"User ID: <code>{uid}</code>\n\n"
        f"Type your private note.",
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
            "text": text,
            "ts":   datetime.now().isoformat(),
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
        f"<b>LOOKUP HISTORY</b>  <code>{uid}</code>\n"
        f"<i>{page*pp+1}–{min((page+1)*pp,tot)} of {tot}</i>\n"
        f"<code>────────────────────────</code>"
    ]
    if not chunk:
        lines.append("\n<i>No history yet.</i>")
    for e in chunk:
        ts = e.get("ts", "")[:16].replace("T", "  ")
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


# ── Message user (admin to user direct) ───────────────────────────────────────

async def cbUserMsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = int(q.data.split("_")[-1])
    ctx.user_data["msgTarget"] = uid
    await q.edit_message_text(
        f"<b>Message User</b>\n\n"
        f"<code>{uid}</code>\n\n"
        f"Type the message to send.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data=f"usr_view_{uid}")]
        ])
    )
    return AWAIT_ADMIN_REPLY

async def receiveDirectMsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = ctx.user_data.get("msgTarget")
    if not uid:
        return ConversationHandler.END
    text = update.message.text.strip()
    try:
        await ctx.bot.send_message(
            chat_id=uid,
            text=(
                f"<b>Message from Admin</b>\n"
                f"<code>────────────────────────</code>\n\n"
                f"{text}\n\n"
                f"<i>@drazeforce</i>"
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


# ── Inbox ─────────────────────────────────────────────────────────────────────

async def cbAdminInbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    page = int(q.data.split("_")[-1]) if q.data.split("_")[-1].isdigit() else 0
    db   = loadDb()
    msgs = list(reversed(db.get("inbox", [])))
    pp   = 5
    tot  = len(msgs)
    chunk = msgs[page*pp : page*pp+pp]

    unread = len([m for m in msgs if not m.get("read")])
    lines  = [
        f"<b>INBOX</b>   <code>{unread} unread</code>\n"
        f"<code>────────────────────────</code>"
    ]
    kb = []
    for m in chunk:
        dot  = "●" if not m.get("read") else " "
        name = (m.get("fromName") or "Unknown")[:14]
        tag  = " [APPEAL]" if m.get("type") == "appeal" else ""
        ago  = timeAgo(m.get("ts", ""))
        kb.append([InlineKeyboardButton(
            f"{dot} {name}{tag}   {ago}",
            callback_data=f"inbox_open_{m['id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"adm_inbox_{page-1}"))
    if (page+1)*pp < tot:
        nav.append(InlineKeyboardButton("Next", callback_data=f"adm_inbox_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("Back", callback_data="adm_dashboard")])

    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cbInboxOpen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    mid = q.data.replace("inbox_open_", "")
    m   = getInboxMessage(mid)
    if not m:
        await q.edit_message_text("<b>Message not found.</b>", parse_mode=ParseMode.HTML)
        return

    markInboxRead(mid)
    tag     = "APPEAL" if m.get("type") == "appeal" else "MESSAGE"
    replies = m.get("replies", [])

    lines = [
        f"<b>{tag}</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>From</b>      <code>{m.get('fromName','?')}</code>  "
        f"<code>@{m.get('fromUsername','null')}</code>\n"
        f"<b>User ID</b>   <code>{m.get('fromId','?')}</code>\n"
        f"<b>Time</b>      <code>{m.get('ts','')[:16].replace('T','  ')}</code>\n"
        f"<code>────────────────────────</code>\n"
        f"{m.get('text','')}"
    ]

    if replies:
        lines.append(f"\n<code>────────────────────────</code>\n<b>REPLIES</b>")
        for r in replies:
            who = "Admin" if r.get("fromAdmin") else "User"
            ts  = r.get("ts", "")[:16].replace("T", "  ")
            lines.append(f"\n<b>{who}</b>  <code>{ts}</code>\n{r.get('text','')}")

    ctx.user_data["inboxReplyTarget"] = mid
    ctx.user_data["inboxReplyUserId"] = m.get("fromId")

    kb = [
        [InlineKeyboardButton("Reply",        callback_data=f"inbox_reply_{mid}"),
         InlineKeyboardButton("Mark Read",    callback_data=f"inbox_read_{mid}")],
        [InlineKeyboardButton("Delete",       callback_data=f"inbox_delete_{mid}")],
    ]
    if m.get("type") == "appeal":
        kb.insert(0, [
            InlineKeyboardButton("Approve (Unban)", callback_data=f"inbox_approve_{mid}"),
            InlineKeyboardButton("Reject",          callback_data=f"inbox_reject_{mid}"),
        ])
    kb.append([InlineKeyboardButton("Back to Inbox", callback_data="adm_inbox_0")])

    await q.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cbInboxReply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    mid = q.data.replace("inbox_reply_", "")
    ctx.user_data["inboxReplyTarget"] = mid
    m   = getInboxMessage(mid)
    ctx.user_data["inboxReplyUserId"] = m.get("fromId") if m else None
    ctx.user_data["inboxReplying"]    = True
    await q.edit_message_text(
        "<b>Reply</b>\n\n"
        "Type your reply. Send multiple messages.\n"
        "Type <code>end</code> to finish.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="adm_inbox_0")]
        ])
    )
    return AWAIT_ADMIN_REPLY

async def receiveInboxReply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("inboxReplying"):
        return ConversationHandler.END
    text   = update.message.text.strip()
    mid    = ctx.user_data.get("inboxReplyTarget")
    userId = ctx.user_data.get("inboxReplyUserId")

    if text.lower() == "end":
        ctx.user_data["inboxReplying"] = False
        await update.message.reply_text(
            "<b>Reply session ended.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=adminDashboardKb()
        )
        return ConversationHandler.END

    if mid:
        addInboxReply(mid, text, fromAdmin=True)
    if userId:
        await notifyUser(
            ctx.bot, userId,
            f"<b>Admin replied to your message</b>\n"
            f"<code>────────────────────────</code>\n\n"
            f"{text}\n\n"
            f"<i>@drazeforce</i>"
        )
    await update.message.reply_text(
        "<b>Sent.</b>  Send another or type <code>end</code> to finish.",
        parse_mode=ParseMode.HTML
    )
    return AWAIT_ADMIN_REPLY

async def cbInboxMarkRead(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    mid = q.data.replace("inbox_read_", "")
    markInboxRead(mid)
    await q.edit_message_text(
        "<b>Marked as read.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Inbox", callback_data="adm_inbox_0")]
        ])
    )

async def cbInboxDelete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    mid = q.data.replace("inbox_delete_", "")
    deleteInboxMessage(mid)
    await q.edit_message_text(
        "<b>Message deleted.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Inbox", callback_data="adm_inbox_0")]
        ])
    )

async def cbInboxApprove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    mid = q.data.replace("inbox_approve_", "")
    m   = getInboxMessage(mid)
    if not m:
        await q.answer("Message not found.")
        return
    uid = m.get("fromId")
    db  = loadDb()
    if str(uid) in db["users"]:
        db["users"][str(uid)]["banned"] = False
        saveDb(db)
    await notifyUser(
        ctx.bot, uid,
        "<b>Appeal Approved</b>\n\n"
        "Your appeal has been reviewed and approved.\n"
        "Your account has been reinstated."
    )
    deleteInboxMessage(mid)
    await q.edit_message_text(
        f"<b>Appeal Approved</b>\n<code>{uid}</code> has been unbanned.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Inbox", callback_data="adm_inbox_0")]
        ])
    )

async def cbInboxReject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    mid = q.data.replace("inbox_reject_", "")
    m   = getInboxMessage(mid)
    uid = m.get("fromId") if m else None
    if uid:
        await notifyUser(
            ctx.bot, uid,
            "<b>Appeal Rejected</b>\n\n"
            "Your appeal has been reviewed and rejected.\n"
            "The ban remains in place."
        )
    deleteInboxMessage(mid)
    await q.edit_message_text(
        "<b>Appeal Rejected.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Inbox", callback_data="adm_inbox_0")]
        ])
    )


# ── Contact Admin (user to admin chat) ────────────────────────────────────────

async def cbContactAdmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    if u.get("chatSession"):
        await q.edit_message_text(
            "<b>Active Chat Session</b>\n\n"
            "You already have an active chat with admin.\n"
            "Type <code>end</code> to close it first.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data="back_main")]
            ])
        )
        return
    ctx.user_data["contactingAdmin"] = True
    await q.edit_message_text(
        "<b>Contact Admin</b>\n\n"
        "Type your message below.\n"
        "Send multiple messages. Type <code>end</code> to finish.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="back_main")]
        ])
    )
    db["users"][str(uid)]["chatSession"] = True
    saveDb(db)
    return AWAIT_CONTACT_MSG

async def receiveContactMsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("contactingAdmin") and not ctx.user_data.get("awaitingAppeal"):
        return ConversationHandler.END
    u    = update.effective_user
    text = update.message.text.strip()

    if text.lower() == "end":
        ctx.user_data["contactingAdmin"] = False
        db  = loadDb()
        uid = str(u.id)
        if uid in db["users"]:
            db["users"][uid]["chatSession"] = False
            saveDb(db)
        await update.message.reply_text(
            "<b>Chat session ended.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=mainMenuKb(u.id)
        )
        return ConversationHandler.END

    mid = addInboxMessage(u.id, u.first_name or "Unknown", u.username, text, msgType="message")
    db  = loadDb()
    for aid in db.get("adminIds", []):
        try:
            await ctx.bot.send_message(
                chat_id=aid,
                text=(
                    f"<b>NEW MESSAGE</b>\n"
                    f"<code>────────────────────────</code>\n"
                    f"<b>From</b>  <code>{u.first_name or 'null'}</code>  "
                    f"<code>@{u.username or 'null'}</code>\n"
                    f"<b>ID</b>    <code>{u.id}</code>\n\n"
                    f"Tap to open."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Open Inbox", callback_data="adm_inbox_0")]
                ])
            )
        except Exception:
            pass

    await update.message.reply_text(
        "<b>Sent.</b>  Send another message or type <code>end</code> to finish.",
        parse_mode=ParseMode.HTML
    )
    return AWAIT_CONTACT_MSG


# ── Get User ID ───────────────────────────────────────────────────────────────

async def cbGetUserId(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "<b>Get User ID</b>\n\n"
        "Choose an option below.",
        parse_mode=ParseMode.HTML,
        reply_markup=getUserIdKb()
    )

async def cbUidSelf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    u   = update.effective_user
    db  = loadDb()
    rec = db["users"].get(str(u.id), {})
    joined = rec.get("joinedAt", "N/A")[:10]
    await q.edit_message_text(
        f"<b>YOUR INFO</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Name</b>      <code>{u.first_name or 'null'}</code>\n"
        f"<b>Username</b>  <code>@{u.username or 'null'}</code>\n"
        f"<b>User ID</b>   <code>{u.id}</code>\n"
        f"<b>Joined</b>    <code>{joined}</code>\n"
        f"<code>────────────────────────</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="getuserid")]
        ])
    )

async def cbUidOther(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(
            "Select a contact",
            request_users=KeyboardButtonRequestUsers(request_id=1, user_is_bot=False)
        )]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await q.message.reply_text(
        "<b>Get Someone's User ID</b>\n\n"
        "Tap the button below to open your contacts\n"
        "and select the person.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    ctx.user_data["awaitingUserIdContact"] = True

async def receiveUserIdContact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaitingUserIdContact"):
        return
    ctx.user_data["awaitingUserIdContact"] = False
    users_shared = update.message.users_shared
    if not users_shared or not users_shared.users:
        await update.message.reply_text(
            "<b>No contact received.</b>  Try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove()
        )
        return
    person = users_shared.users[0]
    await update.message.reply_text(
        f"<b>USER ID</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Name</b>      <code>{person.first_name or 'null'}</code>\n"
        f"<b>Username</b>  <code>@{person.username or 'null'}</code>\n"
        f"<b>User ID</b>   <code>{person.id}</code>\n"
        f"<code>────────────────────────</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )


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
        f"<b>LIVE FEED</b>   "
        f"<i>{page*pp+1}–{min((page+1)*pp,tot)} of {tot}</i>\n"
        f"<code>────────────────────────</code>"
    ]
    for l in chunk:
        ts = l.get("ts", "")[:16].replace("T", "  ")
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


# ── Stats ─────────────────────────────────────────────────────────────────────

async def cbAdminRate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    db   = loadDb()
    tot  = len(db["lookups"])
    succ = len([l for l in db["lookups"] if l["success"]])
    rate = round(succ / tot * 100, 1) if tot else 0
    ts   = date.today().isoformat()
    todL = len([l for l in db["lookups"] if l["ts"].startswith(ts)])

    topU = {}
    for l in db["lookups"]:
        uid = l.get("userId")
        topU[uid] = topU.get(uid, 0) + 1
    top5 = sorted(topU.items(), key=lambda x: x[1], reverse=True)[:5]

    lines = [
        f"<b>STATS</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Total Lookups</b>   <code>{tot}</code>\n"
        f"<b>Today</b>           <code>{todL}</code>\n"
        f"<b>Successful</b>      <code>{succ}</code>\n"
        f"<b>Failed</b>          <code>{tot-succ}</code>\n"
        f"<b>Success Rate</b>    <code>{rate}%</code>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>TOP USERS</b>"
    ]
    for uid, count in top5:
        info = db["users"].get(str(uid), {})
        lines.append(
            f"\n<code>{sv(info.get('firstName'))}</code>  "
            f"<code>@{sv(info.get('username'))}</code>  "
            f"<code>{count} lookups</code>"
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
        "<b>Broadcast</b>\n\n"
        "Send the message to push to all users.",
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
                    f"<code>────────────────────────</code>\n\n"
                    f"{text}\n\n"
                    f"<i>@drazeforce</i>"
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
        parse_mode=ParseMode.HTML,
        reply_markup=adminDashboardKb()
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
        f"Enter the maintenance password to confirm.",
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
            "<b>Incorrect password.</b>  State unchanged.",
            parse_mode=ParseMode.HTML,
            reply_markup=adminDashboardKb()
        )
        return ConversationHandler.END
    newState = ctx.user_data.pop("pendingMaintenance", False)
    db       = loadDb()
    db["maintenance"] = newState
    saveDb(db)
    label = "ENABLED" if newState else "DISABLED"
    await update.message.reply_text(
        f"<b>Maintenance {label}</b>\n\n"
        f"{'Users will see a maintenance message.' if newState else 'Bot is back online.'}",
        parse_mode=ParseMode.HTML,
        reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


# ── Back to main ──────────────────────────────────────────────────────────────

async def cbBackMain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    u    = update.effective_user
    name = u.first_name or "there"
    await q.edit_message_text(
        f"<b>Welcome, {name}</b>\n\n"
        f"<code>────────────────────────</code>\n"
        f"Telegram username to phone lookup.\n"
        f"Get detailed profile info on any user.\n"
        f"<code>────────────────────────</code>\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=mainMenuKb(u.id)
    )
    return ConversationHandler.END


# ── Stats callback ────────────────────────────────────────────────────────────

async def cbMyStats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    u  = update.effective_user
    s  = getUserStats(u.id)
    j  = s["joinedAt"][:10] if s["joinedAt"] != "N/A" else "N/A"
    ls = s["lastSeen"][:10] if s["lastSeen"] != "N/A" else "N/A"
    await q.edit_message_text(
        f"<b>YOUR STATS</b>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Total</b>        <code>{s['total']}</code>\n"
        f"<b>Today</b>        <code>{s['today']}</code>\n"
        f"<b>Successful</b>   <code>{s['successful']}</code>\n"
        f"<code>────────────────────────</code>\n"
        f"<b>Since</b>        <code>{j}</code>\n"
        f"<b>Last</b>         <code>{ls}</code>\n"
        f"<code>────────────────────────</code>\n"
        f"<i>@drazeforce</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Menu", callback_data="back_main")]
        ])
    )


# ── Lookup input ──────────────────────────────────────────────────────────────

async def cbTgToNum(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    db  = loadDb()
    u   = db["users"].get(str(uid), {})
    if u.get("chatSession"):
        await q.edit_message_text(
            "<b>Active Chat Session</b>\n\n"
            "You have an active conversation with admin.\n"
            "Type <code>end</code> to close it first.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back", callback_data="back_main")]
            ])
        )
        return
    await q.edit_message_text(
        "<b>Telegram OSINT</b>\n\n"
        "Enter the target username\n"
        "with or without the @ sign.\n\n"
        "example   <code>@drazeforce</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="back_main")]
        ])
    )
    return AWAIT_INPUT


# ── Lookup flow ───────────────────────────────────────────────────────────────

async def receiveInput(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    msg = update.message
    db  = loadDb()

    if db.get("maintenance") and not isAdmin(u.id):
        await msg.reply_text(
            "<b>Under Maintenance</b>\n\n"
            "The service is temporarily unavailable.\n"
            "Please check back shortly.",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    allowed, reason = checkUserAccess(u.id)
    if not allowed:
        if reason == "banned":
            await msg.reply_text(
                "<b>Access Denied</b>\n\n"
                "Your account has been suspended.\n"
                "Contact @drazeforce if you believe this is an error.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Submit Appeal", callback_data="appeal_ban")]
                ])
            )
        elif reason.startswith("cooldown:"):
            mins = reason.split(":")[1]
            await msg.reply_text(
                f"<b>Cooldown Active</b>\n\n"
                f"You are on cooldown for <b>{mins} more minutes</b>.\n"
                f"Lookups are paused until then.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Menu", callback_data="back_main")]
                ])
            )
        elif reason.startswith("limit:"):
            lim = reason.split(":")[1]
            await msg.reply_text(
                f"<b>Daily Limit Reached</b>\n\n"
                f"You have reached your limit of <b>{lim} lookups</b> for today.\n"
                f"Your limit resets at midnight.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to Menu", callback_data="back_main")]
                ])
            )
        return ConversationHandler.END

    raw = msg.text.strip().lstrip("@") if msg.text else ""
    if not raw or len(raw) < 3 or len(raw) > 32 or \
       not all(c.isalnum() or c == "_" for c in raw):
        await msg.reply_text(
            "<b>Invalid username.</b>\n\n"
            "Must be 3 to 32 characters.\n"
            "Letters, numbers and underscores only.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Cancel", callback_data="back_main")]
            ])
        )
        return AWAIT_INPUT
    q = raw

    dq   = f"@{q}"
    wait = await msg.reply_text(
        f"<b>Looking up</b>  <code>{dq}</code>\n\nPlease wait...",
        parse_mode=ParseMode.HTML
    )
    data, err = await fetchUserInfo(q)
    await wait.delete()

    if err == "timeout":
        await msg.reply_text(
            "<b>Request Timed Out</b>\n\nThe lookup took too long. Try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, q, None, False)
        return ConversationHandler.END

    if err or data is None:
        await msg.reply_text(
            "<b>Service Unavailable</b>\n\nThe lookup API could not be reached.",
            parse_mode=ParseMode.HTML,
            reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, q, None, False)
        return ConversationHandler.END

    pi = data.get("phone_info", {})
    if not pi.get("success"):
        reason = pi.get("message", "Could not fetch details")
        await msg.reply_text(
            f"<b>Lookup Failed</b>\n\n"
            f"<code>{reason}</code>\n\n"
            f"{dq} may not exist or has no linked number.",
            parse_mode=ParseMode.HTML,
            reply_markup=afterResultKb()
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
                f"Open the bot to see the result.",
                parse_mode=ParseMode.HTML
            ),
        )
    ], cache_time=0)


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u  = update.effective_user
    db = loadDb()
    ur = db["users"].get(str(u.id), {})
    if ur.get("chatSession") or ctx.user_data.get("contactingAdmin"):
        await receiveContactMsg(update, ctx)
        return
    await update.message.reply_text(
        "Use the menu to navigate.",
        parse_mode=ParseMode.HTML,
        reply_markup=mainMenuKb(u.id)
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
            AWAIT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveInput)],
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
    directMsgConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbUserMsg, pattern="^usr_msg_")],
        states={
            AWAIT_ADMIN_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveDirectMsg)],
        },
        fallbacks=[], allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    inboxReplyConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbInboxReply, pattern="^inbox_reply_")],
        states={
            AWAIT_ADMIN_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveInboxReply)],
        },
        fallbacks=[], allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    contactConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbContactAdmin, pattern="^contact_admin$")],
        states={
            AWAIT_CONTACT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveContactMsg)],
        },
        fallbacks=[CallbackQueryHandler(cbBackMain, pattern="^back_main$")],
        allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )
    appealConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbAppealBan, pattern="^appeal_ban$")],
        states={
            AWAIT_CONTACT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveAppeal)],
        },
        fallbacks=[CallbackQueryHandler(cbBackMain, pattern="^back_main$")],
        allow_reentry=True, per_message=False, per_chat=True, per_user=True,
    )

    app.add_error_handler(errorHandler)

    for conv in [
        adminConv, lookupConv, maintConv, apiKeyConv,
        cooldownConv, limitConv, noteConv,
        directMsgConv, inboxReplyConv, contactConv, appealConv,
    ]:
        app.add_handler(conv)

    app.add_handler(CommandHandler("start", cmdStart))
    app.add_handler(CommandHandler("stats", cmdStats))
    app.add_handler(CommandHandler("admin", cmdAdmin))
    app.add_handler(InlineQueryHandler(inlineQuery))

    # Contact picker response
    app.add_handler(MessageHandler(filters.StatusUpdate.USER_SHARED, receiveUserIdContact))

    app.add_handler(CallbackQueryHandler(cbBackMain,             pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cbMyStats,              pattern="^myStats$"))
    app.add_handler(CallbackQueryHandler(cbAdminDashboard,       pattern="^adm_dashboard$"))
    app.add_handler(CallbackQueryHandler(cbAdminUsers,           pattern="^adm_users_"))
    app.add_handler(CallbackQueryHandler(cbAdminLookups,         pattern="^adm_lookups_"))
    app.add_handler(CallbackQueryHandler(cbAdminRate,            pattern="^adm_rate$"))
    app.add_handler(CallbackQueryHandler(cbAdminBroadcastPrompt, pattern="^adm_broadcast$"))
    app.add_handler(CallbackQueryHandler(cbAdminClose,           pattern="^adm_close$"))
    app.add_handler(CallbackQueryHandler(cbAdminInbox,           pattern="^adm_inbox_"))
    app.add_handler(CallbackQueryHandler(cbInboxOpen,            pattern="^inbox_open_"))
    app.add_handler(CallbackQueryHandler(cbInboxMarkRead,        pattern="^inbox_read_"))
    app.add_handler(CallbackQueryHandler(cbInboxDelete,          pattern="^inbox_delete_"))
    app.add_handler(CallbackQueryHandler(cbInboxApprove,         pattern="^inbox_approve_"))
    app.add_handler(CallbackQueryHandler(cbInboxReject,          pattern="^inbox_reject_"))
    app.add_handler(CallbackQueryHandler(cbUserView,             pattern="^usr_view_"))
    app.add_handler(CallbackQueryHandler(cbUserBan,              pattern="^usr_ban_"))
    app.add_handler(CallbackQueryHandler(cbUserRemoveCooldown,   pattern="^usr_rmcooldown_"))
    app.add_handler(CallbackQueryHandler(cbUserRemoveLimit,      pattern="^usr_rmlimit_"))
    app.add_handler(CallbackQueryHandler(cbUserHistory,          pattern="^usr_history_"))
    app.add_handler(CallbackQueryHandler(cbGetUserId,            pattern="^getuserid$"))
    app.add_handler(CallbackQueryHandler(cbUidSelf,              pattern="^uid_self$"))
    app.add_handler(CallbackQueryHandler(cbUidOther,             pattern="^uid_other$"))
    app.add_handler(CallbackQueryHandler(cbAppealBan,            pattern="^appeal_ban$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    threading.Thread(target=startHealthServer, daemon=True).start()
    threading.Thread(target=startSelfPing,     daemon=True).start()

    log.info("Bot running")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()