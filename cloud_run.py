"""Cloud runner for the USDA pork feed (GitHub Actions).

First run (no stored data): backfills report 2498 (LM_PK602) from `backfill_since` (or `backfill_years`).
Later runs: daily pull (new days first, then the optional revision re-check).
Always writes usda_pork.json (in docs/ or the root) (the payload the Reports Hub USDA tab draws) and
status.json, even when USDA does not answer, so the page can say so.
Exit code 1 when the USDA pull failed, so GitHub marks the run red and emails you.
"""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# works with collect.py in usda_pork/ or next to this file (flat upload)
SRC = next(p for p in (ROOT / "usda_pork" / "collect.py", ROOT / "collect.py") if p.exists())
spec = importlib.util.spec_from_file_location("collect", SRC)
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


def main(fetch=None, pause=None, today=None, now=None):
    cfg = json.loads((ROOT / "cloud_config.json").read_text(encoding="utf-8"))
    data_dir = ROOT / "data"
    first = not (data_dir / "usda_pork" / "cutout.csv").exists()
    mode = "backfill" if first else "daily"
    print("[cloud] %s run, data in %s" % (mode, data_dir))
    ok, err = True, ""
    try:
        if first:
            if cfg.get("backfill_since"):
                C.run("backfill", cfg, str(data_dir), since=cfg["backfill_since"], fetch=fetch, pause=pause, today=today)
            else:
                C.run("backfill", cfg, str(data_dir), years=cfg.get("backfill_years", 3), fetch=fetch, pause=pause, today=today)
        else:
            C.run("daily", cfg, str(data_dir), fetch=fetch, pause=pause, today=today)
    except C.CollectError as e:
        ok, err = False, str(e)
        print("[cloud] USDA pull failed: %s" % e)
    payload = C.export(cfg, str(data_dir), {}, today=today, now=now)
    # publish next to index.html: docs/ if that folder exists, otherwise the repository root
    docs = ROOT / "docs" if (ROOT / "docs" / "index.html").exists() else ROOT
    (docs / "usda_pork.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    status = {"built_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
              "mode": mode, "ok": ok, "error": err,
              "latest_report": payload.get("latest_date"), "report_days_stored": payload.get("n_dates_stored"),
              "first_report": payload.get("first_date_stored"),
              "flags": [f.get("text") for f in payload.get("flags", [])]}
    (docs / "status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
    print("[cloud] wrote docs/usda_pork.json (%d KB); latest report %s; ok=%s"
          % ((docs / "usda_pork.json").stat().st_size // 1024, payload.get("latest_date"), ok))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
    
