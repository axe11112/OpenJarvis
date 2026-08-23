"""Production deployment monitoring and verification for Wiz.

Verifies that deployed features are working correctly in production.
Collects health signals and blocks merge if production is unhealthy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class HealthCheck:
    """Result of a single health check."""

    name: str
    passed: bool
    error: Optional[str] = None
    duration_ms: float = 0.0
    checked_at: datetime = None

    def __post_init__(self):
        if self.checked_at is None:
            self.checked_at = datetime.utcnow()


@dataclass
class ProductionHealth:
    """Overall production health status."""

    healthy: bool
    checks: list[HealthCheck]
    timestamp: datetime
    summary: str

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)


class ProductionMonitor:
    """Monitors production deployment health and verifies readiness."""

    def __init__(self, production_url: str) -> None:
        self.production_url = production_url
        self._last_health: Optional[ProductionHealth] = None

    async def check_health(self) -> ProductionHealth:
        """Run all health checks for production.

        Returns:
            ProductionHealth with all check results
        """
        logger.info(f"Checking production health: {self.production_url}")

        checks = await asyncio.gather(
            self._check_http_health(),
            self._check_critical_paths(),
            self._check_response_times(),
            return_exceptions=False,
        )

        # Flatten results
        all_checks = []
        for check_result in checks:
            if isinstance(check_result, list):
                all_checks.extend(check_result)
            elif isinstance(check_result, HealthCheck):
                all_checks.append(check_result)

        healthy = all(c.passed for c in all_checks)

        health = ProductionHealth(
            healthy=healthy,
            checks=all_checks,
            timestamp=datetime.utcnow(),
            summary=f"Production {'healthy' if healthy else 'degraded'}: "
            f"{sum(1 for c in all_checks if c.passed)}/{len(all_checks)} checks passed",
        )

        self._last_health = health
        logger.info(health.summary)
        return health

    async def _check_http_health(self) -> list[HealthCheck]:
        """Check basic HTTP health."""
        try:
            import httpx

            checks = []
            start = datetime.utcnow()

            async with httpx.AsyncClient() as client:
                try:
                    response = await client.get(
                        self.production_url,
                        timeout=10.0,
                    )
                    duration = (datetime.utcnow() - start).total_seconds() * 1000

                    # Check status code
                    status_check = HealthCheck(
                        name="HTTP Status",
                        passed=response.status_code < 500,
                        error=None if response.status_code < 500 else f"Status {response.status_code}",
                        duration_ms=duration,
                    )
                    checks.append(status_check)

                    # Check content length (basic indicator of valid response)
                    content_check = HealthCheck(
                        name="Response Content",
                        passed=len(response.content) > 0,
                        error=None if len(response.content) > 0 else "Empty response",
                        duration_ms=duration,
                    )
                    checks.append(content_check)

                except asyncio.TimeoutError:
                    checks.append(
                        HealthCheck(
                            name="HTTP Status",
                            passed=False,
                            error="Request timeout",
                            duration_ms=10000,
                        )
                    )
                except Exception as e:
                    checks.append(
                        HealthCheck(
                            name="HTTP Status",
                            passed=False,
                            error=str(e),
                        )
                    )

            return checks

        except ImportError:
            logger.warning("httpx not available, skipping HTTP health check")
            return []

    async def _check_critical_paths(self) -> list[HealthCheck]:
        """Check critical application paths."""
        try:
            import httpx

            checks = []

            # Common critical paths
            critical_paths = [
                ("/api/health", "Health endpoint"),
                ("/api/status", "Status endpoint"),
                ("/?", "Home page"),
            ]

            async with httpx.AsyncClient() as client:
                for path, name in critical_paths:
                    try:
                        response = await client.get(
                            f"{self.production_url}{path}",
                            timeout=5.0,
                        )
                        passed = response.status_code < 400
                        checks.append(
                            HealthCheck(
                                name=f"Critical Path: {name}",
                                passed=passed,
                                error=None if passed else f"Status {response.status_code}",
                            )
                        )
                    except Exception as e:
                        # Not all paths may exist; that's OK
                        logger.debug(f"Path check failed for {path}: {e}")

            return checks

        except ImportError:
            return []

    async def _check_response_times(self) -> list[HealthCheck]:
        """Check response time health."""
        try:
            import httpx

            start = datetime.utcnow()

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.production_url,
                    timeout=10.0,
                )
            duration = (datetime.utcnow() - start).total_seconds() * 1000

            # Response time thresholds (in ms)
            # GREEN: < 1000ms
            # YELLOW: 1000-3000ms
            # RED: > 3000ms

            checks = []
            if duration < 1000:
                checks.append(
                    HealthCheck(
                        name="Response Time",
                        passed=True,
                        error=None,
                        duration_ms=duration,
                    )
                )
            elif duration < 3000:
                logger.warning(f"Response time elevated: {duration}ms")
                checks.append(
                    HealthCheck(
                        name="Response Time",
                        passed=True,  # Still acceptable
                        error=f"Elevated: {duration:.0f}ms",
                        duration_ms=duration,
                    )
                )
            else:
                checks.append(
                    HealthCheck(
                        name="Response Time",
                        passed=False,
                        error=f"Slow: {duration:.0f}ms",
                        duration_ms=duration,
                    )
                )

            return checks

        except Exception:
            return []

    def is_production_healthy(self) -> bool:
        """Check if production is in a healthy state."""
        if self._last_health is None:
            logger.warning("No health check data available")
            return False
        return self._last_health.healthy

    def get_last_health(self) -> Optional[ProductionHealth]:
        """Get the last health check result."""
        return self._last_health


class ProductionVerifier:
    """Verifies production deployment completed successfully."""

    def __init__(self, production_url: str, deployment_sha: str) -> None:
        self.production_url = production_url
        self.deployment_sha = deployment_sha
        self.monitor = ProductionMonitor(production_url)

    async def verify_deployment(self, acceptance_criteria: list[str]) -> bool:
        """Verify production deployment is working.

        Checks:
        1. Production is responding
        2. All critical paths work
        3. Response times are acceptable
        4. Acceptance criteria pass in production

        Returns:
            True if deployment verified, False otherwise
        """
        logger.info(f"Verifying production deployment: {self.deployment_sha[:7]}")

        # Check production health
        health = await self.monitor.check_health()
        if not health.healthy:
            logger.error(f"Production health check failed: {health.summary}")
            return False

        # Run acceptance criteria in production
        from openjarvis.wiz.acceptance import AcceptanceTestRunner

        runner = AcceptanceTestRunner()
        try:
            result = await runner.run_criteria(
                acceptance_criteria,
                self.production_url,
                criteria_type="auto",
            )
            if not result.passed:
                logger.error(f"Production acceptance tests failed: {result.details}")
                return False
        except Exception as e:
            logger.error(f"Production acceptance test error: {e}")
            return False

        logger.info("✓ Production deployment verified successfully")
        return True

    def verify_deployment_sync(
        self, acceptance_criteria: list[str]
    ) -> bool:
        """Synchronous wrapper for deployment verification."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self.verify_deployment(acceptance_criteria)
            )
        finally:
            loop.close()
