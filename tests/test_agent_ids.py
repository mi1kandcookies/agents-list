from app.common.agent_ids import generate_agent_id, is_valid_agent_id, require_agent_id


def test_generated_agent_ids_have_valid_check_digits():
    values = {generate_agent_id() for _ in range(1000)}
    assert len(values) > 990
    assert all(is_valid_agent_id(value) for value in values)


def test_agent_id_check_digit_rejects_mutation():
    value = generate_agent_id()
    parts = value.split("-")
    replacement = "0" if parts[-1] != "0" else "1"
    assert not is_valid_agent_id("-".join(parts[:-1] + [replacement]))
    assert require_agent_id(value) == value
