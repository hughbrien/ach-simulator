"""Run with:  python -m unittest discover -s tests -v"""
import copy
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ach_sim import nacha, rdfi, telemetry  # noqa: E402
from ach_sim.client import OperatorClient  # noqa: E402
from ach_sim.cluster import start_local  # noqa: E402
from ach_sim.participants import (FIRST_EXAMPLE_BANK, PARTICIPANTS, RDFI_ACCOUNTS,  # noqa: E402
                                  SECOND_SAMPLE_BANK)
from ach_sim.settlement import ET, settlement_for  # noqa: E402

EX = ROOT / "examples"
VALID = [p for p in sorted(EX.glob("*.ach")) if "INVALID" not in p.name]


def mutate(text, line_prefix, start, end, new):
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(line_prefix):
            lines[i] = ln[:start - 1] + new + ln[end:]
            break
    return "\n".join(lines) + "\n"


class RoutingTests(unittest.TestCase):
    def test_known_check_digits(self):
        # Published routing numbers whose check digits are well known.
        for r in ("011000015", "021000021", "123456780"):
            self.assertTrue(nacha.valid_routing(r), r)
        self.assertFalse(nacha.valid_routing("123456789"))


class ExampleFileTests(unittest.TestCase):
    def test_examples_are_valid(self):
        self.assertGreaterEqual(len(VALID), 7)
        for p in VALID:
            with self.subTest(p.name):
                res = nacha.validate(p.read_text())
                self.assertEqual(res.errors, [])

    def test_shape(self):
        for p in VALID:
            lines = p.read_text().splitlines()
            self.assertTrue(all(len(ln) == 94 for ln in lines), p.name)
            self.assertEqual(len(lines) % 10, 0, p.name)
            self.assertEqual(lines[0][:3], "101")
            self.assertEqual(lines[0][34:40], "094101")

    def test_round_trip(self):
        for p in VALID:
            text = p.read_text()
            self.assertEqual(nacha.parse(text).to_text(), text, p.name)

    def test_invalid_example_is_rejected(self):
        res = nacha.validate((EX / "90_INVALID_bad_entry_hash.ach").read_text())
        self.assertFalse(res.ok)
        self.assertIn("entry hash", res.errors[0])

    def test_returns_reference_real_traces(self):
        originals = set()
        for name in ("01_ppd_payroll_credits.ach", "02_ccd_vendor_debits_with_remittance.ach",
                     "03_web_consumer_debits.ach"):
            f = nacha.parse((EX / name).read_text())
            originals |= {(b.company_id, e.trace_number) for b in f.batches for e in b.entries}
        f = nacha.parse((EX / "05_returns_R01_R02_R03_R10_R29.ach").read_text())
        codes = set()
        for b in f.batches:
            for e in b.entries:
                a = e.addenda[0]
                codes.add(a["return_reason_code"])
                self.assertIn((b.company_id, a["original_entry_trace_number"]), originals)
        self.assertEqual(codes, {"R01", "R02", "R03", "R10", "R29"})


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.text = (EX / "01_ppd_payroll_credits.ach").read_text()

    def test_amount_tamper_breaks_totals(self):
        bad = mutate(self.text, "622", 30, 39, "0000999999")
        errs = " ".join(nacha.validate(bad).errors)
        self.assertIn("total credit", errs)

    def test_missing_padding(self):
        bad = "\n".join(self.text.splitlines()[:-1]) + "\n"
        self.assertIn("multiple of 10", " ".join(nacha.validate(bad).errors))

    def test_debit_in_credit_batch(self):
        bad = mutate(self.text, "622", 2, 3, "27")
        errs = " ".join(nacha.validate(bad).errors)
        self.assertIn("debit in a credits-only", errs)

    def test_bad_routing_check_digit(self):
        bad = mutate(self.text, "622", 12, 12, "9")
        self.assertIn("ABA check digit", " ".join(nacha.validate(bad).errors))

    def test_web_debit_needs_payment_type(self):
        text = (EX / "03_web_consumer_debits.ach").read_text()
        bad = mutate(text, "627", 77, 78, "  ")
        self.assertIn("payment type code", " ".join(nacha.validate(bad).errors))

    def test_short_record(self):
        bad = self.text.replace("JANE Q PUBLIC  ", "JANE Q PUBLIC", 1)
        self.assertIn("94 characters", nacha.validate(bad).errors[0])


