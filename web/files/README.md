# Maple Tracker – setup (about 20 minutes, all free tiers)

1. **Supabase**: create a project, open *SQL Editor*, run `supabase/schema.sql`. From *Project Settings > API* copy the **Project URL** and the **anon public key**.
2. **Configure**: put the URL/key into `web/index.html` (`SB_URL`, `SB_KEY`) and into `maple_exp_tracker.py` (`API_URL`, `API_KEY`). The anon key is meant to be public; row-level security in the schema limits what it can do.
3. **Website (Cloudflare Pages)**: create a Pages project from your GitHub repo with build output directory `web` (no build command), or run `npx wrangler pages deploy web`.
4. **Downloadable app**: push this folder to a GitHub repo, set `DOWNLOAD_URL` in `web/index.html` to `https://github.com/<you>/<repo>/releases/latest/download/MapleTracker-windows.zip`, then publish a release with `git tag v1.0.0 && git push --tags`. The workflow builds the Windows app with PyInstaller and attaches the zip. Users unzip and run `MapleTracker.exe` – no Python needed.

Notes: the exe is unsigned, so Windows SmartScreen will warn once. Moderate/delete rows from the Supabase dashboard. Submissions are self-reported.
