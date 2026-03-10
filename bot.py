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
from datetime import datetime, date
from dotenv import load_dotenv  # type: ignore
from telegram import (  # type: ignore
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import ( # type: ignore
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

BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
ADMIN_PASS_HASH  = hashlib.sha256(os.getenv("ADMIN_PASSWORD", "pootilangaadi").encode()).hexdigest()
MAINT_PASS_HASH  = hashlib.sha256("pari".encode()).hexdigest()

# ── Your TGOSINT API ──────────────────────────────────────────────────────────
TGOSINT_URL = "https://tgosint.vercel.app/"
TGOSINT_KEY = "drazeX"
# ─────────────────────────────────────────────────────────────────────────────

DB_FILE = "db.json"

AWAIT_INPUT      = 1
AWAIT_ADMIN_PW   = 2
AWAIT_BROADCAST  = 3
AWAIT_MAINT_PW   = 4

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


def loadDb():
    if not os.path.exists(DB_FILE):
        return {"users": {}, "lookups": [], "adminSessions": [], "maintenance": False}
    with open(DB_FILE, "r") as f:
        data = json.load(f)
    if "maintenance" not in data:
        data["maintenance"] = False
    return data

def saveDb(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

def registerUser(userId, username, firstName):
    db = loadDb()
    uid = str(userId)
    if uid not in db["users"]:
        db["users"][uid] = {
            "userId": userId,
            "username": username or "",
            "firstName": firstName or "",
            "joinedAt": datetime.now().isoformat(),
            "totalLookups": 0,
            "lastSeen": datetime.now().isoformat(),
        }
    else:
        db["users"][uid]["lastSeen"] = datetime.now().isoformat()
        if username:
            db["users"][uid]["username"] = username
    saveDb(db)

def logLookup(userId, username, firstName, query, result, success):
    db = loadDb()
    uid = str(userId)
    if uid in db["users"]:
        db["users"][uid]["totalLookups"] += 1
        db["users"][uid]["lastSeen"] = datetime.now().isoformat()
    db["lookups"].append({
        "ts": datetime.now().isoformat(),
        "userId": userId,
        "username": username or "",
        "firstName": firstName or "",
        "query": query,
        "success": success,
        "phone": result.get("phone_info", {}).get("number", "") if result else "",
        "country": result.get("phone_info", {}).get("country", "") if result else "",
    })
    saveDb(db)

def getUserStats(userId):
    db = loadDb()
    uid = str(userId)
    user = db["users"].get(uid, {})
    userLookups = [l for l in db["lookups"] if l["userId"] == userId]
    todayStr = date.today().isoformat()
    todayLookups = [l for l in userLookups if l["ts"].startswith(todayStr)]
    successfulLookups = [l for l in userLookups if l["success"]]
    return {
        "total": user.get("totalLookups", 0),
        "today": len(todayLookups),
        "successful": len(successfulLookups),
        "joinedAt": user.get("joinedAt", "N/A"),
        "lastSeen": user.get("lastSeen", "N/A"),
    }

def getAdminStats():
    db = loadDb()
    totalUsers = len(db["users"])
    totalLookups = len(db["lookups"])
    todayStr = date.today().isoformat()
    todayLookups = [l for l in db["lookups"] if l["ts"].startswith(todayStr)]
    successfulLookups = [l for l in db["lookups"] if l["success"]]
    recentUsers = sorted(db["users"].values(), key=lambda u: u.get("lastSeen",""), reverse=True)[:5]
    return {
        "totalUsers": totalUsers,
        "totalLookups": totalLookups,
        "todayLookups": len(todayLookups),
        "successRate": round(len(successfulLookups) / totalLookups * 100, 1) if totalLookups else 0,
        "recentUsers": recentUsers,
        "allLookups": db["lookups"],
        "allUsers": db["users"],
    }


def mainMenuKb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Tg to Num", callback_data="tgtonum")],
    ])

def afterResultKb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Look Up Another", callback_data="tgtonum")],
        [InlineKeyboardButton("My Stats", callback_data="myStats")],
        [InlineKeyboardButton("Back to Menu", callback_data="back_main")],
    ])

