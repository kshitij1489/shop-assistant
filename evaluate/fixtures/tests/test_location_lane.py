"""Foreign-address isolation: exclusive scheduling and force-cleanup reclaim."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings

from orders import models as om
from evaluate.contracts.models import RunConfiguration, ExecutionIdentity, ModelNames, ScenarioPlan
from evaluate.contracts.interfaces import Blocked
from evaluate.datasets.loader import load_dataset, read_json
from evaluate.identity import instance_id
from evaluate.fixtures.provision import DjangoProvisioner, FOREIGN_ADDRESS_ID
from evaluate.scenarios.runtime import LocalRuntime
from evaluate.scenarios.controls import DatasetControls
from evaluate.runner.scheduler import WorkItem

ROOT = Path(__file__).resolve().parents[2]


class ForeignAddressLaneTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = ScenarioPlan.model_validate(read_json(ROOT / 'scenarios/execution.plan.json'))
        cls.bundle = load_dataset(ROOT.parent / 'test_data', cls.plan)
        cls.case = next(s for s in cls.bundle.scenarios if s.source_id == 's119_foreign_address_id')

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = LocalRuntime(self.temp.name)
        self.provisioner = DjangoProvisioner(self.runtime)
        self.runtime.provisioner = self.provisioner
        self.controls = DatasetControls(self.provisioner, self.plan)
        self.config = RunConfiguration(
            run_id='lane-tests', scenario_plan_version=self.plan.version,
            dataset_directory=str(ROOT.parent / 'test_data'),
            models=ModelNames(chat='unused', translate='unused', analytics='unused'))

    def _provision(self):
        identity = ExecutionIdentity(
            run_id=self.config.run_id, scenario_id=self.case.scenario_id,
            scenario_instance_id=instance_id(self.config.run_id, self.case.scenario_id, 0), attempt=1)
        with override_settings(LOCATION_PROVIDER='google'):
            lease = self.provisioner.provision(self.config, self.case, identity)
        self.addCleanup(self.runtime.release, lease)
        self.controls.before_turn(lease, identity, self.case, None)
        return lease

    def test_unique_canaries_do_not_require_exclusive_scheduling(self):
        self.assertFalse(WorkItem(self.case, 'instance').exclusive)

    def test_preserved_attempts_have_distinct_visible_canaries(self):
        first = self._provision()
        first_id = self.provisioner.inspect(first)['identities']['addresses']['foreign']
        self.provisioner.finish(first, succeeded=False)
        second = self._provision()
        second_id = self.provisioner.inspect(second)['identities']['addresses']['foreign']
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(om.CustomerAddress.objects.filter(pk=first_id).exists())
        self.assertTrue(om.CustomerAddress.objects.filter(pk=second_id).exists())
        self.provisioner.cleanup(first, force=True)
        self.assertFalse(om.CustomerAddress.objects.filter(pk=first_id).exists())
        self.assertTrue(om.CustomerAddress.objects.filter(pk=second_id).exists())
        self.provisioner.cleanup(second, force=True)
