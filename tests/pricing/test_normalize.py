from datetime import date

import pytest

from ultimatescrape.pricing.fx import FxTable
from ultimatescrape.pricing import normalize

FX = FxTable(as_of=date(2026, 9, 21), to_usd={"USD": 1.0, "EUR": 1.18, "INR": 0.012})

CASES = [
    # (pay dict, worker_gig, expected min, expected max, quarantined)
    ({"pay_min": 25, "pay_max": 40, "pay_currency": "USD", "pay_unit": "hour"},
     True, 25.0, 40.0, False),
    # annual salary, corporate: /2080
    ({"pay_min": 104000, "pay_max": None, "pay_currency": "USD", "pay_unit": "year"},
     False, 50.0, None, False),
    # annual unit on a GIG is nonsense — do not convert
    ({"pay_min": 104000, "pay_max": None, "pay_currency": "USD", "pay_unit": "year"},
     True, None, None, False),
    # per-word/task never converts
    ({"pay_min": 0.02, "pay_max": None, "pay_currency": "USD", "pay_unit": "word"},
     True, None, None, False),
    ({"pay_min": 5, "pay_max": None, "pay_currency": "USD", "pay_unit": "task"},
     True, None, None, False),
    # FX conversion
    ({"pay_min": 20, "pay_max": 30, "pay_currency": "EUR", "pay_unit": "hour"},
     True, 23.6, 35.4, False),
    # unknown currency: honest NULL
    ({"pay_min": 20, "pay_max": None, "pay_currency": "XYZ", "pay_unit": "hour"},
     True, None, None, False),
    # missing currency: honest NULL
    ({"pay_min": 20, "pay_max": None, "pay_currency": "", "pay_unit": "hour"},
     True, None, None, False),
    # sanity quarantine: negative and absurd
    ({"pay_min": -5, "pay_max": None, "pay_currency": "USD", "pay_unit": "hour"},
     True, None, None, True),
    ({"pay_min": 9000, "pay_max": None, "pay_currency": "USD", "pay_unit": "hour"},
     True, None, None, True),
    # no pay at all
    ({"pay_min": None, "pay_max": None, "pay_currency": "", "pay_unit": ""},
     True, None, None, False),
]


@pytest.mark.parametrize("pay,gig,lo,hi,quar", CASES)
def test_usd_hourly(pay, gig, lo, hi, quar):
    out = normalize.usd_hourly(pay, worker_gig=gig, fx=FX)
    assert out["quarantined"] is quar
    if lo is None:
        assert out["pay_usd_hour_min"] is None
    else:
        assert out["pay_usd_hour_min"] == pytest.approx(lo, abs=0.01)
    if hi is None:
        assert out["pay_usd_hour_max"] is None
    else:
        assert out["pay_usd_hour_max"] == pytest.approx(hi, abs=0.01)


def test_fx_stamp_only_when_converted():
    converted = normalize.usd_hourly(
        {"pay_min": 20, "pay_max": None, "pay_currency": "EUR", "pay_unit": "hour"},
        worker_gig=True, fx=FX)
    native = normalize.usd_hourly(
        {"pay_min": 20, "pay_max": None, "pay_currency": "USD", "pay_unit": "hour"},
        worker_gig=True, fx=FX)
    assert converted["pay_fx_as_of"] == FX.as_of
    assert native["pay_fx_as_of"] is None


@pytest.mark.parametrize("raw,iso", [
    ("Remote - United States", "US"),
    ("São Paulo, Brazil", "BR"),
    ("Kolkata, India", "IN"),
    ("Remote", ""),
    ("", ""),
    ("Manila, Philippines", "PH"),
    ("United Kingdom", "GB"),
    ("Kyiv, Ukraine", "UA"),
    ("Remote - UK", "GB"),
])
def test_country_from_location(raw, iso):
    assert normalize.country_from_location(raw) == iso
