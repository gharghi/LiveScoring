#!/usr/bin/env python3
"""Download an RFAE task and replay it through the live scoring API.

The source page is treated as an immutable fixture: HTML, the generated
``.xctsk`` task, all IGCs, metadata and the final comparison are written below
``--download-dir``.  A new event/task id is used on every run, so replays never
mix points with an earlier experiment.

Example::

  .venv/bin/python rfae_replay.py --task 4 --speed 300 \
      --download-dir rfae_downloads

Use ``--dry-run`` to download and validate without sending anything.
"""
from __future__ import annotations

import argparse, html, json, os, re, ssl, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine.igc import parse_igc

UA = "livescoring-rfae-replay/1.0"

def key_from_env():
    value = os.getenv("LS_API_KEY")
    if value: return value.strip()
    p = Path(__file__).with_name(".ls_api_key")
    return p.read_text().strip() if p.exists() else None

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    # The public RFAE host currently serves an expired certificate.
    with urllib.request.urlopen(req, context=ssl._create_unverified_context(), timeout=90) as r:
        return r.read()

def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()

def time_seconds(s):
    h, m, *rest = s.rstrip("Z").split(":")
    return int(h) * 3600 + int(m) * 60 + int(rest[0]) if rest else int(h) * 3600 + int(m) * 60

def api(base, method, path, payload=None, api_key=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"User-Agent": UA, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    if api_key: req.add_header("X-API-Key", api_key)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode() or "{}")
            return r.status, body, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try: body = json.loads(raw or "{}")
        except ValueError: body = {"raw": raw[:500]}
        return e.code, body, (time.perf_counter() - started) * 1000

def parse_official_results(page):
    """Extract the published leaderboard from an RFAE task page.

    RFAE uses ``result`` for both geometry and leaderboard rows. Geometry rows
    have a non-numeric second cell (for example ``TURN``), whereas leaderboard
    rows start with rank, pilot id, name and end with Total points.
    """
    official = {}
    rows = re.findall(r'<tr[^>]*class=["\']result["\'][^>]*>(.*?)</tr>', page, re.I | re.S)
    for row in rows:
        cells = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)]
        if len(cells) < 4 or not re.fullmatch(r"\d+", cells[0]):
            continue
        pilot_id = cells[1].strip()
        if not pilot_id:
            continue
        # The last cell is Total points. Keep a fallback for older pages that
        # omit an empty trailing cell for an unscored pilot.
        try:
            score = float(cells[-1].replace(",", "."))
        except ValueError:
            nums = [x.replace(",", ".") for x in reversed(cells)
                    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", x.replace(",", "."))]
            if not nums:
                continue
            score = float(nums[0])
        official[pilot_id] = {
            "rank": int(cells[0]),
            "name": cells[2] if len(cells) > 2 else pilot_id,
            "score": score,
        }
    if not official:
        raise RuntimeError("could not parse the published leaderboard")
    return official


def parse_scoring_parameters(page):
    """Extract the GAP parameters supported by the scoring worker."""
    labels = {
        "Nominal distance": "nominal_distance_km",
        "Minimum distance": "minimum_distance_km",
        "Nominal time": "nominal_time_min",
        "Leading-time ratio": "leading_time_ratio",
    }
    params = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
        cells = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)]
        if len(cells) < 2 or cells[0] not in labels:
            continue
        value = cells[1].replace(",", ".")
        try:
            number = float(value)
        except ValueError:
            continue
        key = labels[cells[0]]
        # SVL displays this as a percentage (26.0), while the engine accepts
        # the fraction used by S7F (0.26).
        params[key] = number / 100.0 if key == "leading_time_ratio" and number > 1 else number
    return params


