import unittest

from ops.deployment_test_profile import (
    MINIMUM_API_V2_PASSED,
    MINIMUM_DEPLOYMENT_HARDENING_PASSED,
    MINIMUM_DEPLOYMENT_TEST_PROFILE_PASSED,
    MINIMUM_HTTP_CLIENT_PASSED,
    MINIMUM_NGINX_DIAGNOSTICS_PASSED,
    MINIMUM_OPS_PASSED,
    BootstrapEvidence,
    TestProfile,
    TestProfileError,
    ValidationEvidence,
    requires_django_test_database,
    validate_bootstrap_evidence,
    validate_predeployment_evidence,
    validate_test_command,
)


D7 = "d7de839466e3cf3e1a8455376a48f1292d6d4e08"
TARGET = "1bc524a487811c3a524025ceb323f82e4821e7bc"
CUMULATIVE_TARGET = "617e450675776128f37b7d527b9795a3d0c042dc"
CUMULATIVE_PATHS = (
    "ops/README.md",
    "ops/deployment_hardening.py",
    "ops/deployment_test_profile.py",
    "ops/td02c_authenticated_get_runner.py",
    "ops/td02c_http_client.py",
    "ops/tests/test_deployment_hardening.py",
    "ops/tests/test_deployment_test_profile.py",
    "ops/tests/test_td02c_authenticated_get_runner.py",
)


def approved_bootstrap_evidence(**overrides):
    values = {
        "target_sha": CUMULATIVE_TARGET,
        "commit_sequence": (CUMULATIVE_TARGET,),
        "authorized_paths": CUMULATIVE_PATHS,
        "api_v2_passed": MINIMUM_API_V2_PASSED,
        "http_client_passed": MINIMUM_HTTP_CLIENT_PASSED,
        "nginx_diagnostics_passed": MINIMUM_NGINX_DIAGNOSTICS_PASSED,
        "deployment_test_profile_passed": MINIMUM_DEPLOYMENT_TEST_PROFILE_PASSED,
        "deployment_hardening_passed": MINIMUM_DEPLOYMENT_HARDENING_PASSED,
        "ops_passed": MINIMUM_OPS_PASSED,
        "linux_repetitions_passed": True,
        "postgresql_major": 17,
    }
    values.update(overrides)
    return BootstrapEvidence(**values)


def approved_evidence(**overrides):
    values = {
        "target_sha": TARGET,
        "commit_sequence": (D7, TARGET),
        "api_v2_passed": MINIMUM_API_V2_PASSED,
        "http_client_passed": MINIMUM_HTTP_CLIENT_PASSED,
        "ops_passed": MINIMUM_OPS_PASSED,
        "linux_repetitions_passed": True,
        "postgresql_major": 17,
    }
    values.update(overrides)
    return ValidationEvidence(**values)


class ProductionTestProfileTests(unittest.TestCase):
    def test_detects_django_suite_that_requires_test_database(self):
        self.assertTrue(
            requires_django_test_database(
                ["python", "manage.py", "test", "relay.tests.test_bulk_v2_api"]
            )
        )

    def test_rejects_database_creating_suite_in_production(self):
        with self.assertRaisesRegex(TestProfileError, "forbidden in production"):
            validate_test_command(
                TestProfile.PRODUCTION,
                ["python", "manage.py", "test", "relay.tests.test_bulk_v2_api"],
                effective_user="app",
            )

    def test_allows_database_creating_suite_in_isolated_profile(self):
        validate_test_command(
            TestProfile.ISOLATED,
            ["python", "manage.py", "test", "relay.tests.test_bulk_v2_api"],
            effective_user="tester",
        )

    def test_production_profile_never_requests_createdb(self):
        for command in (
            ["createdb", "test_doppler_prod"],
            ["psql", "-c", "ALTER ROLE app CREATEDB"],
            ["psql", "-c", "CREATE DATABASE test_doppler_prod"],
        ):
            with self.subTest(command=command):
                with self.assertRaises(TestProfileError):
                    validate_test_command(
                        TestProfile.PRODUCTION, command, effective_user="app"
                    )

    def test_production_profile_rejects_superusers(self):
        for user in ("root", "postgres"):
            with self.subTest(user=user):
                with self.assertRaisesRegex(TestProfileError, "privileged"):
                    validate_test_command(
                        TestProfile.PRODUCTION,
                        ["python", "-m", "py_compile", "ops/tool.py"],
                        effective_user=user,
                    )

    def test_allows_non_destructive_production_checks_as_app(self):
        commands = (
            ["python", "-m", "py_compile", "ops/tool.py"],
            ["python", "-m", "unittest", "discover", "-s", "ops/tests"],
            ["python", "manage.py", "check", "--deploy"],
        )
        for command in commands:
            with self.subTest(command=command):
                validate_test_command(
                    TestProfile.PRODUCTION, command, effective_user="app"
                )

    def test_accepts_evidence_bound_to_exact_target(self):
        validate_predeployment_evidence(
            approved_evidence(), target_sha=TARGET, expected_commits=(D7, TARGET)
        )

    def test_rejects_evidence_for_another_sha(self):
        wrong = "a" * 40
        with self.assertRaisesRegex(TestProfileError, "does not match"):
            validate_predeployment_evidence(
                approved_evidence(), target_sha=wrong,
                expected_commits=(D7, wrong),
            )

    def test_evidence_above_minimum_floors_is_accepted(self):
        """Floors are minimums, not exact snapshots: suites may grow over time."""
        validate_predeployment_evidence(
            approved_evidence(
                api_v2_passed=MINIMUM_API_V2_PASSED + 5,
                http_client_passed=MINIMUM_HTTP_CLIENT_PASSED + 5,
                ops_passed=MINIMUM_OPS_PASSED + 5,
            ),
            target_sha=TARGET, expected_commits=(D7, TARGET),
        )

    def test_evidence_below_minimum_floors_is_rejected(self):
        cases = {
            "api_v2_passed": (MINIMUM_API_V2_PASSED - 1, "API V2"),
            "http_client_passed": (MINIMUM_HTTP_CLIENT_PASSED - 1, "HTTP client"),
            "ops_passed": (MINIMUM_OPS_PASSED - 1, "Ops suite"),
        }
        for field, (value, message) in cases.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(TestProfileError, message):
                    validate_predeployment_evidence(
                        approved_evidence(**{field: value}),
                        target_sha=TARGET, expected_commits=(D7, TARGET),
                    )


