import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch


UTGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTGEN_DIR))

from utgen import Project  # noqa: E402
from utils import (  # noqa: E402
    LLMConfigurationError,
    LLMProtocolError,
    LLMTransportError,
    SETUP_COV,
    build_llm_url,
    coverage_setup,
    create_dir_with_cmake,
    get_test_from_LLM,
    resolve_test_types,
)


class ResolveTestTypesTests(unittest.TestCase):
    def test_defaults_to_all_test_types(self) -> None:
        self.assertEqual(
            resolve_test_types(None), ["", "_cxt", "_req", "_req_cxt"]
        )

    def test_maps_cli_names_to_internal_suffixes(self) -> None:
        self.assertEqual(
            resolve_test_types(["base", "req-cxt"]), ["", "_req_cxt"]
        )

    def test_removes_duplicate_selections(self) -> None:
        self.assertEqual(
            resolve_test_types(["req", "req", "cxt"]), ["_req", "_cxt"]
        )


class BuildLLMURLTests(unittest.TestCase):
    def test_appends_chat_completions_path_to_base_url(self) -> None:
        self.assertEqual(
            build_llm_url("https://llm.example/", "chat_completions"),
            "https://llm.example/v1/chat/completions",
        )

    def test_appends_responses_path_after_a_provider_prefix(self) -> None:
        self.assertEqual(
            build_llm_url("https://llm.example/gateway", "responses"),
            "https://llm.example/gateway/v1/responses",
        )

    def test_replaces_a_legacy_full_endpoint(self) -> None:
        self.assertEqual(
            build_llm_url(
                "https://llm.example/v1/chat/completions",
                "responses",
            ),
            "https://llm.example/v1/responses",
        )

    def test_does_not_duplicate_a_trailing_v1(self) -> None:
        self.assertEqual(
            build_llm_url("https://llm.example/api/v1", "responses"),
            "https://llm.example/api/v1/responses",
        )

    def test_rejects_unknown_api_styles(self) -> None:
        with self.assertRaisesRegex(
            LLMConfigurationError,
            "Unsupported LLM API style",
        ):
            build_llm_url("https://llm.example", "unknown")

    def test_rejects_non_base_urls(self) -> None:
        for url in (
            "llm.example",
            "ftp://llm.example",
            "https://llm.example?token=value",
            "https://llm.example#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(
                LLMConfigurationError
            ):
                build_llm_url(url, "chat_completions")
        with self.assertRaises(LLMConfigurationError):
            build_llm_url(None, "chat_completions")  # type: ignore[arg-type]


