#!/usr/bin/env python3
"""Collecte des données du Radar MEL, lancée toutes les 10 minutes par GitHub Actions.

Écrit dans --out :
  rt.json        retards et suppressions SNCF à Lille Flandres / Lille Europe (temps réel)
  traffic.json   événements routiers Bison Futé autour de Lille (temps réel)
  schedule.json  arrivées prévues en gare sur 48 h (GTFS SNCF, une fois par jour)
  events.json    agenda Zénith, Lille Grand Palais et matchs du LOSC à domicile (une fois par jour)
Aucune clé d'API n'est nécessaire.
"""
import argparse, collections, csv, datetime as dt, html, io, json, os, re, sys, urllib.request, zipfile
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Paris')
UA = {'User-Agent': 'Mozilla/5.0 (radar-mel; collecte quotidienne)'}
STOPS = {'87286005': 'flandres', '87223263': 'europe'}
BBOX = (50.50, 2.85, 50.80, 3.30)
GTFS_URL = 'https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip'
RT_URL = 'https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates'
BF_URL = 'https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/Evenementiel-DIR/grt/RRN/content.xml'
MONTHS = {'janv': 1, 'janvier': 1, 'févr': 2, 'février': 2, 'mars': 3, 'avr': 4, 'avril': 4, 'mai': 5, 'juin': 6,
          'juil': 7, 'juillet': 7, 'août': 8, 'sept': 9, 'septembre': 9, 'oct': 10, 'octobre': 10,
          'nov': 11, 'novembre': 11, 'déc': 12, 'décembre': 12}


def now():
    return dt.datetime.now(TZ)


def iso(d):
    return d.astimezone(TZ).strftime('%Y-%m-%dT%H:%M')


def get(url, timeout=90):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def save(out, name, data):
    data['updated'] = iso(now())
    with open(os.path.join(out, name), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))


def station_of(stop_id):
    for code, st in STOPS.items():
        if stop_id.endswith(code):
            return st
    return None


def mode_of(stop_id):
    m = re.search(r'OCE(.+?)-\d{8}$', stop_id or '')
    raw = (m.group(1) if m else '').strip()
    return {'Train TER': 'TER', 'TGV INOUI': 'TGV INOUI', 'OUIGO': 'OUIGO', 'INTERCITES': 'Intercités'}.get(raw, raw or 'Train')


def seats(mode):
    m = mode.lower()
    return 1000 if 'ouigo' in m else 750 if 'eurostar' in m else 550 if ('tgv' in m or 'inoui' in m) else 400 if 'intercit' in m else 250


# ---------- Horaires SNCF (GTFS) ----------
def schedule():
    z = zipfile.ZipFile(io.BytesIO(get(GTFS_URL, 240)))
    today = now().date()
    days = [today + dt.timedelta(d) for d in range(-1, 3)]
    active = collections.defaultdict(set)
    if 'calendar.txt' in z.namelist():
        for r in csv.DictReader(io.TextIOWrapper(z.open('calendar.txt'), 'utf-8-sig')):
            a, b = dt.datetime.strptime(r['start_date'], '%Y%m%d').date(), dt.datetime.strptime(r['end_date'], '%Y%m%d').date()
            flags = [r['monday'], r['tuesday'], r['wednesday'], r['thursday'], r['friday'], r['saturday'], r['sunday']]
            for d in days:
                if a <= d <= b and flags[d.weekday()] == '1':
                    active[r['service_id']].add(d)
    for r in csv.DictReader(io.TextIOWrapper(z.open('calendar_dates.txt'), 'utf-8-sig')):
        d = dt.datetime.strptime(r['date'], '%Y%m%d').date()
        if d in days:
            (active[r['service_id']].add if r['exception_type'] == '1' else active[r['service_id']].discard)(d)
    trips = {r['trip_id']: r['service_id'] for r in csv.DictReader(io.TextIOWrapper(z.open('trips.txt'), 'utf-8-sig'))}
    seq = {}
    hits = []
    for r in csv.DictReader(io.TextIOWrapper(z.open('stop_times.txt'), 'utf-8-sig')):
        tid, s = r['trip_id'], int(r['stop_sequence'])
        lo, hi = seq.get(tid, (s, s))
        seq[tid] = (min(lo, s), max(hi, s))
        st = station_of(r['stop_id'])
        if st:
            hits.append((tid, st, r['arrival_time'] or r['departure_time'], s, r['stop_id']))
    start, end = now() - dt.timedelta(hours=1), now() + dt.timedelta(hours=48)
    out = []
    for tid, st, hms, s, stop_id in hits:
        lo, hi = seq[tid]
        if s == lo:  # Lille est le départ du train : personne n'y descend
            continue
        h, m, sec = (int(x) for x in hms.split(':'))
        for d in active.get(trips.get(tid), ()):
            t = dt.datetime(d.year, d.month, d.day, tzinfo=TZ) + dt.timedelta(hours=h, minutes=m, seconds=sec)
            if start <= t <= end:
                mode = mode_of(stop_id)
                out.append({'trip': tid, 'st': st, 't': iso(t), 'mode': mode, 'seats': seats(mode), 'term': s == hi})
    out.sort(key=lambda x: x['t'])
    return {'trains': out}