class SettlementTests(unittest.TestCase):
    def at(self, s):
        return datetime.fromisoformat(s).replace(tzinfo=ET)

    def test_same_day_windows(self):
        mon = date(2026, 11, 2)
        self.assertEqual(settlement_for(mon, self.at("2026-11-02T10:30"), True), (mon, "SDA-1"))
        self.assertEqual(settlement_for(mon, self.at("2026-11-02T10:31"), True)[1], "SDA-2")
        self.assertEqual(settlement_for(mon, self.at("2026-11-02T16:45"), True)[1], "SDA-3")
        self.assertEqual(settlement_for(mon, self.at("2026-11-02T16:46"), True),
                         (date(2026, 11, 3), "NEXT-DAY"))

    def test_ineligible_goes_next_day(self):
        mon = date(2026, 11, 2)
        self.assertEqual(settlement_for(mon, self.at("2026-11-02T09:00"), False)[1], "NEXT-DAY")

    def test_weekend_and_holiday(self):
        # Saturday 31 Oct 2026 -> Monday 2 Nov
        self.assertEqual(settlement_for(date(2026, 10, 31), self.at("2026-10-31T09:00"), True)[0],
                         date(2026, 11, 2))
        # Effective Wed 11 Nov 2026 (Veterans Day) -> Thu 12 Nov
        self.assertEqual(settlement_for(date(2026, 11, 11), self.at("2026-11-02T09:00"), True)[0],
                         date(2026, 11, 12))


class RdfiTests(unittest.TestCase):
    def test_decisions(self):
        accts = copy.deepcopy(RDFI_ACCOUNTS[SECOND_SAMPLE_BANK])
        f = nacha.parse((EX / "03_web_consumer_debits.ach").read_text())
        b = f.batches[0]
        got = [rdfi.decide(e, b, accts)[0][:2] for e in b.entries]
        self.assertEqual(got, [("post", None), ("return", "R01"), ("return", "R10"),
                               ("return", "R03")])
        self.assertEqual(accts["1000234567"]["balance"], 2_500_00 - 14_99)


class OperatorEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cluster = start_local(self.tmp.name)
        self.c = OperatorClient(self.cluster.operator_url,
                                PARTICIPANTS[FIRST_EXAMPLE_BANK]["api_key"],
                                "2026-11-02T08:30:00-05:00")

    def tearDown(self):
        self.cluster.stop()
        self.tmp.cleanup()

    def test_submit_duplicate_and_returns(self):
        text = (EX / "03_web_consumer_debits.ach").read_text()
        status, ack = self.c.send_file(text)
        self.assertEqual(status, 200)
        self.assertEqual(ack["batches"][0]["settlement_date"], "2026-11-03")
        status, _ = self.c.send_file(text)
        self.assertEqual(status, 409)
        _, box = self.c.inbox()
        self.assertEqual(len(box["files"]), 1)
        _, ret = self.c.download(box["files"][0]["name"])
        self.assertTrue(nacha.validate(ret).ok)
        codes = sorted(e.addenda[0]["return_reason_code"]
                       for b in nacha.parse(ret).batches for e in b.entries)
        self.assertEqual(codes, ["R01", "R03", "R10"])

    def test_bad_key_and_bad_file(self):
        bad = OperatorClient(self.c.url, "nope", None)
        self.assertEqual(bad._req("GET", "/v1/mailbox")[0], 401)
        text = (EX / "90_INVALID_bad_entry_hash.ach").read_text()
        self.assertEqual(self.c.send_file(text, precheck=False)[0], 422)

    def test_events_logged(self):
        self.c.send_file((EX / "01_ppd_payroll_credits.ach").read_text())
        events = [json.loads(x)["event"] for x in
                  (Path(self.tmp.name) / "events.jsonl").read_text().splitlines()]
        self.assertIn("file_accepted", events)
        self.assertIn("rdfi_decision", events)

    def test_rdfi_returns_come_back_as_its_own_file(self):
        status, ack = self.c.send_file((EX / "03_web_consumer_debits.ach").read_text())
        returns_id = ack["forwarded"][0]["rdfi_returns_file"]
        self.assertIsNotNone(returns_id)
        self.assertEqual(self.c.status(returns_id)[1]["status"], "accepted")
        submitters = [json.loads(x).get("submitter") for x in
                      (Path(self.tmp.name) / "events.jsonl").read_text().splitlines()
                      if json.loads(x)["event"] == "file_accepted"]
        self.assertEqual(submitters, [FIRST_EXAMPLE_BANK, SECOND_SAMPLE_BANK])

    def test_validation_service_down(self):
        self.cluster.op.validation_url = "http://127.0.0.1:9"  # nothing listens there
        status, resp = self.c.send_file((EX / "01_ppd_payroll_credits.ach").read_text())
        self.assertEqual(status, 503)
        self.assertIn("validation service unavailable", resp["errors"][0])