def _task_page_config(page, source_timezone):
    """Return task date/times and generated XCTrack config from an RFAE page.

    SVL displays task times in the competition's local timezone.  The scoring
    engine consumes UTC epochs, so convert the displayed local gate/deadline
    before writing the xctsk clock fields.  The page contains the competition
    date range first and the actual task date in an ``h3``; selecting the
    latter avoids accidentally using the event start date.
    """
    try:
        zone = ZoneInfo(source_timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"unknown source timezone: {source_timezone}") from exc

    # Extract task date from <h3> tag - this is the actual task date, not the event range
    task_date_match = re.search(r"<h3[^>]*>\s*(20\d\d-\d\d-\d\d)\s*</h3>", page, re.I)
    if task_date_match:
        date_str = task_date_match.group(1)
        print(f"✓ Found task date in <h3>: {date_str}")
    else:
        print("✗ No task date found in <h3> tags")
        # Fallback: find all dates and warn about which one we're using
        dates = re.findall(r"20\d\d-\d\d-\d\d", page)
        if not dates:
            raise RuntimeError("task date not found in page")
        print(f"  Found dates in page: {sorted(set(dates))}")
        print(f"  Using last found date: {dates[-1]} (may be event end date, not task date!)")
        date_str = dates[-1]
    day = int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    gate_m = re.search(r"StartGate(?:s)?[\s\S]{0,180}?(\d{1,2}:\d\d)", page, re.I)
    deadline_m = re.search(r"Task deadline:[\s\S]{0,180}?(\d{1,2}:\d\d)", page, re.I)
    gate = gate_m.group(1) if gate_m else "11:00"
    deadline = deadline_m.group(1) if deadline_m else "17:00"

    def utc_clock(local_clock):
        local = datetime.strptime(f"{date_str} {local_clock}", "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        utc = local.astimezone(timezone.utc)
        return utc.strftime("%H:%M")

    gate_utc, deadline_utc = utc_clock(gate), utc_clock(deadline)
    cells = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", page, re.I | re.S)]
    kinds = {"TOFF":"TAKEOFF", "SSS":"SSS", "TURN":"TURNPOINT", "ESS":"ESS", "GOAL":"GOAL"}
    turnpoints = []
    for i, cell in enumerate(cells):
        if cell not in kinds or i + 5 >= len(cells): continue
        try: name, radius, lat, lon = cells[i+2], float(cells[i+3]), float(cells[i+4]), float(cells[i+5])
        except (ValueError, IndexError): continue
        turnpoints.append({"type": kinds[cell], "radius": radius * 1000,
                           "waypoint": {"name": name, "lat": lat, "lon": lon}})
    if len(turnpoints) < 2: raise RuntimeError("could not parse task waypoints")
    # SVL/RFAE published task distances match WGS84 here. FAI_SPHERE makes
    # this task about 0.18% short: 76.236 km instead of the official 76.37 km.
    #
    # SVL 1.158's displayed Task 4 distances also line up with its older
    # relative cylinder tolerance convention: outer = max(r * 1.001, r + 5m).
    # Keep this task-local so the engine's default 2026 flat +5 m tolerance is
    # unchanged for native inputs.
    xctsk = {"earthModel":"WGS84", "radiusTolerance":0.001,
             "absoluteTolerance":5.0, "measurementRadius":"outer",
             "progressCurve":"HUMP_V2A",
             "turnpoints":turnpoints,
             "sss":{"type":"RACE", "direction":"EXIT", "timeGates":[gate_utc+":00Z"]},
             "goal":{"type":"CYLINDER", "deadline":deadline_utc+":00Z"}}
    return date_str, day, gate, deadline, gate_utc, deadline_utc, xctsk


def parse_source(index_url, task_number, out, source_timezone):
    index_url = index_url.rstrip("/") + "/"
    index_raw = fetch(index_url); (out / "index.html").write_bytes(index_raw)
    index = index_raw.decode("utf-8", "replace")
    links = re.findall(r'href=["\']([^"\']*task(\d+)\.html)["\']', index, re.I)
    if not links: raise RuntimeError("no task pages found in " + index_url)
    number = task_number or max(int(n) for _, n in links)
    href = next((h for h, n in links if int(n) == number), None)
    if not href: raise RuntimeError(f"task {number} is not linked from {index_url}")
    task_url = urllib.parse.urljoin(index_url, href)
    page_raw = fetch(task_url); (out / "task.html").write_bytes(page_raw)
    page = page_raw.decode("utf-8", "replace")
    date_str, day, gate, deadline, gate_utc, deadline_utc, xctsk = _task_page_config(page, source_timezone)
    (out / "task.xctsk").write_text(json.dumps(xctsk, indent=2), encoding="utf-8")
    igc_urls = sorted(set(re.findall(r'href=["\']([^"\']+\.igc)["\']', page, re.I)))
    if not igc_urls: raise RuntimeError("no IGC links found on " + task_url)
    igc_dir = out / "igc"; igc_dir.mkdir(exist_ok=True)
    for i, href in enumerate(igc_urls, 1):
        name = Path(urllib.parse.urlparse(href).path).name or f"{i}.igc"
        (igc_dir / name).write_bytes(fetch(urllib.parse.urljoin(task_url, href)))

    # Validate that IGC dates match the task date
    try:
        validate_igc_dates(igc_dir, date_str)
    except RuntimeError as e:
        print(f"\n{'='*70}")
        print(f"CRITICAL: Date validation failed")
        print(f"{'='*70}")
        print(str(e))
        print(f"\nTask date: {date_str}")
        print(f"This will cause ALL pilots to score 0 because start gate validation will fail.")
        print(f"{'='*70}")
        raise

    official = parse_official_results(page)
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    meta = {"index_url": index_url, "task_url": task_url, "task_number": number,
            "event_name": text(title.group(1)) if title else "RFAE competition",
            "date": date_str, "day_epoch": day, "gate": gate, "deadline": deadline,
            "gate_utc": gate_utc, "deadline_utc": deadline_utc,
            "source_timezone": source_timezone,
            "scoring_parameters": parse_scoring_parameters(page),
            "igc_count": len(igc_urls), "official_results": official}
    (out / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta, xctsk, igc_dir

def validate_igc_dates(igc_dir, expected_date_str):
    """Validate that IGC files match the task date.

    Args:
        igc_dir: Path to directory containing IGC files
        expected_date_str: Task date as YYYY-MM-DD string

    Raises:
        RuntimeError if IGC dates don't match task date
    """
    igc_files = sorted(igc_dir.glob("*.igc"))
    if not igc_files:
        raise RuntimeError("no IGC files found")

    # Extract date from expected_date_str
    expected_year, expected_month, expected_day = expected_date_str.split("-")

    igc_dates = set()
    date_mismatches = []

    for igc_file in igc_files:
        content = igc_file.read_text()
        # Look for HFDTE header: HFDTE + DDMMYY
        match = re.search(r"^HFDTE(\d{6})", content, re.MULTILINE)
        if match:
            date_str = match.group(1)
            day, month, year = date_str[0:2], date_str[2:4], date_str[4:6]
            full_year = f"20{year}"
            igc_date = f"{full_year}-{month}-{day}"
            igc_dates.add(igc_date)

            if igc_date != expected_date_str:
                date_mismatches.append((igc_file.name, igc_date))

    # Report findings
    print(f"\n✓ Validating IGC file dates against task date: {expected_date_str}")
    print(f"  Found {len(igc_dates)} unique date(s) in {len(igc_files)} IGC files: {sorted(igc_dates)}")

    if date_mismatches:
        print(f"\n✗ DATE MISMATCH: {len(date_mismatches)} IGC files have wrong date!")
        for fname, igc_date in date_mismatches[:5]:
            print(f"    {fname}: {igc_date} (expected {expected_date_str})")
        if len(date_mismatches) > 5:
            print(f"    ... and {len(date_mismatches) - 5} more")
        raise RuntimeError(
            f"IGC files have mismatched dates: {sorted(igc_dates)} vs task date {expected_date_str}\n"
            "This will cause scoring to fail because start gate validation will reject all pilots.\n"
            "Check if the task date extraction is correct, or if the IGC files are from a different day."
        )

    print(f"  ✓ All IGC dates match task date")


def pilots_from(igc_dir):
    groups = {}
    for p in sorted(igc_dir.glob("*.igc")):
        pilot, day, fixes = parse_igc(p.read_bytes(), p.name)
        if not fixes: continue
        k = pilot.strip().lower(); e = groups.setdefault(k, {"name":pilot or p.stem, "stem":p.stem, "day":day, "fixes":[]})
        e["fixes"].extend(fixes); e["day"] = e["day"] or day
    result = []
    for e in groups.values():
        seen = set(); fixes = []
        for f in sorted(e["fixes"], key=lambda x:x.t):
            if f.t not in seen: seen.add(f.t); fixes.append(f)
        m = re.match(r"\d+", e["stem"]); pid = m.group(0) if m else re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", e["name"].lower())).strip("_")
        result.append({"pilot_id":pid or e["stem"], "name":e["name"], "day":e["day"], "fixes":fixes})
    if not result: raise RuntimeError("no usable IGC files")
    return sorted(result, key=lambda x:x["pilot_id"])

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://scoring.rfae.es/campeonato-sport/")
    ap.add_argument("--task", type=int, help="RFAE task number (default: latest linked task)")
    ap.add_argument("--source-timezone", default="Europe/Madrid",
                    help="timezone used for times displayed on the source page (default: Europe/Madrid)")
    ap.add_argument("--download-dir", default="rfae_downloads")
    ap.add_argument("--reuse-dir", help="reuse an existing downloaded run directory; skips source download")
    ap.add_argument("--base-url", default=os.getenv("LS_BASE_URL", "https://ls.buildmycabin.com"))
    ap.add_argument("--api-key", default=key_from_env())
    ap.add_argument("--event-id"); ap.add_argument("--task-id")
    ap.add_argument("--speed", type=float, default=300); ap.add_argument("--batch-seconds", type=float, default=15)
    ap.add_argument("--batch-points", "--max-points", dest="batch_points", type=int, default=1000,
                    help="maximum newly unsent fixes per API request (default: 1000)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--score-tolerance", type=float, default=0.15,
                    help="maximum absolute score difference in points (default: 0.15)")
    ap.add_argument("--wait-timeout", type=float, default=180,
                    help="seconds to wait for the async scorer after replay (default: 180)")
    ap.add_argument("--poll-interval", type=float, default=1.0,
                    help="seconds between result polls (default: 1)")
    ap.add_argument("--fail-on-mismatch", action="store_true",
                    help="exit with status 2 when official and server results differ")
    args = ap.parse_args()
    if args.batch_points < 1:
        raise SystemExit("--batch-points must be at least 1")
    if args.batch_seconds <= 0:
        raise SystemExit("--batch-seconds must be greater than 0")
    if args.wait_timeout <= 0:
        raise SystemExit("--wait-timeout must be greater than 0")
    if args.poll_interval < 0:
        raise SystemExit("--poll-interval cannot be negative")
    if args.score_tolerance < 0:
        raise SystemExit("--score-tolerance cannot be negative")
    if args.reuse_dir:
        run = Path(args.reuse_dir)
        meta = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
        xctsk = json.loads((run / "task.xctsk").read_text(encoding="utf-8"))
        igc_dir = run / "igc"
        if not igc_dir.is_dir(): raise SystemExit(f"missing IGC directory: {igc_dir}")
        task_page = (run / "task.html").read_text(encoding="utf-8", errors="replace")
        date_str, day, gate, deadline, gate_utc, deadline_utc, xctsk = _task_page_config(
            task_page, args.source_timezone)

        # Validate IGC dates for reused directory
        try:
            validate_igc_dates(igc_dir, date_str)
        except RuntimeError as e:
            print(f"\n{'='*70}")
            print(f"CRITICAL: Date validation failed for reused directory")
            print(f"{'='*70}")
            print(str(e))
            print(f"\nTask date: {date_str}")
            print(f"{'='*70}")
            raise SystemExit(1)

        meta.update({"date": date_str, "day_epoch": day, "gate": gate, "deadline": deadline,
                     "gate_utc": gate_utc, "deadline_utc": deadline_utc,
                     "source_timezone": args.source_timezone,
                     "scoring_parameters": parse_scoring_parameters(task_page)})
        meta["official_results"] = parse_official_results(task_page)
        (run / "task.xctsk").write_text(
            json.dumps(xctsk, indent=2), encoding="utf-8")
        (run / "metadata.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Reusing downloaded fixture {run}")
    else:
        run = Path(args.download_dir) / ("run_" + str(int(time.time())))
        run.mkdir(parents=True, exist_ok=True)
        try:
            meta, xctsk, igc_dir = parse_source(args.url, args.task, run, args.source_timezone)
        except urllib.error.URLError as exc:
            raise SystemExit(f"could not download {args.url}: {exc.reason}")
    pilots = pilots_from(igc_dir); day = meta["day_epoch"]
    # A reused fixture must still get a fresh event/task by default; otherwise
    # a second validation run would hit the old task's deduplication cursor and
    # never trigger a new score. Explicit --event-id/--task-id can be used when
    # intentionally resuming an existing replay.
    replay_token = str(int(time.time())) if args.reuse_dir else run.name[4:]
    suffix = f"{meta['date'].replace('-', '')}_t{meta['task_number']}_{replay_token}"
    event_id = args.event_id or "evt_rfae_" + suffix; task_id = args.task_id or "task_rfae_" + suffix
    stream = sorted((f.t, p["pilot_id"], f) for p in pilots for f in p["fixes"])
    first, last = stream[0][0], stream[-1][0]
    print(f"Downloaded {len(pilots)} pilots / {len(stream):,} fixes to {run}")
    print(f"Task {meta['task_url']} · {meta['date']} · {len(xctsk['turnpoints'])} turnpoints")
    if args.dry_run: return 0
    if not args.api_key: raise SystemExit("API key missing: pass --api-key or set LS_API_KEY")
    event = {"schema_version":"1.0", "event_id":event_id, "event_name":meta["event_name"],
             "sent_at":datetime.now(timezone.utc).isoformat(),
             "formula":{"type":"GAP", "parameters":meta.get("scoring_parameters", {})},
             "categories":[{"category_id":"cat_open","name":"Open"}],
             "pilots":[{"pilot_id":p["pilot_id"],"name":p["name"],"category_id":"cat_open","tracker_id":p["pilot_id"]} for p in pilots]}
    status, body, _ = api(args.base_url, "POST", "/events/sync", event, args.api_key)
    if status >= 300: raise SystemExit(f"event sync failed ({status}): {body}")
    task = {"schema_version":"1.0", "event_id":event_id, "task_id":task_id, "task_date":meta["date"],
            "scheduled_start_time":f"{meta['date']}T{meta.get('gate_utc', meta['gate'])}:00Z", "status":"running",
            "pilots":[p["pilot_id"] for p in pilots], "xctsk":xctsk, "date_epoch":day,
            "sent_at":datetime.now(timezone.utc).isoformat()}
    status, body, _ = api(args.base_url, "POST", f"/events/{event_id}/tasks/sync", task, args.api_key)
    if status >= 300: raise SystemExit(f"task sync failed ({status}): {body}")
    latencies=[]; accepted=duplicates=0; i=0; cutoff=first
    while i < len(stream):
        cutoff = min(cutoff + args.batch_seconds, last); j=i
        while j < len(stream) and stream[j][0] <= cutoff: j += 1
        batch=stream[i:j]; i=j
        # `i` is the only replay cursor.  Chunks are slices of this newly
        # consumed range; they never include an earlier slice, so reconnects
        # and large time windows cannot resend points from the beginning.
        request_count = 0
        for k in range(0, len(batch), args.batch_points):
            chunk=batch[k:k+args.batch_points]
            request_count += 1
            payload={"schema_version":"1.0","event_id":event_id,"task_id":task_id,"cutoff_epoch":int(cutoff),
                     "points":[{"pilot_id":pid,"epoch":int(f.t),"lat":f.lat,"lon":f.lon,"alt":f.alt_gps} for _,pid,f in chunk]}
            status, body, elapsed=api(args.base_url,"POST",f"/tasks/{task_id}/points",payload,args.api_key)
            if status >= 300: raise SystemExit(f"points failed ({status}): {body}")
            latencies.append(elapsed); accepted += body.get("accepted",0); duplicates += body.get("duplicates",0)
        print(f"  {datetime.fromtimestamp(cutoff,timezone.utc).strftime('%H:%M:%S')}  new_points={len(batch):4d} requests={request_count} accepted={accepted:6d} duplicates={duplicates:5d} processed_epoch={body.get('processed_epoch', cutoff)} total={body.get('processing_ms',0):.0f}ms ingest={body.get('ingestion_ms',0):.0f}ms score={body.get('scoring_ms',0):.0f}ms")
        if args.speed > 0: time.sleep(args.batch_seconds / args.speed)
    # Scoring is outside Django's request path. Wait until the stored snapshot
    # covers the final replay epoch before comparing it with the official page.
    target_epoch = int(last)
    target_points = int(accepted)
    wait_started = time.perf_counter()
    result = None
    while True:
        status, candidate, _ = api(args.base_url, "GET", f"/tasks/{task_id}/results", None, args.api_key)
        if status >= 300:
            raise SystemExit(f"results failed ({status}): {candidate}")
        result = candidate
        scored_epoch = candidate.get("processed_epoch")
        snapshot_points = candidate.get("point_count") or 0
        if (candidate.get("status") == "ok" and scored_epoch is not None
                and int(scored_epoch) >= target_epoch and snapshot_points >= target_points):
            break
        waited = time.perf_counter() - wait_started
        if waited >= args.wait_timeout:
            raise SystemExit(
                f"scorer did not catch up within {args.wait_timeout:g}s: "
                f"status={candidate.get('status')} scored_epoch={scored_epoch} "
                f"target_epoch={target_epoch} point_count={snapshot_points}/{target_points}"
            )
        time.sleep(max(0.05, args.poll_interval))

    rows = result.get("pilots") or result.get("ranking") or []
    server = {str(r.get("pilot_id")): r for r in rows if r.get("pilot_id") is not None}
    official = meta["official_results"]
    source_pilot_ids = {p["pilot_id"] for p in pilots}
    # Published pages can contain pilots without a linked IGC (usually a
    # non-starter or an absent tracklog). They cannot be scored by this replay;
    # report them separately and compare every pilot for which real data was
    # downloaded.
    unavailable = sorted(set(official) - source_pilot_ids)
    comparison = []
    missing = []
    for pid, expected in official.items():
        if pid not in source_pilot_ids:
            continue
        actual = server.get(pid)
        if actual is None:
            missing.append(pid)
            comparison.append({"pilot_id": pid, "name": expected["name"],
                               "official_rank": expected["rank"], "official_score": expected["score"],
                               "server_rank": None, "server_score": None,
                               "score_delta": None, "rank_match": False, "score_match": False,
                               "match": False})
            continue
        server_score = float(actual.get("score") or 0)
        score_delta = server_score - float(expected["score"])
        rank_match = int(actual.get("rank") or 0) == int(expected["rank"])
        score_match = abs(score_delta) <= args.score_tolerance
        comparison.append({"pilot_id": pid, "name": expected["name"],
                           "official_rank": expected["rank"], "official_score": expected["score"],
                           "server_rank": actual.get("rank"), "server_score": server_score,
                           "score_delta": round(score_delta, 4), "rank_match": rank_match,
                           "score_match": score_match, "match": rank_match and score_match})
    unexpected = sorted(set(server) - set(official))
    rank_matches = sum(x["rank_match"] for x in comparison)
    score_matches = sum(x["score_match"] for x in comparison)
    comparable_count = len(official) - len(unavailable)
    all_match = (not missing and not unexpected and len(comparison) == len(server)
                 and rank_matches == comparable_count and score_matches == comparable_count)
    report={"event_id":event_id,"task_id":task_id,"task_url":meta["task_url"],"official_url":meta["task_url"],
            "accepted":accepted,"duplicates":duplicates,"http_ms":latencies,
            "wait_seconds":round(time.perf_counter() - wait_started, 3),
            "score_tolerance":args.score_tolerance,"official_count":len(official),
            "comparable_count":comparable_count,"source_pilot_count":len(source_pilot_ids),
            "server_count":len(server),"official_without_igc":unavailable,
            "missing_on_server":missing,"unexpected_on_server":unexpected,
            "rank_matches":rank_matches,"score_matches":score_matches,"all_match":all_match,
            "server_results":result,"comparison":comparison}
    (run/"comparison.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"Final: {len(rows)} server rows; official={len(official)} "
          f"(comparable={comparable_count}, no IGC={len(unavailable)}); "
          f"ranks {rank_matches}/{comparable_count}; scores {score_matches}/{comparable_count} "
          f"(±{args.score_tolerance:g}); {'MATCH' if all_match else 'MISMATCH'}")
    for x in sorted(comparison, key=lambda z: abs(z["score_delta"] or 0), reverse=True)[:10]:
        if x["server_score"] is None:
            print(f"  {x['pilot_id']}: missing on server (official rank {x['official_rank']}, score {x['official_score']:.1f})")
        else:
            print(f"  {x['pilot_id']}: official rank {x['official_rank']} / {x['official_score']:.1f} · "
                  f"server rank {x['server_rank']} / {x['server_score']:.1f} · "
                  f"delta {x['score_delta']:+.1f} · "
                  f"{'OK' if x['match'] else 'DIFF'}")
    if latencies:
        q=sorted(latencies); print(f"HTTP performance: p50={q[len(q)//2]:.0f}ms p95={q[min(len(q)-1,int(len(q)*.95))]:.0f}ms max={q[-1]:.0f}ms")
    print(f"Report: {run/'comparison.json'}")
    return 0 if all_match or not args.fail_on_mismatch else 2

if __name__ == "__main__": sys.exit(main())
