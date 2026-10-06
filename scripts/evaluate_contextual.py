"""Opt-in live semantics checks; evidence stays outside the repository.

Run from the repository root with --live. These synthetic cases exercise the
real provider, unlike the HTTP replay fixtures in tests/unit.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import os
import re
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def matches(actual, expected):
    """Partial object expectations, exact ordered arrays, no prose matching."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and matches(actual[key], value)
                                                for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            matches(a, e) for a, e in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def contains_literal(text, value):
    """Match words without accepting fragments inside other words; allow punctuation."""
    value = value.casefold()
    prefix = r'(?<!\w)' if re.match(r'\w', value) else ''
    suffix = r'(?!\w)' if re.search(r'\w$', value) else ''
    return re.search(prefix + re.escape(value) + suffix, text) is not None


def validate_output(case, parsed):
    """Check typed decisions and per-operation clarification content offline or live."""
    rows = parsed['classifications'] if parsed else []
    first = rows[0] if rows else {}
    errors = []
    if not rows:
        errors.append('missing parsed output')
    routes = case.get('routes', [case['route']] if case.get('route') else [])
    if routes and first.get('intent', '') + '/' + first.get('sub_intent', '') not in routes:
        errors.append('route')
    if case['reply_to'] != first.get('reply_to'):
        errors.append('reply_to')
    # Some known items still need a variant: either classification or the
    # order extractor may ask, provided neither invents a selection.
    if case['clarify'] is not None and case['clarify'] != bool(first.get('clarification')):
        errors.append('clarification')
    if bool((parsed or {}).get('declared_constraints')) != case.get('declares', False):
        errors.append('declared constraints')
    if 'expected_queries' in case and [row['query'] for row in rows] != case['expected_queries']:
        errors.append('handler values')
    checks = [(0, case.get('clarification_contains', []))]
    checks.extend((check['unit'], check['contains'])
                  for check in case.get('clarification_checks', []))
    for index, requirements in checks:
        question = (rows[index].get('clarification') or '').casefold() if index < len(rows) else ''
        # Each requirement is a literal or a list of acceptable wordings.
        if any(not any(contains_literal(question, word) for word in
                       (requirement if isinstance(requirement, list) else [requirement]))
               for requirement in requirements):
            errors.append(f'selection choices (unit {index})')
    if any(value not in first.get('query', '') for value in case.get('query_contains', [])):
        errors.append('resolved identity')
    if 'expected_unit_count' in case and len(rows) != case['expected_unit_count']:
        errors.append('unit count')
    if 'expected_units' in case and not matches(rows, case['expected_units']):
        errors.append('typed decisions')
    if 'expected_basket_targets' in case:
        from chatbot_core.logic.action_resolver import resolve_action
        try:
            targets = [list(resolve_action(
                row['action'], basket=case['context']['basket'],
                focus=case['context'].get('basket_focus'),
            ).basket_targets) for row in rows]
            if targets != case['expected_basket_targets']:
                errors.append('basket targets')
        except (ValueError, KeyError, TypeError):
            errors.append('basket targets')
    if 'response_language' in case and (parsed or {}).get('response_language') != case['response_language']:
        errors.append('response language')
    for check in case.get('rewrite_checks', []):
        index = check['unit']
        rewrite = (rows[index].get('rephrased_sentence') or '').casefold() if index < len(rows) else ''
        if any(not any(contains_literal(rewrite, word) for word in
                       (requirement if isinstance(requirement, list) else [requirement]))
               for requirement in check.get('contains', [])):
            errors.append(f'English rewrite (unit {index})')
        if any(contains_literal(rewrite, word) for word in check.get('not_contains', [])):
            errors.append(f'English rewrite contains another unit (unit {index})')
    return errors


def evaluate_case(case, system):
    from chatbot_core.llm.chains import structured_chain
    from chatbot_core.llm.schemas import NormalizedClassifiedMessages

    started = time.monotonic()
    result = dict(case)
    try:
        response = structured_chain(NormalizedClassifiedMessages, system, include_raw=True).invoke({
            'input': json.dumps({'new_user_message': case['text'],
                                 'conversation_context': case['context']}, ensure_ascii=False),
        })
        parsed = response['parsed'].model_dump() if response.get('parsed') else None
        errors = validate_output(case, parsed)
        result.update(output=parsed, errors=errors, usage=response['raw'].usage_metadata)
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)[:300]
    result['latency_ms'] = round((time.monotonic() - started) * 1000)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize real provider calls')
    parser.add_argument('--env-file', default='.env.dev')
    parser.add_argument('--model')
    parser.add_argument('--output', default='/tmp/chatbot-live-contextual.json')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--cases', type=Path, default=ROOT / 'tests/fixtures/contextual_classification.json')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required to make provider calls')
    output = Path(args.output).resolve()
    if output.is_relative_to(ROOT):
        parser.error('Live evidence must be outside the repository')

    os.environ['DJANGO_SETTINGS_MODULE'] = 'tests.settings.integration'
    import django
    django.setup()
    from django.conf import settings
    from dotenv import dotenv_values
    from evaluate.datasets.loader import classification_documents
    from chatbot_core.logic.cafe.prompts.normalize_and_classify_prompt import SYSTEM_PROMPT
    from chatbot_core.logic.cafe.prompts.normalize_and_classify import SYSTEM_ID

    config = dotenv_values(args.env_file)
    settings.OPENAI_API_KEY = config.get('OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not settings.OPENAI_API_KEY:
        parser.error('OPENAI_API_KEY is not configured')
    settings.LLM_MODEL = args.model or config.get('LLM_MODEL') or 'gpt-6-luna'
    settings.LLM_TIMEOUT = 35
    settings.LLM_MAX_RETRIES = 0
    schema = {}
    for doc in classification_documents(ROOT / 'test_data'):
        if doc['payload']['enabled']:
            schema.setdefault(doc['intent'], {})[doc['sub_intent']] = {
                'description': doc['payload']['description'], 'examples': doc['payload'].get('examples', [])}
    system = SYSTEM_PROMPT + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    cases = json.loads(args.cases.read_text())
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        results = list(pool.map(lambda case: evaluate_case(case, system), cases))
    failures = [(row['id'], row.get('error') or row['errors'])
                for row in results if row.get('error') or row['errors']]
    totals = {key: sum((row.get('usage') or {}).get(key, 0) for row in results)
              for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    report = {
        'model': settings.LLM_MODEL, 'prompt_version': SYSTEM_ID,
        'prompt_sha256': hashlib.sha256(system.encode()).hexdigest(),
        'catalog_configuration': 'synthetic café and tea; test_data intent descriptions',
        'provider_calls': len(results), 'tokens': totals,
        'median_latency_ms': statistics.median(row['latency_ms'] for row in results),
        'monetary_cost': None, 'cost_note': 'Provider billing rates were not supplied.',
        'results': results,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'cases': len(results), 'failures': failures, 'report': str(output)}, ensure_ascii=False))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
