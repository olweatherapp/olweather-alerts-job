"""Official alerts for the EarthLens preview: NOAA/NWS (US), MET Norway MetAlerts, and India's SACHET (through OLWeather's own Worker).

Normalises all three into one list. Nothing is drawn or guessed here: an alert has a shape only when its issuer supplied one
(NWS polygon, NWS forecast-zone polygons fetched from api.weather.gov, MET Norway polygon). SACHET's live feed gives only a centre
point and an area in km2; the real shape is then read from SACHET's own CAP XML: the alert's Polygon XML (official shape), else the
LGD districts it lists, drawn from the district boundary file work/india-districts.json. Without either, it stays a point.
Anything that cannot be fetched stays unavailable.

Run as a module of work/earthlens-server.py (AlertsService.start()). Cache: work/alerts-cache/zones.json (zone polygons, 14 days).
"""
import json, math, re, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import os as _os
# NOAA and MET Norway ask for an identifying User-Agent with a way to reach the operator; override in the job with ALERTS_USER_AGENT.
UA = _os.environ.get('ALERTS_USER_AGENT', 'OLWeather-alerts/1.0 (+https://olweather.com/contact)')
NWS_ACTIVE = 'https://api.weather.gov/alerts/active?status=actual&message_type=alert,update'
METNO_CURRENT = 'https://api.met.no/weatherapi/metalerts/2.0/current.json'
SACHET_WORKER = 'https://olweather-rc2.olweather-app.workers.dev/api/v1/alerts/sachet'
SACHET_BASE = 'https://sachet.ndma.gov.in/cap_public_website/'
SACHET_TTL = 6 * 3600
DISTRICTS_FILE = Path(__file__).resolve().parent / 'india-districts.json'
ZONE_TTL = 14 * 86400
REFRESH_SECONDS = 300
SEVERITY_ORDER = {'Extreme': 0, 'Severe': 1, 'Moderate': 2, 'Minor': 3, 'Unknown': 4}

# ---------------------------------------------------------------- classification
def hazard_kind(source, event, type_code=None):
    """Hazard family used to pick the map animation. Decorative only: it never implies intensity."""
    t = (event or '').lower()
    if source == 'MET Norway' and type_code:
        n = str(type_code).split(';')[0].strip()
        return {'1': 'wind', '2': 'winter', '3': 'thunder', '4': 'haze', '5': 'heat', '6': 'winter', '7': 'flood',
                '8': 'fire', '9': 'ground', '10': 'flood', '11': 'flood', '12': 'flood'}.get(n, 'other')
    def has(*words):
        return any(w in t for w in words)
    if has('tsunami', 'earthquake', 'seismic'): return 'quake'
    if has('hurricane', 'typhoon', 'cyclone', 'tropical'): return 'cyclone'
    if has('tornado', 'thunder', 'hail'): return 'thunder'
    if has('flood', 'storm surge', 'surf', 'rip current', 'seiche', 'tidal'): return 'flood'
    if has('blizzard', 'snow', 'winter', 'ice', 'freez', 'frost', 'wind chill', 'sleet', 'squall'): return 'winter'
    if has('heat'): return 'heat'
    if has('red flag', 'fire', 'wildfire'): return 'fire'
    if has('fog', 'smoke', 'dust', 'haze', 'visibility', 'air quality', 'ashfall', 'volcan'): return 'haze'
    if has('avalanche', 'landslide', 'debris', 'mudslide'): return 'ground'
    if has('wind', 'gale', 'storm warning', 'small craft', 'brisk', 'squall'): return 'wind'
    return 'other'


def norm_severity(value):
    v = (value or '').strip().capitalize()
    return v if v in SEVERITY_ORDER else 'Unknown'