# ---------- Temps réel SNCF (GTFS-RT) ----------
def realtime():
    from google.transit import gtfs_realtime_pb2
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(get(RT_URL))
    out = []
    for e in feed.entity:
        if not e.HasField('trip_update'):
            continue
        tu = e.trip_update
        for su in tu.stop_time_update:
            st = station_of(su.stop_id)
            if not st:
                continue
            ev = su.arrival if su.HasField('arrival') else su.departure
            out.append({'trip': tu.trip.trip_id, 'st': st,
                        't': iso(dt.datetime.fromtimestamp(ev.time, TZ)) if ev.time else None,
                        'delay': round(ev.delay / 60) if ev.delay else 0,
                        'canceled': tu.trip.schedule_relationship == 3 or su.schedule_relationship == 1,
                        'mode': mode_of(su.stop_id)})
    return {'trains': out}


# ---------- Bison Futé (DATEX II) ----------
LABELS = {'Accident': 'Accident', 'VehicleObstruction': 'Véhicule gênant', 'AbnormalTraffic': 'Bouchon',
          'RoadOrCarriagewayOrLaneManagement': 'Voie fermée', 'MaintenanceWorks': 'Travaux',
          'ReroutingManagement': 'Déviation', 'ConstructionWorks': 'Chantier', 'PoorEnvironmentConditions': 'Météo difficile',
          'GeneralObstruction': 'Obstacle', 'NetworkManagement': 'Restriction'}


def traffic():
    x = get(BF_URL).decode('utf-8', 'replace')
    out, seen = [], set()
    for b in re.split(r'<(?:\w+:)?situationRecord\b', x)[1:]:
        m = re.search(r'<(?:\w+:)?latitude>([\d.]+)</(?:\w+:)?latitude>\s*<(?:\w+:)?longitude>([\d.]+)<', b)
        if not m:
            continue
        lat, lng = float(m.group(1)), float(m.group(2))
        if not (BBOX[0] < lat < BBOX[2] and BBOX[1] < lng < BBOX[3]):
            continue
        t = re.search(r'xsi:type="(?:\w+:)?(\w+)"', b)
        kind = t.group(1) if t else 'Event'
        road = re.search(r'<(?:\w+:)?roadNumber>([^<]+)<', b)
        key = (kind, round(lat, 3), round(lng, 3))
        if key in seen:
            continue
        seen.add(key)
        txt = [html.unescape(v).strip() for v in re.findall(r'<(?:\w+:)?value[^>]*>([^<]{4,200})<', b)]
        out.append({'kind': kind, 'label': LABELS.get(kind, kind), 'lat': lat, 'lng': lng,
                    'road': road.group(1).strip() if road else '', 'txt': next((s for s in txt if not s.startswith('CIGT')), '')[:160]})
    return {'events': out}


# ---------- Agenda ----------
def text_of(raw):
    s = raw.decode('utf-8', 'replace')
    s = re.sub(r'(?is)<(script|style).*?</\1>', ' ', s)
    return html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s)))


def month(word):
    return MONTHS.get(word.lower().rstrip('.'))


DAYS_RX = r'(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)'


def single_shows(t, src):
    """« jeudi 29 octobre 2026 - 20:00 Zénith de Lille JOSMAN 50€ » et variantes."""
    out = []
    rx = re.compile(DAYS_RX + r' (\d{1,2}) (\w+) (\d{4}) - (\d{1,2}):(\d{2}) (Zénith de Lille|Théâtre Louis Pasteur|Grand Théâtre|Lille Grand Palais) (.{2,70}?)(?= \d+[,.]?\d*\s?€| Complet| Gratuit| \d{1,2} (?:janv|févr|mars|avr|mai|juin|juil|août|sept|oct|nov|déc)| ' + DAYS_RX + r'|$)', re.I)
    for m in rx.finditer(t):
        mo = month(m.group(2))
        if not mo:
            continue
        start = dt.datetime(int(m.group(3)), mo, int(m.group(1)), int(m.group(4)), int(m.group(5)), tzinfo=TZ)
        venue, title = (m.group(6) or ''), m.group(7).strip(' -')
        if not venue or not title or title.lower().startswith('voir') or re.match(DAYS_RX, title, re.I):
            continue  # les pages répètent chaque date sans le lieu : on ne garde que la ligne complète
        zen = venue.lower().startswith('zénith')
        out.append({'place': 'zenith' if zen else 'lgp', 'label': title, 'start': iso(start),
                    'end': iso(start + dt.timedelta(minutes=165 if zen else 135)), 'size': 'm' if zen else 's', 'src': src})
    return out


