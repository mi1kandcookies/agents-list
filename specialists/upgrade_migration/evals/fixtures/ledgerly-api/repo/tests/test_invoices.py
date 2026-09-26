from app.invoices import is_record, stamp


def test_stamp_adds_timestamp():
    assert "issued_at" in stamp({"id": "INV-1"})


def test_stamp_keeps_fields():
    assert stamp({"id": "INV-2", "total": 10})["total"] == 10


def test_is_record():
    assert is_record({"a": 1}) and not is_record([1])
