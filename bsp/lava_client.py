"""
LAVA REST API client for submitting and monitoring HIL test jobs.

This module wraps the LAVA v0.2 REST API.  Authentication is performed via a
token header (``Authorization: Token <token>``).  The client is intentionally
lightweight — it only covers the subset of the API needed by the ``bsp test``
command:

* Submitting a job definition YAML
* Polling job status until completion (or timeout)
* Fetching per-suite/per-case test results

All network I/O is handled by the ``requests`` library.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import requests
    from requests import Response
    REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    REQUESTS_AVAILABLE = False

# =============================================================================
# Result data classes
# =============================================================================


@dataclass
class LavaTestCase:
    """
    A single Robot Framework / LAVA test case result.

    Attributes:
        name: Test case name
        result: ``"pass"`` or ``"fail"`` (lower-case, as returned by LAVA)
        metadata: Optional extra metadata dict from the LAVA API
    """
    name: str
    result: str
    metadata: Dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Return ``True`` when the test case result is ``"pass"``."""
        return self.result.lower() == "pass"


@dataclass
class LavaTestSuite:
    """
    A collection of test cases belonging to the same suite.

    Attributes:
        name: Suite name
        cases: Individual test case results
    """
    name: str
    cases: List[LavaTestCase] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return ``True`` when every test case in the suite passed."""
        return all(tc.passed for tc in self.cases)

    @property
    def total(self) -> int:
        """Total number of test cases."""
        return len(self.cases)

    @property
    def failures(self) -> int:
        """Number of failed test cases."""
        return sum(1 for tc in self.cases if not tc.passed)


# =============================================================================
# LAVA client
# =============================================================================


class LavaClient:
    """
    Thin wrapper around the LAVA v0.2 REST API.

    Args:
        server: Base URL of the LAVA server (e.g. ``https://lava.example.com``).
                A trailing slash is stripped automatically.
        token: Authentication token (``Authorization: Token <token>``).
        username: LAVA username (currently unused by the v0.2 API but kept for
                  forward compatibility).
        timeout: HTTP request timeout in seconds (default: 30).
    """

    _API_PREFIX = "/api/v0.2"

    def __init__(
        self,
        server: str,
        token: str = "",
        username: str = "",
        timeout: int = 30,
    ) -> None:
        if not REQUESTS_AVAILABLE:
            raise RuntimeError(  # pragma: no cover
                "The 'requests' package is required for LAVA integration. "
                "Install it with: pip install requests"
            )
        self.server = server.rstrip("/")
        self.token = token
        self.username = username
        self.timeout = timeout
        self.logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        """Build the HTTP headers dict (auth + content-type)."""
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        return headers

    def _url(self, path: str) -> str:
        """Construct a full URL from a relative API path."""
        return f"{self.server}{self._API_PREFIX}{path}"

    def _get(self, path: str) -> dict:
        """Perform a GET request and return the parsed JSON body."""
        url = self._url(path)
        self.logger.debug("GET %s", url)
        resp: Response = requests.get(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        """Perform a POST request and return the parsed JSON body."""
        url = self._url(path)
        self.logger.debug("POST %s", url)
        resp: Response = requests.post(
            url, data=data, headers=self._headers(), timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_job(self, job_yaml: str) -> int:
        """
        Submit a LAVA job definition and return the assigned job ID.

        Args:
            job_yaml: Complete LAVA job definition in YAML format.

        Returns:
            Integer job ID assigned by the LAVA scheduler.

        Raises:
            requests.HTTPError: On 4xx/5xx responses.
            ValueError: When the server response does not contain a job ID.
        """
        result = self._post("/jobs/", {"definition": job_yaml})
        job_ids = result.get("job_ids")
        if job_ids:
            job_id = int(job_ids[0])
        else:
            job_id = result.get("id")
            if job_id is None:
                raise ValueError(
                    f"LAVA did not return a job ID. Response: {result}"
                )
            job_id = int(job_id)
        self.logger.info("Submitted LAVA job — ID: %d", job_id)
        return job_id

    def get_job_status(self, job_id: int) -> str:
        """
        Return the current state of a LAVA job.

        Args:
            job_id: LAVA job identifier.

        Returns:
            Job state string as returned by LAVA (e.g. ``"Submitted"``,
            ``"Running"``, ``"Finished"``).
        """
        result = self._get(f"/jobs/{job_id}/")
        return result.get("state", "Unknown")

    def get_job_health(self, job_id: int) -> str:
        """
        Return the health (pass/fail outcome) of a finished LAVA job.

        Args:
            job_id: LAVA job identifier.

        Returns:
            Health string as returned by LAVA (e.g. ``"Complete"``,
            ``"Incomplete"``, ``"Canceled"``).
        """
        result = self._get(f"/jobs/{job_id}/")
        return result.get("health", "Unknown")

    def wait_for_job(
        self,
        job_id: int,
        timeout: int = 3600,
        poll_interval: int = 30,
    ) -> str:
        """
        Block until a LAVA job finishes or the timeout expires.

        Args:
            job_id: LAVA job identifier.
            timeout: Maximum wait time in seconds.
            poll_interval: How often to poll the LAVA API in seconds.

        Returns:
            Health string of the finished job (e.g. ``"Complete"``).

        Raises:
            TimeoutError: When the job has not finished within *timeout* seconds.
        """
        elapsed = 0
        self.logger.info(
            "Waiting for LAVA job %d (timeout: %ds, poll every %ds)...",
            job_id,
            timeout,
            poll_interval,
        )
        while elapsed < timeout:
            state = self.get_job_status(job_id)
            self.logger.debug("Job %d state: %s (elapsed: %ds)", job_id, state, elapsed)
            if state == "Finished":
                health = self.get_job_health(job_id)
                self.logger.info(
                    "LAVA job %d finished — health: %s", job_id, health
                )
                return health
            time.sleep(poll_interval)
            elapsed += poll_interval

        raise TimeoutError(
            f"LAVA job {job_id} did not finish within {timeout} seconds."
        )

    def get_job_results(self, job_id: int) -> List[LavaTestSuite]:
        """
        Fetch per-suite test results for a finished LAVA job.

        Results are fetched from ``/api/v0.2/jobs/{id}/suites/`` and the
        individual test cases from ``/api/v0.2/jobs/{id}/tests/``.

        Args:
            job_id: LAVA job identifier.

        Returns:
            List of :class:`LavaTestSuite` objects, each containing the test cases
            that belong to it.
        """
        # Fetch all test cases for the job in one call
        tests_url = f"/jobs/{job_id}/tests/"
        all_cases: List[dict] = []
        page_url: Optional[str] = tests_url
        while page_url:
            if page_url.startswith("/"):
                data = self._get(page_url)
            else:
                # Absolute URL returned by LAVA pagination
                self.logger.debug("GET %s", page_url)
                resp = requests.get(
                    page_url, headers=self._headers(), timeout=self.timeout
                )
                resp.raise_for_status()
                data = resp.json()
            all_cases.extend(data.get("results", []))
            next_link = data.get("next")
            # Strip server prefix so we can reuse _get()
            if next_link and next_link.startswith(self.server):
                next_link = next_link[len(self.server):]
            page_url = next_link or None

        # Group by suite name
        suites: Dict[str, LavaTestSuite] = {}
        for tc in all_cases:
            suite_name = tc.get("suite", "default")
            if suite_name not in suites:
                suites[suite_name] = LavaTestSuite(name=suite_name)
            suites[suite_name].cases.append(
                LavaTestCase(
                    name=tc.get("name", ""),
                    result=tc.get("result", "unknown"),
                    metadata=tc.get("metadata") or {},
                )
            )

        return list(suites.values())

    def job_url(self, job_id: int) -> str:
        """Return the human-readable scheduler URL for a job."""
        return f"{self.server}/scheduler/job/{job_id}"
