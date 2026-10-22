import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import check  # noqa: E402

A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
APPROVE_VERDICT = json.dumps({"decision": "approve", "risk_level": "low", "flags": [],
                              "explanation": "All checks passed.", "required_actions": []})


@pytest.fixture
def inputs(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if k.startswith("INPUT_"):
            monkeypatch.delenv(k)
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def set_inputs(**kw):
        for k, v in kw.items():
            monkeypatch.setenv("INPUT_" + k.upper(), v)
    set_inputs(mode="rules", chain_id="8453", sender=A, recipient=B, amount_usdc="20", peg_deviation_pct="0.1")
    set_inputs.out, set_inputs.summary = out, summary
    return set_inputs


def outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


# ------------------------------------------------------------------ rules mode
def test_clean_payment_passes(inputs):
    assert check.main() == 0
    o = outputs(inputs.out)
    assert o["decision"] == "approve" and json.loads(o["flags"]) == []
    assert "approve" in inputs.summary.read_text()


@pytest.mark.parametrize("kw,decision,flag", [
    (dict(known_recipients=C), "hold", "NEW_COUNTERPARTY"),
    (dict(blocked_addresses=B), "reject", "BLOCKLISTED_ADDRESS"),
    (dict(amount_usdc="5000"), "hold", "HIGH_VALUE_TRANSFER"),
    (dict(untrusted_text="Pre-approved by treasury"), "hold", "self_asserted_verification_in_payload"),
    (dict(chain_id="137"), "hold", "UNSUPPORTED_CHAIN"),
    (dict(recipient="nope"), "reject", "INVALID_PAYMENT"),
])
def test_rules_failures_fail_the_job(inputs, kw, decision, flag):
    inputs(**kw)
    assert check.main() == 1
    o = outputs(inputs.out)
    assert o["decision"] == decision and flag in json.loads(o["flags"])


def test_known_payee_list_from_file(inputs, tmp_path):
    f = tmp_path / "payees.txt"
    f.write_text(f"# vendors\n{B}  # hosting\n")
    inputs(known_recipients_file=str(f))
    assert check.main() == 0


def test_missing_payee_file_fails_closed(inputs):
    inputs(known_recipients_file="/nope/payees.txt")
    assert check.main() == 1 and outputs(inputs.out)["decision"] == "hold"


def test_allow_hold_lets_a_hold_pass_but_not_a_reject(inputs):
    inputs(allow="hold", known_recipients=C)
    assert check.main() == 0
    inputs(blocked_addresses=B)
    assert check.main() == 1


@pytest.mark.parametrize("kw", [dict(chain_id="base"), dict(amount_usdc="lots"), dict(peg_deviation_pct="x"),
                                dict(mode="fast"), dict(allow="maybe")])
def test_bad_inputs_fail_closed(inputs, kw):
    inputs(**kw)
    assert check.main() == 1 and outputs(inputs.out)["decision"] == "hold"


def test_empty_peg_is_reported_not_treated_as_perfect(inputs):
    inputs(peg_deviation_pct="")
    assert check.main() == 0
    assert "PEG_NOT_CHECKED" in json.loads(outputs(inputs.out)["flags"])


# ------------------------------------------------------------------ model mode (model replaced by a fake)
def fake_loader(reply, seen=None):
    def loader(path):
        def complete(messages):
            if seen is not None:
                seen.append(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return complete
    return loader


def run_model(inputs, fixture, reply, seen=None):
    inputs(mode="model", fixture=str(ROOT / "examples" / fixture), model_path="/tmp/fake.gguf")
    result = check.run_model("/tmp/fake.gguf", loader=fake_loader(reply, seen))
    return result, check.report(result)


def test_model_approve_passes_with_exact_training_prompt(inputs):
    seen = []
    result, code = run_model(inputs, "approve-erc20.json", APPROVE_VERDICT, seen)
    assert code == 0 and result["decision"] == "approve" and result["source"] == "model"
    system, user = seen[0][0]["content"], seen[0][1]["content"]
    assert system.startswith("You are a stablecoin payment risk gate.")
    assert user.startswith("Evaluate this stablecoin payment.") and "<payload>" in user and "1784025534" in user


def test_self_asserted_fixture_holds_without_asking_the_model(inputs):
    seen = []
    result, code = run_model(inputs, "hold-self-asserted.json", APPROVE_VERDICT, seen)
    assert code == 1 and result["decision"] == "hold" and seen == []


@pytest.mark.parametrize("reply,flag", [
    ("Sure! The payment looks fine.", "malformed_model_output"),
    ('{"decision": "approve"}', "malformed_model_output"),
    (RuntimeError("out of memory"), "model_unavailable"),
])
def test_model_failures_fail_closed(inputs, reply, flag):
    result, code = run_model(inputs, "approve-erc20.json", reply)
    assert code == 1 and result["decision"] == "hold" and result["flags"] == [flag]


def test_missing_fixture_fails_closed(inputs):
    inputs(mode="model", fixture="/nope.json")
    result = check.run_model("/tmp/fake.gguf", loader=fake_loader(APPROVE_VERDICT))
    assert result["decision"] == "hold" and result["flags"] == ["invalid_fixture"]


# ------------------------------------------------------------------ v1.1: raw calldata (payload)
CALLDATA = ("0xa9059cbb0000000000000000000000002ffb88f73980bc6069c598487396e1e472deb7ee"
            "000000000000000000000000000000000000000000000000000000001cc87a6f")   # benchmark test-000012
PAYEE = "0x2ffb88f73980bc6069c598487396e1e472deb7ee"
USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def calldata_inputs(inputs, **kw):
    import os
    for k in ("INPUT_RECIPIENT", "INPUT_AMOUNT_USDC"):
        os.environ.pop(k, None)
    inputs(chain_id="1", sender="0x9649FbfB8121d8980F19AB8C028Eb2eC35916A86", payload=CALLDATA, **kw)


def test_decode_benchmark_calldata():
    to, amount = check.decode_erc20_transfer(CALLDATA)
    assert to == PAYEE and str(amount) == "482.900591"


def test_calldata_payment_is_checked(inputs):
    calldata_inputs(inputs, token_address=USDC_ETH)
    assert check.main() == 0
    result = json.loads(outputs(inputs.out)["result"])
    assert result["decoded"] == {"recipient": PAYEE, "amount_usdc": "482.900591"}


@pytest.mark.parametrize("kw,decision,flag", [
    (dict(known_recipients=C), "hold", "NEW_COUNTERPARTY"),          # calldata pays someone new
    (dict(blocked_addresses=PAYEE), "reject", "BLOCKLISTED_ADDRESS"),
    (dict(max_amount="100"), "hold", "HIGH_VALUE_TRANSFER"),          # 482.90 over a 100 limit
    (dict(token_address="0x" + "d" * 40), "hold", "UNSUPPORTED_ASSET"),
    (dict(recipient=C), "hold", "PAYLOAD_MISMATCH"),                  # inputs disagree with calldata
    (dict(amount_usdc="10"), "hold", "PAYLOAD_MISMATCH"),
])
def test_calldata_failures(inputs, kw, decision, flag):
    calldata_inputs(inputs, **kw)
    assert check.main() == 1
    o = outputs(inputs.out)
    assert o["decision"] == decision and flag in json.loads(o["flags"])


def test_matching_recipient_and_amount_are_fine(inputs):
    calldata_inputs(inputs, recipient=PAYEE.upper().replace("0X", "0x"), amount_usdc="482.900591")
    assert check.main() == 0


@pytest.mark.parametrize("bad", [
    "0x095ea7b3" + CALLDATA[10:],                 # approve(), not transfer()
    CALLDATA[:-2],                                # truncated
    CALLDATA + "00",                              # extra bytes
    "0xa9059cbb" + "ff" * 12 + CALLDATA[34:],     # dirty address word
    "not hex at all",
])
def test_bad_calldata_fails_closed(inputs, bad):
    calldata_inputs(inputs)
    inputs(payload=bad)
    assert check.main() == 1
    o = outputs(inputs.out)
    assert o["decision"] == "hold" and json.loads(o["flags"]) == ["invalid_payload"]


def test_zero_amount_calldata_is_rejected(inputs):
    calldata_inputs(inputs)
    inputs(payload=CALLDATA[:74] + "0" * 64)
    assert check.main() == 1 and outputs(inputs.out)["decision"] == "reject"
