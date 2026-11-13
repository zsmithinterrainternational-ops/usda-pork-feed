"""
USDA pork module - collection layer.

Pulls USDA AMS report 2498 (LM_PK602, National Daily Pork Report, FOB Plant,
Negotiated Sales, Afternoon) from the LMPR DataMart API (no key required),
keeps a permanent local history, logs every run, and exports a compact data
payload for the module page.

Standard library only. Nothing here touches OneDrive: the history lives in the
local data_dir; only the finished bundle goes to output_dir.
"""
import csv
import json
import os
import time
import urllib.request
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://mpr.datamart.ams.usda.gov/services/v1.1/reports/2498"
MODULE_ID = "usda_pork"
CHUNK_DAYS = 30          # USDA caps a request at 180 days; 30 keeps responses small
PAUSE_SECONDS = 2.0      # pause between requests (USDA blocks high-frequency callers)
# Days of history sent to the page. 0 = everything stored (config: modules.usda_pork.export_days)

PRIMALS = ["carcass", "loin", "butt", "picnic", "rib", "ham", "belly"]

# section name -> {json field: our column}
CUTOUT_MAP = {
    "Cutout and Primal Values": {
        "total_loads_date_1": "loads_total",
        "pork_carcass": "v_carcass", "pork_loin": "v_loin", "pork_butt": "v_butt",
        "pork_picnic": "v_picnic", "pork_rib": "v_rib", "pork_ham": "v_ham",
        "pork_belly": "v_belly",
    },
    "Change From Prior Day": {
        "chg_prev_carcass": "c_carcass", "chg_prev_loin": "c_loin", "chg_prev_butt": "c_butt",
        "chg_prev_pic": "c_picnic", "chg_prev_rib": "c_rib", "chg_prev_ham": "c_ham",
        "chg_prev_belly": "c_belly",
    },
    "5-Day Average Cutout and Primal Values": {
        "avg_5day_carcass": "a_carcass", "avg_5day_loin": "a_loin", "avg_5day_butt": "a_butt",
        "avg_5day_pic": "a_picnic", "avg_5day_rib": "a_rib", "avg_5day_ham": "a_ham",
        "avg_5day_belly": "a_belly",
    },
    "Current Volume": {
        "temp_cuts_total_load": "loads_cuts",
        "temp_process_total_load": "loads_process",
    },
}
CUTOUT_FIELDS = [c for m in CUTOUT_MAP.values() for c in m.values()]
CUTOUT_COLS = (["report_date", "published_ct", "is_correction"] + CUTOUT_FIELDS
               + ["first_seen_utc", "last_changed_utc"])
PRICE_COLS = ["report_date", "section", "item", "total_lbs", "low", "high", "wavg"]
PRICE_FIELDS = ["total_lbs", "low", "high", "wavg"]
ITEM_COLS = ["section_idx", "section", "item", "order", "first_seen", "last_seen"]
REV_COLS = ["detected_utc", "table", "report_date", "section", "item", "field", "kind", "old", "new"]
CUT_REQUIRED = ["Item_Description", "total_pounds", "price_range_low", "price_range_high",
                "weighted_average"]


class CollectError(Exception):
    pass


# ----------------------------------------------------------------- utilities
def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def mdy_to_iso(s):
    return datetime.strptime(s, "%m/%d/%Y").date().isoformat()


def iso_to_mdy(s):
    d = date.fromisoformat(s)
    return "%02d/%02d/%04d" % (d.month, d.day, d.year)