def adminDashboardKb(page=0):
    db = loadDb()
    maintLabel = "Maintenance  ON" if db.get("maintenance") else "Maintenance  OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("All Users",        callback_data=f"adm_users_{page}"),
         InlineKeyboardButton("All Lookups",      callback_data=f"adm_lookups_{page}")],
        [InlineKeyboardButton("Today's Activity", callback_data="adm_today"),
         InlineKeyboardButton("Success Rate",     callback_data="adm_rate")],
        [InlineKeyboardButton("Recent Queries",   callback_data="adm_recent"),
         InlineKeyboardButton("Broadcast",        callback_data="adm_broadcast")],
        [InlineKeyboardButton(maintLabel,         callback_data="adm_maintenance")],
        [InlineKeyboardButton("Close",            callback_data="adm_close")],
    ])


def safeVal(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return "N/A"
    return str(v)

def boolEmoji(v):
    if v is None:
        return "N/A"
    return "Yes" if v else "No"

def buildResultMsg(data):
    p = data.get("phone_info", {})

    uname          = safeVal(data.get("username"))
    uid            = safeVal(data.get("user_id"))
    fn             = safeVal(data.get("first_name"))
    ln             = safeVal(data.get("last_name"))
    full           = safeVal(data.get("full_name"))
    bio            = safeVal(data.get("bio"))
    status         = safeVal(data.get("status"))
    dc             = safeVal(data.get("dc_id"))
    wasOnline      = safeVal(data.get("was_online"))
    commonChats    = safeVal(data.get("common_chats_count"))
    restrictReason = safeVal(data.get("restriction_reason"))
    searchType     = safeVal(data.get("search_type"))
    inputType      = safeVal(data.get("input_type"))

    phone   = safeVal(p.get("number"))
    country = safeVal(p.get("country"))
    cc      = safeVal(p.get("country_code"))

    isBot        = boolEmoji(data.get("is_bot"))
    isVerified   = boolEmoji(data.get("is_verified"))
    isPremium    = boolEmoji(data.get("is_premium"))
    isScam       = boolEmoji(data.get("is_scam"))
    isFake       = boolEmoji(data.get("is_fake"))
    isRestricted = boolEmoji(data.get("is_restricted"))
    isSupport    = boolEmoji(data.get("is_support"))
    isContact    = boolEmoji(data.get("is_contact"))
    isMutual     = boolEmoji(data.get("is_mutual_contact"))

    msg = (
        f"<b>USER PROFILE</b>  <code>@{uname}</code>\n"
        f"<code>{'━'*30}</code>\n"
        f"<b>User ID</b>        <code>{uid}</code>\n"
        f"<b>First Name</b>     <code>{fn}</code>\n"
        f"<b>Last Name</b>      <code>{ln}</code>\n"
        f"<b>Full Name</b>      <code>{full}</code>\n"
        f"<b>Bio</b>            <code>{bio}</code>\n"
        f"<b>Status</b>         <code>{status}</code>\n"
        f"<b>Last Online</b>    <code>{wasOnline}</code>\n"
        f"<b>DC ID</b>          <code>{dc}</code>\n"
        f"<b>Common Chats</b>   <code>{commonChats}</code>\n"
        f"<b>Search Type</b>    <code>{searchType}</code>\n"
        f"<b>Input Type</b>     <code>{inputType}</code>\n"
        f"\n"
        f"<b>PHONE INFO</b>\n"
        f"<code>{'━'*30}</code>\n"
        f"<b>Number</b>         <tg-spoiler><code>{phone}</code></tg-spoiler>\n"
        f"<b>Country</b>        <code>{country}</code>\n"
        f"<b>Country Code</b>   <code>{cc}</code>\n"
        f"\n"
        f"<b>ACCOUNT FLAGS</b>\n"
        f"<code>{'━'*30}</code>\n"
        f"<b>Bot</b>          <code>{isBot}</code>   <b>Verified</b>   <code>{isVerified}</code>\n"
        f"<b>Premium</b>      <code>{isPremium}</code>   <b>Scam</b>       <code>{isScam}</code>\n"
        f"<b>Fake</b>         <code>{isFake}</code>   <b>Restricted</b> <code>{isRestricted}</code>\n"
        f"<b>Support</b>      <code>{isSupport}</code>   <b>Contact</b>    <code>{isContact}</code>\n"
        f"<b>Mutual</b>       <code>{isMutual}</code>\n"
        f"<b>Restriction</b>  <code>{restrictReason}</code>\n"
        f"\n"
        f"<i>Response time: {safeVal(data.get('response_time'))}  —  @drazeforce</i>"
    )
    return msg


async def fetchUserInfo(query):
    isId = str(query).lstrip("-").isdigit()
    param = query if isId else f"@{query}"
    url = f"{TGOSINT_URL}?key={TGOSINT_KEY}&q={param}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status != 200:
                    return None, f"API returned HTTP {r.status}"
                data = await r.json()
                return data, None
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        log.error("fetchUserInfo error: %s", e)
        return None, "unreachable"


async def cmdStart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    registerUser(u.id, u.username, u.first_name)
    db = loadDb()
    if db.get("maintenance"):
        await update.message.reply_text(
            "<b>Bot Under Maintenance</b>\n\n"
            "We are currently performing maintenance.\n"
            "<i>Please check back shortly.</i>",
            parse_mode=ParseMode.HTML
        )
        return
    name = u.first_name or "there"
    msg = (
        f"<b>Welcome, {name}</b>\n\n"
        f"<code>Telegram Username to Phone Lookup</code>\n\n"
        f"Find detailed profile information and phone numbers\n"
        f"linked to any Telegram username.\n\n"
        f"<i>@drazeforce</i>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=mainMenuKb())


async def cmdStats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    registerUser(u.id, u.username, u.first_name)
    s = getUserStats(u.id)
    joined   = s["joinedAt"][:10] if s["joinedAt"] != "N/A" else "N/A"
    lastSeen = s["lastSeen"][:10] if s["lastSeen"] != "N/A" else "N/A"
    msg = (
        f"<b>YOUR STATS</b>\n"
        f"<code>{'━'*26}</code>\n"
        f"<b>Total Lookups</b>       <code>{s['total']}</code>\n"
        f"<b>Lookups Today</b>       <code>{s['today']}</code>\n"
        f"<b>Successful</b>          <code>{s['successful']}</code>\n"
        f"<b>Member Since</b>        <code>{joined}</code>\n"
        f"<b>Last Active</b>         <code>{lastSeen}</code>\n"
        f"<code>{'━'*26}</code>\n"
        f"<i>@drazeforce</i>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=mainMenuKb())


async def cmdAdmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>ADMIN ACCESS</b>\n\nEnter the admin password to continue.",
        parse_mode=ParseMode.HTML
    )
    return AWAIT_ADMIN_PW


async def receiveAdminPw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    enteredHash = hashlib.sha256(entered.encode()).hexdigest()

    try:
        await update.message.delete()
    except:
        pass

    if enteredHash != ADMIN_PASS_HASH:
        await update.message.reply_text(
            "<b>Incorrect password.</b>\n<i>Access denied.</i>",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    db = loadDb()
    db["adminSessions"].append({"userId": update.effective_user.id, "ts": datetime.now().isoformat()})
    saveDb(db)

    stats = getAdminStats()
    msg = (
        f"<b>ADMIN DASHBOARD</b>\n"
        f"<code>{'─'*28}</code>\n\n"
        f"<b>Total Users</b>      <code>{stats['totalUsers']}</code>\n"
        f"<b>Total Lookups</b>    <code>{stats['totalLookups']}</code>\n"
        f"<b>Today's Lookups</b>  <code>{stats['todayLookups']}</code>\n"
        f"<b>Success Rate</b>     <code>{stats['successRate']}%</code>\n\n"
        f"<code>{'─'*28}</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=adminDashboardKb())
    return ConversationHandler.END


async def cbAdminUsers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    page = int(data.split("_")[-1]) if data.split("_")[-1].isdigit() else 0

    db = loadDb()
    users = list(db["users"].values())
    perPage = 5
    total = len(users)
    start = page * perPage
    end = start + perPage
    chunk = users[start:end]

    lines = [f"<b>ALL USERS</b>  <i>(page {page+1})</i>\n<code>{'─'*28}</code>\n"]
    for u in chunk:
        joined = u.get("joinedAt","")[:10]
        lines.append(
            f"\n<b>{u.get('firstName','?')}</b>  <code>@{u.get('username','?')}</code>\n"
            f"ID: <code>{u.get('userId','?')}</code>\n"
            f"Lookups: <code>{u.get('totalLookups',0)}</code>   Joined: <code>{joined}</code>"
        )

    navBtns = []
    if page > 0:
        navBtns.append(InlineKeyboardButton("Prev", callback_data=f"adm_users_{page-1}"))
    if end < total:
        navBtns.append(InlineKeyboardButton("Next", callback_data=f"adm_users_{page+1}"))

    kb = []
    if navBtns:
        kb.append(navBtns)
    kb.append([InlineKeyboardButton("Back to Dashboard", callback_data="adm_dashboard")])

    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cbAdminLookups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    page = int(data.split("_")[-1]) if data.split("_")[-1].isdigit() else 0

    db = loadDb()
    lookups = list(reversed(db["lookups"]))
    perPage = 5
    total = len(lookups)
    start = page * perPage
    end = start + perPage
    chunk = lookups[start:end]

    lines = [f"<b>ALL LOOKUPS</b>  <i>(page {page+1})</i>\n<code>{'─'*28}</code>\n"]
    for l in chunk:
        ts = l.get("ts","")[:16].replace("T"," ")
        lines.append(
            f"\n<b>@{l.get('query','?')}</b>\n"
            f"By: <code>{l.get('firstName','?')}</code> (<code>@{l.get('username','?')}</code>)\n"
            f"Phone: <tg-spoiler><code>{l.get('phone','N/A') or 'N/A'}</code></tg-spoiler>   "
            f"Country: <code>{l.get('country','N/A') or 'N/A'}</code>\n"
            f"Status: <code>{'Success' if l.get('success') else 'Failed'}</code>   Time: <code>{ts}</code>"
        )

    navBtns = []
    if page > 0:
        navBtns.append(InlineKeyboardButton("Prev", callback_data=f"adm_lookups_{page-1}"))
    if end < total:
        navBtns.append(InlineKeyboardButton("Next", callback_data=f"adm_lookups_{page+1}"))

    kb = []
    if navBtns:
        kb.append(navBtns)
    kb.append([InlineKeyboardButton("Back to Dashboard", callback_data="adm_dashboard")])

    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def cbAdminToday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = loadDb()
    todayStr = date.today().isoformat()
    todayLookups = [l for l in db["lookups"] if l["ts"].startswith(todayStr)]
    uniqueUsers = len(set(l["userId"] for l in todayLookups))
    successful = len([l for l in todayLookups if l["success"]])

    lines = [f"<b>TODAY'S ACTIVITY</b>\n<code>{'─'*28}</code>\n"]
    lines.append(f"\n<b>Total Lookups</b>   <code>{len(todayLookups)}</code>")
    lines.append(f"<b>Unique Users</b>    <code>{uniqueUsers}</code>")
    lines.append(f"<b>Successful</b>      <code>{successful}</code>")
    lines.append(f"<b>Failed</b>          <code>{len(todayLookups)-successful}</code>\n")
    lines.append(f"<code>{'─'*28}</code>")

    for l in reversed(todayLookups[-10:]):
        ts = l.get("ts","")[11:16]
        lines.append(
            f"\n<code>{ts}</code>  <b>@{l.get('query','?')}</b>\n"
            f"<code>{'Success' if l.get('success') else 'Failed'}</code>  by {l.get('firstName','?')}"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="adm_dashboard")]])
    )


