# Deploying the dashboard (browser only, no laptop scripts)

## 1. Put the data in Google Sheets
- Create a Google Sheet (or several tabs) with your raw data, one header row on top.
- Click **Share → General access → Anyone with the link → Viewer**.
- Copy the link from the address bar (it includes `#gid=...` for the tab you're on).

## 2. Create the GitHub repo
1. Go to github.com → **New repository** → name it e.g. `business-dashboard` → Create.
2. Click **Add file → Upload files** and drag in: `app.py`, `pipeline.py`, `ai.py`, `requirements.txt`, `DEPLOY.md` and the `.streamlit` folder
   (it holds `config.toml`, the light theme). If the `.streamlit` folder is hidden on your computer, instead click
   **Add file → Create new file**, type `.streamlit/config.toml` as the name and paste that file's contents.
3. Click **Commit changes**. (Do NOT upload secrets or your `.env`.)

## 3. Deploy on Streamlit Community Cloud
1. Go to share.streamlit.io and sign in with GitHub.
2. **Create app → Deploy a public app from GitHub** → pick your repo, branch `main`, file `app.py`.
3. Click **Advanced settings → Secrets** and paste the contents of `secrets.toml.example`
   with your real Gemini key and Sheet links.
4. Click **Deploy**. In ~2 minutes you get a URL like `https://your-app.streamlit.app`.

## 4. Share it
- Send the URL to the team. Nobody installs anything.
- To restrict access: app → **Share** → make it private and invite teammates by email.

## How updates flow
- Someone edits / pastes new rows into the Google Sheet.
- The next time anyone opens the dashboard, it re-reads the sheet, re-runs the SQL cleaning
  and KPI calculations, and shows the new numbers. Nothing runs in the background in between.
- Someone who already has it open can press **Refresh now**.
- Prefer live updates on open screens? Set `REFRESH_MODE = "auto"` in Secrets (checks every `REFRESH_MINUTES`).
- To add a new source, add a line under `[data_sources]` in the app's Secrets. No code change.
- To change code later, edit `app.py` directly on GitHub (pencil icon); the app redeploys itself.

## Notes
- Free apps sleep after a few days with no visitors; the first visit wakes them (~30 s).
- "Anyone with the link" sheets are readable by anyone who has the link. For confidential data,
  the next upgrade is a Google service account (private sheet access).

## How the backend works
- `pipeline.py` is the SQL backend (DuckDB, embedded, nothing to install or sign up for).
  - **Stage 1 – cleaning SQL:** trims text, turns blanks/"n/a" into NULL, converts "₹1,200"/"12%" text to numbers,
    drops empty rows/columns and removes duplicate rows.
  - **Stage 2 – KPI SQL:** one aggregate query computes every headline KPI.
  - **Stage 3 – breakdown SQL:** grouped queries for monthly trend and revenue by product/region.
- Open **SQL behind these numbers** at the bottom of the dashboard to see the live queries.
- Google Sheets stays the source of truth; SQL re-runs on every refresh, so nothing goes stale.

## AI features (need GEMINI_API_KEY)
- **AI-designed dashboard (default):** on open, Gemini reads the columns, works out what kind of data it is and
  designs the dashboard: KPIs (including unusual columns like discounts, returns, ratings), charts, filters and
  which breakdowns to watch. Every query is test-run; broken ones are auto-repaired or dropped. SQL calculates
  every number. The design is reused until the columns change (no AI cost per visit).
- **Changes worth a look:** SQL compares the latest month with the previous one (same days if the month is
  partial) and flags big moves, e.g. "Revenue in Zone = North ▼ 85%".
- **AI insights:** written automatically once per data change (button when filters are on).
- If Gemini is down or out of quota, the app falls back to the Standard dashboard automatically.
- **AI column mapping:** when column names are unusual ("Net Rcpts", "Zone"), Gemini works out which column is
  revenue, region, etc. AI-matched fields show 🤖 in *Review column mapping*.
- **Ask your data:** type a question; Gemini writes SQL, the SQL runs on your data (read-only, locked down),
  and you get the answer, a chart, a table and the SQL used. Broken SQL is fixed automatically once.
- **AI-suggested analyses:** Gemini proposes the most useful analyses for the dataset, each run as SQL and charted.
- **AI analysis (Dashboard tab):** a written summary of the KPIs on screen.
- Privacy: the AI only sees column names, types and up to 3 example values per column (and the KPI totals for
  the summary), never the full dataset.
