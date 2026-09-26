from app.hiring.deliverables import build_api_test_plan


def test_api_plan_is_bound_to_source_and_never_executes_it():
    spec = """# Orders\nGET /v1/orders\nPOST /v1/orders\n"""
    plan = build_api_test_plan(spec, specialist="Quartz QA Test Planner", agent_id="AGT-TEST")
    assert plan["type"] == "api_test_plan"
    assert plan["source"]["endpoint_count"] == 2
    assert plan["test_case_count"] == 6
    assert plan["source"]["sha256"].startswith("0x")
    assert "execute" in plan["safety_note"]


def test_free_text_task_still_returns_a_reviewable_plan():
    plan = build_api_test_plan("Describe the acceptance criteria", specialist="QA", agent_id="AGT-X")
    assert plan["source"]["endpoint_count"] == 0
    assert plan["test_cases"][0]["method"] is None
