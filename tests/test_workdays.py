from datetime import date

import pytest

from apps.accounts.models import AppSetting
from apps.jobs import workdays
from apps.jobs.workdays import WorkCalendar

MON_THU = WorkCalendar()


def test_default_week_is_monday_to_thursday():
    assert MON_THU.is_workday(date(2026, 11, 12))  # Thursday
    assert not MON_THU.is_workday(date(2026, 11, 13))  # Friday
    assert not MON_THU.is_workday(date(2026, 11, 15))  # Sunday


def test_add_business_days_skips_fridays_weekends_and_holidays():
    assert MON_THU.add_business_days(date(2026, 11, 12), 1) == date(2026, 11, 16)
    assert MON_THU.add_business_days(date(2026, 11, 12), 14) == date(2026, 12, 8)
    with_holiday = WorkCalendar(holidays=frozenset({date(2026, 11, 16)}))
    assert with_holiday.add_business_days(date(2026, 11, 12), 1) == date(2026, 11, 17)


def test_add_business_days_starting_on_a_day_off():
    # From Saturday, the first working day is Monday.
    assert MON_THU.add_business_days(date(2026, 11, 14), 1) == date(2026, 11, 16)


def test_workdays_until():
    assert MON_THU.workdays_until(date(2026, 11, 12), date(2026, 11, 19)) == 4
    assert MON_THU.workdays_until(date(2026, 11, 19), date(2026, 11, 12)) == -4
    assert MON_THU.workdays_until(date(2026, 11, 12), date(2026, 11, 12)) == 0


def test_no_working_days_is_refused():
    with pytest.raises(ValueError):
        WorkCalendar(working_days=frozenset()).add_business_days(date(2026, 11, 12), 1)


@pytest.mark.django_db
def test_settings_override_the_defaults():
    assert workdays.turnaround_days() == 14
    assert workdays.at_risk_days() == 4
    AppSetting.objects.create(key="working_days", value=[0, 1, 2, 3, 4])
    AppSetting.objects.create(key="holidays", value=["2026-12-25"])
    cal = workdays.calendar()
    assert cal.is_workday(date(2026, 11, 13))  # Friday now counts
    assert not cal.is_workday(date(2026, 12, 25))
