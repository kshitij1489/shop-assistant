"""Replay a persisted turn or recording export without live application I/O."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from chatbot_core.recording.replay import replay_export, replay_turn, ReplayUnavailable
from chatbot_core.recording.replay_codec import CodecError


class Command(BaseCommand):
    help = 'Replay one conversation graph or workflow step using recorded dependencies only.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant-id', required=True, type=int)
        parser.add_argument('--turn-id', required=True)
        parser.add_argument('--step-id')
        parser.add_argument('--input', type=Path, help='Turn/session export JSON; omit to read persisted events.')
        parser.add_argument('--output', type=Path, help='Write JSON report here; otherwise print to stdout.')

    def handle(self, *args, **options):
        arguments = {name: options[name] for name in ('tenant_id', 'turn_id', 'step_id')}
        try:
            if options['input']:
                with options['input'].open(encoding='utf-8') as source:
                    result = replay_export(json.load(source), **arguments)
            else:
                result = replay_turn(**arguments)
        except (ReplayUnavailable, CodecError, OSError, KeyError, TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if options['output']:
            options['output'].write_text(text + '\n', encoding='utf-8')
        else:
            self.stdout.write(text)
        if result['status'] != 'matched':
            raise CommandError(f'Replay {result["status"]}; see the JSON report.')
