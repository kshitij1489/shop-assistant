"""Opt-in live address extraction checks using synthetic customer messages."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.evaluate_contextual import contains_literal


def validate_output(case, output):
    """Require all supplied fields, no inferred fields, and retained street details."""
    if output is None:
        return ['extraction failed']
    expected = case['expected_fields']
    street = case.get('street_contains', [])
    keys = set(expected) | ({'street_address'} if street else set())
    errors = []
    if set(output) != keys:
        errors.append('field set')
    for key, value in expected.items():
        if output.get(key, '').strip().casefold() != value.strip().casefold():
            errors.append(key)
    actual_street = output.get('street_address', '').casefold()
    for detail in street:
        if not contains_literal(actual_street, detail):
            errors.append('street detail: ' + detail)
    return errors


def evaluate_case(case, repetition):
    from chatbot_core.logic.cafe.prompt_builder import extract_address_with_gpt

    started = time.monotonic()
    output = extract_address_with_gpt(
        case['resolved'], original_text=case['original'], pending=case['pending'],
        rephrased_sentence=case['rewrite'],
    )
    return {
        'id': case['id'], 'repetition': repetition, 'input': case,
        'output': output, 'errors': validate_output(case, output),
        'latency_ms': round((time.monotonic() - started) * 1000),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize real provider calls')
    parser.add_argument('--env-file', default='.env.dev')
    parser.add_argument('--model')
    parser.add_argument('--cases', type=Path, default=ROOT / 'tests/fixtures/address_extraction.json')
    parser.add_argument('--output', type=Path, default=Path('/tmp/chatbot-live-address-extraction.json'))
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required to make provider calls')
    if args.repeats < 1 or not 1 <= args.workers <= 4:
        parser.error('--repeats must be positive and --workers must be between 1 and 4')
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error('Live evidence must be outside the repository')

    os.environ['DJANGO_SETTINGS_MODULE'] = 'tests.settings.integration'
    import django
    django.setup()
    from django.conf import settings
    from dotenv import dotenv_values
    from chatbot_core.logic.cafe.prompt_builder import extract_address_with_gpt

    config = dotenv_values(args.env_file)
    settings.OPENAI_API_KEY = config.get('OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not settings.OPENAI_API_KEY:
        parser.error('OPENAI_API_KEY is not configured')
    settings.LLM_MODEL = args.model or config.get('LLM_MODEL') or 'gpt-6-luna'
    settings.LLM_TIMEOUT = 35
    settings.LLM_MAX_RETRIES = 0
    cases_text = args.cases.read_text()
    cases = json.loads(cases_text)
    jobs = [(case, repetition) for repetition in range(1, args.repeats + 1) for case in cases]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda job: evaluate_case(*job), jobs))
    report = {
        'model': settings.LLM_MODEL,
        'extractor_sha256': hashlib.sha256(inspect.getsource(extract_address_with_gpt).encode()).hexdigest(),
        'cases_sha256': hashlib.sha256(cases_text.encode()).hexdigest(),
        'extraction_calls': len(results), 'results': results,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    failures = [(row['id'], row['repetition'], row['errors']) for row in results if row['errors']]
    print(json.dumps({'cases': len(cases), 'extraction_calls': len(results),
                      'failures': failures, 'report': str(output)}, ensure_ascii=False))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