def num(s):
    """USDA strings ('1,168,500', '.86', '-.47', None) -> float or None."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s == "":
        return None
    return float(s)


def canon(x):
    """Canonical text for a number so re-runs compare equal."""
    if x is None:
        return ""
    s = ("%.4f" % x).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def atomic_write_csv(path, cols, rows):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    os.replace(tmp, path)


def read_csv(path):
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv(path, cols, rows):
    if not rows:
        return
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


# ------------------------------------------------------------------- holidays
def _observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def usda_holidays(year):
    """The six holidays USDA says normally have no report (observed dates)."""
    def nth_weekday(y, m, wd, n):
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def last_monday(y, m):
        d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
        return d - timedelta(days=(d.weekday() - 0) % 7)

    return {
        _observed(date(year, 1, 1)), last_monday(year, 5), _observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1), nth_weekday(year, 11, 3, 4), _observed(date(year, 12, 25)),
    }


def is_expected_report_day(d):
    if d.weekday() >= 5:
        return False
    return d not in usda_holidays(d.year) and d not in usda_holidays(d.year + 1)


# ----------------------------------------------------------------------- fetch
def fetch_json(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json", "User-Agent": "ReportsHub/1.0 (USDA pork module)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8-sig"))
        except Exception as e:  # network, HTTP, JSON
            last = e
            if attempt < retries - 1:
                time.sleep((15, 60, 120)[min(attempt, 2)])
    raise CollectError("request failed after %d tries: %s (%s)" % (retries, last, url))


def window_url(start_iso, end_iso):
    return "%s?q=report_date=%s:%s&allSections=true" % (
        BASE_URL, iso_to_mdy(start_iso), iso_to_mdy(end_iso))


# ----------------------------------------------------------------------- parse
def parse_response(payload):
    """allSections response -> dict(dates, cutout, prices, item_order)."""
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise CollectError("unexpected response shape: %s" % type(payload).__name__)
    empty = {"dates": [], "cutout": {}, "prices": {}, "order": OrderedDict(), "sections": []}
    if not payload:
        return empty          # window with no reports (weekend / holiday)
    sections = OrderedDict()
    for s in payload:
        if "reportSection" not in s or "results" not in s:
            raise CollectError("response section missing reportSection/results keys: %s"
                               % list(s.keys())[:8])
        sections[s["reportSection"]] = s["results"]

    summary = sections.get("Summary")
    if summary is None:
        raise CollectError("no Summary section in response; sections=%s" % list(sections))
    if not summary:
        return empty
    dates, cutout = {}, {}
    for r in summary:
        d = mdy_to_iso(r["report_date"])
        dates[d] = True
        cutout[d] = {c: "" for c in CUTOUT_FIELDS}
        cutout[d]["published_ct"] = r.get("published_date") or ""
        cutout[d]["is_correction"] = "" if r.get("is_correction") in (None, "") else str(r["is_correction"])

    for sec, fmap in CUTOUT_MAP.items():
        rows = sections.get(sec)
        if rows is None:
            raise CollectError("section missing from response: %r" % sec)
        for r in rows:
            for src in fmap:
                if src not in r:
                    raise CollectError("field %r missing in section %r (found: %s)"
                                       % (src, sec, sorted(r.keys())))
            d = mdy_to_iso(r["report_date"])
            if d not in cutout:
                continue
            for src, col in fmap.items():
                cutout[d][col] = canon(num(r[src]))

    prices, order = {}, OrderedDict()
    newest = max(dates) if dates else None
    for sec_idx, (sec, rows) in enumerate(sections.items()):
        if not sec.endswith("Cuts"):
            continue
        for r in rows:
            for src in CUT_REQUIRED:
                if src not in r:
                    raise CollectError("field %r missing in section %r (found: %s)"
                                       % (src, sec, sorted(r.keys())))
            d = mdy_to_iso(r["report_date"])
            item = r["Item_Description"]
            if d == newest:
                order.setdefault(sec, [])
                if item not in order[sec]:
                    order[sec].append(item)
            w = num(r["weighted_average"])
            if w is None:
                continue          # no published price that day: not stored, never zero-filled
            prices[(d, sec, item)] = {
                "total_lbs": canon(num(r["total_pounds"])), "low": canon(num(r["price_range_low"])),
                "high": canon(num(r["price_range_high"])), "wavg": canon(w)}
    return {"dates": sorted(dates), "cutout": cutout, "prices": prices, "order": order,
            "sections": [s for s in sections if s.endswith("Cuts")]}


# ----------------------------------------------------------------------- store
class Store:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cutout = {r["report_date"]: r for r in read_csv(self.dir / "cutout.csv")}
        self.prices = {(r["report_date"], r["section"], r["item"]): r
                       for r in read_csv(self.dir / "prices.csv")}
        self.items = OrderedDict(((r["section"], r["item"]), r) for r in read_csv(self.dir / "items.csv"))
        self.by_date = {}
        for k in self.prices:
            self.by_date.setdefault(k[0], set()).add(k)

    def save(self):
        atomic_write_csv(self.dir / "cutout.csv", CUTOUT_COLS,
                         [self.cutout[d] for d in sorted(self.cutout)])
        atomic_write_csv(self.dir / "prices.csv", PRICE_COLS,
                         [dict(report_date=k[0], section=k[1], item=k[2], **{f: v[f] for f in PRICE_FIELDS})
                          for k, v in sorted(self.prices.items())])
        rows = sorted(self.items.values(), key=lambda r: (int(r["section_idx"]), int(r["order"])))
        atomic_write_csv(self.dir / "items.csv", ITEM_COLS, rows)


def upsert(store, parsed, now):
    """Merge parsed data into the store. Returns (stats, revision rows)."""
    st, revs = Counter(), []
    for d in parsed["dates"]:
        known = d in store.cutout
        new_c = parsed["cutout"][d]
        if not known:
            store.cutout[d] = dict(report_date=d, first_seen_utc=now, last_changed_utc=now, **new_c)
            st["dates_new"] += 1
        else:
            old = store.cutout[d]
            changed = False
            for col in CUTOUT_FIELDS + ["is_correction"]:
                if old.get(col, "") != new_c.get(col, ""):
                    revs.append(dict(detected_utc=now, table="cutout", report_date=d, section="",
                                     item="", field=col, kind="changed",
                                     old=old.get(col, ""), new=new_c.get(col, "")))
                    old[col] = new_c.get(col, "")
                    changed = True
            old["published_ct"] = new_c.get("published_ct", old.get("published_ct", ""))
            if changed:
                old["last_changed_utc"] = now
                st["dates_revised"] += 1

        new_keys = {k for k in parsed["prices"] if k[0] == d}
        old_keys = set(store.by_date.get(d, set()))
        for k in sorted(new_keys):
            newv = parsed["prices"][k]
            if k not in old_keys:
                store.prices[k] = dict(newv)
                store.by_date.setdefault(d, set()).add(k)
                st["rows_new"] += 1
                if known:
                    revs.append(dict(detected_utc=now, table="prices", report_date=d, section=k[1],
                                     item=k[2], field="wavg", kind="added", old="", new=newv["wavg"]))
            else:
                oldv = store.prices[k]
                diffs = [f for f in PRICE_FIELDS if oldv[f] != newv[f]]
                for f in diffs:
                    revs.append(dict(detected_utc=now, table="prices", report_date=d, section=k[1],
                                     item=k[2], field=f, kind="changed", old=oldv[f], new=newv[f]))
                if diffs:
                    store.prices[k] = dict(newv)
                    st["rows_changed"] += 1
        if known:
            for k in sorted(old_keys - new_keys):
                revs.append(dict(detected_utc=now, table="prices", report_date=d, section=k[1],
                                 item=k[2], field="wavg", kind="removed",
                                 old=store.prices[k]["wavg"], new=""))
                del store.prices[k]
                store.by_date[d].discard(k)
                st["rows_removed"] += 1

    # item catalogue: order follows the newest report in this pull
    if parsed["dates"]:
        newest = parsed["dates"][-1]
        sec_names = parsed["sections"]
        seen = set()
        for si, sec in enumerate(sec_names):
            for oi, item in enumerate(parsed["order"].get(sec, [])):
                key = (sec, item)
                seen.add(key)
                row = store.items.get(key)
                if row is None:
                    store.items[key] = dict(section_idx=si, section=sec, item=item, order=oi,
                                            first_seen=newest, last_seen=newest)
                    st["items_new"] += 1
                else:
                    row.update(section_idx=si, order=oi)
                    if newest > row["last_seen"]:
                        row["last_seen"] = newest
    return st, revs


# ------------------------------------------------------------------- gap check
def unexpected_gaps(store, today=None, lookback_days=60):
    if not store.cutout:
        return []
    today = today or date.today()
    first = max(date.fromisoformat(min(store.cutout)), today - timedelta(days=lookback_days))
    last = date.fromisoformat(max(store.cutout))
    out, d = [], first
    while d <= last:
        if is_expected_report_day(d) and d.isoformat() not in store.cutout:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------- run log
def log_run(folder, entry):
    with open(Path(folder) / "run_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_run_log(folder):
    p = Path(folder) / "run_log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# ----------------------------------------------------------------------- runs
def _chunks(start, end):
    s = start
    while s <= end:
        e = min(s + timedelta(days=CHUNK_DAYS - 1), end)
        yield s.isoformat(), e.isoformat()
        s = e + timedelta(days=1)


def run(mode, mcfg, data_dir, years=None, since=None, fetch=None, pause=None, today=None):
    """mode: 'daily' or 'backfill'. Returns the run-log entry."""
    fetch = fetch or fetch_json
    pause = PAUSE_SECONDS if pause is None else pause
    today = today or date.today()
    folder = Path(data_dir) / MODULE_ID
    store = Store(folder)
    now = utc_now()
    entry = {"run_utc": now, "mode": mode, "status": "ok", "requests": 0, "dates_in_response": 0,
             "dates_new": 0, "dates_revised": 0, "rows_new": 0, "rows_changed": 0,
             "rows_removed": 0, "items_new": 0, "errors": []}
    try:
        # windows: (start, end, optional). Daily runs fetch the NEW days first in a small request; the
        # look-back re-check for USDA revisions comes after and is optional, so a slow USDA response on
        # the larger re-check cannot cost the new day's prices.
        if mode == "backfill":
            start = date.fromisoformat(since) if since else today - timedelta(days=int(365 * (years or 3)))
            windows = [(a, b, False) for a, b in _chunks(start, today)]
        elif store.cutout:
            lb = int(mcfg.get("lookback_days", 14))
            latest = date.fromisoformat(max(store.cutout))
            start = min(latest - timedelta(days=lb), today)
            windows = []
            if latest + timedelta(days=1) <= today:
                windows += [(a, b, False) for a, b in _chunks(latest + timedelta(days=1), today)]
            windows += [(a, b, True) for a, b in _chunks(start, min(latest, today))]
        else:
            start = today - timedelta(days=30)
            windows = [(a, b, False) for a, b in _chunks(start, today)]
        end = today
        entry["window"] = [start.isoformat(), end.isoformat()]
        all_revs, since_save = [], 0
        for i, (a, b, optional) in enumerate(windows):
            if i:
                time.sleep(pause)
            try:
                payload = fetch(window_url(a, b))
            except CollectError as e:
                if not optional:
                    raise
                entry.setdefault("warnings", []).append("re-check %s -> %s skipped: %s" % (a, b, e))
                print("  %s -> %s: re-check for USDA revisions skipped (USDA did not answer); new days are kept" % (a, b))
                continue
            entry["requests"] += 1
            parsed = parse_response(payload)
            entry["dates_in_response"] += len(parsed["dates"])
            st, revs = upsert(store, parsed, now)
            for k, v in st.items():
                entry[k] += v
            all_revs += revs
            since_save += 1
            print("  %s -> %s: %d report days, +%d rows%s" % (a, b, len(parsed["dates"]), st["rows_new"],
                                                           " (re-check)" if optional else ""))
            if since_save >= 6:
                store.save()
                append_csv(folder / "revisions.csv", REV_COLS, all_revs)
                all_revs, since_save = [], 0
        store.save()
        append_csv(folder / "revisions.csv", REV_COLS, all_revs)
        if store.cutout:
            entry["latest_report_date"] = max(store.cutout)
            entry["earliest_report_date"] = min(store.cutout)
        gaps = unexpected_gaps(store, today)
        entry["unexpected_gaps"] = gaps
        if entry["dates_in_response"] == 0 and mode == "daily":
            entry["note"] = "No report dates in window (weekend/holiday or USDA has not published)."
    except Exception as e:
        entry["status"] = "error"
        entry["errors"].append(str(e))
        log_run(folder, entry)
        raise
    log_run(folder, entry)
    return entry


# ---------------------------------------------------------------------- export
def _focus(mcfg):
    return list(mcfg.get("focus_items") or mcfg.get("watchlist") or [])


def _prev_business_day(d):
    d -= timedelta(days=1)
    while not is_expected_report_day(d):
        d -= timedelta(days=1)
    return d


def pending_today(entry, today=None):
    """Hook for hub.py --wait-for-today: True when today is a normal report day
    and today's report is not stored yet."""
    today = today or date.today()
    if not is_expected_report_day(today):
        return False
    latest = (entry or {}).get("latest_report_date")
    return (not latest) or latest < today.isoformat()