class BootstrapEvidenceTests(unittest.TestCase):
    def test_accepts_evidence_bound_to_exact_target_and_paths(self):
        validate_bootstrap_evidence(
            approved_bootstrap_evidence(),
            target_sha=CUMULATIVE_TARGET,
            expected_commits=(CUMULATIVE_TARGET,),
            authorized_paths=CUMULATIVE_PATHS,
        )

    def test_rejects_evidence_for_another_sha(self):
        wrong = "b" * 40
        with self.assertRaisesRegex(TestProfileError, "does not match"):
            validate_bootstrap_evidence(
                approved_bootstrap_evidence(), target_sha=wrong,
                expected_commits=(wrong,), authorized_paths=CUMULATIVE_PATHS,
            )

    def test_rejects_evidence_with_extra_or_missing_authorized_path(self):
        for candidate in (
            CUMULATIVE_PATHS + ("ops/extra.py",),
            CUMULATIVE_PATHS[:-1],
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(TestProfileError, "authorized paths"):
                    validate_bootstrap_evidence(
                        approved_bootstrap_evidence(),
                        target_sha=CUMULATIVE_TARGET,
                        expected_commits=(CUMULATIVE_TARGET,),
                        authorized_paths=candidate,
                    )

    def test_evidence_above_minimum_floors_is_accepted(self):
        validate_bootstrap_evidence(
            approved_bootstrap_evidence(
                api_v2_passed=MINIMUM_API_V2_PASSED + 1,
                http_client_passed=MINIMUM_HTTP_CLIENT_PASSED + 1,
                nginx_diagnostics_passed=MINIMUM_NGINX_DIAGNOSTICS_PASSED + 1,
                deployment_test_profile_passed=MINIMUM_DEPLOYMENT_TEST_PROFILE_PASSED + 1,
                deployment_hardening_passed=MINIMUM_DEPLOYMENT_HARDENING_PASSED + 1,
                ops_passed=MINIMUM_OPS_PASSED + 1,
            ),
            target_sha=CUMULATIVE_TARGET,
            expected_commits=(CUMULATIVE_TARGET,),
            authorized_paths=CUMULATIVE_PATHS,
        )

    def test_evidence_below_minimum_floors_is_rejected(self):
        cases = {
            "api_v2_passed": (MINIMUM_API_V2_PASSED - 1, "API V2"),
            "http_client_passed": (MINIMUM_HTTP_CLIENT_PASSED - 1, "HTTP client"),
            "nginx_diagnostics_passed": (MINIMUM_NGINX_DIAGNOSTICS_PASSED - 1, "Nginx discovery diagnostics"),
            "deployment_test_profile_passed": (MINIMUM_DEPLOYMENT_TEST_PROFILE_PASSED - 1, "deployment_test_profile"),
            "deployment_hardening_passed": (MINIMUM_DEPLOYMENT_HARDENING_PASSED - 1, "deployment_hardening"),
            "ops_passed": (MINIMUM_OPS_PASSED - 1, "ops suite"),
        }
        for field, (value, message) in cases.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(TestProfileError, message):
                    validate_bootstrap_evidence(
                        approved_bootstrap_evidence(**{field: value}),
                        target_sha=CUMULATIVE_TARGET,
                        expected_commits=(CUMULATIVE_TARGET,),
                        authorized_paths=CUMULATIVE_PATHS,
                    )

    def test_rejects_wrong_commit_sequence_order(self):
        old = "e47582655d84c1da85880ff8f1b55a731e5be4c5"
        with self.assertRaisesRegex(TestProfileError, "commit sequence"):
            validate_bootstrap_evidence(
                approved_bootstrap_evidence(
                    commit_sequence=(CUMULATIVE_TARGET, old),
                ),
                target_sha=CUMULATIVE_TARGET,
                expected_commits=(old, CUMULATIVE_TARGET),
                authorized_paths=CUMULATIVE_PATHS,
            )

    def test_bootstrap_evidence_field_set_differs_from_validation_evidence(self):
        """The loaders reject on exact field-set inequality (see
        ops.deployment_hardening._load_validation_evidence /
        _load_bootstrap_evidence), so any difference in the field sets is
        enough to make the two schemas mutually non-substitutable."""
        import dataclasses

        bootstrap_fields = {field.name for field in dataclasses.fields(BootstrapEvidence)}
        validation_fields = {field.name for field in dataclasses.fields(ValidationEvidence)}
        self.assertNotEqual(bootstrap_fields, validation_fields)
        self.assertTrue(validation_fields < bootstrap_fields)


if __name__ == "__main__":
    unittest.main()
