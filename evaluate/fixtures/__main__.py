"""Evaluation-only provision/inspect/cleanup CLI. Never sends chat or model calls."""
import argparse
import json
import os
from pathlib import Path
from uuid import UUID


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', help='Django settings module; otherwise DJANGO_SETTINGS_MODULE must be set')
    sub = parser.add_subparsers(dest='operation', required=True)
    mapping = sub.add_parser('plan', help='Print all reviewed cases and runtime setup blockers, no database needed')
    mapping.add_argument('--dataset', type=Path, default=Path('test_data'))
    provision = sub.add_parser('provision', help='Create one evaluation-owned scenario and apply its setup actions')
    provision.add_argument('--config', type=Path, required=True)
    provision.add_argument('--scenario', required=True, help='Namespaced dataset scenario ID')
    provision.add_argument('--repetition', type=int, default=0)
    provision.add_argument('--attempt', type=int, default=1)
    provision.add_argument('--output', type=Path, required=True, help='New manifest file; never overwritten')
    for name in ('inspect', 'cleanup'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--manifest', type=Path, required=True)
    for cmd in (provision, sub.choices['inspect'], sub.choices['cleanup']):
        cmd.add_argument('--state-dir', type=Path, required=True, help='Dedicated evaluation provider state directory')
    args = parser.parse_args(argv)
    from evaluate.scenarios.plan import execution_mapping, load_plan
    from evaluate.contracts.interfaces import Blocked, Lease
    from evaluate.contracts.models import RunConfiguration, ExecutionIdentity
    from evaluate.datasets.loader import read_json
    from evaluate.identity import instance_id
    if args.operation == 'plan':
        print(json.dumps(execution_mapping(args.dataset), indent=2))
        return 0
    if args.settings:
        os.environ['DJANGO_SETTINGS_MODULE'] = args.settings
    if not os.environ.get('DJANGO_SETTINGS_MODULE'):
        parser.error('--settings or DJANGO_SETTINGS_MODULE is required')
    import django
    django.setup()
    from evaluate.fixtures.provision import DjangoProvisioner
    from evaluate.scenarios.controls import DatasetControls
    from evaluate.scenarios.runtime import LocalRuntime
    runtime = LocalRuntime(args.state_dir)
    owner = DjangoProvisioner(runtime)
    lease = None
    try:
        if args.operation == 'provision':
            config = RunConfiguration.model_validate(read_json(args.config))
            plan, bundle = load_plan(config.dataset_directory)
            scenario = next((s for s in bundle.scenarios if s.scenario_id == args.scenario), None)
            if scenario is None or args.repetition < 0 or args.attempt < 1:
                raise Blocked('Unknown scenario or invalid repetition/attempt')
            identity = ExecutionIdentity(run_id=config.run_id, scenario_id=scenario.scenario_id,
                scenario_instance_id=instance_id(config.run_id, scenario.scenario_id, args.repetition), attempt=args.attempt)
            # Reserve output before database mutation, so a path error cannot
            # strand created resources without a reviewable ownership handle.
            with args.output.open('x') as output:
                try:
                    lease = owner.provision(config, scenario, identity)
                    DatasetControls(owner, plan).before_turn(lease, identity, scenario, None)
                except BaseException:
                    if lease:
                        owner.finish(lease, succeeded=False)
                        json.dump(owner.inspect(lease), output, indent=2)
                    else:
                        json.dump({'status': 'blocked_before_provisioning'}, output)
                    raise
                json.dump(owner.inspect(lease), output, indent=2)
            print(json.dumps({'manifest': str(args.output), 'status': 'provisioned', 'chat_turns_sent': 0}))
        else:
            manifest = read_json(args.manifest)
            lease = Lease(str(UUID(manifest['lease_id'])), manifest['scenario_instance_id'])
            if args.operation == 'inspect':
                print(json.dumps(owner.inspect(lease), indent=2))
            else:
                owner.cleanup(lease, force=True)
                # Also handles a prior crash after database cleanup.
                runtime.cleanup_files(lease)
                print(json.dumps({'lease_id': lease.handle, 'status': 'cleaned'}))
        return 0
    except Blocked as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}))
        return 2
    except (OSError, ValueError, KeyError):
        print(json.dumps({'status': 'error', 'reason': 'Invalid inputs or local artifact operation failed'}))
        return 1
    finally:
        if lease:
            runtime.release(lease)


if __name__ == '__main__':
    raise SystemExit(main())
