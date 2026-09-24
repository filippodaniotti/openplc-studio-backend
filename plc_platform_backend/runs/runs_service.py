from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import pickle
import tarfile
import threading
import traceback
from functools import lru_cache

import numpy as np
import plctestbench.loss_simulator
import plctestbench.output_analyser
import plctestbench.plc_algorithm
import redis.asyncio as aioredis
from plctestbench.file_wrapper import OutputAnalysis
from plctestbench.models import DBPlatform, TestbenchConfiguration
from plctestbench.output_analyser import PEAQData, SimpleCalculatorData
from plctestbench.plc_testbench import PLCTestbench
from plctestbench.settings import CrossfadeSettings, OriginalAudioSettings
from plctestbench.utils import get_class
from plctestbench.worker import OriginalAudio

from plc_platform_backend import actors
from plc_platform_backend.assets.assets_models import TestbenchNodeDepth
from plc_platform_backend.assets.assets_repository import (
    AssetsRepository,
    get_assets_repository,
)
from plc_platform_backend.assets.assets_service import AssetsService, get_assets_service
from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.commons.interceptable_tqdm import InterceptableTqdm
from plc_platform_backend.commons.redis_client import get_redis_client
from plc_platform_backend.modules.modules_models import (
    Module,
    ModuleParameter,
    ModuleType,
)
from plc_platform_backend.modules.modules_validator import ModuleConfigValidator
from plc_platform_backend.runs.runs_models import (
    NodeProgress,
    Run,
    RunCompletionMessage,
    RunCreateDto,
    RunPage,
    RunProgressMessage,
    RunStateChangeMessage,
    RunStatus,
)
from plc_platform_backend.runs.runs_repository import (
    RunsRepository,
    get_runs_repository,
)
from plc_platform_backend.runs.runs_ws import (
    RUN_COMPLETION_CHANNEL,
    RUN_PROGRESS_CHANNEL,
    RUN_STATE_CHANGE_CHANNEL,
)

from plc_platform_backend.modules.modules_service import ModuleService
from plc_platform_backend.runs.runs_models import (
    RunConfigDto,
    RunConfigValidationError,
)

_PROGRESS_POLL_INTERVAL = 0.1
logger = logging.getLogger(__name__)


class RunNotDeletableError(Exception):
    pass


class RunNotExecutableError(Exception):
    pass


class RunQueueError(Exception):
    pass


class RunPreparationError(Exception):
    pass


def _seed_progress_state(testbench: PLCTestbench) -> dict[str, NodeProgress]:
    """
    Walks every node of every tree in the run and seeds an entry for it,
    so nodes that haven't started yet (and nodes that finish between two
    polls) still show up in every RunProgressMessage.
    """
    progress_state: dict[str, NodeProgress] = {}
    for root_node in testbench.data_manager.root_nodes:
        levels = testbench.get_nodes_by_depth(root_node)
        for nodes in levels.values():
            for node in nodes:
                node_id = node.get_id()
                progress_state[node_id] = NodeProgress(
                    description=str(node.get_worker()),
                    node_id=node_id,
                    current=0,
                    total=None,
                )
    return progress_state


def _get_module_parameter(settings, parameter):
    return [s.value for s in settings if s.name == parameter][0]


def _hydrate_crossfade_settings(crossfade_list: list) -> list[CrossfadeSettings]:
    """Idrata una lista di crossfade settings da dizionari a oggetti CrossfadeSettings"""
    result = []
    for xf in crossfade_list:
        crossfade_settings_cls = getattr(plctestbench.settings, xf["name"])
        result.append(
            crossfade_settings_cls(
                **{xfs["name"]: xfs["value"] for xfs in xf["settings"]}
            )
        )
    return result


