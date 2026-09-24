import pytest

from tier2_agent.escalation import ESCALATION_REASON_LOOKUP, EscalationRecord
from tier2_agent.exceptions import InvalidEscalationError


def test_from_dict_missing_required_field_raises(escalation_dict_factory):
    data = escalation_dict_factory()
    del data["dependency_status"]
    with pytest.raises(InvalidEscalationError):
        EscalationRecord.from_dict(data)


def test_from_dict_keeps_raw_untouched(escalation_dict_factory):
    data = escalation_dict_factory(count=99)
    rec = EscalationRecord.from_dict(data)
    assert rec.raw == data
    assert rec.raw is not data  # copy, không alias để tránh Tier2 vô tình sửa ngược


@pytest.mark.parametrize(
    "status,config_finding,expected",
    [
        ("OBSERVED_WEAK", None, True),
        ("UNKNOWN", None, True),
        ("NOT_OBSERVED", "FOUND_UNRESOLVED", True),
        ("NOT_OBSERVED", "NOT_FOUND", True),
        ("NOT_OBSERVED", "FOUND_STRUCTURAL", False),
        ("NOT_OBSERVED", "FOUND_EMBEDDED", False),
        ("NOT_OBSERVED", None, False),
        ("OBSERVED_STRONG", None, False),
        ("NEEDS_MANUAL_SELECTOR", None, False),
    ],
)
def test_should_escalate_matrix(escalation_dict_factory, status, config_finding, expected):
    rec = EscalationRecord.from_dict(
        escalation_dict_factory(dependency_status=status, config_finding=config_finding)
    )
    assert rec.should_escalate() is expected


def test_escalation_reason_lookup_covers_all_escalating_statuses(escalation_dict_factory):
    for status in ("NOT_OBSERVED", "OBSERVED_WEAK", "UNKNOWN"):
        rec = EscalationRecord.from_dict(escalation_dict_factory(dependency_status=status))
        assert rec.escalation_reason == ESCALATION_REASON_LOOKUP[status]


def test_identity_key_stable_and_distinguishes_edges(escalation_dict_factory):
    a = EscalationRecord.from_dict(escalation_dict_factory())
    b = EscalationRecord.from_dict(escalation_dict_factory())
    assert a.identity_key == b.identity_key

    c = EscalationRecord.from_dict(escalation_dict_factory(port=9999))
    assert a.identity_key != c.identity_key


def test_canonical_services_returns_both_ends(escalation_dict_factory):
    rec = EscalationRecord.from_dict(escalation_dict_factory())
    assert rec.canonical_services == frozenset({"frontend", "payment"})
