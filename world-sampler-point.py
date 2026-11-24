#!/usr/bin/env python3
"""Builds the saved world forecast file (schema olweather.earthlens.world-grid.v1) from OLWeather's one-call-per-point Worker route.

One request per lattice point (35 x 72 = 2,520 points, 5 degrees apart), each one WeatherAPI call made by the Worker, which holds the key:
no key is used or needed here. Compared with the older 3x3 route (nine calls per point, 25 hours) this costs a ninth and holds up to 72 hours.

Writes the file only when it is complete enough to trust: at least 98% of the points must have data and every point is aligned to the same UTC
start hour. A refused run leaves the old file untouched (the caller does not upload anything).

Run:  python world-sampler-point.py --run --out world-grid.json            (default is a dry run that makes no request)
Env:  WORLD_POINT_ENDPOINT overrides the Worker address; WORLD_SAMPLER_USER_AGENT overrides the User-Agent.
"""
import argparse, concurrent.futures, json, math, os, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

SCHEMA = 'olweather.earthlens.world-grid.v1'
POINT_SCHEMA = 'olweather.weather-world-point.v1'
ENDPOINT = os.environ.get('WORLD_POINT_ENDPOINT', 'https://olweather-rc2.olweather-app.workers.dev/api/v1/weather-world-point')
UA = os.environ.get('WORLD_SAMPLER_USER_AGENT', 'OLWeather-world-sampler/1.0 (+https://olweather.com/contact)')
FIELDS = ('t', 'h', 'uv', 'p', 'ws', 'wd', 'c', 'r')
STEP = 5
LATS = [-85 + STEP * i for i in range(35)]       # -85 .. 85
LONS = [-180 + STEP * j for j in range(72)]      # -180 .. 175
MIN_GOOD_SHARE = 0.98
MAX_BYTES = 256 * 1024


def default_fetch(lat, lon, hours):
    url = '%s?lat=%d&lon=%d&hours=%d' % (ENDPOINT, lat, lon, hours)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('response too large')
    return json.loads(raw)


def valid_point(body, hours):
    """A point answer is usable only if it has the expected schema and every field is a list of `hours` numbers or nulls."""
    if not isinstance(body, dict) or body.get('schema') != POINT_SCHEMA:
        return False
    if not isinstance(body.get('startEpoch'), int) or not isinstance(body.get('hours'), int):
        return False
    f = body.get('fields')
    if not isinstance(f, dict):
        return False
    for code in FIELDS:
        series = f.get(code)
        if not isinstance(series, list) or len(series) != body['hours']:
            return False
        for v in series:
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                return False
    return any(v is not None for v in f['t'])


class Limiter:
    def __init__(self, per_second):
        self.gap = 1.0 / per_second
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            time.sleep(at - now)


def build(hours, fetch=default_fetch, rate=4.0, workers=4, progress=None, retries=3, sleep=time.sleep, now=time.time, second_passes=2, second_pass_pause=20):
    """Returns the world file as a dict, or raises SystemExit with a reason when too few points came back."""
    limiter = Limiter(rate)
    results, lock, done = {}, threading.Lock(), [0]
    total = len(LATS) * len(LONS)

    refused = set()    # points the route will never serve (400/422): never retried

    def ask(i, j):
        """Up to `retries` tries for one point; returns the body or None."""
        for attempt in range(retries):
            limiter.wait()
            try:
                got = fetch(LATS[i], LONS[j], hours)
                if valid_point(got, hours):
                    return got
            except urllib.error.HTTPError as e:
                if e.code in (400, 422):
                    refused.add((i, j))
                    return None
                sleep(2 * (attempt + 1))
            except Exception:
                sleep(2 * (attempt + 1))
        return None

    def job(i, j):
        body = ask(i, j)
        with lock:
            results[(i, j)] = body
            done[0] += 1
            if progress and (done[0] % 100 == 0 or done[0] == total):
                progress(done[0], total)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for f in [pool.submit(job, i, j) for i in range(len(LATS)) for j in range(len(LONS))]:
            f.result()

    # Second pass: one-off failures (a timeout, a busy moment) are asked again after a pause, so a run rarely ends with gaps.
    # Points the route refused are not asked again, and a point that still fails stays unavailable (never filled from a neighbour).
    for round_ in range(second_passes):
        again = [k for k, v in results.items() if v is None and k not in refused]
        if not again:
            break
        sleep(second_pass_pause)
        if progress:
            progress(total, total)
        for k in sorted(again):
            results[k] = ask(*k)

    good = [b for b in results.values() if b is not None]
    if len(good) < MIN_GOOD_SHARE * total:
        raise SystemExit('only %d of %d points came back: not writing a file' % (len(good), total))
    # one shared start hour: the latest any point started (a point served from an older cache starts earlier and is trimmed)
    start = max(b['startEpoch'] for b in good)
    usable = min(b['hours'] - (start - b['startEpoch']) // 3600 for b in good)
    if usable < 24:
        raise SystemExit('the shortest point covers only %d hours: not writing a file' % usable)
    usable = min(usable, hours)
    fields = {code: [] for code in FIELDS}
    missing = []
    for i in range(len(LATS)):
        for j in range(len(LONS)):
            b = results.get((i, j))
            if b is None or all(v is None for v in b['fields']['t']):
                missing.append(i * len(LONS) + j)
                for code in FIELDS:
                    fields[code].append([None] * usable)
                continue
            off = (start - b['startEpoch']) // 3600
            for code in FIELDS:
                fields[code].append(b['fields'][code][off:off + usable])
    return {
        'schema': SCHEMA,
        'source': 'WeatherAPI.com via the OLWeather Worker (weather-world-point, one lattice point per request)',
        'synthetic': False,
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now())),
        'startEpoch': start, 'hours': usable, 'stepDegrees': float(STEP),
        'lat0': float(LATS[0]), 'lon0': float(LONS[0]), 'rows': len(LATS), 'cols': len(LONS),
        'calls': len(good), 'dateLineStandIn': 'the -180 column was fetched at -179.5',
        'missingPoints': missing, 'fields': fields,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--hours', type=int, default=60, help='hourly frames wanted from the current UTC hour (24..72, default 60)')
    ap.add_argument('--rate', type=float, default=4.0, help='requests per second to the Worker (default 4, about 11 minutes)')
    ap.add_argument('--run', action='store_true', help='really call the Worker (default: dry run, no request)')
    ap.add_argument('--out', default='world-grid.json')
    a = ap.parse_args()
    if not 24 <= a.hours <= 72:
        raise SystemExit('--hours must be 24..72')
    n = len(LATS) * len(LONS)
    print('%d points, one Worker request (one WeatherAPI call) each, %.1f per second: about %.0f minutes.' % (n, a.rate, n / a.rate / 60))
    if not a.run:
        print('Dry run only. Add --run to call the Worker.')
        return
    world = build(a.hours, rate=a.rate, progress=lambda d, t: print('  %d / %d' % (d, t), flush=True))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp')
    tmp.write_text(json.dumps(world, separators=(',', ':')), encoding='utf-8')
    tmp.replace(out)
    print('Wrote %s (%.2f MB): %d hours from %s, %d of %d points without data.' % (
        out, out.stat().st_size / 1048576, world['hours'], time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(world['startEpoch'])), len(world['missingPoints']), n))


if __name__ == '__main__':
    main()
