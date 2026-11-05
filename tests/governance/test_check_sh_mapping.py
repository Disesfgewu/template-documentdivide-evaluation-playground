import os
import subprocess
import unittest

PATHS = {
    ".github/workflows/ci.yml": {"governance", "api", "api-db", "worker", "parser", "web", "contracts"},
    "scripts/check.sh": {"governance", "api", "api-db", "worker", "parser", "web", "contracts"},
    "tools/governance/run_tests.py": {"governance", "api", "api-db", "worker", "parser", "web", "contracts"},
    "tools/governance/check_repo.py": {"governance"},
    "services/api/lab_api/main.py": {"api", "api-db"},
    "tests/api/test_app.py": {"api", "api-db"},
    "tests/api_db/test_readiness_real_db.py": {"api-db"},
    "deploy/compose.dev.yml": {"api-db"},
    "services/worker/cmd/lab-worker/main.go": {"worker"},
    "services/worker/go.mod": {"worker"},
    "docs/TESTING.md": {"governance"},
    "web/src/App.tsx": {"web"},
    "web/package-lock.json": {"web"},
    "web/README.md": {"web", "governance"},
    "services/parser/README.md": {"parser", "governance"},
    "services/parser/lab_parser/main.py": {"parser"},
    "tests/parser/test_parser_service.py": {"parser"},
    "contracts/lab_contracts/source.py": {"contracts"},
    "contracts/examples/valid/SourceRegion.text.json": {"contracts"},
    "contracts/README.md": {"contracts", "governance"},
    "tests/contracts/test_contracts.py": {"contracts"},
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