class GetTestFromLLMTests(unittest.TestCase):
    def setUp(self) -> None:
        config = patch.multiple(
            "utils",
            URL="https://llm.example",
            API_KEY="test-key",
            MODEL="test-model",
            LLM_API_STYLE="chat_completions",
            LLM_STREAM=False,
            LLM_CONNECT_TIMEOUT=15,
            LLM_READ_TIMEOUT=180,
        )
        config.start()
        self.addCleanup(config.stop)

    @staticmethod
    def chat_response(content: str) -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": content}}]
        }
        return response

    @staticmethod
    def stream_response(lines: list[str | bytes]) -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.iter_lines.return_value = lines
        return response

    def test_requests_missing_choices_when_provider_ignores_n(self) -> None:
        responses = [
            self.chat_response(f"```cpp\nTEST(Target, {name}) {{}}\n```")
            for name in ("First", "Second")
        ]

        with patch("utils.rq.post", side_effect=responses) as post:
            tests = get_test_from_LLM("prompt", 2)

        self.assertEqual(
            tests,
            ["TEST(Target, First) {}", "TEST(Target, Second) {}"],
        )
        self.assertEqual(post.call_count, 2)
        first_payload = post.call_args_list[0].kwargs["json"]
        second_payload = post.call_args_list[1].kwargs["json"]
        self.assertEqual(first_payload["n"], 2)
        self.assertEqual(second_payload["n"], 1)
        self.assertFalse(first_payload["stream"])
        self.assertEqual(
            post.call_args_list[0].args[0],
            "https://llm.example/v1/chat/completions",
        )
        self.assertEqual(
            post.call_args_list[0].kwargs["timeout"],
            (15.0, 180.0),
        )

    def test_rejects_response_without_choices(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": []}

        with (
            patch("utils.rq.post", return_value=response),
            patch("utils.time.sleep") as sleep,
            self.assertRaisesRegex(LLMProtocolError, "No choices"),
        ):
            get_test_from_LLM("prompt", 1)
        self.assertEqual(sleep.call_args_list, [call(5), call(10)])

    def test_repeated_http_failure_trips_transport_circuit_breaker(self) -> None:
        responses = []
        for _ in range(3):
            response = MagicMock()
            response.status_code = 429
            responses.append(response)

        with (
            patch("utils.rq.post", side_effect=responses) as post,
            patch("utils.time.sleep") as sleep,
            self.assertRaisesRegex(LLMTransportError, "HTTP 429"),
        ):
            get_test_from_LLM("prompt", 1)

        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(5), call(10)])

    def test_non_retryable_http_failure_fails_immediately(self) -> None:
        response = MagicMock()
        response.status_code = 401

        with (
            patch("utils.rq.post", return_value=response) as post,
            patch("utils.time.sleep") as sleep,
            self.assertRaisesRegex(LLMConfigurationError, "HTTP 401"),
        ):
            get_test_from_LLM("prompt", 1)

        post.assert_called_once()
        sleep.assert_not_called()

    def test_invalid_stream_and_timeout_settings_fail_before_request(
        self,
    ) -> None:
        for setting, value, message in (
            ("LLM_STREAM", "true", "LLM_STREAM must be a boolean"),
            ("LLM_CONNECT_TIMEOUT", 0, "timeouts must be positive"),
            ("LLM_READ_TIMEOUT", "invalid", "timeouts must be positive"),
        ):
            with (
                self.subTest(setting=setting),
                patch(f"utils.{setting}", value),
                patch("utils.rq.post") as post,
                self.assertRaisesRegex(LLMConfigurationError, message),
            ):
                get_test_from_LLM("prompt", 1)
            post.assert_not_called()

    def test_http_failure_retries_once_then_recovers(self) -> None:
        failed_response = MagicMock()
        failed_response.status_code = 429
        recovered_response = self.chat_response("recovered test")

        with (
            patch(
                "utils.rq.post",
                side_effect=[failed_response, recovered_response],
            ) as post,
            patch("utils.time.sleep") as sleep,
        ):
            tests = get_test_from_LLM("prompt", 1)

        self.assertEqual(tests, ["recovered test"])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_http_failure_retries_twice_then_recovers(self) -> None:
        first_failed_response = MagicMock()
        first_failed_response.status_code = 503
        second_failed_response = MagicMock()
        second_failed_response.status_code = 503
        recovered_response = self.chat_response("recovered test")

        with (
            patch(
                "utils.rq.post",
                side_effect=[
                    first_failed_response,
                    second_failed_response,
                    recovered_response,
                ],
            ) as post,
            patch("utils.time.sleep") as sleep,
        ):
            tests = get_test_from_LLM("prompt", 1)

        self.assertEqual(tests, ["recovered test"])
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(5), call(10)])

    def test_chat_completions_stream_assembles_interleaved_choices(self) -> None:
        response = self.stream_response(
            [
                ": keep-alive",
                "",
                'data: {"choices":[{"index":1,"delta":{"content":"Second"}}]}',
                "",
                b'data: {"choices":[{"index":0,"delta":{"content":"First"}}]}',
                b"",
                'data: {"choices":[{"index":1,"delta":{"content":" answer"}}]}',
                "",
                "data: [DONE]",
                "",
            ]
        )

        with (
            patch("utils.LLM_STREAM", True),
            patch("utils.rq.post", return_value=response) as post,
        ):
            tests = get_test_from_LLM("prompt", 2)

        self.assertEqual(tests, ["First", "Second answer"])
        payload = post.call_args.kwargs["json"]
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["n"], 2)
        self.assertTrue(post.call_args.kwargs["stream"])

    def test_incomplete_chat_stream_is_discarded_before_retry(self) -> None:
        incomplete = self.stream_response(
            [
                'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}',
                "",
            ]
        )
        recovered = self.stream_response(
            [
                'data: {"choices":[{"index":0,"delta":{"content":"complete"}}]}',
                "",
                "data: [DONE]",
                "",
            ]
        )

        with (
            patch("utils.LLM_STREAM", True),
            patch("utils.rq.post", side_effect=[incomplete, recovered]) as post,
            patch("utils.time.sleep") as sleep,
        ):
            tests = get_test_from_LLM("prompt", 1)

        self.assertEqual(tests, ["complete"])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5)
        incomplete.close.assert_called_once()
        recovered.close.assert_called_once()

    def test_responses_non_streaming_requests_one_candidate_at_a_time(
        self,
    ) -> None:
        responses = []
        for answer in ("first", "second"):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": answer}
                        ],
                    }
                ],
            }
            responses.append(response)

        with (
            patch("utils.LLM_API_STYLE", "responses"),
            patch("utils.rq.post", side_effect=responses) as post,
        ):
            tests = get_test_from_LLM("prompt", 2)

        self.assertEqual(tests, ["first", "second"])
        self.assertEqual(post.call_count, 2)
        for request_call in post.call_args_list:
            self.assertEqual(
                request_call.args[0],
                "https://llm.example/v1/responses",
            )
            self.assertEqual(
                request_call.kwargs["json"],
                {
                    "model": "test-model",
                    "input": "prompt",
                    "stream": False,
                },
            )

    def test_responses_stream_assembles_text_after_completed_event(self) -> None:
        response = self.stream_response(
            [
                "event: response.output_text.delta",
                "data: "
                '{"type":"response.output_text.delta","output_index":0,'
                '"content_index":0,"delta":"streamed"}',
                "",
                "event: response.output_text.done",
                "data: "
                '{"type":"response.output_text.done","output_index":0,'
                '"content_index":0,"text":"streamed answer"}',
                "",
                "event: response.completed",
                'data: {"type":"response.completed","response":{"status":"completed"}}',
                "",
            ]
        )

        with (
            patch("utils.LLM_API_STYLE", "responses"),
            patch("utils.LLM_STREAM", True),
            patch("utils.rq.post", return_value=response) as post,
        ):
            tests = get_test_from_LLM("prompt", 1)

        self.assertEqual(tests, ["streamed answer"])
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "model": "test-model",
                "input": "prompt",
                "stream": True,
            },
        )

    def test_responses_stream_can_use_text_from_completed_response(self) -> None:
        response = self.stream_response(
            [
                "event: response.completed",
                "data: "
                '{"type":"response.completed","response":'
                '{"status":"completed","output":[{"type":"message",'
                '"content":[{"type":"output_text","text":"final text"}]}]}}',
                "",
                'data: {"type":"response.failed"}',
                "",
            ]
        )

        with (
            patch("utils.LLM_API_STYLE", "responses"),
            patch("utils.LLM_STREAM", True),
            patch("utils.rq.post", return_value=response),
        ):
            tests = get_test_from_LLM("prompt", 1)

        self.assertEqual(tests, ["final text"])

    def test_incomplete_responses_stream_is_discarded_before_retry(
        self,
    ) -> None:
        incomplete = self.stream_response(
            [
                'data: {"type":"response.output_text.delta","delta":"partial"}',
                "",
            ]
        )
        recovered = self.stream_response(
            [
                'data: {"type":"response.output_text.delta","delta":"complete"}',
                "",
                'data: {"type":"response.completed","response":{"status":"completed"}}',
                "",
            ]
        )

        with (
            patch("utils.LLM_API_STYLE", "responses"),
            patch("utils.LLM_STREAM", True),
            patch("utils.rq.post", side_effect=[incomplete, recovered]) as post,
            patch("utils.time.sleep") as sleep,
        ):
            tests = get_test_from_LLM("prompt", 1)

        self.assertEqual(tests, ["complete"])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_responses_stream_failure_is_retried_and_reported(self) -> None:
        response = self.stream_response(
            [
                "event: response.failed",
                'data: {"type":"response.failed","response":{"status":"failed"}}',
                "",
            ]
        )

        with (
            patch("utils.LLM_API_STYLE", "responses"),
            patch("utils.LLM_STREAM", True),
            patch("utils.rq.post", return_value=response) as post,
            patch("utils.time.sleep") as sleep,
            self.assertRaisesRegex(LLMProtocolError, "response.failed"),
        ):
            get_test_from_LLM("prompt", 1)

        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(5), call(10)])

    def test_repeated_network_failure_trips_transport_circuit_breaker(self) -> None:
        with (
            patch(
                "utils.rq.post",
                side_effect=[
                    RuntimeError("offline"),
                    ConnectionError("disconnected"),
                    TimeoutError("timed out"),
                ],
            ) as post,
            patch("utils.time.sleep") as sleep,
            self.assertRaisesRegex(
                LLMTransportError,
                "LLM API request failed after 2 retries: TimeoutError",
            ),
        ):
            get_test_from_LLM("prompt", 1)

        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(5), call(10)])


