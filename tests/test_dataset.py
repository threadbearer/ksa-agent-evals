from collections import Counter


def test_forty_scenarios_ten_per_trade(scenarios):
    assert len(scenarios) == 40
    assert Counter(s.trade for s in scenarios) == {"hvac": 10, "plumbing": 10, "electrical": 10, "roofing": 10}


def test_every_scenario_is_gradeable(scenarios, businesses):
    for s in scenarios:
        assert s.trade in businesses
        assert s.beats, s.id
        assert any(c.severity in ("critical", "major") for c in s.criteria), f"{s.id} has nothing that can fail it"
        ids = [c.id for c in s.criteria]
        assert len(ids) == len(set(ids)), f"{s.id} has duplicate criterion ids"


def test_safety_scenarios_have_a_deterministic_backstop(scenarios):
    for s in scenarios:
        if s.type == "emergency" and s.trade != "roofing":
            assert any(c.kind == "mentions_any" for c in s.checks), s.id
