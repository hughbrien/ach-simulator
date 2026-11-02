# ACH File Transmission Simulator

Example Nacha-format ACH files and a Python simulation of transmitting them
through a simulated ACH Operator, in both directions:

- **Outbound (origination):** an ODFI validates its files and transmits them. The
  operator acknowledges or rejects each file, assigns settlement dates and
  Same Day windows, routes entries to the RDFIs, and the simulated RDFIs send back
  returns and Notifications of Change (NOCs).
- **Inbound (receiving):** another bank originates entries to the ODFI's
  customers. The bank pulls its mailbox, posts entries to its ledger, and
  transmits returns for what it cannot post.

Python 3.9+ standard library only. No packages to install. OpenTelemetry export of
metrics, logs and traces is optional (see [Observability](#observability)).

Every bank, routing number, account, person and company is made up. Routing
numbers carry a valid ABA check digit so they pass validation; they are not meant
to identify a real institution.

## Quick start

```bash
python3 run_demo.py                          # full business day, end to end
python3 -m unittest discover -s tests -v     # 22 tests (1 skipped without OpenTelemetry)
python3 generate_examples.py                 # rebuild examples/ from code
```

`demo_output.txt` is the output of a run of `run_demo.py`.

The simulated network is a set of services, each its own process (see
[Services](#services)). Running it by hand, in two terminals:

```bash
# terminal 1: every service plus the dashboard (Ctrl+C stops them all)
python3 -m ach_sim.cluster --data-dir sim_data

# terminal 2: a bank
python3 -m ach_sim.client validate examples/03_web_consumer_debits.ach
python3 -m ach_sim.client --sim-time 2026-11-02T08:30:00-05:00 send examples/03_web_consumer_debits.ach
python3 -m ach_sim.client inbox
python3 -m ach_sim.client --sim-time 2026-11-02T16:00:00-05:00 receive --ledger examples/first_example_bank_ledger.json
python3 -m ach_sim.client --key tdcu-demo-key send examples/07_inbound_from_third_demo_cu.ach
```

To watch what happens, open the dashboard at http://127.0.0.1:8780 (the cluster
starts it). It reads `sim_data/` and refreshes every 2 seconds, so it also works
after `run_demo.py` has finished, started on its own:

```bash
python3 -m ach_sim.dashboard --data-dir sim_data
```

It shows totals, every submitted file with its settlement window, each entry's
outcome (posted, returned, NOC, including returns the receiving bank sends back
later), the event log, and each bank's mailbox, with any file decoded.

`--sim-time` sets the simulated submission clock (Eastern Time), which drives the
Same Day window and settlement date. Without it the operator uses the real clock.
`receive` updates the ledger file in place.

## Services

| Service | Default port | Module | Role |
|---|---|---|---|
| `ach-operator` | 8770 | `operator_server` | Gateway banks talk to: auth, duplicate check, routing, mailboxes, event log |
| `ach-validation` | 8771 | `validation_service` | Nacha format and balancing edits (`POST /v1/validate`) |
| `ach-settlement` | 8772 | `settlement_service` | Settlement dates and Same Day windows (`POST /v1/settle`) |
| `ach-rdfi-second-sample-bank` | 8773 | `rdfi_service` | Second Sample Bank: its accounts and post/return/NOC decisions |
| `ach-rdfi-third-demo-credit-union` | 8774 | `rdfi_service` | Third Demo Credit Union, likewise |
| dashboard | 8780 | `dashboard` | Read-only web UI over `sim_data/` |

`python3 -m ach_sim.cluster` starts them all as separate processes and stops them
together; `--log-dir DIR` sends each service's output to `DIR/<service>.log`. Each
can also be started on its own (`python3 -m ach_sim.<module> --help`); the defaults
let them find each other on the ports above. `run_demo.py` starts the same set on
free ports with logs in `sim_data/logs/`.

A simulated RDFI answers the way a real one does: it transmits its returns and NOCs
to the operator as a file of its own, under its own API key. So the file IDs in an
acknowledgment skip numbers (the receiving banks' return files are submissions
too), and one trace covers the bank client, the operator, validation, settlement,
the RDFI, and the RDFI's return file coming back through the operator.

If validation or settlement is down, the operator rejects the file with HTTP 503.
If an RDFI service is down, the entries still land in that bank's mailbox and the
acknowledgment carries a warning.

## Layout

```
ach_sim/
  nacha.py            record layouts, builder, parser, validator, describe()
  rdfi.py             RDFI decisions (post / return / NOC) and return/NOC file builder
  settlement.py       Fed business days, holidays, Same Day windows
  participants.py     simulated banks, API keys, receiver accounts
  operator_server.py  ach-operator: the gateway (HTTP)
  validation_service.py, settlement_service.py, rdfi_service.py   the other services
  service.py          shared HTTP plumbing: traced client calls, handler base, run loop, ports
  cluster.py          starts every service as its own process (or in threads, for tests)
  client.py           bank-side client and CLI (send, inbox, receive, status, validate)
  dashboard.py        read-only web dashboard over the data dir (page in dashboard.html)
  telemetry.py        optional OpenTelemetry metrics, logs and traces (no-op when not installed)
examples/             the example files (see below) and a ledger for the receiving bank
generate_examples.py  builds examples/ with the builder API
run_demo.py           end-to-end scenario
tests/                unit and end-to-end tests
```

## The simulated network

| Role | Name | Routing | API key | Behaviour |
|---|---|---|---|---|
| ACH Operator | SIM ACH OPERATOR | 099900014 | | Receives, edits, routes, settles |
| ODFI / RDFI (you) | FIRST EXAMPLE BANK | 123456780 | `feb-demo-key` | Files wait in its mailbox for the client |
| RDFI | SECOND SAMPLE BANK | 111000025 | `ssb-demo-key` | Simulated by the operator |
| RDFI / ODFI | THIRD DEMO CREDIT UNION | 222000037 | `tdcu-demo-key` | Simulated by the operator; also originates file 07 |

Receiver accounts at the operator-simulated banks are in `participants.py`. Each
one is set up to produce a specific outcome (closed account, insufficient funds,
consumer dispute, account type mismatch, renumbered account).

## Example files

All dated Monday 2 Nov 2026. Files 05 and 06 are not hand-written: they come from
running the RDFI decision engine over files 01 to 03, so every return and NOC
points at a real trace number in those files.

| File | What it shows | Outcome in the simulation |
|---|---|---|
| `01_ppd_payroll_credits.ach` | PPD payroll credits (220), a savings credit with an 05 addenda, a prenote (23) | R02 closed account; NOC C05 (sent as checking, account is savings); NOC C01 (corrected account number) |
| `02_ccd_vendor_debits_with_remittance.ach` | CCD B2B debits (225), each with an 05 addenda carrying an X12 `RMR` remittance segment | R29 corporate customer says not authorized |
| `03_web_consumer_debits.ach` | WEB consumer debits with payment type codes `R` (recurring) and `S` (single) | R01 insufficient funds, R10 consumer says not authorized, R03 no account |
| `04_same_day_ppd_credits.ach` | Same Day: effective date equals the file date, company descriptive date `SD1300` | Settles same day in window SDA-1 when sent before 10:30 ET |
| `05_returns_R01_R02_R03_R10_R29.ach` | Returns as delivered to the ODFI: codes 21/26, 99 addenda with original trace and original RDFI | |
| `06_notifications_of_change_C01_C05.ach` | NOCs in COR batches: zero amount, 98 addenda with corrected data | |
| `07_inbound_from_third_demo_cu.ach` | Inbound to First Example Bank: PPD payroll and a CCD rent debit, two batches | Bank posts two credits, NOC C05, returns R03 and R01 |
| `90_INVALID_bad_entry_hash.ach` | File 01 with a corrupted batch control entry hash | Blocked by the client's pre-check; HTTP 422 from the operator |
| `91_same_day_over_1m_limit.ach` | $1.25M credit with a same-day effective date | Accepted with a warning, settles next business day |

## What the validator checks

These are the checks `nacha.validate()` applies, and the operator applies them to
every submitted file:

- Every record is 94 characters; the file is a multiple of 10 records; everything after the File Control is `9` padding.
- Record order (1, then 5/6/7/8 groups, then 9).
- File header constants: priority `01`, record size `094`, blocking factor `10`, format code `1`; ABA check digit on the immediate destination.
- Batch: valid service class and SEC code; no debits in a 220 batch or credits in a 225 batch; batch control count, entry hash, debit and credit totals, company ID, ODFI ID and batch number all match what is computed from the entries.
- Entry: known transaction code; RDFI routing check digit; prenotes are zero and live entries are not; trace numbers start with the batch's ODFI ID, ascend within the batch and are unique in the file; addenda indicator matches; WEB/TEL debits carry `R` or `S`; forward entries carry only 05 addenda, returns exactly one 99, COR entries exactly one 98; 05 addenda sequence numbers are correct.
- File control: batch count, block count, entry/addenda count, entry hash, totals.
- Warning for a same-day effective date on an entry over $1M.

Entry hash: the sum of the 8-digit RDFI routing numbers (positions 4 to 11 of
each type 6 record), keeping the rightmost 10 digits.

## Endpoint API

Auth header `X-API-Key`. Optional `X-Sim-Time` (ISO 8601) sets the simulated clock.

| Method and path | Purpose |
|---|---|
| `POST /v1/files` | Submit a file (body is the file text). 200 accepted, 401 bad key, 403 origin does not match the key's bank, 409 duplicate, 422 failed edits |
| `GET /v1/files/<file_id>` | Acknowledgment or rejection for a submitted file |
| `GET /v1/mailbox` | Files waiting for the caller's bank |
| `GET /v1/mailbox/<name>` | Download one file |
| `POST /v1/mailbox/<name>/ack` | Confirm pickup (moves it to `picked_up/`) |
| `GET /v1/events?limit=N` | Recent processing events |
| `GET /v1/health` | Liveness |

The acceptance acknowledgment returns the file ID, totals, each batch's
settlement date and window, the files forwarded to each RDFI, and warnings.
Returns and NOCs are never in the acknowledgment; they arrive in the mailbox,
as in the real network.

The operator treats a file as a duplicate when the immediate origin, file
creation date, creation time and file ID modifier match a file already accepted.

### What the operator does with an accepted file

1. Sends the file to the validation service; any edit error rejects it (422).
2. Asks the settlement service for each batch's settlement date (written as a Julian day in batch header positions 76 to 78) and a window: `SDA-1` / `SDA-2` / `SDA-3` (Same Day deadlines 10:30, 14:45, 16:45 ET), `NEXT-DAY`, or `FUTURE-DATED`. Weekends and Fed holidays are skipped.
3. Splits entries by RDFI and writes one forwarded file per RDFI into its mailbox (operator as immediate origin, settlement date filled in).
4. Hands the forwarded file for each simulated bank to its RDFI service, which decides each entry: R03 unknown account, R02 closed, R16 frozen, R10 (consumer SEC) or R29 (CCD) for a disputed originator, R01 debit over the balance; otherwise posts it, and sends NOC C05 for a checking/savings mismatch or the account's configured NOC.
5. The RDFI service builds a return/NOC file (returns in batches mirroring the original batch header, NOCs in COR batches, trace numbers prefixed with the RDFI's ID) and submits it to the operator, which edits and routes it to the ODFI's mailbox like any other file.
6. Entries addressed to a routing number that is not a participant come back as R13 from the operator.

Every step is written to `sim_data/events.jsonl` (`file_accepted`, `file_rejected`,
`entries_forwarded`, `rdfi_decision`, `returns_delivered`, `operator_return`,
`mailbox_download`, `mailbox_ack`, `return_or_noc_received`) with file IDs and
trace numbers, so the log can feed an observability tool.

## Record layouts (positions are 1-based, inclusive)

| Record | Key fields |
|---|---|
| 1 File Header | 2-3 priority `01` · 4-13 immediate destination (space + routing) · 14-23 immediate origin · 24-29 date YYMMDD · 30-33 time HHMM · 34 file ID modifier · 35-37 `094` · 38-39 `10` · 40 `1` · 41-63 destination name · 64-86 origin name · 87-94 reference |
| 5 Batch Header | 2-4 service class · 5-20 company name · 21-40 discretionary · 41-50 company ID · 51-53 SEC · 54-63 entry description · 64-69 descriptive date · 70-75 effective date · 76-78 settlement date (operator) · 79 originator status `1` · 80-87 ODFI ID · 88-94 batch number |
| 6 Entry Detail | 2-3 transaction code · 4-11 RDFI routing · 12 check digit · 13-29 account · 30-39 amount (cents) · 40-54 individual ID · 55-76 name · 77-78 discretionary / payment type · 79 addenda indicator · 80-94 trace number |
| 7 Addenda 05 | 4-83 payment related information · 84-87 addenda sequence · 88-94 entry detail sequence (last 7 of trace) |
| 7 Addenda 99 (return) | 4-6 return reason · 7-21 original trace · 22-27 date of death · 28-35 original RDFI ID · 36-79 addenda information · 80-94 trace |
| 7 Addenda 98 (NOC) | 4-6 change code · 7-21 original trace · 28-35 original RDFI ID · 36-64 corrected data · 80-94 trace |
| 8 Batch Control | 2-4 service class · 5-10 entry/addenda count · 11-20 entry hash · 21-32 total debits · 33-44 total credits · 45-54 company ID · 55-73 MAC · 80-87 ODFI ID · 88-94 batch number |
| 9 File Control | 2-7 batch count · 8-13 block count · 14-21 entry/addenda count · 22-31 entry hash · 32-43 total debits · 44-55 total credits |

## Observability

The operator, the bank client and `run_demo.py` emit OpenTelemetry metrics, logs
and traces. Without the OpenTelemetry packages every call is a no-op.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-otel.txt

# send to any OTLP/HTTP receiver (OpenTelemetry Collector, Jaeger, Grafana Alloy, ...)
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# or print everything to stderr, no backend needed
export ACH_SIM_OTEL=console

python3 run_demo.py
```

Export is on when `ACH_SIM_OTEL=otlp|console` or an `OTEL_EXPORTER_OTLP_*ENDPOINT`
variable is set, and off with `ACH_SIM_OTEL=off`. All standard `OTEL_*` variables
apply (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_HEADERS`,
`OTEL_METRIC_EXPORT_INTERVAL` in ms, default 60000). Service names: `ach-operator`,
`ach-validation`, `ach-settlement`, `ach-rdfi-<bank>` and `ach-bank-client` (the CLI
client and `run_demo.py`). The cluster launcher clears `OTEL_SERVICE_NAME` so each
process keeps its own name. `ACH_SIM_LOG_LEVEL` sets console log verbosity
(operator default INFO; client and demo default WARNING).

**Dynatrace via `.env`.** If the project has a `.env` file with `DT_TENANT_URL` and
`DT_PLATFORM_TOKEN` (or `DT_API_TOKEN`), every run exports there automatically:

```bash
DT_TENANT_URL=https://<environment-id>.apps.dynatrace.com
DT_PLATFORM_TOKEN=dt0s16....
```

`telemetry.py` derives the OTLP endpoint from the tenant URL (OTLP ingest is on the
environment domain, so `.apps.` is dropped: `https://<environment-id>.live.dynatrace.com/api/v2/otlp`),
sends platform tokens as `Authorization: Bearer` and API tokens as `Api-Token`, and
sets delta metric temporality. Variables already set in the shell take precedence
over `.env`, and an explicit `OTEL_EXPORTER_OTLP_ENDPOINT` disables the mapping. Use
`ACH_SIM_OTEL=off` for a run without export. `.env` is listed in `.gitignore`.

Or set the OTLP variables yourself (metrics must use delta temporality):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://<environment-id>.live.dynatrace.com/api/v2/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Api-Token <token>"
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta
```

The token needs the `openTelemetryTrace.ingest`, `metrics.ingest` and `logs.ingest` scopes.

**Traces.** Each client call is one distributed trace, with W3C `traceparent`
carried on every hop: `ach.client.send_file` (ach-bank-client) → `ach.file.submit`
(ach-operator) → `ach.validate` (ach-validation) → `ach.settle` (ach-settlement) →
`ach.rdfi.route` and `ach.rdfi.dispatch` per RDFI → `ach.rdfi.process` (ach-rdfi-*,
one `ach.rdfi.decision` span event per entry) → `ach.rdfi.send_returns` → a second
`ach.file.submit` in the operator for the RDFI's return file. `receive` traces as `ach.client.receive` → `ach.client.process_file`
per mailbox file. Spans carry `ach.file.id`, bank names, trace numbers and outcomes.

**Metrics.**

| Metric | Unit | Attributes |
|---|---|---|
| `ach.files` | file | `ach.result`, `ach.reason` (accepted, duplicate, failed_edits, ...), `ach.submitter` |
| `ach.file.processing.duration` | s | same as `ach.files` |
| `ach.entries.forwarded` | entry | `ach.rdfi.name` |
| `ach.entries.outcomes` | entry | `ach.rdfi.name`, `ach.outcome` (post/return/noc), `ach.code`, `ach.sec` |
| `ach.amount` | USD | `ach.sec`, `ach.direction` (credit/debit), `ach.window` |
| `ach.mailbox.operations` | file | `ach.operation` (deliver/download/ack), `ach.bank.name` |
| `ach.ledger.postings` | entry | client side: `ach.outcome`, `ach.code`, `ach.bank.name` |
| `http.server.request.duration`, `http.client.request.duration` | s | method, route, status |

**Logs.** The package logs through Python `logging` (logger `ach_sim`). Every
operator event (the same ones written to `events.jsonl`) is also a log record with
the event fields as `ach.*` attributes. Log records, console log lines and
`events.jsonl` entries all carry the trace ID of the request that produced them.

## How this differs from the real network

The simulator keeps the file format and the flow of files faithful and
simplifies the rest:

- **Timing.** Simulated RDFIs return entries the moment the file is processed. Real RDFIs generally have until the second banking day after settlement (60 days for unauthorized consumer debits), so returns arrive over days.
- **Transport and security.** Real files move over secure file transfer or the operator's own connection, usually encrypted and with separate control totals. Here it is plain HTTP on localhost with a static API key.
- **Acknowledgments.** Real operators send their own acknowledgment formats and reports. Here the acknowledgment is JSON.
- **Settlement.** Settlement dates and windows are assigned, but there is no settlement accounting between banks.
- **Same Day ineligible entries.** The simulator settles an over-$1M entry with a same-day effective date on the next business day and warns. Check your operator's documented handling before relying on that.
- **Out of scope:** IAT (international) entries, OFAC screening, dishonored and contested returns, refused NOCs, reversals, and file-level batch splitting at the ODFI.
- **Routing numbers** are fictitious; real operator routing numbers are not used.