# ---------------------------------------------------------------- geometry helpers
def _perp(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def simplify(ring, tol=0.004):
    """Douglas-Peucker on a closed ring (iterative). Keeps at least 4 points (a triangle) or drops the ring."""
    if len(ring) <= 5:
        return [[round(x, 3), round(y, 3)] for x, y in ring]
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        lo, hi = stack.pop()
        worst, idx = 0.0, -1
        for i in range(lo + 1, hi):
            d = _perp(ring[i], ring[lo], ring[hi])
            if d > worst:
                worst, idx = d, i
        if worst > tol and idx != -1:
            keep[idx] = True
            stack.append((lo, idx))
            stack.append((idx, hi))
    out = [[round(p[0], 3), round(p[1], 3)] for p, k in zip(ring, keep) if k]
    return out if len(out) >= 4 else []


def polygons_of(geometry, tol=0.004):
    """GeoJSON Polygon/MultiPolygon -> list of polygons; each polygon is a list of rings (outer first) of [lon, lat]."""
    if not geometry:
        return []
    kind, coords = geometry.get('type'), geometry.get('coordinates')
    polys = [coords] if kind == 'Polygon' else coords if kind == 'MultiPolygon' else []
    out = []
    for poly in polys:
        rings = []
        for ring in poly:
            try:
                s = simplify([(float(p[0]), float(p[1])) for p in ring], tol)
            except (TypeError, ValueError, IndexError):
                s = []
            if s:
                rings.append(s)
        if rings:
            out.append(rings)
    return out


def centroid(polys):
    pts = [p for poly in polys for p in poly[0]]
    if not pts:
        return None
    return [round((min(p[0] for p in pts) + max(p[0] for p in pts)) / 2, 3), round((min(p[1] for p in pts) + max(p[1] for p in pts)) / 2, 3)]


# ---------------------------------------------------------------- normalisers
def _iso(value):
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return None


def zone_key(url):
    """'https://api.weather.gov/zones/forecast/PKZ661' -> 'forecast/PKZ661'; anything else is refused."""
    m = re.fullmatch(r'https://api\.weather\.gov/zones/(forecast|county|fire)/([A-Z0-9]{5,8})', str(url))
    return m.group(1) + '/' + m.group(2) if m else None


def normalize_nws(feature):
    p = feature.get('properties') or {}
    polys = polygons_of(feature.get('geometry'))
    zones = [k for k in (zone_key(z) for z in (p.get('affectedZones') or [])) if k]
    desc = (p.get('description') or '').strip().replace('\n', ' ')
    return {
        'id': 'nws:' + str(p.get('id') or feature.get('id')), 'source': 'NWS', 'issuer': p.get('senderName') or 'NWS',
        'event': p.get('event') or 'Alert', 'kind': hazard_kind('NWS', p.get('event')), 'severity': norm_severity(p.get('severity')),
        'urgency': p.get('urgency') or 'Unknown', 'certainty': p.get('certainty') or 'Unknown',
        'onset': _iso(p.get('onset') or p.get('effective')), 'ends': _iso(p.get('ends') or p.get('expires')),
        'headline': p.get('headline'), 'area': p.get('areaDesc'), 'summary': desc[:420], 'instruction': (p.get('instruction') or '').strip().replace('\n', ' ')[:300] or None,
        'url': str(p.get('@id') or feature.get('id') or ''), 'polygons': polys, 'zoneIds': [] if polys else zones[:80], 'zonesTotal': 0 if polys else len(zones),
        'point': None, 'shape': 'official' if polys else ('zones' if zones else 'none'),
    }


LEVEL_SEVERITY = {'yellow': 'Moderate', 'orange': 'Severe', 'red': 'Extreme', 'green': 'Minor'}


def normalize_metno(feature):
    p = feature.get('properties') or {}
    polys = polygons_of(feature.get('geometry'), tol=0.003)
    level = str(p.get('awareness_level') or '')
    colour = (level.split(';')[1].strip().lower() if ';' in level else str(p.get('riskMatrixColor') or '').lower())
    interval = (feature.get('when') or {}).get('interval') or []
    onset = _iso(interval[0]) if len(interval) > 0 else None
    ends = _iso(p.get('eventEndingTime')) or (_iso(interval[1]) if len(interval) > 1 else None)
    link = None
    for r in p.get('resources') or []:
        if isinstance(r, dict) and str(r.get('uri', '')).startswith('https://'):
            link = r['uri']
            break
    return {
        'id': 'metno:' + str(p.get('id')), 'source': 'MET Norway', 'issuer': 'MET Norway', 'event': p.get('eventAwarenessName') or p.get('event') or 'Alert',
        'kind': hazard_kind('MET Norway', p.get('event'), p.get('awareness_type')), 'severity': LEVEL_SEVERITY.get(colour, norm_severity(p.get('severity'))),
        'urgency': 'Unknown', 'certainty': p.get('certainty') or 'Unknown', 'onset': onset, 'ends': ends, 'headline': p.get('title'), 'area': p.get('area'),
        'summary': (p.get('description') or '').strip().replace('\n', ' ')[:420], 'instruction': (p.get('instruction') or '').strip().replace('\n', ' ')[:300] or None,
        'url': link or 'https://www.met.no/', 'polygons': polys, 'zoneIds': [], 'zonesTotal': 0, 'point': None, 'shape': 'official' if polys else 'none', 'level': colour or None,
    }


SACHET_COLOUR_SEVERITY = {'green': 'Minor', 'blue': 'Minor', 'yellow': 'Moderate', 'orange': 'Severe', 'red': 'Extreme'}


def normalize_sachet(a):
    try:
        lon, lat = float(a['centroid_lon']), float(a['centroid_lat'])
        point = [round(lon, 3), round(lat, 3)] if -180 <= lon <= 180 and -90 <= lat <= 90 else None
    except (TypeError, ValueError, KeyError):
        point = None
    colour = str(a.get('severity_color') or '').lower()
    km2 = a.get('area_covered_sq_km')
    try:
        km2 = float(km2)
    except (TypeError, ValueError):
        km2 = None
    return {
        'id': 'sachet:' + str(a.get('identifier')), 'source': 'SACHET', 'issuer': a.get('alert_source') or 'SACHET', 'event': a.get('disaster_type') or 'Alert',
        'kind': hazard_kind('SACHET', a.get('disaster_type')), 'severity': SACHET_COLOUR_SEVERITY.get(colour, 'Unknown'), 'urgency': 'Unknown', 'certainty': 'Unknown',
        'onset': _iso(a.get('effective_start')), 'ends': _iso(a.get('effective_end')), 'headline': a.get('disaster_type'), 'area': a.get('area_description'),
        'summary': str(a.get('warning_message') or '').strip().replace('\n', ' ')[:420], 'instruction': None, 'url': 'https://sachet.ndma.gov.in/',
        'polygons': [], 'zoneIds': [], 'zonesTotal': 0, 'point': point, 'shape': 'point' if point else 'none', 'areaKm2': km2, 'level': colour or None,
    }


# ---------------------------------------------------------------- SACHET CAP / polygon XML (regex parsing: no XML entity expansion)
CAP_LEVELS = {'urgency': {'Immediate', 'Expected', 'Future', 'Past', 'Unknown'}, 'certainty': {'Observed', 'Likely', 'Possible', 'Unlikely', 'Unknown'}}


def sachet_id(value):
    s = str(value or '').strip()
    return s if re.fullmatch(r'\d{10,20}', s) else None


def _tag(block, name):
    m = re.search(r'<cap:' + name + r'>(.*?)</cap:' + name + '>', block, re.S)
    if not m:
        return None
    import html
    text = html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()
    return text or None


def parse_cap(text):
    """SACHET CAP 1.2 XML -> the fields EarthLens uses. English block preferred; district codes gathered from every block."""
    text = str(text or '')
    infos = re.findall(r'<cap:info>(.*?)</cap:info>', text, re.S)
    if not infos:
        return None
    info = next((i for i in infos if (_tag(i, 'language') or '').lower().startswith('en')), infos[0])
    codes = []
    for c in re.findall(r'<cap:valueName>\s*LGD District Code\s*</cap:valueName>\s*<cap:value>\s*(\d{1,4})\s*</cap:value>', text, re.S):
        if c not in codes:
            codes.append(c)
    out = {k: _tag(info, k) for k in ('event', 'urgency', 'severity', 'certainty', 'headline', 'description', 'instruction', 'onset', 'effective', 'expires', 'areaDesc')}
    out['districtCodes'] = codes
    return out


def parse_polygon_xml(text, tol=0.004):
    """SACHET FetchPolygonXMLFile: <polygon>lat,lon lat,lon ...</polygon> (one per area) -> list of polygons of [lon, lat] rings."""
    polys = []
    for body in re.findall(r'<polygon>(.*?)</polygon>', str(text or ''), re.S):
        ring = []
        for pair in body.split():
            try:
                lat, lon = pair.split(',')[:2]
                lat, lon = float(lat), float(lon)
            except ValueError:
                continue
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                ring.append((lon, lat))
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            s = simplify(ring, tol)
            if s:
                polys.append([s])
    return polys


_districts_cache = {}


def load_districts():
    if 'd' not in _districts_cache:
        try:
            _districts_cache['d'] = json.loads(DISTRICTS_FILE.read_text(encoding='utf-8')).get('districts', {})
        except (OSError, ValueError):
            _districts_cache['d'] = {}
    return _districts_cache['d']


def district_shape(codes, districts):
    """LGD district codes -> (polygons, names found, codes not in the boundary file)."""
    polys, names, missing = [], [], 0
    for c in codes:
        d = districts.get(str(c))
        if not d:
            missing += 1
            continue
        polys += d['polygons']
        names.append(d['name'])
    return polys, names, missing


# ---------------------------------------------------------------- service
def _get_json(url, timeout=40, accept='application/geo+json, application/json'):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept})
    limit = 12 * 1024 * 1024
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('response too large')
    return raw.decode('utf-8', 'replace') if 'xml' in accept else json.loads(raw)


