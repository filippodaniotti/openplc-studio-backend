from __future__ import annotations

import io
import json
import os
import pickle
import tarfile
import traceback
from functools import lru_cache

import numpy as np
import plctestbench.loss_simulator
import plctestbench.output_analyser
import plctestbench.plc_algorithm
from plctestbench.file_wrapper import OutputAnalysis
from plctestbench.models import DBPlatform, TestbenchConfiguration
from plctestbench.output_analyser import PEAQData, SimpleCalculatorData
from plctestbench.plc_testbench import PLCTestbench
from plctestbench.settings import CrossfadeSettings, OriginalAudioSettings
from plctestbench.worker import OriginalAudio
from redis import Redis

from plc_platform_backend import actors
from plc_platform_backend.assets.assets_models import TestbenchNodeDepth
from plc_platform_backend.assets.assets_repository import (
    AssetsRepository,
    get_assets_repository,
)
from plc_platform_backend.assets.assets_service import AssetsService, get_assets_service
from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.commons.redis_client import get_redis_client
from plc_platform_backend.modules.modules_models import ModuleParameter, ModuleType
from plc_platform_backend.runs.runs_models import Run, RunCreateDto, RunStatus
from plc_platform_backend.runs.runs_repository import (
    RunsRepository,
    get_runs_repository,
)

import redis.asyncio as aioredis
from plc_platform_backend.runs.runs_messages import RunCompletionMessage

RUN_COMPLETION_CHANNEL = "run.complete"



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
            crossfade_frequencies = [int(f) for f in s.value]
            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=crossfade_frequencies)
            )
        elif s.name == "crossover_order" and s.value:
            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=int(s.value))
            )
        elif s.name == "band_settings":
            for band in {"linked", "mid", "side", "left", "right"}:
                if not band in s.value.keys():
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
                        [ModuleParameter(name=as_["name"], value=as_["value"])
                        for as_ in algorithm["settings"]],
                        run_service
                    )
                    advanced_plc_band_settings[band].append(
                        algorithm_settings_cls(
                            **{has.name: has.value for has in hydrated_algorithm_settings}
                        )
                    )

            hydrated_module_settings.append(
                ModuleParameter(name=s.name, value=advanced_plc_band_settings)
            )
        elif s.name == "frequencies":
            for band in ["linked", "mid", "side", "left", "right"]:
                if not band in s.value.keys():
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

async def _publish_run_completion(run_name: str, success: bool) -> None:
    config = get_configuration()
    redis_client = aioredis.from_url(config.redis_url)
    message = RunCompletionMessage(run_name=run_name, success=success)
    await redis_client.publish(RUN_COMPLETION_CHANNEL, message.json())
    await redis_client.close()

async def _launch_run(
    run: Run,
    run_repository: RunsRepository,
    run_service: RunsService,
    redis_client: Redis,
) -> None:
    original_audio_tracks = [
        (OriginalAudio, OriginalAudioSettings(track)) for track in run.tracks
    ]

    packet_loss_simulators = []
    plc_algorithms = []
    output_analysers = []

    for module in run.modules[ModuleType.PacketLossSimulator]:
        cls_ = getattr(plctestbench.loss_simulator, module.name)
        settings_cls = getattr(
            plctestbench.settings,
            run_service.get_module_settings_class_name(module.name),
        )

        packet_loss_simulators.append(
            (cls_, settings_cls(**{s.name: s.value for s in module.settings}))
        )

    for module in run.modules[ModuleType.PLCAlgorithm]:
        cls_ = getattr(plctestbench.plc_algorithm, module.name, None)
        settings_cls = getattr(
            plctestbench.settings,
            run_service.get_module_settings_class_name(module.name),
            None,
        )

        if cls_ is None:
            import importlib.util
            import sys
            from pathlib import Path

            plugin_file_path = (
                Path(get_configuration().plugins_directory)
                / f"{module.name}Algorithm.py"
            )

            if not plugin_file_path.exists():
                raise ImportError(f"Plugin file {plugin_file_path} not found")

            spec = importlib.util.spec_from_file_location(
                f"{module.name}PLCAlgorithm", plugin_file_path
            )
            plugin_module = importlib.util.module_from_spec(spec)
            sys.modules[f"{module.name}PLCAlgorithm"] = plugin_module
            spec.loader.exec_module(plugin_module)

            cls_ = getattr(plugin_module, module.name)

            settings_cls = getattr(
                plugin_module,
                run_service.get_module_settings_class_name(module.name),
                None,
            )

        hydrated_module_settings = _get_hydrated_module_settings(
            module.settings, run_service
        )

        plc_algorithms.append(
            (cls_, settings_cls(**{s.name: s.value for s in hydrated_module_settings}))
        )

    for module in run.modules[ModuleType.OutputAnalyser]:
        cls_ = getattr(plctestbench.output_analyser, module.name)
        settings_cls = getattr(
            plctestbench.settings,
            run_service.get_module_settings_class_name(module.name),
        )

        output_analysers.append(
            (cls_, settings_cls(**{s.name: s.value for s in module.settings}))
        )

    testbench = PLCTestbench(
        original_audio_tracks,
        packet_loss_simulators,
        plc_algorithms,
        output_analysers,
        run_service.testbench_settings,
    )

    run.status = RunStatus.RUNNING
    run.testbench_internal_id = testbench.run_id
    await run_repository.update_run(run.id, run)

    try:
        testbench.run()
    except Exception as e:
        traceback.print_exception(e)
        run.status = RunStatus.FAILED
        await run_repository.update_run(run.id, run)
        await _publish_run_completion(run.name, success=False)
        return

    run.status = RunStatus.COMPLETED
    await run_repository.update_run(run.id, run)
    await _publish_run_completion(run.name, success=True)

   


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
        self.redis_client: Redis = get_redis_client()

    async def save_run(self, run: RunCreateDto) -> Run:
        saved_run = await self.runs_repository.create_run(run)
        actors.launch_run.send(run_id=saved_run.id)
        # await self.launch_run_synch(saved_run)
        return Run.from_document(saved_run)

    async def find_by_id(self, run_id: str) -> Run:
        return Run.from_document(await self.runs_repository.get_run(run_id))

    async def get_all(self) -> list[Run]:
        return [Run.from_document(run) for run in await self.runs_repository.get_all()]

    async def launch_run_synch(self, run: Run) -> Run:
        await _launch_run(run, self.runs_repository, self, self.redis_client)

    async def get_assets_tar_by_depth(
        self, run_id: str, depth: TestbenchNodeDepth
    ) -> list[str]:
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
                    data: np.ndarray = np.load(p, allow_pickle=True)
                    tar = self.assets_service.add_json_to_tar(data, tar, p, ".npy")
                elif depth == TestbenchNodeDepth.OUTPUT_ANALYSIS:
                    with open(p, "rb") as pkl:
                        data: OutputAnalysis = pickle.load(pkl)
                        if isinstance(data, SimpleCalculatorData):
                            data = data.get_error()
                            data = np.nan_to_num(data, nan=0)
                            data = data.T
                        elif isinstance(data, PEAQData):
                            data = np.array([data.get_di(), data.get_odg()])

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
