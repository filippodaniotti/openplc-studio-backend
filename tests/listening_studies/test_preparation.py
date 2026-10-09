"""Run-scoped assembly tests without MongoDB, Redis, or browser dependencies."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

import numpy as np
import pytest
from plctestbench.file_wrapper import AudioFile
from plctestbench.human_listening import prepare_condition
from plctestbench.plc_testbench import PLCTestbench
from plctestbench.settings import HumanCalculatorSettings

from plc_platform_backend.listening_studies.studies_service import prepare_studies
from plc_platform_backend.listening_studies.studies_models import ListeningStudyView
from plc_platform_backend.modules.modules_models import Module, ModuleType
from plc_platform_backend.runs.runs_models import Run, RunDocument, RunStatus


def prepared_run(tmp_path, humans=1, simulators=1):
    modules = {
        ModuleType.PacketLossSimulator: [Module(name="BinomialPLS", node_ids=[], settings=[]) for _ in range(simulators)],
        ModuleType.PLCAlgorithm: [Module(name="ZerosPLC", node_ids=[], settings=[]) for _ in range(2)],
        ModuleType.OutputAnalyser: [Module(name="HumanCalculator", node_ids=[], settings=[]) for _ in range(humans)],
    }
    run = Run(
        id="a" * 24, author="test", name="Human run", status=RunStatus.RUNNING,
        created=datetime.utcnow(), updated=datetime.utcnow(),
        testbench_internal_id="internal", tracks=["one.wav", "two.wav"], modules=modules,
    )
    roots = []
    for track in range(2):
        directory = tmp_path / f"track-{track}"
        directory.mkdir()
        waveform = np.sin(np.arange(8000 * 6) * 0.1).astype(np.float32) * 0.2
        original = AudioFile(waveform, str(directory / "original.wav"), samplerate=8000)
        restored = AudioFile(waveform, str(directory / "restored.wav"), samplerate=8000)
        settings = HumanCalculatorSettings(trials_per_track=1)
        settings.add("fs", 8000)
        settings.add("packet_size", 80)
        data = prepare_condition(original, restored, np.arange(24000, 24080), settings)
        nodes = []
        for simulator in range(simulators):
            simulator_id = f"sim-{track}-{simulator}"
            run.modules[ModuleType.PacketLossSimulator][simulator].node_ids.append(simulator_id)
            for plc in range(2):
                plc_id = f"plc-{track}-{simulator}-{plc}"
                run.modules[ModuleType.PLCAlgorithm][plc].node_ids.append(plc_id)
                for human in range(humans):
                    node_id = f"human-{track}-{simulator}-{plc}-{human}"
                    run.modules[ModuleType.OutputAnalyser][human].node_ids.append(node_id)
                    nodes.append(SimpleNamespace(
                        get_id=lambda value=node_id: value,
                        get_data=lambda value=data: value,
                        get_file=lambda path=str(directory / f"{node_id}.pickle"): SimpleNamespace(get_path=lambda: path),
                        get_lost_samples_mask_node=lambda value=simulator_id: SimpleNamespace(get_id=lambda: value),
                        get_reconstructed_track_node=lambda value=plc_id: SimpleNamespace(get_id=lambda: value),
                    ))
        roots.append(SimpleNamespace(outputs=nodes))
    testbench = cast(PLCTestbench, SimpleNamespace(
        data_manager=SimpleNamespace(root_nodes=roots),
        get_nodes_by_depth=lambda root: {3: root.outputs},
    ))
    return run, testbench


def test_single_study_covers_all_tracks_and_plc_instances(tmp_path):
    run, testbench = prepared_run(tmp_path)
    studies = prepare_studies(run, testbench, str(tmp_path))
    assert len(studies) == 1
    assert (studies[0].human_module_index, studies[0].simulator_module_index) == (0, 0)
    for study in studies:
        assert study.trial_count == 2
        assert len(study.sources) == 4
        assert len({source.track_id for source in study.sources}) == 2
        assert {source.condition_id for source in study.sources} == {"plc-0", "plc-1"}
        assert Path(study.artifact_directory, "config.yaml").is_file()
        assert study.audio_files
        public = ListeningStudyView(**study.model_dump()).model_dump()
        assert "sources" not in public and "artifact_directory" not in public
    again = prepare_studies(run, testbench, str(tmp_path))
    assert [study.id for study in again] == [study.id for study in studies]
    # Another run may reuse preparation, but never published study identities.
    different = prepare_studies(run.model_copy(update={"id": "b" * 24}), testbench, str(tmp_path))
    assert set(study.id for study in studies).isdisjoint(study.id for study in different)


def test_ambiguous_module_mappings_fail_instead_of_merging_participants(tmp_path):
    run, testbench = prepared_run(tmp_path)
    node_id = run.modules[ModuleType.PLCAlgorithm][0].node_ids[0]
    run.modules[ModuleType.PLCAlgorithm][1].node_ids.append(node_id)
    with pytest.raises(ValueError, match="unambiguous"):
        prepare_studies(run, testbench, str(tmp_path))


@pytest.mark.parametrize('humans,simulators', [(2, 1), (1, 2), (1, 0)])
def test_preparation_rejects_invalid_human_cardinality(tmp_path, humans, simulators):
    run, testbench = prepared_run(tmp_path, humans=humans, simulators=simulators)
    with pytest.raises(ValueError, match='exactly one'):
        prepare_studies(run, testbench, str(tmp_path))


def test_human_launch_waits_and_does_not_publish_completion(tmp_path, monkeypatch):
    for key, value in {
        "MONGO_INITDB_ROOT_USERNAME": "test", "MONGO_INITDB_ROOT_PASSWORD": "test",
        "PLC_ROOT_FOLDER": str(tmp_path), "PLUGINS_DIRECTORY": str(tmp_path),
        "REDIS_URL": "redis://localhost:6379/0",
    }.items():
        monkeypatch.setenv(key, value)
    from plc_platform_backend.runs.runs_service import _launch_run

    run, testbench = prepared_run(tmp_path)
    queued = run.model_copy(update={"status": RunStatus.QUEUED})
    running_document = RunDocument(**run.model_dump())
    waiting_document = RunDocument(**run.model_copy(update={"status": RunStatus.AWAITING_INPUT}).model_dump())
    repository = SimpleNamespace(transition_status=AsyncMock(side_effect=[running_document, waiting_document]))
    redis = SimpleNamespace(publish=AsyncMock())
    service = SimpleNamespace(testbench_settings=SimpleNamespace(root_folder=str(tmp_path), progress_monitor=None))
    fake_testbench = SimpleNamespace(data_manager=SimpleNamespace(root_nodes=[]), run=MagicMock())
    with (
        patch("plc_platform_backend.runs.runs_service.PLCTestbench", return_value=fake_testbench),
        patch("plc_platform_backend.runs.runs_service.prepare_studies", return_value=[]) as prepare,
        patch("plc_platform_backend.runs.runs_service.publish_prepared_studies", new_callable=AsyncMock) as publish,
    ):
        asyncio.run(_launch_run(queued, repository, service, redis))
    prepare.assert_called_once()
    publish.assert_awaited_once()
    assert repository.transition_status.await_args_list[-1].args == (run.id, RunStatus.RUNNING, RunStatus.AWAITING_INPUT)
    assert all('"type":"run.complete"' not in call.args[1] for call in redis.publish.await_args_list)
