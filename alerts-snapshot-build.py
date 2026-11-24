"""Builds the compact worldwide alerts snapshot for the OLWeather app (and any other reader): ONE file, written by ONE scheduled job.

Usage:
  python work/alerts-snapshot-build.py --from-url http://127.0.0.1:8792/api/alerts --out work/alerts-test/alerts-compact.json   (no upstream calls)
  python work/alerts-snapshot-build.py --out alerts-compact.json            (runs one live refresh of NOAA/NWS, MET Norway and SACHET: this IS the job)

Same data and same rules as the website's Earth-Lens alerts (work/alerts_service.py): an alert has a shape only when its issuer gave one (official
polygon, the forecast zones an NWS alert names, SACHET CAP polygon or LGD districts); otherwise a point or nothing. Nothing is drawn or guessed.
What this adds is only size reduction so a phone can read it: zone polygons are resolved here (the app never loads zones), shared shapes are stored
once, rings are simplified (Douglas-Peucker, about 2 km) and rounded to 0.01 degree, holes are dropped, long texts are cut.

Schema 'olweather.alerts-compact.v1':
  {schema, generatedAt, sources:{name:{ok,count}}, shapes:[ [ring,...] ], alerts:[{id,src,issuer,event,kind,sev,urgency,certainty,onset,ends,area,summary,url,shapes:[index,...],point}]}
  ring = [[lon,lat],...] (exterior only); sev 0 Unknown, 1 Minor, 2 Moderate, 3 Severe, 4 Extreme.
"""
import argparse, hashlib, json, math, pathlib, sys, time, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SEV = {'Extreme': 4, 'Severe': 3, 'Moderate': 2, 'Minor': 1, 'Unknown': 0}
EPS = 0.02        # simplification tolerance in degrees (about 2 km)
MAX_SUMMARY = 240
MAX_AREA = 140


def _rdp(points, eps):
    if len(points) < 3:
        return points
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy)
    best, index = 0.0, 0
    for i in range(1, len(points) - 1):
        x, y = points[i]
        d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm if norm else math.hypot(x - x1, y - y1)
        if d > best:
            best, index = d, i
    if best > eps:
        return _rdp(points[:index + 1], eps)[:-1] + _rdp(points[index:], eps)
    return [points[0], points[-1]]


def simplify_ring(ring):
    pts = [(round(float(p[0]), 2), round(float(p[1]), 2)) for p in ring if len(p) >= 2]
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return None
    out = _rdp(pts + [pts[0]], EPS)[:-1]
    # drop consecutive duplicates created by rounding
    clean = [out[0]]
    for p in out[1:]:
        if p != clean[-1]:
            clean.append(p)
    return [[x, y] for x, y in clean] if len(clean) >= 3 else None


def shapes_of(alert, zones):
    """Polygons of one alert: its own, else the zones it names. Exterior rings only."""
    polys = alert.get('polygons') or []
    if not polys:
        for k in alert.get('zoneIds') or []:
            polys.extend(zones.get(k) or [])
    rings = []
    for poly in polys:
        if poly and poly[0]:
            ring = simplify_ring(poly[0])
            if ring:
                rings.append(ring)
    return rings


def compact(state):
    zones = state.get('zones') or {}
    shape_index, shapes, alerts = {}, [], []
    for a in state.get('alerts') or []:
        idx = []
        for ring in shapes_of(dict(a), zones):
            key = hashlib.md5(json.dumps(ring, separators=(',', ':')).encode()).hexdigest()
            if key not in shape_index:
                shape_index[key] = len(shapes)
                shapes.append([ring])
            idx.append(shape_index[key])
        point = a.get('point')
        if not idx and not point:
            continue   # nothing to draw: it stays out (the website lists it, the map cannot place it)
        alerts.append({
            'id': a.get('id'), 'src': a.get('source'), 'issuer': (a.get('issuer') or '')[:60], 'event': (a.get('event') or '')[:80],
            'kind': a.get('kind') or 'other', 'sev': SEV.get(a.get('severity'), 0), 'urgency': a.get('urgency'), 'certainty': a.get('certainty'),
            'onset': a.get('onset'), 'ends': a.get('ends'), 'area': (a.get('area') or '')[:MAX_AREA],
            'summary': (a.get('summary') or a.get('headline') or '').replace('\n', ' ')[:MAX_SUMMARY], 'url': a.get('url'),
            'shapes': idx, 'point': point if point and len(point) == 2 else None,
        })
    srcs = {k: {'ok': bool(v.get('ok', True)), 'count': v.get('count')} for k, v in (state.get('sources') or {}).items()}
    return {'schema': 'olweather.alerts-compact.v1', 'generatedAt': state.get('fetchedAt') or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'sources': srcs, 'shapes': shapes, 'alerts': alerts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-url')
    ap.add_argument('--from-file')
    ap.add_argument('--out', required=True)
    ap.add_argument('--out-full', help='also write the full snapshot the website Earth-Lens reads (KV key alerts)')
    args = ap.parse_args()
    if args.from_url:
        req = urllib.request.Request(args.from_url, headers={'User-Agent': 'OLWeather-alerts-snapshot/1.0'})
        state = json.loads(urllib.request.urlopen(req, timeout=60).read())
    elif args.from_file:
        state = json.loads(pathlib.Path(args.from_file).read_text(encoding='utf-8'))
    else:
        import alerts_service
        service = alerts_service.AlertsService(HERE / 'alerts-cache')
        service.refresh()
        state = service.snapshot()
    srcs = state.get('sources') or {}
    if srcs and not any(v.get('ok') for v in srcs.values()):
        sys.exit('every official feed failed: not writing a snapshot (the old one stays until it is too old to show)')
    out = compact(state)
    text = json.dumps(out, separators=(',', ':'), ensure_ascii=False)
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(text, encoding='utf-8')
    if args.out_full:
        pathlib.Path(args.out_full).write_text(json.dumps(state, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    print(f"wrote {args.out}: {len(text) / 1024:.0f} KB, {len(out['alerts'])} alerts of {len(state.get('alerts') or [])}, {len(out['shapes'])} shapes")


if __name__ == '__main__':
    main()
