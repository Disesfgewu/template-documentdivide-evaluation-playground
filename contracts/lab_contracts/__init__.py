"""Versioned LAB data contracts (SPEC v0.1 sections 4.1, 16.1, 16.4).

Pydantic models are the source of truth; contracts/schemas/<version>/*.json
are generated from them (python -m lab_contracts.export) and CI fails on drift.
"""
SCHEMA_VERSION = "v1"
