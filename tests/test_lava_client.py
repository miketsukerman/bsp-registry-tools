"""
Tests for LavaClient (bsp/lava_client.py).

All network I/O is mocked via unittest.mock so no live LAVA server is needed.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from bsp.lava_client import LavaClient, LavaTestCase, LavaTestSuite


# =============================================================================
# TestCase / TestSuite unit tests
# =============================================================================

class TestTestCase:
    def test_passed_true_for_pass(self):
        tc = LavaTestCase(name="boot", result="pass")
        assert tc.passed is True

    def test_passed_false_for_fail(self):
        tc = LavaTestCase(name="boot", result="fail")
        assert tc.passed is False

    def test_passed_case_insensitive(self):
        assert LavaTestCase(name="x", result="PASS").passed is True
        assert LavaTestCase(name="x", result="FAIL").passed is False

    def test_metadata_defaults_to_empty_dict(self):
        tc = LavaTestCase(name="x", result="pass")
        assert tc.metadata == {}


class TestTestSuite:
    def test_passed_when_all_cases_pass(self):
        suite = LavaTestSuite(name="smoke", cases=[
            LavaTestCase("a", "pass"),
            LavaTestCase("b", "pass"),
        ])
        assert suite.passed is True

    def test_not_passed_when_any_case_fails(self):
        suite = LavaTestSuite(name="smoke", cases=[
            LavaTestCase("a", "pass"),
            LavaTestCase("b", "fail"),
        ])
        assert suite.passed is False

    def test_total(self):
        suite = LavaTestSuite(name="s", cases=[LavaTestCase("a", "pass"), LavaTestCase("b", "fail")])
        assert suite.total == 2

    def test_failures(self):
        suite = LavaTestSuite(name="s", cases=[LavaTestCase("a", "pass"), LavaTestCase("b", "fail")])
        assert suite.failures == 1

    def test_empty_suite_passes(self):
        suite = LavaTestSuite(name="empty")
        assert suite.passed is True
        assert suite.total == 0
        assert suite.failures == 0


# =============================================================================
# LavaClient unit tests (mocked HTTP)
# =============================================================================

def _make_client(**kwargs) -> LavaClient:
    """Return a LavaClient pointing at a fake server."""
    return LavaClient(server="http://lava.test", token="testtoken", **kwargs)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock requests.Response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


class TestLavaClientInit:
    def test_strips_trailing_slash(self):
        c = LavaClient(server="http://lava.test/")
        assert c.server == "http://lava.test"

    def test_stores_token(self):
        c = LavaClient(server="http://lava.test", token="abc")
        assert c.token == "abc"

    def test_headers_include_auth(self):
        c = LavaClient(server="http://lava.test", token="mytoken")
        assert c._headers()["Authorization"] == "Token mytoken"

    def test_headers_no_auth_when_no_token(self):
        c = LavaClient(server="http://lava.test", token="")
        assert "Authorization" not in c._headers()

    def test_url_construction(self):
        c = _make_client()
        assert c._url("/jobs/1/") == "http://lava.test/api/v0.2/jobs/1/"

    def test_job_url(self):
        c = _make_client()
        assert c.job_url(42) == "http://lava.test/scheduler/job/42"


class TestLavaClientSubmitJob:
    @patch("requests.post")
    def test_submit_returns_job_id(self, mock_post):
        mock_post.return_value = _mock_response({"job_ids": ["99"]})
        c = _make_client()
        job_id = c.submit_job("job: yaml")
        assert job_id == 99

    @patch("requests.post")
    def test_submit_falls_back_to_id_field(self, mock_post):
        mock_post.return_value = _mock_response({"id": 55})
        c = _make_client()
        job_id = c.submit_job("job: yaml")
        assert job_id == 55

    @patch("requests.post")
    def test_submit_raises_when_no_id(self, mock_post):
        mock_post.return_value = _mock_response({})
        c = _make_client()
        with pytest.raises(ValueError, match="did not return a job ID"):
            c.submit_job("job: yaml")

    @patch("requests.post")
    def test_submit_sends_definition(self, mock_post):
        mock_post.return_value = _mock_response({"job_ids": ["1"]})
        c = _make_client()
        c.submit_job("my_yaml_content")
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["data"]["definition"] == "my_yaml_content"


class TestLavaClientGetStatus:
    @patch("requests.get")
    def test_get_job_status(self, mock_get):
        mock_get.return_value = _mock_response({"state": "Running"})
        c = _make_client()
        assert c.get_job_status(1) == "Running"

    @patch("requests.get")
    def test_get_job_health(self, mock_get):
        mock_get.return_value = _mock_response({"state": "Finished", "health": "Complete"})
        c = _make_client()
        assert c.get_job_health(1) == "Complete"

    @patch("requests.get")
    def test_unknown_state_when_missing(self, mock_get):
        mock_get.return_value = _mock_response({})
        c = _make_client()
        assert c.get_job_status(1) == "Unknown"


class TestLavaClientWaitForJob:
    @patch("time.sleep", return_value=None)
    @patch("requests.get")
    def test_wait_returns_health_on_finished(self, mock_get, _sleep):
        mock_get.return_value = _mock_response({"state": "Finished", "health": "Complete"})
        c = _make_client()
        health = c.wait_for_job(1, timeout=60, poll_interval=1)
        assert health == "Complete"

    @patch("time.sleep", return_value=None)
    @patch("requests.get")
    def test_wait_polls_until_finished(self, mock_get, _sleep):
        running = _mock_response({"state": "Running", "health": "Unknown"})
        finished = _mock_response({"state": "Finished", "health": "Complete"})
        mock_get.side_effect = [running, running, finished, finished]
        c = _make_client()
        health = c.wait_for_job(1, timeout=60, poll_interval=1)
        assert health == "Complete"

    @patch("time.sleep", return_value=None)
    @patch("requests.get")
    def test_wait_raises_timeout(self, mock_get, _sleep):
        mock_get.return_value = _mock_response({"state": "Running"})
        c = _make_client()
        with pytest.raises(TimeoutError):
            c.wait_for_job(1, timeout=2, poll_interval=3)


class TestLavaClientGetResults:
    @patch("requests.get")
    def test_get_results_groups_by_suite(self, mock_get):
        mock_get.return_value = _mock_response({
            "results": [
                {"suite": "smoke", "name": "boot", "result": "pass"},
                {"suite": "smoke", "name": "ping", "result": "fail"},
                {"suite": "network", "name": "wget", "result": "pass"},
            ],
            "next": None,
        })
        c = _make_client()
        suites = c.get_job_results(1)
        assert len(suites) == 2
        suite_names = {s.name for s in suites}
        assert "smoke" in suite_names
        assert "network" in suite_names

    @patch("requests.get")
    def test_get_results_empty_returns_empty_list(self, mock_get):
        mock_get.return_value = _mock_response({"results": [], "next": None})
        c = _make_client()
        suites = c.get_job_results(1)
        assert suites == []

    @patch("requests.get")
    def test_get_results_handles_missing_suite_name(self, mock_get):
        mock_get.return_value = _mock_response({
            "results": [{"name": "x", "result": "pass"}],
            "next": None,
        })
        c = _make_client()
        suites = c.get_job_results(1)
        assert suites[0].name == "default"
