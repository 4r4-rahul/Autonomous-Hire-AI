# AutonomousHire Autofill (Chrome extension)

Fills job-application forms from your AutonomousHire profile **in your own logged-in
browser**. The extension scrapes the form on the page you're viewing, asks the local
backend for the right answer to each field (reusing the same profile data and the
truthful work-authorization picker), fills them, and highlights the result. **You review,
solve any CAPTCHA, and submit the form yourself** — nothing is submitted automatically.

This replaces the fragile spawned-automation-browser flow: no separate window, no
anti-bot fight, no GUI-session issues.

## One-time install

1. Make sure the backend is running:
   ```
   cd /Users/rahul/Workspace/Job_Finder
   .venv/bin/uvicorn api.main:app --port 8000
   ```
2. Open `chrome://extensions` (or `edge://extensions`).
3. Toggle **Developer mode** on (top-right).
4. Click **Load unpacked** and select the `extension/` folder.
5. (Optional) Pin the extension so its icon is visible in the toolbar.

## Use it

1. In your normal browser, open a job application page (Greenhouse, Lever, Ashby,
   workday, a company careers form — anywhere you'd apply, logged in if needed).
2. Click the **AutonomousHire Autofill** toolbar icon → **Fill this application**.
3. The form fills. A panel appears top-right of the page:
   - **✅ Filled** — fields set for you (green outline).
   - **✋ Needs you** — fields left for you (yellow outline) with a recommendation,
     e.g. cover letters, anything sensitive without an exact option, or a custom
     widget it couldn't set.
4. **Review the highlighted fields** (work-auth, country, etc. should be correct),
   **solve the CAPTCHA**, and click the site's own **Submit** button.

## What it fills

Identity, contact, address, work authorization (picks the truthful "authorized + needs
sponsorship" option, never "by nationality"), sponsorship Yes/No, EEO (only on an exact
option match — never blind-guesses a protected-class value), "how did you hear", start
date, salary, years of experience, current company/title, languages. Open-ended essays
and cover letters are intentionally left for you.

## Notes / limits (v1)

- Needs the backend on `http://localhost:8000`. If you see "Backend unreachable" in the
  panel, start uvicorn (step 1).
- Custom React-Select dropdowns are filled best-effort; if one doesn't take, the panel
  shows the recommended option to pick.
- It never clicks Submit. That's deliberate — you stay in control of the actual send.
