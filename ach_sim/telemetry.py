"""Metrics, logs and traces for the simulator (OpenTelemetry, optional).

The rest of the package calls this module only. When the OpenTelemetry packages
are not installed every call is a no-op, so the simulator still runs on the bare
standard library. To export telemetry:

    pip install -r requirements-otel.txt
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318     # any OTLP/HTTP receiver
    # or, to print everything to stderr instead:  export ACH_SIM_OTEL=console

Configuration is the standard OTEL_* environment variables (endpoint, headers,
OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES, OTEL_METRIC_EXPORT_INTERVAL, ...),
plus ACH_SIM_OTEL = otlp | console | off. Export is enabled when ACH_SIM_OTEL is
otlp/console or an OTLP endpoint variable is set.

Logs: the package logs through the standard `logging` module under the "ach_sim"
logger. setup() bridges those records to OpenTelemetry with the active trace and
span IDs; configure_logging() adds a console handler that prints the trace ID.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager

SERVICE_NAMESPACE = "ach-simulator"
SERVICE_VERSION = "1.1.0"
APPLICATION_ID = "ach-simulator"   # Application_ID tag on every service's resource
log = logging.getLogger("ach_sim")

try:
    from opentelemetry import context as otel_context
    from opentelemetry import metrics as otel_metrics
    from opentelemetry import propagate
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import SpanKind, StatusCode
    HAVE_OTEL = True
except ImportError:  # bare standard library: everything below becomes a no-op
    HAVE_OTEL = False


# ---------------------------------------------------------------------------
# No-op stand-ins used when OpenTelemetry is not installed
# ---------------------------------------------------------------------------
class _NoSpan:
    def set_attribute(self, *a, **k): pass
    def set_attributes(self, *a, **k): pass
    def add_event(self, *a, **k): pass
    def record_exception(self, *a, **k): pass
    def set_status(self, *a, **k): pass
    def is_recording(self): return False


class _NoInstrument:
    def add(self, *a, **k): pass
    def record(self, *a, **k): pass


class _NoMeter:
    def create_counter(self, *a, **k): return _NoInstrument()
    def create_histogram(self, *a, **k): return _NoInstrument()
    def create_up_down_counter(self, *a, **k): return _NoInstrument()


_KINDS = {}
if HAVE_OTEL:
    tracer = otel_trace.get_tracer("ach_sim", SERVICE_VERSION)
    meter = otel_metrics.get_meter("ach_sim", SERVICE_VERSION)
    _KINDS = {"internal": SpanKind.INTERNAL, "server": SpanKind.SERVER,
              "client": SpanKind.CLIENT}
else:
    tracer = None
    meter = _NoMeter()


# ---------------------------------------------------------------------------
# Instruments. The API returns proxies, so these work before and after setup().
# ---------------------------------------------------------------------------
FILES = meter.create_counter(
    "ach.files", unit="{file}",
    description="Files submitted to the operator, by result (accepted / rejected)")
ENTRIES_FORWARDED = meter.create_counter(
    "ach.entries.forwarded", unit="{entry}", description="Entries routed to an RDFI")
ENTRY_OUTCOMES = meter.create_counter(
    "ach.entries.outcomes", unit="{entry}",
    description="RDFI and operator decisions per entry (post / return / noc)")
AMOUNT = meter.create_counter(
    "ach.amount", unit="USD", description="Dollar value of accepted entries")
FILE_DURATION = meter.create_histogram(
    "ach.file.processing.duration", unit="s",
    description="Operator time to edit, settle, route and answer one file")
MAILBOX_OPS = meter.create_counter(
    "ach.mailbox.operations", unit="{file}",
    description="Mailbox deliveries, downloads and acknowledgments")
VALIDATIONS = meter.create_counter(
    "ach.validations", unit="{file}", description="Files checked by the validation service")
SETTLEMENTS = meter.create_counter(
    "ach.settlements", unit="{batch}", description="Batches assigned a settlement window")
LEDGER_POSTINGS = meter.create_counter(
    "ach.ledger.postings", unit="{entry}",
    description="Entries a mailbox bank's client posted, returned or NOC'd")
HTTP_SERVER_DURATION = meter.create_histogram(
    "http.server.request.duration", unit="s", description="Operator HTTP request duration")
HTTP_CLIENT_DURATION = meter.create_histogram(
    "http.client.request.duration", unit="s", description="Bank client HTTP request duration")
STEPS = meter.create_counter(
    "ach.steps", unit="{step}",
    description="Processing steps completed, by step and status (ok / rejected / error)")
STEP_DURATION = meter.create_histogram(
    "ach.step.duration", unit="s", description="Duration of one processing step")


# ---------------------------------------------------------------------------
# Tracing helpers
# ---------------------------------------------------------------------------
@contextmanager
def span(name: str, kind: str = "internal", parent_headers=None, **attributes):
    """Start a span as the current span. `parent_headers` (any mapping with .get)
    continues a W3C trace context received over HTTP."""
    if not HAVE_OTEL:
        yield _NoSpan()
        return
    ctx = propagate.extract(parent_headers) if parent_headers is not None else None
    with tracer.start_as_current_span(name, context=ctx, kind=_KINDS[kind],
                                      attributes=_clean(attributes)) as s:
        yield s


class Step:
    """Handle yielded by step(): set `status`/`detail`, or call reject()/fail()."""

    def __init__(self, name: str):
        self.name, self.status, self.detail = name, "ok", {}
        self.duration = 0.0
        self.span = _NoSpan()

    def reject(self, reason: str, **detail):
        """A business outcome (failed edits, duplicate, ...): not a span error."""
        self.status = "rejected"
        self.detail.update(reason=reason, **detail)

    def fail(self, error, **detail):
        """Something broke (a dependency was down, ...): marks the span as an error."""
        self.status = "error"
        self.detail.update(error=str(error), **detail)


_LOG_LEVELS = {"ok": logging.INFO, "skipped": logging.INFO, "rejected": logging.WARNING,
               "error": logging.ERROR}


@contextmanager
def step(name: str, *, span_name: str | None = None, logger: logging.Logger | None = None,
         on_end=None, metric_attrs: dict | None = None, **attributes):
    """One processing step, observed three ways:

    trace   a span (`ach.step.<name>` unless span_name is given) with ach.step,
            ach.step.status and ach.step.<detail> attributes;
    metrics ach.steps (counter) and ach.step.duration (histogram), by step and status;
    log     one line at the end, inside the span so it carries the trace and span ID.

    `on_end(step)` replaces the log line (the operator uses it to write a
    `file_step` event, which is logged too). It runs inside the span."""
    st = Step(name)
    start = time.perf_counter()
    with span(span_name or f"ach.step.{name}", **{"ach.step": name, **attributes}) as s:
        st.span = s
        try:
            yield st
        except Exception as exc:
            st.fail(exc)
            raise
        finally:
            st.duration = time.perf_counter() - start
            s.set_attributes(_clean({"ach.step.status": st.status,
                                     "ach.step.duration_ms": round(st.duration * 1000, 3),
                                     **{f"ach.step.{k}": v for k, v in st.detail.items()}}))
            if st.status == "error":
                mark_error(s, st.detail.get("error", "step failed"))
            attrs = {"ach.step": name, "ach.step.status": st.status, **(metric_attrs or {})}
            STEPS.add(1, attrs)
            STEP_DURATION.record(st.duration, attrs)
            if on_end is not None:
                on_end(st)
            else:
                (logger or log).log(
                    _LOG_LEVELS.get(st.status, logging.INFO), "step %s %s in %.1f ms%s", name,
                    st.status, st.duration * 1000,
                    "".join(f" {k}={v}" for k, v in st.detail.items()),
                    extra={"attrs": {"ach.step": name, "ach.step.status": st.status,
                                     "ach.step.duration_ms": round(st.duration * 1000, 3),
                                     **{k: v for k, v in attributes.items()},
                                     **{f"ach.step.{k}": v for k, v in st.detail.items()}}})


def http_route(path: str) -> str:
    """Low-cardinality route template for an operator API path (span names, metrics)."""
    p = path.split("?")[0].rstrip("/") or "/"
    if p.startswith("/v1/files/"):
        return "/v1/files/{file_id}"
    if p.startswith("/v1/mailbox/"):
        return "/v1/mailbox/{name}/ack" if p.endswith("/ack") else "/v1/mailbox/{name}"
    return p


def inject(headers: dict) -> dict:
    """Add traceparent/tracestate for the current span to outgoing HTTP headers."""
    if HAVE_OTEL:
        propagate.inject(headers)
    return headers


def mark_error(s, exc_or_message) -> None:
    if not HAVE_OTEL:
        return
    if isinstance(exc_or_message, BaseException):
        s.record_exception(exc_or_message)
        s.set_status(StatusCode.ERROR, str(exc_or_message))
    else:
        s.set_status(StatusCode.ERROR, str(exc_or_message))


def current_ids() -> dict:
    """Trace and span ID of the current span, for correlating other records."""
    if not HAVE_OTEL:
        return {}
    sc = otel_trace.get_current_span().get_span_context()
    if not sc.is_valid:
        return {}
    return {"trace_id": f"{sc.trace_id:032x}", "span_id": f"{sc.span_id:016x}"}


def _clean(attrs: dict) -> dict:
    """OpenTelemetry attributes must be str/bool/int/float or lists of them."""
    out = {}
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)):
            out[k] = v
        elif isinstance(v, (list, tuple)) and all(isinstance(x, (str, bool, int, float)) for x in v):
            out[k] = list(v)
        else:
            out[k] = json.dumps(v, default=str)
    return out


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class _TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = current_ids().get("trace_id", "-")
        return True


def configure_logging(level: str | int | None = None, service: str = "") -> None:
    """Console logging for the CLIs: stderr, with the service name and trace ID on
    each line. Level from the argument, else ACH_SIM_LOG_LEVEL, else INFO."""
    level = level or os.environ.get("ACH_SIM_LOG_LEVEL", "INFO")
    lvl = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    h = logging.StreamHandler(sys.stderr)
    h.addFilter(_TraceIdFilter())
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s " + (f"[{service}] " if service else "")
        + "%(name)s trace=%(trace_id)s  %(message)s"))
    h.setLevel(lvl)
    log.addHandler(h)
    if log.level == logging.NOTSET or log.level > lvl:
        log.setLevel(lvl)
    log.propagate = False


class _OtelLogHandler(logging.Handler):
    """Bridge stdlib logging records to the OpenTelemetry Logs API. Structured
    fields passed as logging `extra={"attrs": {...}}` become log attributes."""

    def __init__(self, logger_provider):
        super().__init__(logging.INFO)
        from opentelemetry._logs import SeverityNumber
        self._logger = logger_provider.get_logger("ach_sim", SERVICE_VERSION)
        self._sev = {logging.DEBUG: SeverityNumber.DEBUG, logging.INFO: SeverityNumber.INFO,
                     logging.WARNING: SeverityNumber.WARN, logging.ERROR: SeverityNumber.ERROR,
                     logging.CRITICAL: SeverityNumber.FATAL}

    def emit(self, record):
        try:
            attrs = _clean(getattr(record, "attrs", {}) or {})
            attrs.update({"code.function.name": record.funcName,
                          "code.file.path": record.pathname,
                          "code.line.number": record.lineno, "logger.name": record.name})
            exc = record.exc_info[1] if record.exc_info else None
            self._logger.emit(
                timestamp=int(record.created * 1e9), context=otel_context.get_current(),
                severity_number=self._sev.get(record.levelno),
                severity_text=record.levelname, body=record.getMessage(),
                attributes=attrs, exception=exc)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# SDK setup
# ---------------------------------------------------------------------------
_state = {"enabled": False, "providers": (), "target": ""}
DOTENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _load_dotenv(path: str = DOTENV) -> None:
    """Read KEY=VALUE lines from the project's .env. Variables already set in the
    environment win, so a shell export always overrides the file."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _dynatrace_env() -> None:
    """Map DT_TENANT_URL / DT_PLATFORM_TOKEN (or DT_API_TOKEN) to the standard OTLP
    variables, unless an OTLP endpoint is already configured.

    OTLP ingest lives on the environment domain, not the apps domain:
    https://<id>.apps.dynatrace.com -> https://<id>.live.dynatrace.com/api/v2/otlp
    Platform tokens (dt0s16...) authenticate as Bearer, API tokens (dt0c01...) as Api-Token.
    """
    url = os.environ.get("DT_TENANT_URL", "").rstrip("/")
    token = os.environ.get("DT_PLATFORM_TOKEN") or os.environ.get("DT_API_TOKEN", "")
    if not url or not token or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    if ".apps.dynatrace.com" in url:
        url = url.replace(".apps.dynatrace.com", ".live.dynatrace.com")
    else:  # e.g. *.sprint.apps.dynatracelabs.com -> *.sprint.dynatracelabs.com
        url = url.replace(".apps.", ".")
    scheme = "Api-Token" if token.startswith("dt0c01.") else "Bearer"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{url}/api/v2/otlp"
    os.environ.setdefault("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization={scheme} {token}")
    # Dynatrace accepts delta temporality only for OTLP metrics.
    os.environ.setdefault("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "delta")


def _mode() -> str:
    _load_dotenv()
    _dynatrace_env()
    mode = os.environ.get("ACH_SIM_OTEL", "").strip().lower()
    if mode in ("off", "none", "false", "0") or \
            os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return ""
    if mode:
        return mode
    endpoints = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                 "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    return "otlp" if any(os.environ.get(e) for e in endpoints) else ""


def setup(service_name: str, *, resource_attrs: dict | None = None, span_exporter=None,
          metric_reader=None, log_exporter=None) -> bool:
    """Install tracer, meter and logger providers. Returns True when telemetry is
    being exported. Exporter/reader arguments override the environment (tests)."""
    if _state["enabled"]:
        return True
    explicit = any(x is not None for x in (span_exporter, metric_reader, log_exporter))
    mode = "explicit" if explicit else _mode()
    if not mode:
        return False
    if not HAVE_OTEL:
        log.warning("telemetry requested but OpenTelemetry is not installed; "
                    "run: pip install -r requirements-otel.txt")
        return False
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("telemetry requested but opentelemetry-sdk is not installed; "
                    "run: pip install -r requirements-otel.txt")
        return False

    if mode == "console":
        from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        span_exporter = ConsoleSpanExporter(out=sys.stderr)
        metric_reader = PeriodicExportingMetricReader(ConsoleMetricExporter(out=sys.stderr))
        log_exporter = ConsoleLogRecordExporter(out=sys.stderr)
    elif mode == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            log.warning("OTLP export requested but opentelemetry-exporter-otlp-proto-http "
                        "is not installed; run: pip install -r requirements-otel.txt")
            return False
        span_exporter = OTLPSpanExporter()
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        log_exporter = OTLPLogExporter()
    elif mode != "explicit":
        log.warning("unknown ACH_SIM_OTEL=%r (use otlp, console or off); telemetry off", mode)
        return False

    attrs = {"service.namespace": SERVICE_NAMESPACE, "service.version": SERVICE_VERSION,
             "Application_ID": APPLICATION_ID, **(resource_attrs or {})}
    if not os.environ.get("OTEL_SERVICE_NAME"):
        attrs["service.name"] = service_name
    resource = Resource.create(attrs)

    tp = TracerProvider(resource=resource)
    if span_exporter is not None:
        tp.add_span_processor(BatchSpanProcessor(span_exporter))
    otel_trace.set_tracer_provider(tp)

    mp = MeterProvider(resource=resource,
                       metric_readers=[metric_reader] if metric_reader is not None else [])
    otel_metrics.set_meter_provider(mp)

    lp = LoggerProvider(resource=resource)
    if log_exporter is not None:
        lp.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(lp)
    log.addHandler(_OtelLogHandler(lp))
    if log.level == logging.NOTSET or log.level > logging.INFO:
        log.setLevel(logging.INFO)

    _state.update(enabled=True, providers=(tp, mp, lp))
    _state["target"] = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "OTLP default endpoint")
                        if mode == "otlp" else mode)
    log.info("telemetry enabled for service %s -> %s",
             os.environ.get("OTEL_SERVICE_NAME", service_name), _state["target"])
    return True


def flush() -> None:
    """Push buffered spans, metrics and logs now (the SDK also does this at exit)."""
    for p in _state["providers"]:
        p.force_flush()