async def cbAdminRate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = loadDb()
    total = len(db["lookups"])
    successful = len([l for l in db["lookups"] if l["success"]])
    failed = total - successful
    rate = round(successful / total * 100, 1) if total else 0

    topUsers = {}
    for l in db["lookups"]:
        uid = l.get("userId")
        topUsers[uid] = topUsers.get(uid, 0) + 1
    topSorted = sorted(topUsers.items(), key=lambda x: x[1], reverse=True)[:5]

    lines = [f"<b>SUCCESS RATE</b>\n<code>{'─'*28}</code>\n"]
    lines.append(f"\n<b>Total Lookups</b>   <code>{total}</code>")
    lines.append(f"<b>Successful</b>      <code>{successful}</code>")
    lines.append(f"<b>Failed</b>          <code>{failed}</code>")
    lines.append(f"<b>Success Rate</b>    <code>{rate}%</code>\n")
    lines.append(f"<code>{'─'*28}</code>")
    lines.append(f"\n<b>TOP USERS BY LOOKUPS</b>")
    for uid, count in topSorted:
        userInfo = db["users"].get(str(uid), {})
        name = userInfo.get("firstName","?")
        lines.append(f"<code>{name}</code>  <code>{count} lookups</code>")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="adm_dashboard")]])
    )