def export(mcfg, data_dir, gcfg, today=None, now=None):
    """Compact payload the module page renders. Pure read; never calls USDA."""
    today = today or date.today()
    now = now or datetime.now()
    folder = Path(data_dir) / MODULE_ID
    store = Store(folder)
    log = read_run_log(folder)
    last_run = log[-1] if log else None
    last_ok = next((e for e in reversed(log) if e.get("status") == "ok"), None)
    base = {
        "report": {"id": "2498", "code": "LM_PK602", "unit": mcfg.get("unit_label", "$/cwt")},
        "built_utc": utc_now(), "focus": _focus(mcfg),
        "last_run": last_run, "last_ok_run": last_ok, "flags": [],
        "live_url": mcfg.get("live_url", ""),
    }
    if not store.cutout:
        base["empty"] = True
        base["flags"].append({"level": "error", "text": "No data yet. Run the backfill first."})
        return base

    latest = max(store.cutout)
    days = int(mcfg.get("export_days", 0) or 0)
    cutoff = (date.fromisoformat(latest) - timedelta(days=days)).isoformat() if days > 0 else ""
    dates = [d for d in sorted(store.cutout) if d >= cutoff]
    di = {d: i for i, d in enumerate(dates)}

    def col(c):
        return [None if store.cutout[d][c] == "" else float(store.cutout[d][c]) for d in dates]

    cut = {"loads": col("loads_total"), "loads_cuts": col("loads_cuts"),
           "loads_process": col("loads_process"),
           "v": {p: col("v_" + p) for p in PRIMALS},
           "c": {p: col("c_" + p) for p in PRIMALS},
           "a": {p: col("a_" + p) for p in PRIMALS},
           "pub": [store.cutout[d]["published_ct"] for d in dates]}

    by_item = {}
    for (d, sec, item), v in store.prices.items():
        if d in di:
            by_item.setdefault((sec, item), []).append(
                [di[d], float(v["wavg"]), float(v["low"]), float(v["high"]), int(float(v["total_lbs"]))])
    sections, items = [], []
    sec_index = {}
    wl_set = set(_focus(mcfg))
    for key, meta in sorted(store.items.items(), key=lambda kv: (int(kv[1]["section_idx"]), int(kv[1]["order"]))):
        sec, item = key
        rows = by_item.get(key)
        wl = (sec + " / " + item) in wl_set or item in wl_set
        if not rows and not wl:
            continue
        if sec not in sec_index:
            sec_index[sec] = len(sections)
            sections.append(sec)
        items.append({"s": sec_index[sec], "n": item, "r": sorted(rows or [])})

    flags = []
    if last_run and last_run.get("status") != "ok":
        flags.append({"level": "error", "text": "Last run failed: " + "; ".join(last_run.get("errors", []))[:300]
                      + ". Showing last good data."})
    exp = _prev_business_day(today)
    if date.fromisoformat(latest) < exp:
        flags.append({"level": "warn", "text": "Latest report is %s; the previous business day (%s) is not in the data yet."
                      % (latest, exp.isoformat())})
    if len(dates) > 1:
        prev = store.cutout[dates[-2]]
        bad = []
        for pr in PRIMALS:
            try:
                got = float(store.cutout[latest]["c_" + pr])
                calc = float(store.cutout[latest]["v_" + pr]) - float(prev["v_" + pr])
            except ValueError:
                continue
            if abs(got - calc) > 0.011:
                bad.append("%s (USDA %.2f vs stored %.2f)" % (pr, got, calc))
        if bad:
            flags.append({"level": "warn", "text": "USDA's reported change on %s does not match the difference in stored values for: %s. "
                          "Check whether %s was revised or a report day is missing." % (latest, ", ".join(bad), dates[-2])})
    if (now.date() == today and is_expected_report_day(today) and now.hour * 60 + now.minute >= 17 * 60 + 30
            and latest < today.isoformat()):
        flags.append({"level": "warn", "text": "Today's report (%s) was not available when this file was built." % today.isoformat()})
    gaps = unexpected_gaps(store, today, 60)
    if gaps:
        flags.append({"level": "warn", "text": "Missing report days (not weekends or the six USDA holidays): "
                      + ", ".join(gaps[-8:])})
    revs = read_csv(folder / "revisions.csv")
    recent = [r for r in revs if r["detected_utc"] >= (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()]
    if recent:
        flags.append({"level": "info", "text": "%d USDA revisions detected in the last 7 days (see revisions.csv)." % len(recent)})

    base.update({
        "dates": dates, "cut": cut, "sections": sections, "items": items,
        "latest_date": latest, "latest_published_ct": store.cutout[latest]["published_ct"],
        "first_date_stored": min(store.cutout), "n_dates_stored": len(store.cutout),
        "catalog_size": len(store.items), "flags": flags})
    return base
