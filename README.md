# ScoreHub: live presentation scoring

300 voters, 40 presenters, one score (1-10) per staff ID per presenter, QR code per presenter, live projector screen, hidden or live leaderboard.

## Run locally (debug)
    pip install -r requirements.txt
    flask --app app seed-demo      # optional: 300 sample staff + 40 sample presenters
    python app.py                  # http://localhost:5000  (debug mode, auto-reload)
Admin: http://localhost:5000/admin  (password from ADMIN_PASSWORD, default `admin123` - CHANGE IT)
Run the automated check: `python test_flow.py`

## Settings (.env file)
Copy `.env.example` to `.env`, then paste your Neon string after `DATABASE_URL=` (no quotes, `&` is fine). On Render, add the same names under Environment instead; never upload `.env` to GitHub.

## Staff ID and voting rules
- Each staff ID exists once. Number-only IDs ignore leading zeros, spaces, full-width and Bengali digits, so 004512 and 4512 are the same person.
- A staff file with a repeated ID is rejected; nothing is imported and the rows are listed.
- One vote per staff ID per presenter, enforced by the database. A voter can change their score while voting is open; it replaces the earlier score, never adds a vote.
- After typing their ID, the voter sees their name ("Scoring as ...") and whether they already scored.
- More than 200 unknown IDs from one network address in 10 minutes pauses ID checks for that address (stops guessing).
- Databases from older versions are upgraded on start: duplicate IDs are merged and the earliest vote is kept (backup written first).

## Fixing mistakes (Admin > Data)
- Delete one presenter or one staff ID, search staff IDs, remove duplicate presenters, reset all scores, or clear everything (type DELETE to confirm; a CSV backup of scores is saved first).
- Import has "Replace the current list" for staff and presenters; presenters already in the list are skipped on "Add".
- Event title and the public QR address can be changed on the Data page.

## Event-day flow
1. Admin > Import: upload the staff CSV (staff_id,name) and presenters CSV (name,portfolio,staff_id optional).
2. Admin > Photos: bulk-upload presenter photos (file name = presenter number, staff ID or exact name, e.g. `12.jpg`). Photos are square-cropped to 480px and stored in the database, so they survive restarts on free hosting.
3. Admin > QR code (one presenter at a time, follows the open presenter, full screen button) (each card shows the presenter photo above the QR). Set BASE_URL to your public address first.
4. Open /display on the projector.
5. Per presenter: **Open voting** -> audience scans and scores -> **Close and show result**.
6. At the end, choose "Hide scores until the end" (if wanted) and press **Reveal final leaderboard**.
7. Download votes + results CSV from the control panel.

## Data safety for the 7-8 hour event
- A vote is committed to the database before the voter sees "recorded"; vote + running total are one transaction.
- Unique constraint (staff_id, presenter) makes duplicates impossible, even with double taps or retries.
- SQLite runs in WAL mode with synchronous=FULL (survives crashes/restarts). Locally the file is data/scores.db.
- A CSV snapshot of all votes is written to ./backups every 5 minutes (last 60 kept).
- Phones retry automatically on a bad connection.
- **Free hosting: use a free Postgres** (Neon or Supabase) and set DATABASE_URL. Free web hosts (Render etc.) wipe local files on restart, so never rely on SQLite there.

## Deploy free (Render + Neon)
1. Create a free Postgres at neon.tech; copy its connection string.
2. Push this folder to GitHub. On render.com create a Web Service from the repo (free plan).
   Build: `pip install -r requirements.txt`   Start: uses the Procfile (gunicorn).
3. Environment variables: DATABASE_URL, ADMIN_PASSWORD, SECRET_KEY (random), BASE_URL (your https://xxx.onrender.com address).
4. Free services sleep when idle: open the site ~10 minutes before the event, and optionally ping it every 5 minutes with UptimeRobot (free).
5. Download the votes CSV every hour as an extra safety copy.