class CreateDirWithCMakeTests(unittest.TestCase):
    def test_can_preserve_an_existing_cmake_file(self) -> None:
        with tempfile.TemporaryDirectory() as project:
            create_dir_with_cmake(project, "llm_tests")
            cmake_path = Path(project, "llm_tests", "CMakeLists.txt")
            cmake_path.write_text("existing target\n")

            create_dir_with_cmake(project, "llm_tests", reset_cmake=False)
            self.assertEqual(cmake_path.read_text(), "existing target\n")

            create_dir_with_cmake(project, "llm_tests")
            self.assertEqual(cmake_path.read_text(), "")


class CoverageSetupTests(unittest.TestCase):
    def test_enables_known_coverage_tool_compatibility_errors(self) -> None:
        self.assertIn("SONARQUBE", SETUP_COV)
        self.assertNotIn("SONARQUBE_ONLY", SETUP_COV)
        self.assertIn("SONARQUBE_ONLY", coverage_setup(True))
        self.assertEqual(coverage_setup(False), SETUP_COV)
        self.assertIn("NO_DEMANGLE", SETUP_COV)
        self.assertIn('"/opt/homebrew/*"', SETUP_COV)
        self.assertIn('"_deps/*"', SETUP_COV)
        self.assertIn(
            '"--ignore-errors" '
            '"source,source,unsupported,inconsistent,inconsistent,'
            'format,format,unused,unused,empty,empty"',
            SETUP_COV,
        )
        self.assertIn(
            'GENHTML_ARGS "--branch-coverage" "--ignore-errors" '
            '"inconsistent,inconsistent"',
            SETUP_COV,
        )


