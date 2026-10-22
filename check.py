"""SRA-RiskGate GitHub Action: check a stablecoin payment in CI and fail the job unless it would be approved.

Two modes, chosen with the `mode` input:

  rules   Deterministic checks from the sra-riskgate SDK: OFAC SDN address match, your blocklist, new-payee
          hold, address and amount validation, per-payment ceiling, depeg, and self-asserted claims in the
          payment's text. Seconds, no model.
  model   A full review of a payment fixture by SRA-RiskGate-4B, run on the runner's CPU with llama.cpp,
          using the exact prompt the model was trained on. The self-asserted-claims check runs first.

Every error (bad input, missing file, model failure, off-schema output) becomes `hold`, never `approve`.
Inputs arrive as INPUT_* environment variables set by action.yml.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

DECISION_RANK = {"approve": 0, "hold": 1, "reject": 2}


# ------------------------------------------------------------------ helpers
def env(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name.upper().replace('-', '_')}", default).strip()


def address_list(text: str) -> Optional[list[str]]:
    """Comma, space or newline separated addresses; empty input means not configured (None)."""
    if not text:
        return None
    return [a for a in text.replace(",", " ").split() if a]


def hold(flag: str, explanation: str) -> dict[str, Any]:
    return {"decision": "hold", "flags": [flag], "explanation": explanation[:500],
            "required_actions": ["manual_review"], "source": "fail_closed"}


# ------------------------------------------------------------------ rules mode
def run_rules() -> dict[str, Any]:
    from sra_riskgate import check_payment

    try:
        chain_id = int(env("chain-id"))
        amount = float(env("amount-usdc"))
    except ValueError:
        return hold("invalid_inputs", "chain-id must be an integer and amount-usdc a number.")
    peg_text = env("peg-deviation-pct")
    try:
        peg = float(peg_text) if peg_text else None
    except ValueError:
        return hold("invalid_inputs", "peg-deviation-pct must be a number, or empty if unknown.")
    known = address_list(env("known-recipients"))
    known_file = env("known-recipients-file")
    if known_file:
        try:
            lines = Path(known_file).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            return hold("invalid_inputs", f"Could not read known-recipients-file: {e}")
        known = (known or []) + [ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()]
    result = check_payment(
        chain_id=chain_id, sender=env("sender"), recipient=env("recipient"), amount=amount,
        peg_deviation_pct=peg, untrusted_request=env("untrusted-text") or None,
        max_amount=float(env("max-amount", "1000") or 1000),
        known_recipients=known, blocked_addresses=address_list(env("blocked-addresses")),
    )
    return {**result.model_dump(mode="json"), "source": "rules"}


# ------------------------------------------------------------------ model mode
def load_fixture(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in ("now_unix", "policy", "payload", "tool_results") if k not in data]
    if missing:
        raise ValueError(f"fixture is missing: {', '.join(missing)}")
    return data


def default_model_loader(model_path: str) -> Callable[[list[dict]], str]:
    from llama_cpp import Llama

    llm = Llama(model_path=model_path, n_ctx=8192, n_threads=os.cpu_count() or 2, verbose=False)

    def complete(messages: list[dict]) -> str:
        out = llm.create_chat_completion(messages=messages, temperature=0.0, max_tokens=512)
        return out["choices"][0]["message"]["content"] or ""
    return complete


def run_model(model_path: str, loader: Callable[[str], Callable[[list[dict]], str]] = default_model_loader
              ) -> dict[str, Any]:
    from sra_riskgate import scan_payload_claims
    from sra_riskgate.payload_claims import FLAG as CLAIMS_FLAG
    from sra_riskgate_mcp.model import PAYMENT_SYSTEM_PROMPT, build_user_message, parse_verdict

    try:
        fx = load_fixture(env("fixture"))
    except Exception as e:
        return hold("invalid_fixture", f"Could not read the fixture: {e}")

    claims = scan_payload_claims(fx["payload"])
    if claims:
        return {"decision": "hold", "risk_level": "high", "flags": [CLAIMS_FLAG],
                "explanation": f"The untrusted payload claims its own verification ({claims[0].match!r}).",
                "required_actions": ["manual_review"], "source": "rules"}
    try:
        complete = loader(model_path)
        t0 = time.time()
        text = complete([
            {"role": "system", "content": PAYMENT_SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(int(fx["now_unix"]), fx["policy"], fx["payload"],
                                                           fx["tool_results"])},
        ])
        seconds = round(time.time() - t0, 1)
    except Exception as e:
        return hold("model_unavailable", f"The model could not run: {type(e).__name__}: {e}")
    verdict = parse_verdict(text)
    if verdict is None:
        return {**hold("malformed_model_output", "The model's answer was not a valid verdict."),
                "raw_output": text[:500]}
    return {**verdict, "source": "model", "seconds": seconds}


# ------------------------------------------------------------------ reporting
def report(result: dict[str, Any]) -> int:
    allow = env("allow", "approve") or "approve"
    if allow not in DECISION_RANK:
        result = hold("invalid_inputs", f"allow must be approve, hold or reject; got {allow!r}.")
        allow = "approve"
    decision = result.get("decision", "hold")
    passed = DECISION_RANK.get(decision, 2) <= DECISION_RANK[allow]

    print(json.dumps(result, indent=2))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"decision={decision}\n")
            f.write(f"flags={json.dumps(result.get('flags', []))}\n")
            f.write(f"result={json.dumps(result)}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        mark = "✅" if passed else "❌"
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### {mark} SRA-RiskGate: `{decision}`\n\n")
            f.write(f"- Flags: {', '.join(f'`{x}`' for x in result.get('flags', [])) or 'none'}\n")
            f.write(f"- Source: {result.get('source', '?')}\n\n{result.get('explanation', '')}\n")
    if not passed:
        print(f"::error title=SRA-RiskGate::Payment decision is '{decision}' "
              f"(allowed up to '{allow}'): {result.get('explanation', '')[:300]}")
    return 0 if passed else 1


def main() -> int:
    mode = env("mode", "rules")
    try:
        if mode == "rules":
            result = run_rules()
        elif mode == "model":
            result = run_model(env("model-path"))
        else:
            result = hold("invalid_inputs", f"mode must be 'rules' or 'model'; got {mode!r}.")
    except Exception as e:  # fail closed: no error path may pass the check
        result = hold("check_failed", f"The check failed ({type(e).__name__}: {e}).")
    return report(result)


if __name__ == "__main__":
    sys.exit(main())
