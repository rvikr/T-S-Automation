"""Tests for the bring-your-own-policy loader (SENTINEL_POLICY_FILE).

These exercise the pure loader functions directly rather than reloading the
module under a mutated environment: module-level constants are imported by
name across the codebase, so a reload would not propagate anyway. What must
hold: a valid file yields the operator's taxonomy, and any structural problem
raises instead of silently enforcing under a different policy.
"""

import json
import tempfile
import unittest
from pathlib import Path

from sentinel.tools.policy_retrieval import build_taxonomy, load_policy_file


def _valid_payload() -> dict:
    return {
        "clauses": [
            {
                "category": "No Violation",
                "pillar": "General",
                "tier": 0,
                "clause_id": "ACME-ALLOW-000",
                "summary": "Benign.",
            },
            {
                "category": "Child Exploitation",
                "pillar": "Safety",
                "tier": 1,
                "clause_id": "ACME-CE-001",
                "summary": "Tier-1 human-only.",
            },
            {
                "category": "Fraud",
                "pillar": "Integrity",
                "tier": 3,
                "clause_id": "ACME-FRAUD-001",
                "summary": "Reject scams.",
            },
        ],
        "aliases": {"Scam": "Fraud"},
    }


class BuildTaxonomyTests(unittest.TestCase):
    def test_valid_payload_builds_clauses_and_aliases(self):
        clauses, aliases = build_taxonomy(_valid_payload())

        self.assertEqual(set(clauses), {"No Violation", "Child Exploitation", "Fraud"})
        self.assertEqual(clauses["Fraud"].tier, 3)
        self.assertEqual(clauses["Fraud"].citation, "ACME-FRAUD-001 (Integrity / Fraud)")
        # Aliases are lowercased so lookup matches normalize_category's key.
        self.assertEqual(aliases, {"scam": "Fraud"})

    def test_missing_no_violation_clause_is_injected(self):
        payload = _valid_payload()
        payload["clauses"] = [c for c in payload["clauses"] if c["category"] != "No Violation"]

        clauses, _ = build_taxonomy(payload)

        self.assertIn("No Violation", clauses)
        self.assertEqual(clauses["No Violation"].tier, 0)

    def test_structural_problems_raise_instead_of_degrading(self):
        cases = {
            "not a mapping": [],
            "clauses not a list": {"clauses": {}},
            "clause missing fields": {"clauses": [{"category": "X"}]},
            "empty category": {
                "clauses": [{"category": " ", "pillar": "P", "tier": 3, "clause_id": "C", "summary": "S"}]
            },
            "bad tier": {
                "clauses": [{"category": "X", "pillar": "P", "tier": "high", "clause_id": "C", "summary": "S"}]
            },
            "tier out of range": {
                "clauses": [{"category": "X", "pillar": "P", "tier": 9, "clause_id": "C", "summary": "S"}]
            },
            "duplicate category": {
                "clauses": [
                    {"category": "X", "pillar": "P", "tier": 3, "clause_id": "C1", "summary": "S"},
                    {"category": "X", "pillar": "P", "tier": 3, "clause_id": "C2", "summary": "S"},
                ]
            },
            "alias to unknown category": {
                "clauses": [{"category": "X", "pillar": "P", "tier": 3, "clause_id": "C", "summary": "S"}],
                "aliases": {"y": "Nope"},
            },
            "non-zero-tier No Violation": {
                "clauses": [
                    {"category": "No Violation", "pillar": "G", "tier": 2, "clause_id": "C", "summary": "S"}
                ]
            },
        }
        for name, payload in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    build_taxonomy(payload)


class LoadPolicyFileTests(unittest.TestCase):
    def test_loads_json_and_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "policy.json"
            json_path.write_text(json.dumps(_valid_payload()), encoding="utf-8")
            clauses, _ = load_policy_file(json_path)
            self.assertIn("Fraud", clauses)

            import yaml

            yaml_path = Path(tmp) / "policy.yaml"
            yaml_path.write_text(yaml.safe_dump(_valid_payload()), encoding="utf-8")
            clauses, aliases = load_policy_file(yaml_path)
            self.assertIn("Child Exploitation", clauses)
            self.assertEqual(aliases["scam"], "Fraud")

    def test_committed_example_policy_file_is_valid(self):
        example = Path(__file__).resolve().parents[1] / "policy" / "policy.example.yaml"
        clauses, aliases = load_policy_file(example)

        tier1 = {category for category, clause in clauses.items() if clause.tier == 1}
        self.assertEqual(tier1, {"Child Exploitation", "Terrorism & Violent Extremism"})
        self.assertIn("No Violation", clauses)
        self.assertEqual(aliases["csam"], "Child Exploitation")


if __name__ == "__main__":
    unittest.main()