async def cbAdminRecent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = loadDb()
    recent = list(reversed(db["lookups"][-20:]))

    lines = [f"<b>RECENT QUERIES</b>\n<code>{'─'*28}</code>\n"]
    for l in recent:
        ts = l.get("ts","")[:16].replace("T"," ")
        lines.append(
            f"\n<code>{ts}</code>\n"
            f"Query: <b>@{l.get('query','?')}</b>\n"
            f"By: <code>{l.get('firstName','?')}</code>  |  "
            f"<code>{'Success' if l.get('success') else 'Failed'}</code>"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="adm_dashboard")]])
    )


async def cbAdminBroadcastPrompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["awaitingBroadcast"] = True
    await query.edit_message_text(
        "<b>BROADCAST</b>\n\nSend the message you want to broadcast to all users.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="adm_dashboard")]])
    )
    return AWAIT_BROADCAST


async def receiveBroadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaitingBroadcast"):
        return ConversationHandler.END
    ctx.user_data["awaitingBroadcast"] = False
    db = loadDb()
    text = update.message.text
    sent = 0
    failed = 0
    for uid in db["users"]:
        try:
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=f"<b>ANNOUNCEMENT</b>\n\n{text}\n\n<i>@drazeforce</i>",
                parse_mode=ParseMode.HTML
            )
            sent += 1
        except:
            failed += 1
    await update.message.reply_text(
        f"<b>Broadcast Complete</b>\n\nSent: <code>{sent}</code>\nFailed: <code>{failed}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


async def cbAdminDashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    stats = getAdminStats()
    msg = (
        f"<b>ADMIN DASHBOARD</b>\n"
        f"<code>{'─'*28}</code>\n\n"
        f"<b>Total Users</b>      <code>{stats['totalUsers']}</code>\n"
        f"<b>Total Lookups</b>    <code>{stats['totalLookups']}</code>\n"
        f"<b>Today's Lookups</b>  <code>{stats['todayLookups']}</code>\n"
        f"<b>Success Rate</b>     <code>{stats['successRate']}%</code>\n\n"
        f"<code>{'─'*28}</code>"
    )
    await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=adminDashboardKb())