class ProjectPrepareTests(unittest.TestCase):
    def test_preserves_unselected_types_and_creates_missing_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir:
            project_path = Path(project_dir)
            base_cmake = project_path / "llm_tests" / "CMakeLists.txt"
            base_cmake.parent.mkdir()
            base_cmake.write_text("existing target\n")
            selected_cmake = (
                project_path / "llm_tests_req_cxt" / "CMakeLists.txt"
            )
            selected_cmake.parent.mkdir()
            selected_cmake.write_text("stale target\n")

            project = Project("sample", project_dir, f"{project_dir}/build")
            with patch("utgen.cmake_config"):
                project.prepare(["_req_cxt"], just_load=False)

            self.assertEqual(base_cmake.read_text(), "existing target\n")
            self.assertEqual(selected_cmake.read_text(), "")
            for directory in ("llm_tests_cxt", "llm_tests_req"):
                placeholder = project_path / directory / "CMakeLists.txt"
                self.assertTrue(placeholder.is_file())
                self.assertEqual(placeholder.read_text(), "")

    def test_coverage_only_config_hides_test_tree_and_restores_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir:
            cmake_path = (
                Path(project_dir) / "llm_tests_req_cxt" / "CMakeLists.txt"
            )
            cmake_path.parent.mkdir()
            cmake_path.write_text("add_subdirectory(sample.c)\n")
            project = Project("sample", project_dir, f"{project_dir}/build")

            def fail_configuration(*_args) -> None:
                self.assertEqual(cmake_path.read_text(), "")
                raise RuntimeError("configuration failed")

            with (
                patch("utgen.cmake_config", side_effect=fail_configuration),
                self.assertRaisesRegex(RuntimeError, "configuration failed"),
            ):
                project.post_analysis(
                    ["_req_cxt"],
                    do_compile=False,
                    do_test=True,
                    do_export=True,
                    kept_only=True,
                )

            self.assertEqual(
                cmake_path.read_text(), "add_subdirectory(sample.c)\n"
            )

    def test_compile_config_keeps_generated_test_targets_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir:
            cmake_path = (
                Path(project_dir) / "llm_tests_req_cxt" / "CMakeLists.txt"
            )
            cmake_path.parent.mkdir()
            cmake_path.write_text("add_subdirectory(sample.c)\n")
            project = Project("sample", project_dir, f"{project_dir}/build")

            def check_configuration(*_args) -> None:
                self.assertEqual(
                    cmake_path.read_text(), "add_subdirectory(sample.c)\n"
                )

            with (
                patch("utgen.cmake_config", side_effect=check_configuration),
                patch("utgen.Expt") as expt_class,
            ):
                expt_class.return_value.get_type.return_value = "_req_cxt"
                project.post_analysis(
                    ["_req_cxt"],
                    do_compile=True,
                    do_test=False,
                    do_export=False,
                )

            self.assertEqual(
                cmake_path.read_text(), "add_subdirectory(sample.c)\n"
            )

    def test_sonarqube_only_option_reaches_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir:
            cmake_path = (
                Path(project_dir) / "llm_tests_req_cxt" / "CMakeLists.txt"
            )
            cmake_path.parent.mkdir()
            cmake_path.write_text("")
            project = Project("sample", project_dir, f"{project_dir}/build")

            with (
                patch("utgen.cmake_config"),
                patch("utgen.Expt") as expt_class,
            ):
                expt_class.return_value.get_type.return_value = "_req_cxt"
                project.post_analysis(
                    ["_req_cxt"],
                    do_compile=False,
                    do_test=False,
                    do_export=False,
                    coverage_sonarqube_only=True,
                )

            expt_class.assert_called_once_with(
                "_req_cxt",
                project_dir,
                coverage_sonarqube_only=True,
                coverage_unity=False,
            )

    def test_coverage_unity_option_reaches_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir:
            cmake_path = (
                Path(project_dir) / "llm_tests_req_cxt" / "CMakeLists.txt"
            )
            cmake_path.parent.mkdir()
            cmake_path.write_text("")
            project = Project("sample", project_dir, f"{project_dir}/build")

            with (
                patch("utgen.cmake_config"),
                patch("utgen.Expt") as expt_class,
            ):
                expt_class.return_value.get_type.return_value = "_req_cxt"
                project.post_analysis(
                    ["_req_cxt"],
                    do_compile=False,
                    do_test=False,
                    do_export=False,
                    coverage_unity=True,
                )

            expt_class.assert_called_once_with(
                "_req_cxt",
                project_dir,
                coverage_sonarqube_only=False,
                coverage_unity=True,
            )


if __name__ == "__main__":
    unittest.main()
