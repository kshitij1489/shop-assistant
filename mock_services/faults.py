"""Deterministic fault injection for the menu, payment and POS simulators.

Rules are process-local and reset on restart. Global rules are shared by every
mock account (legacy behaviour). Account-scoped rules take precedence for that
account only, so one evaluation conversation cannot consume another's fault.
"""
import json
import threading
import time
from collections import Counter, defaultdict

SERVICES = ('menu', 'payment', 'pos')
LIMITS = {'delay_ms': 30000, 'fail_next': 100000, 'timeout_after_commit_next': 100000}


def validate_rules(payload):
    if not isinstance(payload, dict) or set(payload) - set(SERVICES):
        raise ValueError('Expected menu, payment or pos fault rules.')
    for rule in payload.values():
        if not isinstance(rule, dict) or set(rule) - set(LIMITS):
            raise ValueError('Unknown fault rule.')
        if any(type(v) is not int or not 0 <= v <= LIMITS[k] for k, v in rule.items()):
            raise ValueError('Fault values must be bounded nonnegative integers.')
    return payload


class Faults:
    def __init__(self):
        self.lock = threading.Lock()
        self.rules = {}
        self.account_rules = {}
        self.counts = Counter()
        self.account_counts = defaultdict(Counter)

    def configure(self, payload, account=None):
        """Replace the global rules, or one account's rules; `{}` clears them."""
        validate_rules(payload)
        with self.lock:
            if account is None:
                self.rules = payload
            elif payload:
                self.account_rules[account] = payload
            else:
                self.account_rules.pop(account, None)

    def _select(self, service, account):
        """Account rules for a service shadow the global rule for that service."""
        scoped = self.account_rules.get(account)
        if scoped is not None and service in scoped:
            return scoped[service], True
        return self.rules.get(service, {}), False

    def take(self, service, creating=False, account=None):
        """Consume at most one fault for this request; returns (fail, lost)."""
        with self.lock:
            rule, is_scoped = self._select(service, account)
            counters = [self.account_counts[account]] if account is not None else []
            if not is_scoped:
                counters.append(self.counts)
            for counter in counters:
                counter[service + '.requests'] += 1
            delay = rule.get('delay_ms', 0) / 1000
            failure = rule.get('fail_next', 0) > 0
            lost = creating and not failure and rule.get('timeout_after_commit_next', 0) > 0
            for active, key in ((failure, 'fail_next'), (lost, 'timeout_after_commit_next')):
                if active:
                    rule[key] -= 1
                    for counter in counters:
                        counter[service + '.' + key] += 1
        time.sleep(delay)
        return failure, lost

    def summary(self, account=None):
        with self.lock:
            if account is not None:
                payload = dict(account=account, faults=self.account_rules.get(account, {}),
                               counters=dict(self.account_counts.get(account, {})),
                               isolated_services=sorted(self.account_rules.get(account, {})))
            else:
                payload = dict(faults=self.rules, counters=dict(self.counts),
                               accounts={key: dict(faults=value, counters=dict(self.account_counts.get(key, {})))
                                         for key, value in self.account_rules.items()})
            return json.loads(json.dumps(payload))