class AlertsService:
    def __init__(self, cache_dir, fetch=_get_json, workers=4):
        self.cache_dir = Path(cache_dir)
        self.fetch = fetch
        self.workers = workers
        self.lock = threading.Lock()
        self.zones = {}          # key -> {'t': epoch, 'polys': [...]}
        self.failed = {}         # key -> epoch of last failure
        self.sachet = {}         # identifier -> {'t', 'cap', 'polys', 'from', 'districts'}
        self.state = {'fetchedAt': None, 'alerts': [], 'zones': {}, 'sources': {}}
        self.stop_flag = threading.Event()
        self.thread = None
        self._load_cache()
        self._load_sachet_cache()

    # --- cache
    def _load_cache(self):
        path = self.cache_dir / 'zones.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            now = time.time()
            self.zones = {k: v for k, v in data.items() if isinstance(v, dict) and now - v.get('t', 0) < ZONE_TTL and isinstance(v.get('polys'), list)}
        except (OSError, ValueError):
            self.zones = {}

    def _save_cache(self):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / 'zones.json.tmp'
            tmp.write_text(json.dumps(self.zones, separators=(',', ':')), encoding='utf-8')
            tmp.replace(self.cache_dir / 'zones.json')
        except OSError:
            pass

    def _load_sachet_cache(self):
        try:
            data = json.loads((self.cache_dir / 'sachet.json').read_text(encoding='utf-8'))
            now = time.time()
            self.sachet = {k: v for k, v in data.items() if isinstance(v, dict) and now - v.get('t', 0) < (SACHET_TTL if v.get('polys') else 3600)}
        except (OSError, ValueError):
            self.sachet = {}

    def _save_sachet_cache(self):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / 'sachet.json.tmp'
            tmp.write_text(json.dumps(self.sachet, separators=(',', ':')), encoding='utf-8')
            tmp.replace(self.cache_dir / 'sachet.json')
        except OSError:
            pass

    # --- sources
    def _collect(self):
        alerts, sources = [], {}
        try:
            nws = self.fetch(NWS_ACTIVE, 60)
            items = [normalize_nws(f) for f in nws.get('features', []) if isinstance(f, dict)]
            alerts += items
            sources['NWS'] = {'ok': True, 'count': len(items), 'ownShape': sum(1 for a in items if a['shape'] == 'official')}
        except Exception as error:
            sources['NWS'] = {'ok': False, 'error': str(error)[:120], 'count': 0}
        try:
            met = self.fetch(METNO_CURRENT, 30)
            items = [normalize_metno(f) for f in met.get('features', []) if isinstance(f, dict)]
            alerts += items
            sources['MET Norway'] = {'ok': True, 'count': len(items), 'ownShape': sum(1 for a in items if a['shape'] == 'official')}
        except Exception as error:
            sources['MET Norway'] = {'ok': False, 'error': str(error)[:120], 'count': 0}
        try:
            sac = self.fetch(SACHET_WORKER, 30)
            items = [normalize_sachet(a) for a in sac.get('alerts', []) if isinstance(a, dict)]
            alerts += items
            sources['SACHET'] = {'ok': True, 'count': len(items), 'ownShape': 0, 'fetchedAt': sac.get('fetchedAt')}
        except Exception as error:
            sources['SACHET'] = {'ok': False, 'error': str(error)[:120], 'count': 0}
        return alerts, sources

    def _resolve_zones(self, alerts):
        """Fetch missing zone polygons, most severe alerts first, a few at a time. Failures are not retried for 30 minutes."""
        need = {}
        for a in alerts:
            for key in a['zoneIds']:
                if key in self.zones and time.time() - self.zones[key]['t'] < ZONE_TTL:
                    continue
                if time.time() - self.failed.get(key, 0) < 1800:
                    continue
                rank = SEVERITY_ORDER.get(a['severity'], 4)
                if key not in need or rank < need[key]:
                    need[key] = rank
        if not need:
            return
        order = sorted(need, key=lambda k: need[k])

        def one(key):
            if self.stop_flag.is_set():
                return
            time.sleep(0.12)
            try:
                doc = self.fetch('https://api.weather.gov/zones/' + key, 30)
                polys = polygons_of(doc.get('geometry'))
                with self.lock:
                    self.zones[key] = {'t': time.time(), 'polys': polys}
            except urllib.error.HTTPError as error:
                with self.lock:
                    self.failed[key] = time.time()
                if error.code == 429:
                    time.sleep(30)
            except Exception:
                with self.lock:
                    self.failed[key] = time.time()

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for i in range(0, len(order), 40):
                for _ in pool.map(one, order[i:i + 40]):
                    pass
                self.publish(self._last_alerts, self._last_sources)   # progressive: what is ready is served while the rest loads
                self._save_cache()

    def _resolve_sachet(self, alerts):
        """Read each SACHET alert's CAP XML once (cached): its own polygon, else its LGD districts, else it stays a point."""
        changed = False
        for a in alerts:
            if a['source'] != 'SACHET':
                continue
            ident = sachet_id(a['id'].split(':', 1)[-1])
            if not ident or self.stop_flag.is_set():
                continue
            entry = self.sachet.get(ident)
            if not entry and time.time() - self.failed.get('sachet:' + ident, 0) >= 1800:
                entry = self._fetch_sachet(ident)
                changed = changed or bool(entry)
            if entry:
                self._apply_sachet(a, entry)
        if changed:
            self._save_sachet_cache()

    def _fetch_sachet(self, ident):
        try:
            cap = parse_cap(self.fetch(SACHET_BASE + 'FetchXMLFile?identifier=' + ident, 30, 'text/xml, application/xml'))
        except Exception:
            self.failed['sachet:' + ident] = time.time()
            return None
        if not cap:
            self.failed['sachet:' + ident] = time.time()
            return None
        polys, source = [], None
        try:
            polys = parse_polygon_xml(self.fetch(SACHET_BASE + 'FetchPolygonXMLFile?identifier=' + ident, 60, 'text/xml, application/xml'))
            source = 'polygon' if polys else None
        except Exception:
            polys = []
        districts = None
        if not polys and cap['districtCodes']:
            polys, names, missing = district_shape(cap['districtCodes'], load_districts())
            if polys:
                source = 'districts'
                districts = {'found': len(names), 'total': len(cap['districtCodes']), 'names': names[:60]}
        entry = {'t': time.time(), 'cap': cap, 'polys': polys, 'from': source, 'districts': districts}
        self.sachet[ident] = entry
        return entry

    @staticmethod
    def _apply_sachet(a, entry):
        cap = entry['cap']
        if cap.get('urgency') in CAP_LEVELS['urgency']:
            a['urgency'] = cap['urgency']
        if cap.get('certainty') in CAP_LEVELS['certainty']:
            a['certainty'] = cap['certainty']
        if cap.get('severity') in SEVERITY_ORDER:
            a['severity'] = cap['severity']
        text = ' '.join(x for x in (cap.get('headline'), cap.get('description')) if x)
        if text:
            a['summary'] = text.replace('\n', ' ')[:420]
        if cap.get('instruction'):
            a['instruction'] = cap['instruction'].replace('\n', ' ')[:300]
        if cap.get('areaDesc'):
            a['area'] = cap['areaDesc']
        if not a.get('onset'):
            a['onset'] = _iso(cap.get('onset') or cap.get('effective'))
        if not a.get('ends'):
            a['ends'] = _iso(cap.get('expires'))
        if entry['polys']:
            a['polygons'] = entry['polys']
            a['shape'] = 'official'
            a['shapeFrom'] = entry['from']
            if entry.get('districts'):
                a['districts'] = entry['districts']

    def publish(self, alerts, sources):
        zones_out, out = {}, []
        for a in alerts:
            a = dict(a)
            if a['zoneIds']:
                have = [k for k in a['zoneIds'] if k in self.zones and self.zones[k]['polys']]
                for k in have:
                    zones_out[k] = self.zones[k]['polys']
                a['zoneIds'] = have
                a['zonesResolved'] = len(have)
                a['shape'] = 'zones' if have else 'pending'   # 'pending': the zones are known but their polygons have not arrived yet
            out.append(a)
        sources = {k: dict(v) for k, v in sources.items()}
        if 'SACHET' in sources:
            sources['SACHET']['ownShape'] = sum(1 for a in out if a['source'] == 'SACHET' and a['polygons'])
        if 'NWS' in sources:
            sources['NWS']['zoneShapes'] = sum(1 for a in out if a['source'] == 'NWS' and a['shape'] == 'zones')
            sources['NWS']['pendingZones'] = sum(1 for a in alerts if a['source'] == 'NWS' for k in a['zoneIds'] if k not in self.zones and time.time() - self.failed.get(k, 0) >= 1800)
            sources['NWS']['zonesCached'] = len(self.zones)
        with self.lock:
            self.state = {'fetchedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'alerts': out, 'zones': zones_out, 'sources': sources}

    def refresh(self):
        alerts, sources = self._collect()
        self._last_alerts, self._last_sources = alerts, sources
        self.publish(alerts, sources)
        self._resolve_sachet(alerts)
        self.publish(alerts, sources)
        self._resolve_zones(alerts)
        self.publish(alerts, sources)
        self._save_cache()

    def _loop(self):
        while not self.stop_flag.is_set():
            try:
                self.refresh()
            except Exception:
                pass
            self.stop_flag.wait(REFRESH_SECONDS)

    def start(self):
        self._last_alerts, self._last_sources = [], {}
        self.thread = threading.Thread(target=self._loop, daemon=True, name='alerts-service')
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return self.state
