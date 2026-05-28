"""Smoke test do tracker de restricao / auto-pause."""

from app.evolution import EvolutionError, _classify_evolution_error
from app.scheduler import SUSPICION_BUMP, SUSPICION_LIMIT, SuspicionTracker


def test_classify_shadowban_strings():
    cases = [
        (200, {"message": "The message was not sent due to a likely shadow ban"}, "shadowban"),
        (200, {"error": "Whatsapp rejected sending this message"}, "shadowban"),
        (200, {"message": "shadowban detected"}, "shadowban"),
    ]
    for status, payload, expected in cases:
        got = _classify_evolution_error(status, payload)
        assert got == expected, (status, payload, got)
    print("classify_shadowban.ok")


def test_classify_authorization():
    assert _classify_evolution_error(401, {"message": "unauthorized"}) == "not_authorized"
    assert _classify_evolution_error(403, {"message": "forbidden"}) == "not_authorized"
    assert _classify_evolution_error(200, {"message": "Account blocked"}) == "not_authorized"
    print("classify_auth.ok")


def test_classify_rate_limit():
    assert _classify_evolution_error(429, {}) == "rate_limit"
    assert _classify_evolution_error(200, {"message": "Too many requests"}) == "rate_limit"
    assert _classify_evolution_error(200, {"message": "rate limit hit"}) == "rate_limit"
    print("classify_rate_limit.ok")


def test_classify_connection():
    assert _classify_evolution_error(500, {"message": "connection closed"}) == "connection"
    assert _classify_evolution_error(500, {"raw": "socket hang up"}) == "connection"
    print("classify_connection.ok")


def test_classify_generic():
    assert _classify_evolution_error(500, {"message": "internal error"}) == "generic"
    assert _classify_evolution_error(404, {}) == "generic"
    print("classify_generic.ok")


def test_evolution_error_carries_category():
    err = EvolutionError("some shadowban thing", category="shadowban")
    assert err.category == "shadowban", err.category
    default_err = EvolutionError("no category")
    assert default_err.category == "generic", default_err.category
    print("error_category_attr.ok")


def test_tracker_bump_and_decay():
    tracker = SuspicionTracker()
    inst = "vendor_42"
    assert tracker.get(inst) == 0
    assert tracker.bump(inst, "generic") == 1
    assert tracker.bump(inst, "shadowban") == 1 + SUSPICION_BUMP["shadowban"]
    assert tracker.decay(inst) == 1 + SUSPICION_BUMP["shadowban"] - 1
    for _ in range(20):
        tracker.decay(inst)
    assert tracker.get(inst) == 0
    print("tracker_bump_decay.ok")


def test_tracker_reset():
    tracker = SuspicionTracker()
    tracker.bump("vendor_1", "shadowban")
    tracker.bump("vendor_2", "generic")
    tracker.reset("vendor_1")
    assert tracker.get("vendor_1") == 0
    assert tracker.get("vendor_2") == 1
    print("tracker_reset.ok")


def test_tracker_threshold_reachable():
    tracker = SuspicionTracker()
    inst = "vendor_x"
    tracker.bump(inst, "shadowban")
    score = tracker.bump(inst, "shadowban")
    assert score >= SUSPICION_LIMIT, score
    print(f"tracker_two_shadowban_triggers.ok score={score} limit={SUSPICION_LIMIT}")


def test_tracker_generic_needs_sustained_failures():
    tracker = SuspicionTracker()
    inst = "vendor_y"
    score = 0
    for _ in range(3):
        score = tracker.bump(inst, "generic")
    assert score < SUSPICION_LIMIT, score
    while score < SUSPICION_LIMIT:
        score = tracker.bump(inst, "generic")
    assert score >= SUSPICION_LIMIT, score
    print(f"tracker_generic_sustained_failures.ok score={score}")


def test_tracker_decay_after_recovery():
    tracker = SuspicionTracker()
    inst = "vendor_z"
    tracker.bump(inst, "generic")
    tracker.bump(inst, "generic")
    tracker.decay(inst)
    tracker.decay(inst)
    assert tracker.get(inst) == 0
    print("tracker_recovery.ok")


def main():
    test_classify_shadowban_strings()
    test_classify_authorization()
    test_classify_rate_limit()
    test_classify_connection()
    test_classify_generic()
    test_evolution_error_carries_category()
    test_tracker_bump_and_decay()
    test_tracker_reset()
    test_tracker_threshold_reachable()
    test_tracker_generic_needs_sustained_failures()
    test_tracker_decay_after_recovery()
    print("smoke_shadowban.ok")


if __name__ == "__main__":
    main()
