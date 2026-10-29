import json

import pytest
from pydantic import ValidationError

from spc_agent.schemas import Actor, ExecuteRequest, ReconcileRequest, canonical, digest


@pytest.mark.parametrize("value", [1, 1.0, 0, 0.0, False, None, "true", "1", [], {}])
@pytest.mark.parametrize("from_json", [False, True])
@pytest.mark.parametrize("kind", ["execute", "reconcile"])
def test_explicit_confirmations_reject_every_non_true_value(value, from_json, kind):
    request = {"action_id": "confirm-test", "note": "Original synthetic human attestation"}
    if kind == "execute":
        model = ExecuteRequest
        request.update(proposal_hash="a" * 64, observed_at_ms=123, confirmed=value)
    else:
        model = ReconcileRequest
        request.update(call_id="call-test", no_result=value)
    with pytest.raises(ValidationError):
        if from_json:
            model.model_validate_json(json.dumps(request))
        else:
            model.model_validate(request)


@pytest.mark.parametrize("model,fields", [
    (ExecuteRequest, {"proposal_hash": "a" * 64, "observed_at_ms": 123, "confirmed": True}),
    (ReconcileRequest, {"call_id": "call-test", "no_result": True}),
])
def test_explicit_boolean_true_is_accepted(model, fields):
    value = {"action_id": "confirm-test", "note": "Original synthetic human attestation", **fields}
    assert model.model_validate_json(json.dumps(value)) == model.model_validate(value)


@pytest.mark.parametrize("tokens", [True, 1.0, -1, 100001, "0"])
def test_reconciliation_usage_is_strict(tokens):
    with pytest.raises(ValidationError):
        ReconcileRequest.model_validate({
            "action_id": "reconcile-test", "call_id": "call-test", "no_result": True,
            "tokens": tokens, "note": "Original synthetic human attestation",
        })


@pytest.mark.parametrize("tokens", [None, 0, 1, 100000])
def test_unknown_and_zero_usage_remain_distinct(tokens):
    value = ReconcileRequest.model_validate({
        "action_id": "reconcile-test", "call_id": "call-test", "no_result": True,
        "tokens": tokens, "note": "Original synthetic human attestation",
    })
    assert value.tokens is tokens


def test_actor_rejects_duplicate_plants_and_extra_fields():
    with pytest.raises(ValidationError):
        Actor(actor_id="actor", role="admin", plants=["plant", "plant"], acl_version=1)
    with pytest.raises(ValidationError):
        Actor(actor_id="actor", role="admin", plants=["plant"], acl_version=1, token="not-a-token")


def test_canonical_unicode_and_order_are_stable():
    left, right = {"characteristic": "孔径", "unit": "mm"}, {"unit": "mm", "characteristic": "孔径"}
    assert canonical(left) == '{"characteristic":"孔径","unit":"mm"}'
    assert digest(left) == digest(right)
    with pytest.raises(ValueError):
        canonical({"value": float("nan")})
