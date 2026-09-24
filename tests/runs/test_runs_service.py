import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MONGO_INITDB_ROOT_USERNAME", "test")
os.environ.setdefault("MONGO_INITDB_ROOT_PASSWORD", "test")
os.environ.setdefault("PLC_ROOT_FOLDER", "/tmp/plc-testbench-tests")
os.environ.setdefault("PLUGINS_DIRECTORY", "/tmp/plc-testbench-tests/plugins")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from plc_platform_backend.modules.modules_models import (
    Module,
    ModuleParameter,
    ModuleType,
)
from plc_platform_backend.runs.runs_models import (
    Run,
    RunCreateDto,
    RunDocument,
    RunStatus,
)
from plc_platform_backend.runs.runs_service import (
    RunNotDeletableError,
    RunNotExecutableError,
    RunQueueError,
    RunsService,
    _build_testbench_from_config,
    _launch_run,
)


class RunsServiceDeleteRunTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = RunsService.__new__(RunsService)
        self.service.runs_repository = SimpleNamespace(delete_run=AsyncMock())
        self.service.find_by_id = AsyncMock()

    async def test_deletes_created_completed_and_failed_runs(self) -> None:
        for status in (RunStatus.CREATED, RunStatus.COMPLETED, RunStatus.FAILED):
            with self.subTest(status=status):
                self.service.find_by_id.return_value = SimpleNamespace(status=status)
                self.service.runs_repository.delete_run.return_value = True

                await self.service.delete_run("507f1f77bcf86cd799439011")

        self.assertEqual(self.service.runs_repository.delete_run.await_count, 3)

    async def test_rejects_queued_and_running_runs(self) -> None:
        for status in (RunStatus.QUEUED, RunStatus.RUNNING):
            with self.subTest(status=status):
                self.service.find_by_id.return_value = SimpleNamespace(status=status)

                with self.assertRaises(RunNotDeletableError):
                    await self.service.delete_run("507f1f77bcf86cd799439011")

        self.service.runs_repository.delete_run.assert_not_awaited()

    async def test_propagates_not_found_lookup(self) -> None:
        self.service.find_by_id.side_effect = ValueError("Run invalid not found")

        with self.assertRaisesRegex(ValueError, "not found"):
            await self.service.delete_run("invalid")

        self.service.runs_repository.delete_run.assert_not_awaited()

    async def test_treats_a_delete_race_as_not_found(self) -> None:
        self.service.find_by_id.return_value = SimpleNamespace(
            status=RunStatus.COMPLETED
        )
        self.service.runs_repository.delete_run.return_value = False

        with self.assertRaisesRegex(ValueError, "not found"):
            await self.service.delete_run("507f1f77bcf86cd799439011")


def make_run_document(status: RunStatus = RunStatus.CREATED) -> RunDocument:
    return RunDocument(
        _id="507f1f77bcf86cd799439011",
        author="test",
        name="Test run",
        testbench_internal_id="internal-run-id",
        status=status,
        tracks=["track.wav"],
        modules={
            ModuleType.PacketLossSimulator: [],
            ModuleType.PLCAlgorithm: [],
            ModuleType.OutputAnalyser: [],
        },
    )


