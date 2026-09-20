"""P6 calendar parser and working-time arithmetic.

clndr_data grammar:
  (0||CalendarData()(
     (0||DaysOfWeek()( (0||<dow>()( <shifts> )) x7 ))
     (0||Exceptions()( (0||<i>(d|<serial>)( <shifts> )) ... ))))
  shift := (0||<i>(f|HH:MM|s|HH:MM)())   f = shift finish, s = shift start
  dow   := 1=Sunday .. 7=Saturday
  serial:= days since 1899-12-30
No shifts on a weekday or exception -> non-working.
P6 works in HOURS against shift boundaries, not whole days.
"""
import re, datetime as dt

EPOCH = dt.date(1899, 12, 30)
_SHIFT = re.compile(r'\(0\|\|\d+\(f\|(\d{1,2}):(\d{2})\|s\|(\d{1,2}):(\d{2})\)\(\)\)')
_DOW   = re.compile(r'\(0\|\|([1-7])\(\)\(')
_EXC   = re.compile(r'\(0\|\|\d+\(d\|(\d+)\)\(')

def _shifts(blob):
    out = []
    for h1, m1, h2, m2 in _SHIFT.findall(blob or ''):
        s = int(h2) + int(m2)/60.0
        f = int(h1) + int(m1)/60.0
        if f > s: out.append((s, f)) 
    return sorted(out)

def _split(text, start):
    """Return the balanced-paren substring beginning at `start` (which indexes '(')."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '(': depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0: return text[start:i+1]
    return text[start:]

class Calendar:
    def __init__(self, clndr_id, name, data, day_hr):
        self.id, self.name = clndr_id, name
        self.day_hr = float(day_hr or 8)
        self.dow = {d: [] for d in range(1, 8)}
        self.exc = {}
        dw = data.find('DaysOfWeek')
        ex = data.find('Exceptions')
        if dw >= 0:
            seg = data[dw:ex if ex > dw else len(data)]
            for m in _DOW.finditer(seg):
                self.dow[int(m.group(1))] = _shifts(_split(seg, m.start()))
        if ex >= 0:
            seg = data[ex:]
            for m in _EXC.finditer(seg):
                blk = _split(seg, m.start())
                self.exc[EPOCH + dt.timedelta(days=int(m.group(1)))] = _shifts(blk)

    def shifts_on(self, d):
        if d in self.exc: return self.exc[d]
        return self.dow[d.isoweekday() % 7 + 1]

    def hours_on(self, d): return sum(f - s for s, f in self.shifts_on(d))
    def is_workday(self, d): return bool(self.shifts_on(d))

    def _next_worktime(self, ts):
        """Snap a datetime forward to the next instant inside a shift."""
        d, h = ts.date(), ts.hour + ts.minute/60.0
        for _ in range(2000):
            for s, f in self.shifts_on(d):
                if h < s: return d, s
                if s <= h < f: return d, h
            d += dt.timedelta(days=1); h = 0.0
        raise RuntimeError('no working time found')

    def add_hours(self, start, hours):
        """Advance `hours` of working time from datetime `start`. Returns datetime."""
        rem = float(hours)
        # zero-duration (milestone): P6 keeps the instant as-is, no forward snap
        if rem <= 1e-9: return start
        d, h = self._next_worktime(start)
        for _ in range(20000):
            for s, f in self.shifts_on(d):
                if f <= h: continue
                cur = max(h, s)
                avail = f - cur
                if rem <= avail + 1e-9:
                    end = cur + rem
                    return dt.datetime.combine(d, dt.time()) + dt.timedelta(hours=end)
                rem -= avail; h = f
            d += dt.timedelta(days=1); h = 0.0
        raise RuntimeError('add_hours did not terminate')

    def diff_hours(self, a, b):
        """Signed working hours between two datetimes."""
        if a == b: return 0.0
        sign, lo, hi = (1, a, b) if b > a else (-1, b, a)
        tot, d = 0.0, lo.date()
        while d <= hi.date():
            for s, f in self.shifts_on(d):
                s_dt = dt.datetime.combine(d, dt.time()) + dt.timedelta(hours=s)
                f_dt = dt.datetime.combine(d, dt.time()) + dt.timedelta(hours=f)
                tot += max(0.0, (min(f_dt, hi) - max(s_dt, lo)).total_seconds()/3600.0)
            d += dt.timedelta(days=1)
        return sign * tot

def load_calendars(tables):
    return {r['clndr_id']: Calendar(r['clndr_id'], r['clndr_name'], r['clndr_data'], r['day_hr_cnt'])
            for r in tables['CALENDAR']['rows']}
