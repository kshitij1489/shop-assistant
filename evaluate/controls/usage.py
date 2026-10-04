"""Runner UsageSource over collected application journals; absence is unknown."""
from evaluate.evidence.journal import read_journal
from evaluate.runner.ports import Usage
from .ownership import owned


class ApplicationUsage:
    def __init__(self, provisioner, identity_resolver, paths):
        self.provisioner, self.identity_resolver, self.paths = provisioner, identity_resolver, paths

    def usage(self, lease, request_id):
        identity = self.identity_resolver(lease)
        owned(lease, identity, self.provisioner)
        records = {}
        for path in self.paths:
            journal = read_journal(path)
            if not journal.intact:
                return None
            for row in journal.records:
                if (row.get('run_id'), row.get('scenario_instance_id'), row.get('attempt'), row.get('request_id')) != (
                        identity.run_id, identity.scenario_instance_id, identity.attempt, request_id):
                    continue
                previous = records.setdefault(row['event_id'], row)
                if previous != row:
                    return None
        rows = list(records.values())
        if not any(r['event'] == 'http.completed' for r in rows):
            return None
        starts = {r['call_id'] for r in rows if r['event'] == 'llm.started'}
        ends = {r['call_id']: r for r in rows if r['event'] == 'llm.completed'}
        if starts != set(ends) or any(r.get('total_tokens') is None for r in ends.values()):
            return Usage(tokens=None, cost_minor=None)
        return Usage(tokens=sum(r['total_tokens'] for r in ends.values()), cost_minor=None)