def _get_hydrated_module_settings(
    settings: list[ModuleParameter], run_service: RunsService
):
    hydrated_module_settings = []
    for s in settings:
        advanced_plc_band_settings: dict[
            str, list[plctestbench.plc_algorithm.PLCAlgorithm]
        ] = {}
        advanced_plc_frequencies: dict[str, list[int]] = {}
        if s.name == "crossfade":
            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=_hydrate_crossfade_settings(s.value))
            )
        elif s.name == "fade_in":
            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=_hydrate_crossfade_settings(s.value))
            )
        elif s.name == "crossfade_frequencies" and s.value:
            try:
                crossfade_frequencies = [int(f) for f in s.value]
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid crossfade frequencies: {s.value}") from error
            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=crossfade_frequencies)
            )
        elif s.name == "crossover_order" and s.value:
            try:
                crossover_order = int(s.value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid crossover order: {s.value}") from error
            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=crossover_order)
            )
        elif s.name == "band_settings":
            for band in {"linked", "mid", "side", "left", "right"}:
                if band not in s.value.keys():
                    continue

                stereo_image_processing = _get_module_parameter(
                    settings, "stereo_image_processing"
                )
                channel_link = _get_module_parameter(settings, "channel_link")

                if stereo_image_processing == "dual_mono" and band not in {
                    "left",
                    "right",
                }:
                    continue

                if stereo_image_processing == "mid_side" and band not in {
                    "mid",
                    "side",
                }:
                    continue

                if channel_link and band != "linked":
                    continue

                advanced_plc_band_settings[band] = []
                for algorithm in s.value[band]:
                    algorithm_settings_cls = getattr(
                        plctestbench.settings,
                        run_service.get_module_settings_class_name(algorithm["name"]),
                    )
                    hydrated_algorithm_settings = _get_hydrated_module_settings(
                        [
                            ModuleParameter(name=as_["name"], value=as_["value"])
                            for as_ in algorithm["settings"]
                        ],
                        run_service,
                    )
                    advanced_plc_band_settings[band].append(
                        algorithm_settings_cls(
                            **{
                                has.name: has.value
                                for has in hydrated_algorithm_settings
                            }
                        )
                    )

            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=advanced_plc_band_settings)
            )
        elif s.name == "frequencies":
            for band in ["linked", "mid", "side", "left", "right"]:
                if band not in s.value.keys():
                    continue

                stereo_image_processing = _get_module_parameter(
                    settings, "stereo_image_processing"
                )
                channel_link = _get_module_parameter(settings, "channel_link")

                if stereo_image_processing == "dual_mono" and band not in {
                    "left",
                    "right",
                }:
                    continue

                if stereo_image_processing == "mid_side" and band not in {
                    "mid",
                    "side",
                }:
                    continue

                if channel_link and band != "linked":
                    continue

                advanced_plc_frequencies[band] = [f for f in s.value[band]]

            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=advanced_plc_frequencies)
            )
        else:
            hydrated_module_settings.append(s)

    return hydrated_module_settings


async def _publish_run_completion(
    run_id: str, run_name: str, success: bool, redis_client: aioredis.Redis
) -> None:
    message = RunCompletionMessage(run_id=run_id, run_name=run_name, success=success)
    await redis_client.publish(RUN_COMPLETION_CHANNEL, message.model_dump_json())


async def _publish_run_progress(
    run_id: str, run_name: str, nodes: list[NodeProgress], redis_client: aioredis.Redis
) -> None:
    message = RunProgressMessage(run_id=run_id, run_name=run_name, nodes=nodes)
    await redis_client.publish(RUN_PROGRESS_CHANNEL, message.model_dump_json())


async def _publish_run_state_change(
    run_id: str,
    run_name: str,
    previous_status: RunStatus,
    new_status: RunStatus,
    redis_client: aioredis.Redis,
) -> None:
    message = RunStateChangeMessage(
        run_id=run_id,
        run_name=run_name,
        previous_status=previous_status,
        new_status=new_status,
    )
    await redis_client.publish(RUN_STATE_CHANGE_CHANNEL, message.model_dump_json())


async def _transition_run_status(
    run_id: str,
    run_name: str,
    expected_status: RunStatus,
    new_status: RunStatus,
    run_repository: RunsRepository,
    redis_client: aioredis.Redis,
):
    transitioned_document = await run_repository.transition_status(
        run_id, expected_status, new_status
    )
    if transitioned_document is None:
        return None

    try:
        await _publish_run_state_change(
            run_id,
            run_name,
            expected_status,
            new_status,
            redis_client,
        )
    except Exception:
        logger.exception(
            "Could not publish state change for run %s (%s -> %s)",
            run_id,
            expected_status.value,
            new_status.value,
        )

    return transitioned_document


