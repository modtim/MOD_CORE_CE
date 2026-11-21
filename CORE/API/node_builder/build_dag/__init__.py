"""δ DAG-wizard — converts wizard build requests into orchestrator
workflow runs instead of single monolithic ImageBuilder tasks.

Modules:
- ``types``: BuildSpec Pydantic schema (single source of truth)
- ``adapter``: Convert BuildLocalRequest → BuildSpec
- ``generator``: Convert BuildSpec → graph_json the orchestrator
  understands
- ``submit``: Programmatically create + trigger a workflow run

The decomposition lets us replace the wizard's monolithic
imagebuilder dispatch with a DAG of per-variant tasks dispatched
on the existing workflow_orchestrator infrastructure.
"""