async def cbAdminClose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.delete_message()


async def cbMaintenanceToggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = loadDb()
    currentState = db.get("maintenance", False)
    action = "disable" if currentState else "enable"
    ctx.user_data["pendingMaintenance"] = not currentState
    await query.edit_message_text(
        f"<b>Maintenance Mode</b>\n\n"
        f"You are about to <b>{action}</b> maintenance mode.\n\n"
        f"Enter the maintenance password to confirm.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="adm_dashboard")]])
    )
    return AWAIT_MAINT_PW


async def receiveMaintPw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    enteredHash = hashlib.sha256(entered.encode()).hexdigest()

    try:
        await update.message.delete()
    except:
        pass

    if enteredHash != MAINT_PASS_HASH:
        await update.message.reply_text(
            "<b>Incorrect password.</b>\n<i>Maintenance state unchanged.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=adminDashboardKb()
        )
        return ConversationHandler.END

    newState = ctx.user_data.pop("pendingMaintenance", False)
    db = loadDb()
    db["maintenance"] = newState
    saveDb(db)

    label = "ENABLED" if newState else "DISABLED"
    await update.message.reply_text(
        f"<b>Maintenance Mode {label}</b>\n\n"
        f"<i>{'Users will now see a maintenance message.' if newState else 'Bot is back online for all users.'}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=adminDashboardKb()
    )
    return ConversationHandler.END


async def cbBackMain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = update.effective_user
    name = u.first_name or "there"
    msg = (
        f"<b>Welcome, {name}</b>\n\n"
        f"<code>Telegram Username to Phone Lookup</code>\n\n"
        f"Find detailed profile information and phone numbers\n"
        f"linked to any Telegram username.\n\n"
        f"<i>@drazeforce</i>"
    )
    await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=mainMenuKb())
    return ConversationHandler.END


