from datetime import datetime, timezone

from ultimatescrape.jobboards.fetchers import Listing
from ultimatescrape.pricing.diff import (
    ChangeSet, ExistingRow, listing_content_hash, pay_key, plan_changes,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def make(platform="outlier", ext="a1", title="Rater", pay_min=25.0, desc="d"):
    return Listing(platform=platform, company="X", title=title,
                   external_id=ext, pay_min=pay_min, pay_currency="USD",
                   pay_unit="hour", description_full=desc)


def existing_for(l, **over):
    base = dict(id=1, content_hash=listing_content_hash(l), delisted=False,
                pay_key=pay_key(l))
    base.update(over)
    return ExistingRow(**base)


def test_new_listing_is_insert():
    cs = plan_changes({}, [make()], {"outlier"}, NOW)
    assert len(cs.inserts) == 1 and not cs.updates and not cs.delists


def test_unchanged_is_touch_only():
    l = make()
    cs = plan_changes({("outlier", "a1"): existing_for(l)}, [l], {"outlier"}, NOW)
    assert cs.touches == [1] and not cs.updates and not cs.history


def test_pay_change_yields_update_and_history():
    old = make(pay_min=25.0)
    new = make(pay_min=30.0)
    cs = plan_changes({("outlier", "a1"): existing_for(old)}, [new], {"outlier"}, NOW)
    assert cs.updates == [(1, new)]
    assert cs.history[0]["change_type"] == "pay_change"
    assert cs.history[0]["listing_id"] == 1
    assert cs.history[0]["old_pay"] == {"pay_min": 25.0, "pay_max": None, "pay_currency": "USD", "pay_unit": "hour"}


def test_jd_change_without_pay_change():
    old = make(desc="v1")
    new = make(desc="v2")
    cs = plan_changes({("outlier", "a1"): existing_for(old)}, [new], {"outlier"}, NOW)
    assert cs.history[0]["change_type"] == "jd_change"


def test_absent_on_successful_platform_is_delisted():
    old = make()
    cs = plan_changes({("outlier", "a1"): existing_for(old)}, [], {"outlier"}, NOW)
    assert cs.delists == [1]
    assert cs.history[0]["change_type"] == "delisted"


def test_absent_on_FAILED_platform_is_untouched():
    old = make()
    cs = plan_changes({("outlier", "a1"): existing_for(old)}, [], set(), NOW)
    assert not cs.delists and not cs.history and not cs.touches


def test_already_delisted_is_not_redelisted():
    old = make()
    cs = plan_changes({("outlier", "a1"): existing_for(old, delisted=True)},
                      [], {"outlier"}, NOW)
    assert not cs.delists


def test_reappearing_listing_is_relisted():
    l = make()
    cs = plan_changes({("outlier", "a1"): existing_for(l, delisted=True)},
                      [l], {"outlier"}, NOW)
    assert cs.relists == [(1, l)]
    assert cs.history[0]["change_type"] == "relisted"
