"""Read-only manifest creation. Call after artifact outputs are placed outside source."""
from datetime import datetime, timezone
from pathlib import Path

from evaluate.contracts.models import RunConfiguration, RunManifest, ScenarioPlan
from evaluate.datasets.loader import DatasetBundle
from evaluate.identity import application_revision, canonical_hash


def behavioral_configuration(config: RunConfiguration, *, cache_mode: str | None = None,
                             runner_options: dict | None = None,
                             effective_models=None) -> dict:
    """Hash payload for behavioral config only — run_id is identity, not behavior."""
    payload = config.model_dump(mode="json")
    payload.pop("run_id", None)
    if effective_models is not None:
        payload["models"] = (effective_models.model_dump(mode="json")
                             if hasattr(effective_models, "model_dump") else effective_models)
        payload["effective_models"] = payload["models"]
    if cache_mode is not None:
        payload["cache_mode"] = cache_mode
    if runner_options is not None:
        payload["runner_options"] = runner_options
    return payload


def build_manifest(repository: Path, config: RunConfiguration,
                   plan: ScenarioPlan, bundle: DatasetBundle,
                   cache_mode: str | None = None,
                   runner_options: dict | None = None,
                   effective_models=None) -> RunManifest:
    if config.scenario_plan_version != plan.version or any(s.scenario_plan_version != plan.version for s in bundle.scenarios):
        raise ValueError("configuration, normalized scenarios and plan versions must agree")
    models = effective_models if effective_models is not None else config.models
    return RunManifest(
        run_id=config.run_id, application=application_revision(repository),
        dataset_hashes=bundle.dataset_hashes,
        configuration_hash=canonical_hash(behavioral_configuration(
            config, cache_mode=cache_mode, runner_options=runner_options,
            effective_models=effective_models)),
        scenario_plan_hash=canonical_hash(plan), scenario_plan_version=plan.version,
        models=models, scenario_ids=[s.scenario_id for s in bundle.scenarios],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
