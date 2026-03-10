
TG TO NUM BOT  —  SETUP GUIDE
Created by @drazeforce
══════════════════════════════════════


STEP 1 — ADD YOUR BOT TOKEN
─────────────────────────────
Open bot.py and find this line near the top:

    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

Replace it with your token from @BotFather.


STEP 2 — INSTALL DEPENDENCIES
──────────────────────────────
    pip install -r requirements.txt


STEP 3 — RUN
──────────────
Normal run:
    python bot.py

Auto-restart on crash (Windows):
    Double-click start_bot.bat


══════════════════════════════════════
FEATURES
══════════════════════════════════════

/start        Main menu
/stats        Your personal lookup stats
/admin        Admin panel (password: pootilangaadi)

Admin dashboard:
  All Users, All Lookups, Today Activity,
  Success Rate, Recent Queries, Broadcast

Lookup methods:
  Enter Username  — type it manually
  Forward Message — forward from target, username auto-extracted

Result output:
  Full profile, flags, phone in spoiler tag, API quota

Inline mode:
  @yourbotname drazeforce  (in any chat)

Auto-restart:
  Use start_bot.bat on Windows — restarts on crash automatically

Data:
  Stored in db.json (auto-created on first run)