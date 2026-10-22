# SRA-RiskGate payment check (GitHub Action)

[![self-test](https://github.com/sriram1983007-dev/sra-riskgate-action/actions/workflows/self-test.yml/badge.svg)](https://github.com/sriram1983007-dev/sra-riskgate-action/actions/workflows/self-test.yml)

Fail a CI job unless a stablecoin payment would be **approved**. Use it to regression-test the payments an
AI agent produces: if a change to your agent makes it pay a new payee, a blocked address or a memo that
vouches for itself, the build goes red before it ships.

| Mode | What runs | Time |
|---|---|---|
| `rules` (default) | [`sra-riskgate`](https://github.com/sriram1983007-dev/sra-riskgate) checks: OFAC SDN address match, your blocklist, new-payee hold, address and amount validation, per-payment ceiling, USDC depeg, self-asserted claims in the payment text | seconds |
| `model` | A full review of a payment fixture by [SRA-RiskGate-4B](https://huggingface.co/sriram1983007/SRA-RiskGate-4B), run on the runner's CPU with llama.cpp and the exact prompt the model was trained on | a few minutes; the 2.5 GB GGUF is cached between runs |

Every error (bad input, missing file, model failure, off-schema output) is a `hold`, so it fails the job.

## Recommended setup

Run the fast rule checks on every pull request, and the full model review of your payment fixtures
nightly and before a release. Copy this to `.github/workflows/payment-checks.yml`:

```yaml
name: payment-checks

on:
  pull_request:
  schedule:
    - cron: "17 6 * * *"   # nightly
  workflow_dispatch:

jobs:
  rules:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Produce the transaction your agent would send, then check it
      - id: tx
        run: echo "calldata=$(python scripts/agent_dry_run.py)" >> "$GITHUB_OUTPUT"
      - uses: sriram1983007-dev/sra-riskgate-action@v1
        with:
          chain-id: 8453
          sender: ${{ vars.TREASURY_WALLET }}
          token-address: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # USDC on Base
          payload: ${{ steps.tx.outputs.calldata }}
          known-recipients-file: config/verified-payees.txt

  model:
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        fixture: [tests/payments/invoice-known-vendor.json, tests/payments/refund-to-new-address.json]
    steps:
      - uses: actions/checkout@v4
      - uses: sriram1983007-dev/sra-riskgate-action@v1
        with:
          mode: model
          fixture: ${{ matrix.fixture }}
```

Replace `scripts/agent_dry_run.py` with whatever produces your agent's transaction, and write fixtures in
the format of [`examples/approve-erc20.json`](examples/approve-erc20.json). A fixture you expect to be held
can use `allow: hold`.

## Rules mode

```yaml
- uses: sriram1983007-dev/sra-riskgate-action@v1
  with:
    chain-id: 8453
    sender: ${{ vars.TREASURY_WALLET }}
    recipient: ${{ steps.agent.outputs.payee }}
    amount-usdc: ${{ steps.agent.outputs.amount }}
    untrusted-text: ${{ steps.agent.outputs.invoice_memo }}
    known-recipients-file: config/verified-payees.txt   # a payee not listed here is held
    blocked-addresses: "0xbad0000000000000000000000000000000000001"
```

Known payees come only from your repository, never from the agent's output, so a manipulated agent cannot
mark an attacker's address as known.

### Check the exact calldata your agent produced

Pass raw ERC-20 `transfer` calldata as `payload` and the Action decodes the recipient and amount itself:

```yaml
- name: Validate agent calldata risk
  uses: sriram1983007-dev/sra-riskgate-action@v1
  with:
    chain-id: 8453
    sender: ${{ vars.TREASURY_WALLET }}
    token-address: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # USDC on Base
    payload: ${{ steps.tx.outputs.calldata }}                       # 0xa9059cbb...
    known-recipients-file: config/verified-payees.txt
```

Anything else (another function, a truncated or malformed payload, a token that isn't USDC on that chain)
is held. If you also pass `recipient` or `amount-usdc` and they disagree with the calldata, the payment is
held with `PAYLOAD_MISMATCH`.

## Model mode

Write payment fixtures in the model's training format (`now_unix`, `policy`, `payload`, `tool_results`),
for example [`examples/approve-erc20.json`](examples/approve-erc20.json), a case from the public
[benchmark](https://huggingface.co/datasets/sriram1983007/sra-stablecoin-risk-bench):

```yaml
- uses: sriram1983007-dev/sra-riskgate-action@v1
  with:
    mode: model
    fixture: tests/payments/refund-to-new-address.json
```

The self-asserted-claims check runs first, so a fixture whose payload vouches for itself is held without
asking the model.

## Inputs and outputs

| Input | Default | Meaning |
|---|---|---|
| `mode` | `rules` | `rules` or `model` |
| `allow` | `approve` | Highest decision that passes: `approve`, `hold` or `reject` |
| `chain-id`, `sender`, `recipient`, `amount-usdc` | | rules: the payment (amount in whole USDC) |
| `peg-deviation-pct` | empty | rules: USDC's deviation from $1.00 in percent; empty means unknown, and the depeg check is skipped and reported |
| `max-amount` | `1000` | rules: per-payment ceiling in USDC |
| `payload` | | rules: raw ERC-20 `transfer` calldata; recipient and amount are decoded from it |
| `token-address` | | rules: the token contract the calldata goes to; if set, it must be USDC on `chain-id` |
| `untrusted-text` | | rules: the payment's memo or request text as received |
| `known-recipients`, `known-recipients-file` | | rules: verified payees; any other recipient is held |
| `blocked-addresses` | | rules: always rejected |
| `fixture` | | model: path to the fixture JSON |
| `model` | | model: shortcut for `model-repo`; `sriram1983007/SRA-RiskGate-4B` uses its GGUF build |
| `model-repo`, `model-file` | `sriram1983007/SRA-RiskGate-4B-GGUF`, `SRA-RiskGate-4B-Q4_K_M.gguf` | model: which GGUF to run |
| `cache-model` | `true` | model: cache the GGUF with `actions/cache` |

Outputs: `decision`, `flags` (JSON list) and `result` (the full verdict as JSON). A summary is added to the
job page.

## Limits

These checks are risk signals, not a compliance determination. The OFAC match is an exact comparison
against a dated snapshot. The model's published results are on synthetic data, measured by the author; see
the [model card](https://huggingface.co/sriram1983007/SRA-RiskGate-4B) for the numbers and caveats.

## License

Apache-2.0
