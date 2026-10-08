"""Generate knowledge files from the canonical catalog and authored knowledge."""
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from chatbot_core.configuration_files import knowledge_exports


class Command(BaseCommand):
    help = 'Generate menu knowledge and combined knowledge exports from a configuration directory.'

    def add_arguments(self, parser):
        parser.add_argument('directory', type=Path)
        parser.add_argument('--check', action='store_true')

    def handle(self, directory, check=False, **options):
        try:
            for name, data in knowledge_exports(directory).items():
                output = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
                path = directory / name
                if check:
                    if not path.exists() or path.read_text() != output:
                        raise CommandError(f'{name} differs from the canonical configuration.')
                else:
                    path.write_text(output)
        except (ValueError, KeyError, OSError) as exc:
            raise CommandError(str(exc)) from exc
