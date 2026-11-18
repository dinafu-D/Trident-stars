TRIDENT REMINDER BOT  V3.57.1  -  full set of files
====================================================
Files (keep these exact names and folders):
  trident_alert.py                     the bot
  requirements.txt                     Python packages
  .github/workflows/trident_alert.yml  the workflow (started by cron-job.org)
  state/sent_stars.json                memory of alerts already sent (the bot updates it)
  README_BOT.txt                       this note

WHAT IT SENDS (NIFTY + SENSEX, Dhan data)
  1. Star groups (V3.57 rule) on 5m, 15m, 1h, 4h, 1D, 1W, 1M - only when all 3 stars are formed.
     Every star with price, date and % difference. The message is deleted when the group is taken.
  2. Box breaks on 1h, 4h, 1D (U / D, with how long the box lasted).
  3. Dark EOB / Dark Sweep EOB on 15m.
  4. "✅ TRIDENT bot running" once a day (first run after 09:15 that loaded the data).
  5. "⚠️ TRIDENT bot problem" + the reason, if the Dhan login or the data fails (each problem once a day).

SECRETS (Settings > Secrets and variables > Actions) - deleting files does NOT delete these:
  TELEGRAM_BOT_TOKEN  TELEGRAM_CHAT_ID  DHAN_CLIENT_ID  DHAN_PIN  DHAN_TOTP_SECRET
  (DHAN_ACCESS_TOKEN is not needed any more.)  Type them only in GitHub, never in chat.

UPLOAD
  1. Unzip this zip on the laptop. Open the folder "TridentBot".
  2. GitHub > your repo > Add file > Upload files.
  3. Select EVERYTHING inside "TridentBot" (Ctrl+A), including the ".github" and "state" folders,
     and drag it into the upload box. Folders keep their structure.
  4. Commit changes. Check the repo shows: .github / state / README_BOT.txt / requirements.txt / trident_alert.py
     and that .github/workflows/trident_alert.yml exists.

TEST
  Actions > TRIDENT alert > Run workflow.  In market hours you get "✅ TRIDENT bot running" (or "⚠️ ..." with the reason).
  Outside market hours the run just says "outside market hours, exit" - that is normal.

CRON-JOB.ORG (starts the bot every 5 minutes)
  URL     https://api.github.com/repos/dinafu-D/Trident-stars/actions/workflows/trident_alert.yml/dispatches
  Method  POST
  Headers Accept: application/vnd.github+json
          Authorization: Bearer <your GitHub token>      (type the token only in cron-job.org)
          X-GitHub-Api-Version: 2022-11-28
  Body    {"ref":"main"}
  Times   every 5 minutes, Monday-Friday, 09:00-15:59, time zone Asia/Kolkata
  Token   GitHub > Settings > Developer settings > Fine-grained tokens > Generate new token
          Repository access: Only select repositories > Trident-stars
          Permissions: Actions = Read and write.   (Needed again because the repo is private now.)

SWITCHES (optional, add under "env:" of the Run bot step in the yml)
  STAR_TFS=5m,15m,1h,4h,1D,1W,1M   BOX_TFS=1h,4h,1D   EOB_ON=1   ALERT_2STAR=0   DELETE_TAKEN=1   STATUS_MSG=1