def _build_testbench_from_config(
    run: Run | RunCreateDto,
    run_service: RunsService,
) -> PLCTestbench:
    original_audio_tracks = [
        (OriginalAudio, OriginalAudioSettings(track)) for track in run.tracks
    ]

    packet_loss_simulators = []
    plc_algorithms = []
    output_analysers = []

    for module in run.modules[ModuleType.PacketLossSimulator]:
        cls_ = get_class(module.name)
        settings_cls = get_class(
            run_service.get_module_settings_class_name(module.name)
        )

        packet_loss_simulators.append(
            (cls_, settings_cls(**{s.name: s.value for s in module.settings}))
        )

    for module in run.modules[ModuleType.PLCAlgorithm]:
        cls_ = get_class(module.name)
        settings_cls = get_class(
            run_service.get_module_settings_class_name(module.name)
        )

        hydrated_module_settings = _get_hydrated_module_settings(
            module.settings, run_service
        )

        plc_algorithms.append(
            (cls_, settings_cls(**{s.name: s.value for s in hydrated_module_settings}))
        )

    for module in run.modules[ModuleType.OutputAnalyser]:
        cls_ = get_class(module.name)
        settings_cls = get_class(
            run_service.get_module_settings_class_name(module.name)
        )

        output_analysers.append(
            (cls_, settings_cls(**{s.name: s.value for s in module.settings}))
        )

    testbench_settings = run_service.testbench_settings
    testbench_settings.progress_monitor = lambda caller: InterceptableTqdm

    testbench = PLCTestbench(
        original_audio_tracks,
        packet_loss_simulators,
        plc_algorithms,
        output_analysers,
        run_service.testbench_settings,
    )

    # Store each generated execution-node ID on its configured module while the
    # tree is prepared. The progress page can therefore render before execution.
    pls_modules = run.modules[ModuleType.PacketLossSimulator]
    plc_modules = run.modules[ModuleType.PLCAlgorithm]
    oa_modules = run.modules[ModuleType.OutputAnalyser]

    n_pls = len(pls_modules)
    n_plc = len(plc_modules)

    for module in (*pls_modules, *plc_modules, *oa_modules):
        module.node_ids = []

    for root_node in testbench.data_manager.root_nodes:
        testbench_nodes = testbench.get_nodes_by_depth(root_node)
        pls_nodes = testbench_nodes[1]
        plc_nodes = testbench_nodes[2]
        oa_nodes = testbench_nodes[3]

        for module, node in zip(pls_modules, pls_nodes):
            module.node_ids.append(node.get_id())

        for index, module in enumerate(plc_modules):
            start = index * n_pls
            module.node_ids += [
                node.get_id() for node in plc_nodes[start : start + n_pls]
            ]

        oa_step = n_pls * n_plc
        for index, module in enumerate(oa_modules):
            start = index * oa_step
            module.node_ids += [
                node.get_id() for node in oa_nodes[start : start + oa_step]
            ]

    run.testbench_internal_id = testbench.run_id
    run.status = RunStatus.CREATED
    return testbench


async def _launch_run(
    run: Run,
    run_repository: RunsRepository,
    run_service: RunsService,
    redis_client: aioredis.Redis,
) -> None:
    running_document = await _transition_run_status(
        run.id,
        run.name,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        run_repository,
        redis_client,
    )
    if running_document is None:
        return

    run = Run.from_document(running_document)

    try:
        if not run.testbench_internal_id:
            raise ValueError(f"Run {run.id} has not been prepared")

        testbench_settings = run_service.testbench_settings
        testbench_settings.progress_monitor = lambda caller: InterceptableTqdm
        testbench = PLCTestbench(
            testbench_settings=testbench_settings,
            run_id=run.testbench_internal_id,
        )
        progress_state = _seed_progress_state(testbench)
        run_exception: Exception | None = None

        def _run_thread() -> None:
            nonlocal run_exception
            try:
                testbench.run()
            except Exception as error:
                run_exception = error
            finally:
                InterceptableTqdm.reset_all()

        thread = threading.Thread(target=_run_thread, daemon=True)
        thread.start()

        while thread.is_alive():
            active = InterceptableTqdm.get_all()
            closed = InterceptableTqdm.get_all_closed()

            for description, current, total in (
                progress_bar.get_progress() for progress_bar in active.values()
            ):
                if "|" not in description:
                    continue
                description, _, node_id = description.partition("|")
                if not node_id:
                    continue
                progress_state[node_id] = NodeProgress(
                    description=description,
                    node_id=node_id,
                    current=current,
                    total=total,
                )

            for node_id, (description, current, total) in closed.items():
                if "|" in description:
                    description = description.split("|", 1)[0]
                progress_state[node_id] = NodeProgress(
                    description=description,
                    node_id=node_id,
                    current=current,
                    total=total,
                )

            await _publish_run_progress(
                run.id,
                run.name,
                list(progress_state.values()),
                redis_client,
            )
            await asyncio.sleep(_PROGRESS_POLL_INTERVAL)

        thread.join()
        if run_exception is not None:
            raise run_exception
    except Exception as error:
        traceback.print_exception(error)
        await _transition_run_status(
            run.id,
            run.name,
            RunStatus.RUNNING,
            RunStatus.FAILED,
            run_repository,
            redis_client,
        )
        await _publish_run_completion(
            run.id, run.name, success=False, redis_client=redis_client
        )
        return
    finally:
        InterceptableTqdm.reset_all()

    await _transition_run_status(
        run.id,
        run.name,
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        run_repository,
        redis_client,
    )
    await _publish_run_completion(
        run.id, run.name, success=True, redis_client=redis_client
    )


