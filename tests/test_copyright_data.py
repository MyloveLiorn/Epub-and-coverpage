"""The bundled state copyright table."""

from govbooks import copyright_policy
from govbooks.states import US_STATES


def test_every_state_has_a_sourced_policy():
    policies = copyright_policy.state_policies()
    assert set(policies) == set(US_STATES)
    for policy in policies.values():
        assert policy.state == US_STATES[policy.abbr]
        assert policy.status in ("public_domain", "claims_copyright", "mixed", "unclear")
        assert policy.confidence in ("high", "medium", "low")
        assert policy.summary and policy.sources


def test_lookup_by_name_or_code():
    assert copyright_policy.state_policy("ca") == copyright_policy.state_policy("California")
    assert copyright_policy.state_policy("Narnia") is None
