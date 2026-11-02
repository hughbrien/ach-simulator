---
name: add-ach-scenario
description: Add a new return reason, NOC change code, receiver account outcome, or example ACH file to the simulator, keeping examples, tests, demo and README consistent. Use when asked to add/support an R-code or C-code, a new RDFI behaviour, or a new example scenario.
---

# Add an ACH scenario

Outcomes in this repo are data-driven: an entry's fate is decided by `rdfi.decide()` against an account dict, and the example files 05/06 are *derived* by running that engine over files 01–03. So a change in one place ripples into generated files and test expectations. Work through the steps that apply, in order.

## 1. Reference tables — `ach_sim/nacha.py`

- New return reason → add to `RETURN_REASONS`. New NOC → add to `CHANGE_CODES`. `describe()` and `rdfi.describe_outcome()` read these, so nothing else is needed for display.
- New SEC code → `SEC_CODES`, and check `validate()` for SEC-specific rules (e.g. WEB/TEL payment type `R`/`S`) and `rdfi.CONSUMER_SEC` (drives R10 vs R29).
- Never change field positions in the layout lists without re-running the round-trip test; `parse(text).to_text()` must reproduce every valid example byte for byte.

## 2. Decision logic — `ach_sim/rdfi.py` `decide()`

- Returns are checked in a fixed order (R03 → R02 → R16 → disputes R10/R29 → R01) and the first match wins. Place a new check where its precedence makes sense and drive it from an account field (e.g. `status`, a new key) rather than hard-coding account numbers.
- Return outcomes replace posting; NOCs are appended after posting. Keep that shape: `("post"|"return"|"noc", code, data)`.
- `decide()` is shared by the RDFI services (`rdfi_service.py`, one per auto bank), the client's `receive` (First Example Bank's ledger in `examples/first_example_bank_ledger.json`) and `generate_examples.py`. A new account key must be optional (`acct.get(...)`) so existing ledgers keep working.

## 3. Receiver accounts — `ach_sim/participants.py`

- Add a dedicated account in `RDFI_ACCOUNTS` that produces the new outcome, with a trailing comment `# -> Rxx`. Do not repurpose an existing account: each one backs a specific outcome that examples and tests assert on.
- For inbound scenarios (First Example Bank as RDFI), the account goes in `examples/first_example_bank_ledger.json` instead.

## 4. Example files — `generate_examples.py`

- Add an entry addressed to the new account in the relevant builder function, or add a new builder and register it in `main()`'s `files` dict. Use a fresh file number and a unique `file_id_modifier`/creation minute so the operator's duplicate check (origin + date + time + modifier) does not collide.
- If the scenario should appear in the derived return/NOC files, it must be in files 01–03 (the loop in `main()`), and the 05 filename and `assert len(noc_items) == 2` may need updating. Renaming 05/06 means deleting the old file from `examples/` by hand.
- Intentionally invalid files must have `INVALID` in the name so tests skip them.
- Regenerate: `python3 generate_examples.py`. Generation is deterministic; any diff in untouched files means an unintended ripple.
- Never hand-edit `examples/*.ach`.

## 5. Tests — `tests/test_ach_sim.py`

Update the assertions that pin outcomes, then add one for the new case:
- `ExampleFileTests.test_returns_reference_real_traces` (set of codes in file 05)
- `RdfiTests.test_decisions` (file 03 outcomes, balance after posting)
- `OperatorEndToEndTests.test_submit_duplicate_and_returns` (codes returned for file 03)

Run `python3 -m unittest discover -s tests -v`.

## 6. Demo and docs

- If the file should be played in the demo, add it to `run_demo.py`, run `python3 run_demo.py > demo_output.txt` (this wipes `sim_data/`), and check the output reads sensibly.
- Update README.md: the example files table, the "What the operator does" list (if decision rules changed), and the test count in Quick start.
