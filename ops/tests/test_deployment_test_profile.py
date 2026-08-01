import unittest

from ops.deployment_test_profile import (
    TestProfile,
    TestProfileError,
    ValidationEvidence,
    requires_django_test_database,
    validate_predeployment_evidence,
    validate_test_command,
)


D7 = "d7de839466e3cf3e1a8455376a48f1292d6d4e08"
TARGET = "1bc524a487811c3a524025ceb323f82e4821e7bc"


def approved_evidence():
    return ValidationEvidence(
        target_sha=TARGET,
        commit_sequence=(D7, TARGET),
        api_v2_passed=27,
        http_client_passed=8,
        ops_passed=87,
        linux_repetitions_passed=True,
        postgresql_major=17,
    )


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


if __name__ == "__main__":
    unittest.main()
