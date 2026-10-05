"""Регрессия: abandoned и пропущенный существующий компонент не дают зелёный CI."""

import unittest
from scripts.check_ci_results import CiResultPolicy


class CiResultPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(REPOSITORY_RESULT="success", FRONTEND_RESULT="success",
                        BACKEND_RESULT="success", FRONTEND_ACTIVE="true", BACKEND_ACTIVE="true")

    def test_success_and_missing_component(self):
        self.assertIsNone(CiResultPolicy.validate(self.env))
        self.env.update(BACKEND_RESULT="skipped", BACKEND_ACTIVE="false")
        self.assertIsNone(CiResultPolicy.validate(self.env))

    def test_every_non_success_fails_for_required_jobs(self):
        for component in ("REPOSITORY", "FRONTEND", "BACKEND"):
            for result in ("abandoned", "cancelled", "failure", "skipped", "", "queued", "unknown"):
                with self.subTest(component=component, result=result):
                    env = {**self.env, f"{component}_RESULT": result}
                    self.assertIsNotNone(CiResultPolicy.validate(env))

    def test_detection_must_be_present(self):
        for value in ("", "maybe", None):
            self.assertIsNotNone(CiResultPolicy.validate({**self.env, "BACKEND_ACTIVE": value}))


if __name__ == "__main__":
    unittest.main()
