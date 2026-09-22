from ultimatescrape.pricing.rate_units import RATE_UNIT_DECODE, decode


def test_codebook_is_populated_and_contains_hour():
    assert RATE_UNIT_DECODE, "codebook empty — discovery step not done"
    assert "hour" in RATE_UNIT_DECODE.values()


def test_unmapped_code_is_none():
    assert decode("999-nope") is None
    assert decode(None) is None


def test_decode_1_is_exactly_hour():
    # pins the measured mapping itself, not just "hour is present somewhere"
    assert decode("1") == "hour"
    assert decode(1) == "hour"  # int codes from the proxy decode the same
