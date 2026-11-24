"""Tests for world-sampler-point.py with a fake route (no network). Run: python world-sampler-point-tests.py"""
import importlib.util, json, sys, urllib.error
from pathlib import Path

spec = importlib.util.spec_from_file_location('sampler', Path(__file__).with_name('world-sampler-point.py'))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

START = 1795500000 // 3600 * 3600
HOURS = 60
passed = 0


def check(name, cond):
    global passed
    if not cond:
        print('FAIL', name)
        sys.exit(1)
    passed += 1
    print('ok  ', name)


def point(lat, lon, hours=HOURS, start=START, t=None):
    series = lambda base: [base + (k % 7) for k in range(hours)]
    return {'schema': s.POINT_SCHEMA, 'lat': lat, 'lon': lon, 'startEpoch': start, 'hours': hours,
            'fields': {'t': t if t is not None else series(lat / 10.0), 'h': series(50), 'uv': series(3), 'p': series(1010), 'ws': series(10), 'wd': series(90), 'c': series(40), 'r': [0.0] * hours}}


def run(fetch, **kw):
    return s.build(HOURS, fetch=fetch, rate=10000, workers=8, sleep=lambda x: None, now=lambda: START + 600, **kw)


calls = []
def good(lat, lon, hours):
    calls.append((lat, lon, hours))
    return point(lat, lon)

w = run(good)
check('one request per lattice point (2,520) and never more', len(calls) == 2520 and len(set(calls)) == 2520)
check('the lattice is 35 x 72 from -85/-180 with 5 degree steps', w['rows'] == 35 and w['cols'] == 72 and w['lat0'] == -85.0 and w['lon0'] == -180.0 and w['stepDegrees'] == 5.0)
check('every point asks for the requested hours', all(c[2] == HOURS for c in calls))
check('file has the world schema, 60 hours, all 8 fields, 2,520 series each', w['schema'] == s.SCHEMA and w['hours'] == 60 and set(w['fields']) == set(s.FIELDS) and all(len(v) == 2520 and all(len(x) == 60 for x in v) for v in w['fields'].values()))
check('not synthetic and no missing points', w['synthetic'] is False and w['missingPoints'] == [])
check('point order is row (latitude) major', w['fields']['t'][0][0] == point(-85, -180)['fields']['t'][0] and w['fields']['t'][72][0] == point(-80, -180)['fields']['t'][0])

# a point that fails every first try but answers on the second pass is not left missing
failed_once = {}
def flaky(lat, lon, hours):
    key = (lat, lon)
    if key in [(0, 0), (10, 10)]:
        failed_once[key] = failed_once.get(key, 0) + 1
        if failed_once[key] <= 3:             # the three tries of the main pass all fail
            raise urllib.error.URLError('busy')
    return point(lat, lon)
w = run(flaky)
check('a point that fails the main pass is asked again on the second pass and filled', w['missingPoints'] == [] and w['calls'] == 2520)
check('the second pass asks only the failed points (3 failures + 1 success each)', failed_once == {(0, 0): 4, (10, 10): 4})

# a point that keeps failing after the second pass stays missing, and the refused ones are never retried
asked = []
def stubborn(lat, lon, hours):
    asked.append((lat, lon))
    if (lat, lon) == (5, 5):
        raise urllib.error.URLError('down')
    if (lat, lon) == (10, 10):
        raise urllib.error.HTTPError('u', 422, 'x', {}, None)
    return point(lat, lon)
w = run(stubborn)
check('a stubborn point stays missing after the passes', (5 + 85) // 5 * 72 + (5 + 180) // 5 in w['missingPoints'])
check('a refused (422) point is asked once only', asked.count((10, 10)) == 1)
check('a stubborn point is tried 3 times in the main pass and 3 in each of 2 second passes', asked.count((5, 5)) == 9)

# a point that never answers stays unavailable, within the 2% allowance
def one_dead(lat, lon, hours):
    if (lat, lon) == (0, 0):
        raise urllib.error.URLError('down')
    return point(lat, lon)
w = run(one_dead)
check('one dead point is listed as missing and filled with nulls', w['missingPoints'] == [17 * 72 + 36] and all(v is None for v in w['fields']['t'][17 * 72 + 36]))

# too many dead points: no file
def many_dead(lat, lon, hours):
    if lat > 40:
        raise urllib.error.URLError('down')
    return point(lat, lon)
try:
    run(many_dead)
    check('too many dead points refuse to write', False)
except SystemExit as e:
    check('too many dead points refuse to write', 'not writing a file' in str(e))

# a 422 point is never retried
n422 = []
def refuse(lat, lon, hours):
    if (lat, lon) == (10, 10):
        n422.append(1)
        raise urllib.error.HTTPError('u', 422, 'x', {}, None)
    return point(lat, lon)
run(refuse)
check('a 422 point is asked once, not retried', len(n422) == 1)

# a point served from an older cache starts earlier and is trimmed to the shared start
def mixed(lat, lon, hours):
    if (lat, lon) == (20, 20):
        return point(lat, lon, hours=HOURS, start=START - 3 * 3600, t=list(range(100, 160)))
    return point(lat, lon)
w = run(mixed)
idx = 21 * 72 + 40
check('the shared start is the latest start and older points are trimmed', w['startEpoch'] == START and w['hours'] == 57 and all(len(x) == 57 for x in w['fields']['t']))
check('trimmed point keeps the right hours (3 dropped)', w['fields']['t'][idx][0] == 103)

# bad shapes are refused
for name, mut in {
    'wrong schema': lambda b: b.update(schema='x'),
    'series wrong length': lambda b: b['fields'].update(t=b['fields']['t'][:-1]),
    'text in a series': lambda b: b['fields']['t'].__setitem__(0, 'hot'),
    'nan in a series': lambda b: b['fields']['t'].__setitem__(0, float('nan')),
    'bool in a series': lambda b: b['fields']['t'].__setitem__(0, True),
    'missing field': lambda b: b['fields'].pop('uv'),
    'all temperatures null': lambda b: b['fields'].update(t=[None] * HOURS),
}.items():
    b = point(0, 0)
    mut(b)
    check('a point with ' + name + ' is not accepted', not s.valid_point(b, HOURS))
check('a good point is accepted', s.valid_point(point(0, 0), HOURS))

# shortest point too short
def short(lat, lon, hours):
    if (lat, lon) == (0, 0):
        return point(lat, lon, hours=20)
    return point(lat, lon)
try:
    run(short)
    check('a too-short point refuses the file', False)
except SystemExit as e:
    check('a too-short point refuses the file', 'covers only' in str(e))

# the file is the shape the app and website parse
json.dumps(w)
check('the file is plain JSON', True)
print(passed, 'tests passed')