async def cbTgToNum(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancelKb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="back_main")]])
    await query.edit_message_text(
        "<b>LOOKUP</b>\n\n"
        "Send the username  <i>or</i>  forward any message\n"
        "from the target user.\n\n"
        "<i>Example:  drazeforce  or  @drazeforce</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancelKb
    )
    return AWAIT_INPUT


async def cbMyStats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = update.effective_user
    s = getUserStats(u.id)
    joined   = s["joinedAt"][:10] if s["joinedAt"] != "N/A" else "N/A"
    lastSeen = s["lastSeen"][:10] if s["lastSeen"] != "N/A" else "N/A"
    msg = (
        f"<b>YOUR STATS</b>\n"
        f"<code>{'━'*26}</code>\n"
        f"<b>Total Lookups</b>       <code>{s['total']}</code>\n"
        f"<b>Lookups Today</b>       <code>{s['today']}</code>\n"
        f"<b>Successful</b>          <code>{s['successful']}</code>\n"
        f"<b>Member Since</b>        <code>{joined}</code>\n"
        f"<b>Last Active</b>         <code>{lastSeen}</code>\n"
        f"<code>{'━'*26}</code>\n"
        f"<i>@drazeforce</i>"
    )
    await query.edit_message_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Menu", callback_data="back_main")]])
    )


