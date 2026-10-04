"""Local instrumentation microbenchmark. No database, services or model calls."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from timeit import timeit


def main():
    from django.conf import settings
    settings.configure(EVALUATION_ENABLED=False)
    from .context import ControlContext, activate, current
    from .telemetry import emit, logger, EvidenceHandler
    from evaluate.contracts.models import ExecutionIdentity
    identity = ExecutionIdentity(run_id='benchmark', scenario_id='benchmark', scenario_instance_id='benchmark', attempt=1)
    ctx = ControlContext(identity, 'lease', 'tenant', 'customer', 'chat', 'request', datetime.now(timezone.utc))
    count = 10000
    results = {'disabled_context_us': timeit(current, number=count) / count * 1e6}
    settings.EVALUATION_ENABLED = True
    class Sink(logging.Handler):
        def emit(self, record):
            json.dumps(record.evaluation, allow_nan=False)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    sink = Sink()
    logger.addHandler(sink)
    with activate(ctx):
        results['enabled_json_event_us'] = timeit(lambda: emit('benchmark', elapsed_ms=1.0), number=count) / count * 1e6
    logger.removeHandler(sink)
    with TemporaryDirectory() as directory:
        sink = EvidenceHandler(Path(directory) / 'application.jsonl', identity.run_id)
        logger.addHandler(sink)
        try:
            with activate(ctx):
                results['durable_event_us'] = timeit(lambda: emit('benchmark', elapsed_ms=1.0), number=100) / 100 * 1e6
        finally:
            logger.removeHandler(sink)
            sink.close()
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
