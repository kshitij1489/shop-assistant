"""Offline execution of recorded graph turns or individual workflow nodes.

Only registered graph code executes. Every external dependency is consumed in
order, with an exact input match, from immutable evidence. No live fallback.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import sys
from types import SimpleNamespace

from .capture import snapshot
from .replay_capture import _active
from .replay_codec import CodecError, Encoder, decode, encode

STEP_NAMES = (
    'prepare_followup', 'classify_intents', 'resolve_intent', 'cancel_pending',
    'pause_pending', 'match_followup', 'process_followup', 'process_new_query',
    'collect_reply', 'choose_followup', 'render_response',
)


class ReplayUnavailable(ValueError):
    """Evidence cannot establish a replayable input/dependency set."""


class ReplayMismatch(BaseException):
    # Business handlers catch Exception to recover provider failures. A replay
    # mismatch must stop execution rather than look like a provider failure.
    def __init__(self, message, *, expected=None, actual=None):
        super().__init__(message)
        self.expected, self.actual = expected, actual


class RecordedDependencyError(Exception):
    pass


def _audit(event, args):
    if _active.get() is None:
        return
    if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn')) or event in {
            'os.system', 'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.truncate'}:
        raise ReplayMismatch(f'Live I/O is forbidden during replay: {event}')
    if event == 'open':
        import os
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(char in mode for char in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            raise ReplayMismatch('File writes are forbidden during replay')


# Context-local audit checks do not patch provider functions or affect live turns.
sys.addaudithook(_audit)


@contextmanager
def _no_database():
    from django.db import connections

    def reject(execute, sql, params, many, context):
        raise ReplayMismatch('Live database access is forbidden during replay')

    with ExitStack() as stack:
        for connection in connections.all():
            stack.enter_context(connection.execute_wrapper(reject))
        yield


def _difference(expected, actual, path='$'):
    """Return the first actionable mismatch without flooding a report with state."""
    if type(expected) is not type(actual):
        return {'path': path, 'expected': expected, 'actual': actual}
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return {'path': path, 'expected_keys': sorted(expected), 'actual_keys': sorted(actual)}
        for key in expected:
            difference = _difference(expected[key], actual[key], f'{path}.{key}')
            if difference:
                return difference
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return {'path': path, 'expected_length': len(expected), 'actual_length': len(actual)}
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _difference(left, right, f'{path}[{index}]')
            if difference:
                return difference
    elif expected != actual:
        return {'path': path, 'expected': expected, 'actual': actual}
    return None


def _payloads(events, name):
    return [event['payload'] for event in events if event['event'] == name]


class ReplayTape:
    def __init__(self, envelopes, *, tenant_id, turn_id):
        from .inspection import _segment
        from .writer import Envelope
        from uuid import UUID

        turn_id = str(UUID(str(turn_id)))
        events = []
        try:
            for raw in envelopes:
                envelope = Envelope.model_validate(raw)
                if str(envelope.turn_id) != turn_id:
                    continue
                if envelope.tenant_id != str(tenant_id):
                    raise ReplayUnavailable('Recording does not belong to the selected tenant.')
                events.append(deepcopy(raw))
        except (ValueError, TypeError) as exc:
            raise ReplayUnavailable(f'Invalid recording envelope: {exc}') from exc
        if not events:
            raise ReplayUnavailable('Recorded turn not found.')
        if len({(event['channel'], event['session_id']) for event in events}) != 1:
            raise ReplayUnavailable('Turn has inconsistent session scope.')
        primary = {event['segment_id'] for event in events if event['segment_kind'] == 'turn'}
        if len(primary) != 1:
            raise ReplayUnavailable('Replay requires exactly one original turn segment.')
        self.events = sorted((e for e in events if e['segment_id'] in primary), key=lambda e: e['sequence'])
        if len({e['sequence'] for e in self.events}) != len(self.events) or len({e['event_id'] for e in self.events}) != len(self.events):
            raise ReplayUnavailable('Duplicate recording events cannot be replayed.')
        diagnostic = _segment({
            'turn_id': e['turn_id'], 'segment_id': e['segment_id'], 'sequence': e['sequence'],
            'event': e['event'], 'occurred_at': Envelope.model_validate(e).occurred_at,
            'envelope__segment_kind': e['segment_kind'],
            'completion': e['payload'] if e['event'] == 'recording.completed' else None,
        } for e in self.events)
        if not diagnostic['complete']:
            raise ReplayUnavailable('Original turn evidence is incomplete: ' + ' '.join(diagnostic['issues']))
        if _payloads(self.events, 'replay.unavailable'):
            raise ReplayUnavailable('Capture could not serialize all replay state/dependencies.')
        self.execution_status = diagnostic['execution_status']
        self.identity = {key: self.events[0][key] for key in ('tenant_id', 'channel', 'session_id', 'turn_id')}
        self.steps = []
        active = None
        for event in self.events:
            payload = event['payload']
            if event['event'] == 'replay.step.started':
                if active is not None or payload['name'] not in STEP_NAMES:
                    raise ReplayUnavailable('Invalid or unsupported replay step.')
                active = {'id': payload['step_id'], 'name': payload['name'], 'dependencies': []}
                self.steps.append(active)
            elif event['event'] in {'replay.step.input', 'replay.step.output', 'replay.step.error_state'}:
                if active is None:
                    raise ReplayUnavailable('Replay step checkpoint is missing its start.')
                values = decode(payload['snapshot'])
                if values['step_id'] != active['id']:
                    raise ReplayUnavailable('Replay step checkpoint identity mismatch.')
                checkpoint = event['event'].rsplit('.', 1)[-1]
                if checkpoint in active:
                    raise ReplayUnavailable('Duplicate replay step checkpoint.')
                active[checkpoint] = payload['snapshot']
            elif event['event'] == 'replay.dependency.started':
                if active is None or payload['step_id'] != active['id']:
                    raise ReplayUnavailable('Replay dependency is missing its parent step.')
                if active['dependencies'] and 'end' not in active['dependencies'][-1]:
                    raise ReplayUnavailable('Overlapping replay dependencies are unsupported.')
                active['dependencies'].append({'start': payload})
            elif event['event'] in {'replay.dependency.completed', 'replay.dependency.failed'}:
                if active is None or not active['dependencies']:
                    raise ReplayUnavailable('Replay dependency result has no input.')
                dependency = active['dependencies'][-1]
                if ('end' in dependency or dependency['start']['call_id'] != payload['call_id'] or
                        dependency['start']['name'] != payload['name'] or payload['step_id'] != active['id']):
                    raise ReplayUnavailable('Replay dependency call identity mismatch.')
                dependency.update(end=payload, failed=event['event'].endswith('failed'))
            elif event['event'] in {'replay.step.completed', 'replay.step.failed'}:
                if active is None or payload['step_id'] != active['id']:
                    raise ReplayUnavailable('Replay step completion identity mismatch.')
                active['error'] = payload.get('error')
                active = None
        if not self.steps:
            raise ReplayUnavailable('This recording predates replay checkpoints, or ended before the graph started.')
        if active is not None or any('input' not in step or any('end' not in d for d in step['dependencies'])
                                     or ('output' not in step and not step.get('error')) for step in self.steps):
            raise ReplayUnavailable('Replay checkpoints or dependency results are missing.')
        if len({step['id'] for step in self.steps}) != len(self.steps):
            raise ReplayUnavailable('Duplicate replay step identifiers.')
        configuration = _payloads(self.events, 'configuration.loaded')
        if len(configuration) != 1:
            raise ReplayUnavailable('Replay needs the captured runtime configuration.')
        self.configuration = configuration[0]['configuration']
        self.turn_input = _payloads(self.events, 'replay.turn.input')
        self.turn_output = _payloads(self.events, 'replay.turn.output')

    def execute(self, *, step_id=None):
        return ReplayExecution(self).execute(step_id=step_id)


class ReplayExecution:
    def __init__(self, tape):
        self.tape = tape
        self.position = self.dependency_position = 0
        self.selected = tape.steps
        self.active_step = None
        self.results = []
        self.consumed = 0

    def consume(self, name, args, kwargs):
        dependencies = self.active_step['dependencies'] if self.active_step else []
        if self.dependency_position >= len(dependencies):
            raise ReplayMismatch(f'Unexpected dependency: {name}')
        entry = dependencies[self.dependency_position]
        before = entry['start']
        encoder = Encoder()
        actual = snapshot(encoder.encode({'args': args, 'kwargs': kwargs}))
        difference = _difference(before['snapshot'], actual)
        if before['name'] != name or difference:
            raise ReplayMismatch(f'Dependency input mismatch: expected {before["name"]}, requested {name}',
                                 expected=before['snapshot'], actual=actual)
        self.dependency_position += 1
        self.consumed += 1
        after = decode(entry['end']['snapshot'], existing=encoder.objects)
        if entry['failed']:
            error = entry['end']['error']
            from chatbot_core.logic.cafe.prompts.normalize_and_classify import NormalizationClassificationError
            allowed = {'NormalizationClassificationError': NormalizationClassificationError,
                       'ValueError': ValueError, 'RuntimeError': RuntimeError,
                       'TypeError': TypeError, 'KeyError': KeyError}
            cls = allowed.get(error['type'])
            if cls is None:
                raise ReplayMismatch(f'Unsupported recorded dependency failure: {error["type"]}')
            raise cls(*after['error_args'])
        return after['result']

    def run_step(self, function, state, context, has_runtime):
        if self.position >= len(self.selected):
            raise ReplayMismatch(f'Unexpected workflow step: {function.__name__}')
        expected = self.selected[self.position]
        if function.__name__ != expected['name']:
            raise ReplayMismatch(f'Workflow order mismatch: expected {expected["name"]}, requested {function.__name__}')
        self.active_step, self.dependency_position = expected, 0
        self.position += 1
        result = None
        error = None
        with _no_database():
            try:
                result = function(state, SimpleNamespace(context=context)) if has_runtime else function(state)
            except Exception as exc:
                error = snapshot(exc)
        if self.dependency_position != len(expected['dependencies']):
            raise ReplayMismatch(f'Unused recorded dependencies in {expected["name"]}')
        if error is not None or expected.get('error'):
            difference = _difference(expected.get('error'), error)
            if not difference and expected.get('error_state'):
                difference = _difference(expected['error_state'],
                    snapshot(encode({'step_id': expected['id'], 'state': state})))
            self.results.append({'step_id': expected['id'], 'name': expected['name'],
                                 'matched': not difference, 'difference': difference, 'error': error})
            if difference:
                raise ReplayMismatch('Workflow failure differs from the recording', expected=expected.get('error'), actual=error)
            raise RecordedDependencyError(error['message'])
        actual = snapshot(encode({'step_id': expected['id'], 'state': state, 'result': result}))
        difference = _difference(expected['output'], actual)
        self.results.append({'step_id': expected['id'], 'name': expected['name'],
                             'matched': difference is None, 'difference': difference})
        return result

    def execute(self, *, step_id):
        from chatbot_core.logic.cafe.workflow import graph
        from chatbot_core.runtime_configuration import RuntimeConfiguration, _active as configuration_context
        from .capture import _active as recording_context
        from inspect import signature

        if step_id is not None:
            self.selected = [s for s in self.tape.steps if s['id'] == str(step_id)]
            if not self.selected:
                raise ReplayUnavailable('Selected step does not belong to this turn.')
            initial = decode(self.selected[0]['input'])
        else:
            if len(self.tape.turn_input) != 1:
                raise ReplayUnavailable('Turn replay needs a captured graph starting state.')
            initial = decode(self.tape.turn_input[0]['snapshot'])
        config = self.tape.configuration
        configuration = RuntimeConfiguration(**config) if config is not None else None
        report = {'format': 'studio-desk.conversation-replay', 'format_version': 1,
                  'scope': self.tape.identity, 'step_id': str(step_id) if step_id else None,
                  'status': 'matched', 'steps': self.results, 'dependencies_consumed': 0,
                  'boundary': 'conversation_graph', 'side_effects': 'recorded_fixtures',
                  'recorded_turn_status': self.tape.execution_status}
        # Build/import graph before installing I/O guards (imports may write pyc).
        compiled = graph.get_conversation_graph()
        with ExitStack() as stack:
            for variable, value in ((recording_context, None), (configuration_context, configuration), (_active, self)):
                token = variable.set(value)
                stack.callback(variable.reset, token)
            try:
                with _no_database():
                    if step_id is not None:
                        function = getattr(graph, self.selected[0]['name'])
                        result = self.run_step(function, initial['state'], initial['context'],
                                               'runtime' in signature(function).parameters)
                        report['result'] = snapshot(encode({'state': initial['state'], 'result': result}))
                    else:
                        result = compiled.invoke(initial['state'], context=initial['context'],
                            config={'recursion_limit': 10000, 'max_concurrency': 1})
                        report['result'] = snapshot(encode({'state': result}))
                        report['response'] = result.get('response')
                        report['delivered_question'] = result.get('delivered_question')
                        if len(self.tape.turn_output) != 1:
                            raise ReplayMismatch('Recording has no graph outcome for this successful execution.')
                        report['difference'] = _difference(self.tape.turn_output[0]['snapshot'], report['result'])
                        if report['difference']:
                            report['status'] = 'diverged'
            except RecordedDependencyError as exc:
                report['recorded_failure'] = str(exc)
            except ReplayMismatch as exc:
                report.update(status='mismatch', error=str(exc), difference=_difference(exc.expected, exc.actual))
            except Exception as exc:
                report.update(status='error', error=snapshot(exc))
        report['dependencies_consumed'] = self.consumed
        if report['status'] == 'matched':
            if self.position != len(self.selected):
                report.update(status='mismatch', error='Execution left recorded workflow steps unused.')
            elif any(not step['matched'] for step in self.results):
                report['status'] = 'diverged'
        return report


def replay_turn(*, tenant_id, turn_id, step_id=None):
    from .inspection import recorded_events
    events = list(recorded_events(tenant_id=tenant_id).filter(turn_id=turn_id)
                  .values_list('envelope', flat=True))
    return ReplayTape(events, tenant_id=tenant_id, turn_id=turn_id).execute(step_id=step_id)


def replay_export(document, *, tenant_id, turn_id, step_id=None):
    if document.get('format') != 'studio-desk.conversation-recording' or document.get('format_version') != 1:
        raise ReplayUnavailable('Unsupported recording export format.')
    if str(document.get('scope', {}).get('tenant_id')) != str(tenant_id):
        raise ReplayUnavailable('Export does not belong to the selected tenant.')
    return ReplayTape([row['envelope'] for row in document['events']],
                      tenant_id=tenant_id, turn_id=turn_id).execute(step_id=step_id)
