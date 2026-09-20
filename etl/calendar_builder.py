"""Build dim_calendar_day rows from P6 clndr_data.

workday_index is a running count of working days from the project epoch.
Working-day variance is then idx(a) - idx(b): correct across holidays and
site shutdowns, and an integer subtraction rather than a date walk. Calendar
day arithmetic is simply wrong on a 6-day week with holidays, and every
variance metric in the mart depends on getting this right.
"""
import datetime as dt
from canonical import CalendarDay
from p6cal import Calendar


def build_calendar_days(xer_tables, start: dt.date, end: dt.date):
    out = []
    for r in xer_tables["CALENDAR"]["rows"]:
        cal = Calendar(r["clndr_id"], r["clndr_name"], r["clndr_data"], r["day_hr_cnt"])
        idx, d = 0, start
        while d <= end:
            hrs = cal.hours_on(d)
            working = hrs > 0
            if working:
                idx += 1
            out.append(CalendarDay(
                calendar_id=r["clndr_id"],
                calendar_name=r["clndr_name"],
                cal_date=d,
                working_hours=round(hrs, 2),
                is_working_day=working,
                is_exception=d in cal.exc,
            ))
            d += dt.timedelta(days=1)
    return out


def workday_index_map(days):
    """calendar_id -> {date: running working-day index}"""
    out = {}
    for c in days:
        m = out.setdefault(c.calendar_id, {})
        prev = m.get(c.cal_date - dt.timedelta(days=1), 0)
        m[c.cal_date] = prev + (1 if c.is_working_day else 0)
    return out