@unittest.skipUnless(telemetry.HAVE_OTEL, "OpenTelemetry not installed (requirements-otel.txt)")
class TelemetryTests(unittest.TestCase):
    """One client call should produce a single trace spanning client and operator,
    metrics for the outcome, and logs and events.jsonl records carrying that trace ID."""

    @classmethod
    def setUpClass(cls):
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        if not telemetry._state["enabled"]:
            cls.spans, cls.metrics = InMemorySpanExporter(), InMemoryMetricReader()
            cls.logs = InMemoryLogRecordExporter()
            telemetry.setup("ach-test", span_exporter=cls.spans, metric_reader=cls.metrics,
                            log_exporter=cls.logs)
        else:
            raise unittest.SkipTest("telemetry already configured in this process")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cluster = start_local(self.tmp.name)
        self.c = OperatorClient(self.cluster.operator_url,
                                PARTICIPANTS[FIRST_EXAMPLE_BANK]["api_key"],
                                "2026-11-02T08:30:00-05:00")

    def tearDown(self):
        self.cluster.stop()
        self.tmp.cleanup()

    def metric_points(self, name):
        data = self.metrics.get_metrics_data()
        return [pt for rm in data.resource_metrics for sm in rm.scope_metrics
                for m in sm.metrics if m.name == name for pt in m.data.data_points]

    def test_trace_metrics_and_logs(self):
        status, ack = self.c.send_file((EX / "03_web_consumer_debits.ach").read_text())
        self.assertEqual(status, 200)
        telemetry.flush()

        spans = self.spans.get_finished_spans()
        root = next(s for s in spans if s.name == "ach.client.send_file")
        trace_id = root.context.trace_id
        in_trace = {s.name for s in spans if s.context.trace_id == trace_id}
        self.assertTrue({"POST /v1/files", "ach.file.submit", "POST /v1/validate",
                         "ach.validate", "POST /v1/settle", "ach.settle", "ach.rdfi.route",
                         "ach.rdfi.dispatch", "POST /v1/inbound", "ach.rdfi.process",
                         "ach.rdfi.send_returns"} <= in_trace, in_trace)
        # The RDFI's return file is submitted inside the same trace.
        self.assertEqual(sum(1 for s in spans if s.name == "ach.file.submit"
                             and s.context.trace_id == trace_id), 2)
        submitted = {s.attributes["ach.file.id"] for s in spans if s.name == "ach.file.submit"
                     and s.context.trace_id == trace_id}
        self.assertEqual(submitted, {ack["file_id"], ack["forwarded"][0]["rdfi_returns_file"]})

        files = self.metric_points("ach.files")
        self.assertTrue(any(p.attributes["ach.result"] == "accepted" for p in files))
        outcomes = {(p.attributes["ach.outcome"], p.attributes["ach.code"])
                    for p in self.metric_points("ach.entries.outcomes")}
        self.assertTrue({("return", "R01"), ("return", "R03"), ("post", "")} <= outcomes)

        logs = [r.log_record for r in self.logs.get_finished_logs()]
        accepted = [r for r in logs if str(r.body).startswith("file_accepted")
                    and r.trace_id == trace_id]
        self.assertTrue(accepted, "file_accepted log not correlated with the trace")

        events = [json.loads(x) for x in
                  (Path(self.tmp.name) / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[0]["trace_id"], f"{trace_id:032x}")


if __name__ == "__main__":
    unittest.main()
