import os
import subprocess
import unittest

PATHS = {
    ".github/workflows/ci.yml": {"governance", "api", "api-db", "worker", "parser", "web", "contracts", "fixtures", "db", "db-unit", "scoring"},
    "scripts/check.sh": {"governance", "api", "api-db", "worker", "parser", "web", "contracts", "fixtures", "db", "db-unit", "scoring"},
    "tools/governance/run_tests.py": {"governance", "api", "api-db", "worker", "parser", "web", "contracts", "fixtures", "db", "db-unit", "scoring"},
    "tools/governance/check_repo.py": {"governance"},
    "services/api/lab_api/main.py": {"api", "api-db"},
    "tests/api/test_app.py": {"api", "api-db"},
    "tests/api_db/test_readiness_real_db.py": {"api-db"},
    "deploy/compose.dev.yml": {"api-db"},
    "services/worker/cmd/lab-worker/main.go": {"worker"},
    "services/worker/go.mod": {"worker"},
    "docs/TESTING.md": {"governance"},
    "contracts/lab_contracts/source.py": {"contracts", "fixtures", "api", "api-db", "scoring"},
    "contracts/README.md": {"contracts", "fixtures", "api", "api-db", "governance", "scoring"},
    "fixtures/registry.yaml": {"fixtures", "parser"},
    "fixtures/F06/vectors.json": {"fixtures", "scoring"},
    "tests/fixtures/test_registry.py": {"fixtures", "parser"},
    "tests/contracts/test_foundation.py": {"contracts", "fixtures", "api", "api-db", "scoring"},
    "db/lab_db/repo.py": {"db", "db-unit"},
    "db/README.md": {"db", "db-unit", "governance"},
    "tests/db/test_outbox.py": {"db"},
    "tests/dbunit/test_export.py": {"db-unit"},
    "scoring/lab_scoring/rules.py": {"scoring"},
    "tests/scoring/test_cross_checks.py": {"scoring"},
    "web/src/App.tsx": {"web"},
    "web/package-lock.json": {"web"},
    "web/README.md": {"web", "governance"},
    "services/parser/README.md": {"parser", "governance"},
    "services/parser/lab_parser/main.py": {"parser"},
    "tests/parser/test_parser_service.py": {"parser"},
    "unknown/file.go": set(),  # unmapped -> check.sh runs all suites
}


class CheckShMappingTests(unittest.TestCase):
    def test_suite_mapping(self):
        out = subprocess.run(
            ["bash", "scripts/check.sh"],
            env={**os.environ, "CHECK_SH_PRINT_SUITE_FOR": " ".join(PATHS)},
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        got = {}
        for line in out.splitlines():
            path, _, suites = line.partition(": ")
            got[path] = set(suites.split())
        self.assertEqual(got, PATHS)


if __name__ == "__main__":
    unittest.main()
