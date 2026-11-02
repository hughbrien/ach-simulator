# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Nacha-format ACH file toolkit plus a simulated ACH Operator (HTTP server) and bank-side client. Python standard library only, no dependencies. All banks, routing numbers, accounts and names are fictitious (routing numbers carry valid ABA check digits). README.md is the detailed reference for record layouts, validator rules, the endpoint API and the simulated network; read it before changing file formats or operator behaviour.

## Commands

```bash
python3 run_demo.py                          # full simulated business day; WIPES and recreates sim_data/
python3 -m unittest discover -s tests -v     # all tests
python3 -m unittest tests.test_ach_sim.ValidatorTests.test_<name> -v   # single test
python3 generate_examples.py                 # rebuild examples/*.ach from the builder API

python3 -m ach_sim.cluster --data-dir sim_data   # every service as its own process + dashboard
python3 -m ach_sim.operator_server --port 8770 --data-dir sim_data   # one service on its own
python3 -m ach_sim.dashboard --data-dir sim_data --port 8780   # browser UI, read-only
python3 -m ach_sim.client [--key KEY] [--sim-time ISO8601] {validate,send,inbox,receive,status} ...
```

The core needs nothing installed. OpenTelemetry is optional: `pip install -r requirements-otel.txt` (installed in `.venv`, which the user's shell `python3` may already resolve to; test the no-OTel path in a fresh venv). Enable export with `ACH_SIM_OTEL=console` or `OTEL_EXPORTER_OTLP_ENDPOINT=...` (README "Observability"). A `.env` with `DT_TENANT_URL` + `DT_PLATFORM_TOKEN` makes every CLI/demo run export to that Dynatrace tenant (`telemetry._dynatrace_env()`; drops `.apps.` from the URL, Bearer auth for `dt0s16` tokens); use `ACH_SIM_OTEL=off` for local-only runs. Never print the token. `--sim-time` / the `X-Sim-Time` header sets the simulated Eastern Time clock, which drives Same Day windows and settlement dates.

## Architecture

- `ach_sim/nacha.py` is the core: record layouts are declared as field lists (`FILE_HEADER`, `BATCH_HEADER`, `ENTRY_DETAIL`, `ADDENDA_LAYOUTS`, ...) and drive both `format_record` and `parse_record`. `AchFile`/`Batch`/`Entry` is the builder model; `to_text()` computes control records (counts, entry hash, totals, block padding) rather than storing them. `parse()` → `AchFile`, `validate()` → `ValidationResult(errors, warnings)`, `describe()` for human-readable output. Tests require `parse(text).to_text() == text` for every valid example, so any layout change must stay byte-exact on round trip.
- `ach_sim/participants.py` defines the network: `PARTICIPANTS` (routing → name, API key, `mode`) and `RDFI_ACCOUNTS`. Mode `mailbox` (First Example Bank) means files wait for the client to pull; mode `auto` means the operator plays that RDFI itself. Each receiver account is configured to produce a specific return/NOC outcome, and the example files and tests depend on those outcomes.
- `ach_sim/rdfi.py`: `decide()` returns post/return/NOC decisions for an entry against an account dict (mutating balances when `apply=True`); `build_response_file()` builds return and COR/NOC files. Shared by the operator (for `auto` RDFIs), the client's `receive` (posting to a local ledger JSON), and `generate_examples.py` (which produces files 05/06 from files 01–03, so returns reference real trace numbers).
- `ach_sim/settlement.py`: Fed business days, holidays, Same Day windows (SDA-1/2/3, NEXT-DAY, FUTURE-DATED).
- The simulated network is distributed: `operator_server` (gateway, port 8770) calls `validation_service` (8771), `settlement_service` (8772) and one `rdfi_service` per `auto` bank (8773+) over HTTP. `cluster.py` launches them as separate processes (`start_processes`, used by `run_demo.py` and `python3 -m ach_sim.cluster`) or in threads (`start_local`, used by tests). Ports live in `service.DEFAULT_PORTS`.
- `ach_sim/operator_server.py`: `Operator.submit()` → `_submit()` under a lock (validate via service, duplicate check, settle via service, split per RDFI into mailboxes, R13 for non-participants), then outside the lock `_dispatch()` POSTs forwarded files to RDFI services. That ordering matters: an RDFI service answers by submitting its own return file back to `POST /v1/files` (its API key) before replying, so holding the lock would deadlock. The operator turns the RDFI's reply into `rdfi_decision` events, so `events.jsonl` keeps a single writer. Downstream failures: validation/settlement down → 503 reject; RDFI down → mailbox delivery still happens plus a warning.
- `ach_sim/rdfi_service.py`: owns a deepcopy of that bank's `RDFI_ACCOUNTS` (the operator no longer holds accounts); return files get a rotating file ID modifier so several at the same sim time don't trip the duplicate check.
- `ach_sim/service.py`: `call()` (traced HTTP client), `TracedHandler` (one server span per request; subclasses implement `handle_get`/`handle_post` and reply with `send()`), `run_service()` (logging, telemetry, SIGTERM-safe flush).
- `ach_sim/dashboard.py` + `dashboard.html`: separate read-only process that rebuilds everything from `events.jsonl`, `received/` and `mailbox/` (no shared memory with the operator). Per-entry outcomes are keyed by `(file_id, trace)`; returns sent back by a `mailbox` bank are linked to the most recent file that carried the original trace. If you add or rename an event in the operator, update `Observer.outcomes()`/`state()` too.
- `ach_sim/telemetry.py`: the only module that imports OpenTelemetry; with it missing, everything is a no-op shim, so the core stays stdlib-only. Code elsewhere uses `telemetry.span(...)`, the module-level instruments (`FILES`, `ENTRY_OUTCOMES`, ...), `inject()` on client headers and `span(..., parent_headers=...)` on the server for W3C propagation. Logging goes through stdlib `logging` under `ach_sim`; structured fields are passed as `extra={"attrs": {...}}`. `Operator.event()` writes `events.jsonl`, logs the event and stamps the current `trace_id`/`span_id`. CLIs call `configure_logging()` + `setup(service)`; library code and tests never configure providers (tests inject in-memory exporters via `setup(..., span_exporter=...)`).
- `ach_sim/client.py`: `OperatorClient` (urllib) with `send_file` (local `validate()` pre-check unless `precheck=False`), `inbox`/`download`/`ack`, and `receive` (pull mailbox, post entries to a ledger, transmit returns back through the operator).

## Conventions

- `examples/*.ach` are generated artifacts: edit `generate_examples.py` and regenerate rather than hand-editing. Files named `*INVALID*` are intentionally broken and excluded from validity tests.
- `sim_data/` is runtime output, not source.
- README says Python 3.9+; modules use `from __future__ import annotations` so `X | None` hints stay 3.9-compatible. Keep that import in new modules that use such hints.