class PrepareRunTests(IsolatedAsyncioTestCase):
    def test_builds_tree_and_assigns_internal_and_module_node_ids(self) -> None:
        create_dto = RunCreateDto(
            author="test",
            name="Prepared run",
            tracks=["one.wav", "two.wav"],
            modules={
                ModuleType.PacketLossSimulator: [
                    Module(
                        name="PLS",
                        settings=[ModuleParameter(name="value", value=1)],
                    )
                ],
                ModuleType.PLCAlgorithm: [
                    Module(
                        name="PLC",
                        settings=[ModuleParameter(name="value", value=2)],
                    )
                ],
                ModuleType.OutputAnalyser: [
                    Module(
                        name="Output",
                        settings=[ModuleParameter(name="value", value=3)],
                    )
                ],
            },
        )
        roots = [object(), object()]
        nodes_by_root = {
            roots[0]: {
                1: [SimpleNamespace(get_id=lambda: "pls-1")],
                2: [SimpleNamespace(get_id=lambda: "plc-1")],
                3: [SimpleNamespace(get_id=lambda: "out-1")],
            },
            roots[1]: {
                1: [SimpleNamespace(get_id=lambda: "pls-2")],
                2: [SimpleNamespace(get_id=lambda: "plc-2")],
                3: [SimpleNamespace(get_id=lambda: "out-2")],
            },
        }
        testbench = SimpleNamespace(
            run_id="internal-run-id",
            data_manager=SimpleNamespace(root_nodes=roots),
            get_nodes_by_depth=lambda root: nodes_by_root[root],
        )
        settings = SimpleNamespace(progress_monitor=None)
        service = SimpleNamespace(
            testbench_settings=settings,
            get_module_settings_class_name=lambda name: f"{name}Settings",
        )

        class FakeSettings:
            def __init__(self, **values) -> None:
                self.values = values

        with (
            patch(
                "plc_platform_backend.runs.runs_service.get_class",
                side_effect=lambda name: FakeSettings
                if name.endswith("Settings")
                else object,
            ),
            patch(
                "plc_platform_backend.runs.runs_service.PLCTestbench",
                return_value=testbench,
            ),
        ):
            result = _build_testbench_from_config(create_dto, service)

        self.assertIs(result, testbench)
        self.assertEqual(create_dto.testbench_internal_id, "internal-run-id")
        self.assertEqual(create_dto.status, RunStatus.CREATED)
        self.assertEqual(
            create_dto.modules[ModuleType.PacketLossSimulator][0].node_ids,
            ["pls-1", "pls-2"],
        )
        self.assertEqual(
            create_dto.modules[ModuleType.PLCAlgorithm][0].node_ids,
            ["plc-1", "plc-2"],
        )
        self.assertEqual(
            create_dto.modules[ModuleType.OutputAnalyser][0].node_ids,
            ["out-1", "out-2"],
        )


class RunsServiceLifecycleTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = RunsService.__new__(RunsService)
        self.service.runs_repository = SimpleNamespace(
            create_run=AsyncMock(),
            transition_status=AsyncMock(),
        )
        self.service.find_by_id = AsyncMock()

    async def test_save_prepares_without_queueing(self) -> None:
        create_dto = RunCreateDto(
            author="test",
            name="Test run",
            tracks=["track.wav"],
            modules={
                ModuleType.PacketLossSimulator: [],
                ModuleType.PLCAlgorithm: [],
                ModuleType.OutputAnalyser: [],
            },
        )
        self.service.runs_repository.create_run.return_value = make_run_document()

        with (
            patch(
                "plc_platform_backend.runs.runs_service._build_testbench_from_config"
            ) as prepare,
            patch(
                "plc_platform_backend.runs.runs_service.actors.launch_run.send"
            ) as send,
        ):
            saved = await self.service.save_run(create_dto)

        prepare.assert_called_once_with(create_dto, self.service)
        self.service.runs_repository.create_run.assert_awaited_once_with(create_dto)
        send.assert_not_called()
        self.assertEqual(saved.status, RunStatus.CREATED)

    async def test_execute_queues_a_prepared_created_run(self) -> None:
        created_run = Run.from_document(make_run_document())
        queued_document = make_run_document(RunStatus.QUEUED)
        self.service.find_by_id.return_value = created_run
        self.service.runs_repository.transition_status.return_value = queued_document

        with patch(
            "plc_platform_backend.runs.runs_service.actors.launch_run.send"
        ) as send:
            queued = await self.service.execute_run(created_run.id)

        self.service.runs_repository.transition_status.assert_awaited_once_with(
            created_run.id, RunStatus.CREATED, RunStatus.QUEUED
        )
        send.assert_called_once_with(run_id=created_run.id)
        self.assertEqual(queued.status, RunStatus.QUEUED)

    async def test_execute_rejects_a_non_created_run(self) -> None:
        queued_run = Run.from_document(make_run_document(RunStatus.QUEUED))
        self.service.find_by_id.return_value = queued_run
        self.service.runs_repository.transition_status.return_value = None

        with self.assertRaises(RunNotExecutableError):
            await self.service.execute_run(queued_run.id)

    async def test_queue_failure_restores_created_status(self) -> None:
        created_run = Run.from_document(make_run_document())
        queued_document = make_run_document(RunStatus.QUEUED)
        restored_document = make_run_document(RunStatus.CREATED)
        self.service.find_by_id.return_value = created_run
        self.service.runs_repository.transition_status.side_effect = [
            queued_document,
            restored_document,
        ]

        with patch(
            "plc_platform_backend.runs.runs_service.actors.launch_run.send",
            side_effect=RuntimeError("broker unavailable"),
        ):
            with self.assertRaises(RunQueueError):
                await self.service.execute_run(created_run.id)

        self.assertEqual(
            self.service.runs_repository.transition_status.await_args_list[1].args,
            (created_run.id, RunStatus.QUEUED, RunStatus.CREATED),
        )


