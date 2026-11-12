from shorts_digest.dedupe import find_duplicate, normalize_concept, similarity


def test_normalize_concept():
    assert normalize_concept("5 Excel Shortcuts!! 🔥") == "5 excel shortcuts"


def test_near_identical_concepts_are_duplicates():
    assert find_duplicate("5 Excel Shortcuts!!", ["Use Docker", "5 excel shortcuts"]) == (
        "5 excel shortcuts"
    )


def test_different_concepts_are_not_duplicates():
    assert find_duplicate("Python decorators explained", ["Docker volumes explained"]) is None
    assert similarity("Excel XLOOKUP", "Excel pivot tables") < 0.85


def test_empty_concept_never_matches():
    assert find_duplicate("🔥🔥", ["anything"]) is None


def test_one_different_key_word_is_not_a_duplicate():
    assert find_duplicate("Python decorators explained", ["Python generators explained"]) is None
    assert find_duplicate("Concept A", ["Concept B"]) is None
