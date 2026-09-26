"""Public agent ids: format, check symbol, normalization, db-id permutation."""
import random

import pytest

from app.common.agent_ids import (
    CHECK_ALPHABET, MAX_PAYLOAD, AgentIdError, decode, encode, from_db_id, is_valid,
    normalize, to_db_id,
)


def _symbols(agent_id: str) -> str:
    return agent_id[4:8] + agent_id[9:13] + agent_id[14]


def _format(symbols: str) -> str:
    return f"AGT-{symbols[:4]}-{symbols[4:8]}-{symbols[8]}"


def test_encode_shape_and_round_trip():
    for n in (0, 1, 36, 37, MAX_PAYLOAD):
        s = encode(n)
        assert len(s) == 15 and s.startswith("AGT-") and s[8] == "-" and s[13] == "-"
        assert decode(s) == n
    for bad in (-1, MAX_PAYLOAD + 1, True, 1.0):
        with pytest.raises(ValueError):
            encode(bad)


def test_every_single_substitution_and_adjacent_transposition_is_rejected():
    rng = random.Random(1234)
    for _ in range(1000):
        good = _symbols(encode(rng.randrange(MAX_PAYLOAD + 1)))
        for pos in range(9):
            for ch in CHECK_ALPHABET:
                if ch == good[pos]:
                    continue
                typo = good[:pos] + ch + good[pos + 1:]
                assert not is_valid(_format(typo)), (good, typo)
        for pos in range(8):
            if good[pos] == good[pos + 1]:
                continue
            typo = good[:pos] + good[pos + 1] + good[pos] + good[pos + 2:]
            assert not is_valid(_format(typo)), (good, typo)


def test_decode_normalizes_presentation():
    n = 0x1234567890
    s = encode(n)
    variants = [s.lower(), s.replace("-", ""), s[4:], s[4:].replace("-", ""), f"  {s}  "]
    for v in variants:
        assert decode(v) == n, v
    # I/L read as 1 and O as 0
    one = encode(1 << 35)  # leading symbol "1"
    assert one[4] == "1"
    for alias in ("I", "i", "L", "l"):
        assert decode(one[:4] + alias + one[5:]) == 1 << 35
    zero = encode(0)
    assert decode(zero.replace("0", "O")) == 0
    assert normalize(s.lower().replace("-", "")) == s


@pytest.mark.parametrize("bad,code", [
    ("", "INVALID_FORMAT"), ("AGT-1234", "INVALID_FORMAT"), (None, "INVALID_FORMAT"),
    ("AGT-12U4-5678-0", "INVALID_CHAR"), ("AGT-1234-5678-!", "INVALID_CHAR"),
])
def test_malformed_ids(bad, code):
    with pytest.raises(AgentIdError) as exc:
        decode(bad)
    assert exc.value.code == code


def test_bad_check_suggests_unambiguous_transposition_fix():
    rng = random.Random(7)
    found = 0
    for _ in range(2000):
        good = encode(rng.randrange(MAX_PAYLOAD + 1))
        sym = _symbols(good)
        pos = rng.randrange(8)
        if sym[pos] == sym[pos + 1]:
            continue
        typo = _format(sym[:pos] + sym[pos + 1] + sym[pos] + sym[pos + 2:])
        with pytest.raises(AgentIdError) as exc:
            decode(typo)
        if pos == 7 and sym[8] in "*~$=U":  # check-only symbol moved into the payload
            assert exc.value.code == "INVALID_CHAR"
            continue
        assert exc.value.code == "BAD_CHECK"
        # A suggestion, when given, is always the original id.
        if exc.value.suggestion is not None:
            assert exc.value.suggestion == good
            found += 1
    assert found > 1000


def test_from_db_id_is_a_stable_bijection():
    ids = [from_db_id(i) for i in range(1, 5001)]
    assert len(set(ids)) == len(ids)
    assert all(is_valid(s) for s in ids)
    assert from_db_id(1) == from_db_id(1)
    for i in (0, 1, 2, 999, 123_456, MAX_PAYLOAD):
        assert to_db_id(from_db_id(i)) == i
    # sequential ids do not produce sequential public ids
    assert abs(decode(ids[1]) - decode(ids[0])) > 1
    with pytest.raises(ValueError):
        from_db_id(-1)