class LaunchRunTests(IsolatedAsyncioTestCase):
    async def test_launch_reloads_prepared_testbench_and_completes(self) -> None:
        queued_run = Run.from_document(make_run_document(RunStatus.QUEUED))
        running_document = make_run_document(RunStatus.RUNNING)
        completed_document = make_run_document(RunStatus.COMPLETED)
        repository = SimpleNamespace(
            transition_status=AsyncMock(
                side_effect=[running_document, completed_document]
            )
        )
        service = SimpleNamespace(
            testbench_settings=SimpleNamespace(progress_monitor=None)
        )
        redis_client = SimpleNamespace(publish=AsyncMock())
        testbench = SimpleNamespace(
            data_manager=SimpleNamespace(root_nodes=[]),
            run=MagicMock(),
        )

        with patch(
            "plc_platform_backend.runs.runs_service.PLCTestbench",
            return_value=testbench,
        ) as testbench_class:
            await _launch_run(queued_run, repository, service, redis_client)

        testbench_class.assert_called_once_with(
            testbench_settings=service.testbench_settings,
            run_id=queued_run.testbench_internal_id,
        )
        testbench.run.assert_called_once_with()
        self.assertEqual(
            repository.transition_status.await_args_list[0].args,
            (queued_run.id, RunStatus.QUEUED, RunStatus.RUNNING),
        )
        self.assertEqual(
            repository.transition_status.await_args_list[1].args,
            (queued_run.id, RunStatus.RUNNING, RunStatus.COMPLETED),
        )

    async def test_launch_marks_testbench_errors_failed(self) -> None:
        queued_run = Run.from_document(make_run_document(RunStatus.QUEUED))
        running_document = make_run_document(RunStatus.RUNNING)
        failed_document = make_run_document(RunStatus.FAILED)
        repository = SimpleNamespace(
            transition_status=AsyncMock(side_effect=[running_document, failed_document])
        )
        service = SimpleNamespace(
            testbench_settings=SimpleNamespace(progress_monitor=None)
        )
        redis_client = SimpleNamespace(publish=AsyncMock())
        testbench = SimpleNamespace(
            data_manager=SimpleNamespace(root_nodes=[]),
            run=MagicMock(side_effect=RuntimeError("execution failed")),
        )

        with (
            patch(
                "plc_platform_backend.runs.runs_service.PLCTestbench",
                return_value=testbench,
            ),
            patch("plc_platform_backend.runs.runs_service.traceback.print_exception"),
        ):
            await _launch_run(queued_run, repository, service, redis_client)

        self.assertEqual(
            repository.transition_status.await_args_list[1].args,
            (queued_run.id, RunStatus.RUNNING, RunStatus.FAILED),
        )
        completion_payload = redis_client.publish.await_args.args[1]
        self.assertIn('"success":false', completion_payload)