async def receiveInput(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    msg = update.message
    query = None

    db = loadDb()
    if db.get("maintenance"):
        await msg.reply_text(
            "<b>Bot Under Maintenance</b>\n\n<i>Please check back shortly.</i>",
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    if getattr(msg, "forward_origin", None):
        fwd = msg.forward_origin
        if hasattr(fwd, "sender_user") and fwd.sender_user:
            query = fwd.sender_user.username or str(fwd.sender_user.id)
        elif hasattr(fwd, "chat") and fwd.chat:
            query = fwd.chat.username or str(fwd.chat.id)

        if not query:
            await msg.reply_text(
                "<b>Could Not Extract Identity</b>\n\n"
                "This user has hidden their identity in forwards.\n"
                "Try entering their username or user ID manually.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="back_main")]])
            )
            return AWAIT_INPUT
    else:
        raw = msg.text.strip().lstrip("@") if msg.text else ""
        isNumericId = raw.lstrip("-").isdigit()
        if isNumericId:
            query = raw
        else:
            if not raw or len(raw) < 3 or len(raw) > 32 or not all(c.isalnum() or c == "_" for c in raw):
                await msg.reply_text(
                    "<b>Invalid input.</b>\n\n"
                    "Send a username (3-32 chars, letters/numbers/underscores)\n"
                    "or a numeric Telegram user ID.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="back_main")]])
                )
                return AWAIT_INPUT
            query = raw

    displayQuery = f"@{query}" if not str(query).lstrip("-").isdigit() else query
    loadingMsg = await msg.reply_text(
        f"<b>Looking up</b>  <code>{displayQuery}</code>\n<i>Please wait...</i>",
        parse_mode=ParseMode.HTML
    )

    data, err = await fetchUserInfo(query)
    await loadingMsg.delete()

    if err == "timeout":
        await msg.reply_text(
            "<b>Request Timed Out</b>\n\n"
            "The API is taking too long right now.\n"
            "<i>Try again in a moment.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, query, None, False)
        return ConversationHandler.END

    if err or data is None:
        await msg.reply_text(
            "<b>Service Unavailable</b>\n\n"
            "The lookup API is currently unreachable.\n"
            "<i>Try again shortly.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, query, None, False)
        return ConversationHandler.END

    phoneInfo = data.get("phone_info", {})
    if not phoneInfo.get("success"):
        reason = phoneInfo.get("message", "Could not fetch details")
        await msg.reply_text(
            f"<b>Lookup Failed</b>\n\n"
            f"<code>{reason}</code>\n\n"
            f"<code>{displayQuery}</code> may not exist or has no linked number.",
            parse_mode=ParseMode.HTML,
            reply_markup=afterResultKb()
        )
        logLookup(u.id, u.username, u.first_name, query, data, False)
        return ConversationHandler.END

    resultText = buildResultMsg(data)
    pfp = data.get("profile_pic")

    if pfp:
        try:
            photoMsg = await msg.reply_photo(photo=pfp)
            await photoMsg.reply_text(resultText, parse_mode=ParseMode.HTML, reply_markup=afterResultKb())
        except Exception:
            await msg.reply_text(resultText, parse_mode=ParseMode.HTML, reply_markup=afterResultKb())
    else:
        await msg.reply_text(resultText, parse_mode=ParseMode.HTML, reply_markup=afterResultKb())

    logLookup(u.id, u.username, u.first_name, query, data, True)
    return ConversationHandler.END


async def inlineQuery(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query.query.strip().lstrip("@")
    if not q or len(q) < 3:
        await update.inline_query.answer([], cache_time=0)
        return
    results = [
        InlineQueryResultArticle(
            id=q,
            title=f"Look up @{q}",
            description="Tap to fetch profile and phone info",
            input_message_content=InputTextMessageContent(
                f"<b>Lookup initiated for</b> <code>@{q}</code>\n\n"
                f"<i>Open the bot to see the result.</i>",
                parse_mode=ParseMode.HTML
            ),
        )
    ]
    await update.inline_query.answer(results, cache_time=0)


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<i>Use the menu to navigate.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=mainMenuKb()
    )


async def errorHandler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Update %s caused error: %s", update, ctx.error)


# ── Keep-alive server + self-ping ─────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass  # suppress access logs

def startHealthServer():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    log.info("Health server on port %d", port)
    server.serve_forever()

def startSelfPing():
    # Wait for server to start
    time.sleep(15)
    url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not url:
        log.info("No RENDER_EXTERNAL_URL set — self-ping disabled")
        return
    log.info("Self-ping started → %s", url)
    while True:
        try:
            urllib.request.urlopen(url, timeout=10)
            log.info("Self-ping OK")
        except Exception as e:
            log.warning("Self-ping failed: %s", e)
        time.sleep(300)  # ping every 5 minutes


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
        allow_reentry=True,
        per_message=False,
    )

    lookupConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbTgToNum, pattern="^tgtonum$")],
        states={
            AWAIT_INPUT: [MessageHandler((filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, receiveInput)],
        },
        fallbacks=[CallbackQueryHandler(cbBackMain, pattern="^back_main$")],
        allow_reentry=True,
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    maintConv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cbMaintenanceToggle, pattern="^adm_maintenance$")],
        states={
            AWAIT_MAINT_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, receiveMaintPw)],
        },
        fallbacks=[CallbackQueryHandler(cbAdminDashboard, pattern="^adm_dashboard$")],
        allow_reentry=True,
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    app.add_error_handler(errorHandler)
    app.add_handler(adminConv)
    app.add_handler(lookupConv)
    app.add_handler(maintConv)
    app.add_handler(CommandHandler("start", cmdStart))
    app.add_handler(CommandHandler("stats", cmdStats))
    app.add_handler(InlineQueryHandler(inlineQuery))
    app.add_handler(CallbackQueryHandler(cbBackMain,       pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cbMyStats,        pattern="^myStats$"))
    app.add_handler(CallbackQueryHandler(cbAdminDashboard, pattern="^adm_dashboard$"))
    app.add_handler(CallbackQueryHandler(cbAdminUsers,     pattern="^adm_users_"))
    app.add_handler(CallbackQueryHandler(cbAdminLookups,   pattern="^adm_lookups_"))
    app.add_handler(CallbackQueryHandler(cbAdminToday,     pattern="^adm_today$"))
    app.add_handler(CallbackQueryHandler(cbAdminRate,      pattern="^adm_rate$"))
    app.add_handler(CallbackQueryHandler(cbAdminRecent,    pattern="^adm_recent$"))
    app.add_handler(CallbackQueryHandler(cbAdminBroadcastPrompt, pattern="^adm_broadcast$"))
    app.add_handler(CallbackQueryHandler(cbAdminClose,     pattern="^adm_close$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    # Start keep-alive health server
    threading.Thread(target=startHealthServer, daemon=True).start()
    # Start self-ping to prevent Render free tier spin-down
    threading.Thread(target=startSelfPing, daemon=True).start()

    log.info("Bot running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()