def lgp_ranges(t):
    """« Du 31 oct. au 8 nov. 2026 - 10:00 Hall Paris Salon Habitat et Déco 2026 Du 31 oct. … » : un événement par jour."""
    out = []
    rx = re.compile(r'Du (\d{1,2})(?: (\w+)\.?)? au (\d{1,2}) (\w+)\.? (\d{4})(?: - \d{1,2}:\d{2})?(?: (?:Hall \w+|Lille Grand Palais))? (.{2,90}?) Du \1 ', re.I)
    horizon = now().date() + dt.timedelta(days=14)
    for m in rx.finditer(t):
        y, m2 = int(m.group(5)), month(m.group(4))
        m1 = month(m.group(2)) if m.group(2) else m2
        if not (m1 and m2):
            continue
        a = dt.date(y - (1 if m1 > m2 else 0), m1, int(m.group(1)))
        b = dt.date(y, m2, int(m.group(3)))
        title = m.group(6).strip()
        congress = re.search(r'(?i)congr|congress|journ[ée]e|forum|symposium|convention|colloque|assises', title)
        close = 18 if congress else 19
        d = max(a, now().date())
        while d <= min(b, horizon):
            out.append({'place': 'lgp', 'label': title, 'start': iso(dt.datetime(d.year, d.month, d.day, 9, tzinfo=TZ)),
                        'end': iso(dt.datetime(d.year, d.month, d.day, close, tzinfo=TZ)), 'size': 'm', 'src': 'Lille Grand Palais',
                        'kind': 'congrès' if congress else 'salon'})
            d += dt.timedelta(days=1)
    return out


def losc():
    n = now()
    season = n.year if n.month >= 7 else n.year - 1
    data = json.loads(get('https://fixturedownload.com/feed/json/ligue-1-%d' % season))
    out = []
    for f in data:
        if 'lille' not in (f.get('HomeTeam') or '').lower() and 'losc' not in (f.get('HomeTeam') or '').lower():
            continue
        k = dt.datetime.strptime(f['DateUtc'], '%Y-%m-%d %H:%M:%SZ').replace(tzinfo=dt.timezone.utc).astimezone(TZ)
        out.append({'place': 'pm', 'label': 'LOSC – ' + f.get('AwayTeam', ''), 'start': iso(k),
                    'end': iso(k + dt.timedelta(minutes=115)), 'size': 'xl', 'src': 'Ligue 1'})
    return out


def events():
    evs, status = [], {}
    for name, fn in (('Zénith', lambda: single_shows(text_of(get('https://www.zenithdelille.com/')), 'Zénith de Lille')),
                     ('Lille Grand Palais', lambda: lgp_ranges(text_of(get('https://www.lillegrandpalais.com/agenda/'))) +
                      single_shows(text_of(get('https://www.lillegrandpalais.com/agenda/')), 'Lille Grand Palais')),
                     ('LOSC', losc)):
        try:
            got = fn()
            evs += got
            status[name] = len(got)
        except Exception as e:  # une source en panne ne bloque pas les autres
            status[name] = 'erreur : %s' % e
    lo, hi = iso(now() - dt.timedelta(hours=6)), iso(now() + dt.timedelta(days=14))
    seen, out = set(), []
    for e in sorted(evs, key=lambda e: e['start']):
        key = (e['start'][:10], e['label'].lower()[:30])
        if lo <= e['end'] <= hi and key not in seen:
            seen.add(key)
            out.append(e)
    return {'events': out, 'status': status}


def stale(out, name):
    try:
        with open(os.path.join(out, name), encoding='utf-8') as f:
            return json.load(f).get('updated', '')[:10] != now().strftime('%Y-%m-%d')
    except Exception:
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data')
    ap.add_argument('--daily', action='store_true', help='forcer horaires et agenda')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    jobs = [('rt.json', realtime), ('traffic.json', traffic)]
    if a.daily or stale(a.out, 'schedule.json'):
        jobs.append(('schedule.json', schedule))
    if a.daily or stale(a.out, 'events.json'):
        jobs.append(('events.json', events))
    failed = 0
    for name, fn in jobs:
        try:
            data = fn()
            save(a.out, name, data)
            print(name, 'ok', {k: len(v) for k, v in data.items() if isinstance(v, list)})
        except Exception as e:
            failed += 1
            print(name, 'ÉCHEC', e, file=sys.stderr)
    sys.exit(1 if failed == len(jobs) else 0)


if __name__ == '__main__':
    main()
