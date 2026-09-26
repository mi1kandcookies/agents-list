"""The MCP package's vendored agent_ids must behave exactly like the app's."""
import ast
import inspect
import random

import agentslist_mcp.agent_ids as vendored
import app.common.agent_ids as original


def _outcome(mod, fn, arg):
    try:
        return ("ok", getattr(mod, fn)(arg))
    except mod.AgentIdError as exc:
        return ("AgentIdError", exc.code, exc.suggestion, str(exc))
    except ValueError as exc:
        return ("ValueError", str(exc))


def _mutations(rng, agent_id):
    """Valid id plus typical typos: substitution, transposition, case, aliases."""
    body = list(agent_id[4:8] + agent_id[9:13] + agent_id[14])
    sub = body[:]
    sub[rng.randrange(9)] = rng.choice(original.CHECK_ALPHABET)
    swap = body[:]
    i = rng.randrange(8)
    swap[i], swap[i + 1] = swap[i + 1], swap[i]
    fmt = lambda s: f"AGT-{''.join(s[:4])}-{''.join(s[4:8])}-{s[8]}"
    return [
        agent_id,
        fmt(sub),
        fmt(swap),
        agent_id.lower(),
        agent_id.replace("-", "").replace("1", "l").replace("0", "O"),
        agent_id[:-1],
    ]


def test_vendored_logic_is_identical_source():
    def body(mod):
        tree = ast.parse(inspect.getsource(mod))
        tree.body = tree.body[1:]  # drop the module docstring
        return ast.dump(tree)

    assert body(vendored) == body(original)


def test_vendored_matches_app_module_for_1000_ids():
    rng = random.Random(20260926)
    assert vendored.CHECK_ALPHABET == original.CHECK_ALPHABET
    for _ in range(1000):
        n = rng.randrange(original.MAX_PAYLOAD + 1)
        assert vendored.encode(n) == original.encode(n)
        db_id = rng.randrange(1_000_000)
        assert vendored.from_db_id(db_id) == original.from_db_id(db_id)
        for candidate in _mutations(rng, original.encode(n)):
            for fn in ("decode", "normalize", "is_valid"):
                assert _outcome(vendored, fn, candidate) == _outcome(original, fn, candidate), (fn, candidate)
