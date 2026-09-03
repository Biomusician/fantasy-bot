from sleeper_tool.name_matching import build_name_index, normalize_name


def test_normalize_strips_suffixes():
    assert normalize_name("Patrick Mahomes II") == "patrick mahomes"
    assert normalize_name("Michael Pittman Jr.") == "michael pittman"
    assert normalize_name("Odell Beckham Jr") == "odell beckham"


def test_normalize_handles_periods_and_apostrophes():
    assert normalize_name("D.J. Moore") == "dj moore"
    assert normalize_name("Ja'Marr Chase") == "jamarr chase"
    assert normalize_name("O'Brien") == "obrien"


def test_normalize_is_case_and_space_insensitive():
    assert normalize_name("A.J.   Brown") == normalize_name("AJ Brown")


def test_normalize_strips_accents():
    assert normalize_name("José Aléxander") == "jose alexander"


def test_normalize_empty_string():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""


def test_build_name_index_keys_are_normalized():
    players = [{"name": "Patrick Mahomes II"}, {"name": "CeeDee Lamb"}]
    index = build_name_index(players, name_key="name")
    assert "patrick mahomes" in index
    assert index["patrick mahomes"]["name"] == "Patrick Mahomes II"


def test_build_name_index_skips_blank_names():
    players = [{"name": ""}, {"name": "Valid Name"}]
    index = build_name_index(players, name_key="name")
    assert len(index) == 1


def test_normalize_name_is_memoized_without_changing_answers():
    # The hot path calls this ~85k times per run over ~2.5k distinct names,
    # so it is cached. Guard: the cache must never see a non-str key, and
    # repeat calls must agree with the first one.
    first = normalize_name("Amon-Ra St. Brown")
    assert normalize_name("Amon-Ra St. Brown") == first == "amon ra st brown"
    assert normalize_name(None) == ""
    assert normalize_name("") == ""
    assert normalize_name(0) == ""  # falsy non-str short-circuits before the cache
    assert normalize_name(12) == "12"  # truthy non-str is coerced, not crashed on
