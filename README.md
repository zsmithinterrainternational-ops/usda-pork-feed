# usda-pork-feed

Pulls USDA report 2498 (LM_PK602, National Daily Pork - Negotiated Negotiated FOB Plant - Pork Cuts) on a schedule
with GitHub Actions and publishes `docs/usda_pork.json` through GitHub Pages. The Reports Hub USDA tab reads it,
so the USDA prices stay current without any PC running.

Only public USDA data is stored here. No Interra data, no focus list.

## Set up (once, in the browser)

1. **New repository** on github.com: name `usda-pork-feed`, **Public**, no README. Create.
2. On the empty repo page click **uploading an existing file**. Drag in everything from this folder
   **except** the `.github` folder: `README.md`, `cloud_run.py`, `cloud_config.json`, the `usda_pork` folder and the
   `docs` folder. Commit.
3. **Add file > Create new file**. Name it exactly `.github/workflows/usda.yml` and paste the contents of
   `.github/workflows/usda.yml` from this folder (open it in Notepad). Commit.
4. **Actions** tab. If asked, enable workflows. Click **USDA daily > Run workflow > Run workflow**.
   The first run loads history from Oct 12, 2023 (several minutes). A green check means done.
5. **Settings > Pages**: Source **Deploy from a branch**, Branch **main**, folder **/docs**, Save.
   After a minute or two, `https://YOUR-USERNAME.github.io/usda-pork-feed/` shows the latest report.
6. On the PC, point the hub at the feed (see the Reports Hub update 26 instructions).

## Schedule (Eastern)

Weekdays about 4:15 PM and 6:00 PM, plus 8:30 AM Monday to Saturday as a catch-up (one hour later during
daylight time, because GitHub schedules run on UTC). GitHub can start scheduled runs late when it is busy.
**Run workflow** on the Actions tab runs it immediately.

If USDA does not answer, the run turns red and GitHub emails you; the feed keeps the last good data and says
"Last run failed". The next scheduled run fills the gap.