@lru_cache
def get_runs_service() -> RunsService:
    _run_service = RunsService()
    return _run_service


class RunsService:
    def __init__(self) -> None:
        self.runs_repository: RunsRepository = get_runs_repository()
        self.assets_repository: AssetsRepository = get_assets_repository()
        self.assets_service: AssetsService = get_assets_service()
        self.testbench_settings: TestbenchConfiguration = self.get_testbench_settings()
        self.redis_client: aioredis.Redis = get_redis_client()

    async def save_run(self, run: RunCreateDto) -> Run:
        run.status = RunStatus.CREATED
        run.testbench_internal_id = None
        try:
            _build_testbench_from_config(run, self)
        except Exception as error:
            raise RunPreparationError(str(error)) from error

        saved_run = await self.runs_repository.create_run(run)
        return Run.from_document(saved_run)

    async def execute_run(self, run_id: str) -> Run:
        run = await self.find_by_id(run_id)
        if not run.testbench_internal_id:
            raise RunNotExecutableError(f"Run {run_id} has not been prepared")

        queued_document = await _transition_run_status(
            run_id,
            run.name,
            RunStatus.CREATED,
            RunStatus.QUEUED,
            self.runs_repository,
            self.redis_client,
        )
        if queued_document is None:
            current_run = await self.find_by_id(run_id)
            raise RunNotExecutableError(
                f"Run {run_id} cannot be executed while its status is "
                f"{current_run.status.value}"
            )

        try:
            actors.launch_run.send(run_id=run_id)
        except Exception as error:
            await _transition_run_status(
                run_id,
                run.name,
                RunStatus.QUEUED,
                RunStatus.CREATED,
                self.runs_repository,
                self.redis_client,
            )
            raise RunQueueError(f"Run {run_id} could not be queued") from error

        return Run.from_document(queued_document)

    async def find_by_id(self, run_id: str) -> Run:
        document = await self.runs_repository.get_run(run_id)
        if document is None:
            raise ValueError(f"Run {run_id} not found")
        return Run.from_document(document)

    async def get_page(self, page: int, page_size: int) -> RunPage:
        total = await self.runs_repository.count_all()
        skip = (page - 1) * page_size
        documents = await self.runs_repository.get_page(skip, page_size)
        return RunPage(
            items=[Run.from_document(document) for document in documents],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def delete_run(self, run_id: str) -> None:
        run = await self.find_by_id(run_id)
        if run.status not in {
            RunStatus.CREATED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
        }:
            raise RunNotDeletableError(
                f"Run {run_id} cannot be deleted while its status is {run.status.value}"
            )

        deleted = await self.runs_repository.delete_run(run_id)
        if not deleted:
            raise ValueError(f"Run {run_id} not found")

    async def launch_run_synch(self, run: Run) -> None:
        await _launch_run(run, self.runs_repository, self, self.redis_client)

    async def get_assets_tar_by_depth(
        self, run_id: str, depth: TestbenchNodeDepth
    ) -> io.BytesIO:
        run: Run = await self.find_by_id(run_id)

        paths = self.assets_repository.get_assets_paths(
            run, depth, self.testbench_settings
        )

        paths = [self.assets_repository.resolve_asset_path(p, depth) for p in paths]

        tar_buffer = io.BytesIO()

        with tarfile.open(
            fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT
        ) as tar:
            for p in paths:
                if not os.path.exists(p):
                    continue

                if depth == TestbenchNodeDepth.SAMPLE_MASKS:
                    # pi-lens-ignore: python-insecure-deserialization
                    # Sample masks are NumPy files written by our own testbench worker.
                    data: np.ndarray = np.load(p, allow_pickle=True)
                    tar = self.assets_service.add_json_to_tar(data, tar, p, ".npy")
                elif depth == TestbenchNodeDepth.OUTPUT_ANALYSIS:
                    try:
                        with open(p, "rb") as pkl:
                            # pi-lens-ignore: python-insecure-deserialization
                            # Output analyses are pickles written by our own testbench worker.
                            data: OutputAnalysis = pickle.load(pkl)
                            if isinstance(data, SimpleCalculatorData):
                                data = data.get_error()
                                data = np.nan_to_num(data, nan=0)
                                data = data.T
                            elif isinstance(data, PEAQData):
                                data = np.array([data.get_di(), data.get_odg()])
                    except OSError as error:
                        raise OSError(
                            f"Could not read output analysis '{p}': {error}"
                        ) from error

                    json_data = json.dumps(data.tolist())
                    json_buffer = io.BytesIO(json_data.encode("utf-8"))
                    json_filename = p.replace(".pickle", ".json")
                    tarinfo = tarfile.TarInfo(
                        name=self.strip_asset_filenames(json_filename, depth)
                    )
                    tarinfo.size = len(json_data.encode("utf-8"))
                    tar.addfile(tarinfo, json_buffer)
                else:
                    tar.add(p, arcname=self.strip_asset_filenames(p, depth))

        tar_buffer.seek(0)
        return tar_buffer

    def get_testbench_settings(self) -> TestbenchConfiguration:
        config = get_configuration()

        testbench_settings = TestbenchConfiguration(
            root_folder=config.plc_root_folder,
            db_platform=DBPlatform.MONGODB,
            db_ip="mongo",
            db_port="27017",
            db_username=config.mongo_initdb_root_username,
            db_password=config.mongo_initdb_root_password,
        )

        return testbench_settings

    def get_module_settings_class_name(self, module: str) -> str:
        return f"{module}Settings"

    def strip_asset_filenames(self, path, depth):
        items = path.split("/")[2:]
        if depth == TestbenchNodeDepth.ORIGINAL_TRACKS:
            (original_track,) = tuple(items)
            return "/".join([original_track])
        if depth == TestbenchNodeDepth.RECONSTRUCTED_TRACKS:
            original_track, sample_mask, reconstructed_track = tuple(items)
            original_track = original_track.split("-")[0]
            sample_mask = "-".join(sample_mask.split("-")[:2])
            return "/".join([original_track, sample_mask, reconstructed_track])
        if depth == TestbenchNodeDepth.OUTPUT_ANALYSIS:
            original_track, sample_mask, reconstructed_track, output_analysis = tuple(
                items
            )
            original_track = original_track.split("-")[0]
            sample_mask = "-".join(sample_mask.split("-")[:2])
            reconstructed_track = "-".join(reconstructed_track.split("-")[:2])
            return "/".join(
                [original_track, sample_mask, reconstructed_track, output_analysis]
            )

    async def export_run_config(self, run_id: str) -> str:
        run = await self.find_by_id(run_id)
        config = {
            "name": run.name,
            "tracks": run.tracks,
            "modules": {
                module_type: [
                    {
                        "name": module.name,
                        "settings": [
                            {"name": s.name, "value": s.value} for s in module.settings
                        ],
                    }
                    for module in modules
                ]
                for module_type, modules in run.modules.items()
            },
        }
        return json.dumps(config, indent=2)

    async def validate_run_config(
        self, config: RunConfigDto, modules_service: ModuleService
    ) -> list[RunConfigValidationError]:
        return self._validate_config_modules(config.modules, modules_service)

    async def validate_run_create(
        self, run: RunCreateDto, modules_service: ModuleService
    ) -> list[RunConfigValidationError]:
        return self._validate_config_modules(run.modules, modules_service)

    def _validate_config_modules(
        self,
        modules: dict[ModuleType, list[Module]],
        modules_service: ModuleService,
    ) -> list[RunConfigValidationError]:
        errors: list[RunConfigValidationError] = []
        validator = ModuleConfigValidator(modules_service)

        for module_type, module_list in (modules or {}).items():
            available = modules_service.get_all_modules_by_type(module_type)
            available_names = [m.name for m in available]

            for module in module_list or []:
                # Check if the module name is available
                if module.name not in available_names:
                    errors.append(
                        RunConfigValidationError(
                            module_type=module_type.value,
                            module_name=module.name,
                            error="Modulo non trovato",
                        )
                    )
                    continue
                # Check if the module parameters are valid
                available_module = next(m for m in available if m.name == module.name)
                expected_params = [p.name for p in available_module.settings]
                actual_params = [p.name for p in module.settings]

                # Check if the actual parameters match the expected parameters
                if actual_params != expected_params:
                    errors.append(
                        RunConfigValidationError(
                            module_type=module_type.value,
                            module_name=module.name,
                            error=(
                                f"Parametri non validi. Attesi: {expected_params}, "
                                f"ricevuti: {actual_params}"
                            ),
                        )
                    )
                    continue

                # Check that the provided values respect the manifest validation
                for message in validator.validate_module(module_type, module):
                    errors.append(
                        RunConfigValidationError(
                            module_type=module_type.value,
                            module_name=module.name,
                            setting=message.setting,
                            error=message.message,
                        )
                    )

        return errors
