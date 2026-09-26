import pytest

from chain.escrow import EscrowError, SimulatedEscrowService


def test_simulated_escrow_requires_authority_and_is_idempotence_safe():
    service = SimulatedEscrowService()
    with pytest.raises(EscrowError, match="approval"):
        service.fund(engagement_id="eng-1", milestone_id="m-1", amount_atomic=1,
                     approval_id="", screening_id="screen-1")
    receipt = service.fund(engagement_id="eng-1", milestone_id="m-1", amount_atomic=100,
                           approval_id="apr-1", screening_id="screen-1")
    assert receipt.to_dict()["status"] == "confirmed"
    with pytest.raises(EscrowError, match="already funded"):
        service.fund(engagement_id="eng-1", milestone_id="m-1", amount_atomic=100,
                     approval_id="apr-2", screening_id="screen-2")
    release = service.release(engagement_id="eng-1", milestone_id="m-1", amount_atomic=100,
                             approval_id="apr-3", screening_id="screen-3", payee="0xabc")
    assert release.action == "release"
    with pytest.raises(EscrowError, match="already released"):
        service.release(engagement_id="eng-1", milestone_id="m-1", amount_atomic=100,
                        approval_id="apr-4", screening_id="screen-4", payee="0xabc")
