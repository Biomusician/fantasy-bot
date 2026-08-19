from sleeper_tool.formatting import ordinal, ordinal_pct


def test_ordinal_basic_cases():
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"


def test_ordinal_teens_are_all_th():
    # 11th/12th/13th are the classic edge case a naive %10 lookup gets wrong.
    assert ordinal(11) == "11th"
    assert ordinal(12) == "12th"
    assert ordinal(13) == "13th"


def test_ordinal_twenty_something():
    assert ordinal(21) == "21st"
    assert ordinal(22) == "22nd"
    assert ordinal(23) == "23rd"
    assert ordinal(24) == "24th"


def test_ordinal_hundreds():
    assert ordinal(100) == "100th"
    assert ordinal(101) == "101st"
    assert ordinal(111) == "111th"  # still a "teen" pattern at the hundred mark


def test_ordinal_pct_none_is_unknown():
    assert ordinal_pct(None) == "unknown"


def test_ordinal_pct_rounds_before_suffixing():
    assert ordinal_pct(42.6) == "43rd percentile"
    assert ordinal_pct(20.4) == "20th percentile"
