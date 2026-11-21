"""P-wizard B — seed curated workflow templates.

Inserts 4 starter templates if not already present. Idempotent: matched
by (tenant_id IS NULL, name). Edit the seed list below and re-run with
--commit to update an existing template's graph or description.

Templates use **node names** (not ids) in `data.nodeDefinitionName`. The
clone endpoint resolves names → ids against the cloning user's visible
NodeDefinitions, so a template can be authored once and used by every
tenant whose library has matching nodes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _get_session():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    SessionLocal = _db.SessionLocal
    return SessionLocal()


SEED_TEMPLATES = [
    {
        "name": "Hello — Pass-through",
        "description": "Smallest possible workflow. Sends an input image straight to the output. Use it to learn the canvas + run an end-to-end test.",
        "category": "Getting Started",
        "plan_required": "All plans",
        "preview_nodes": ["Input", "Output"],
        "graph_json": {
            "nodes": [
                {"id": "input-1", "type": "input",
                 "position": {"x": 80, "y": 120},
                 "data": {"label": "Input"}},
                {"id": "output-1", "type": "output",
                 "position": {"x": 480, "y": 120},
                 "data": {"label": "Output"}},
            ],
            "edges": [{"id": "e1", "source": "input-1", "target": "output-1"}],
        },
    },
    {
        "name": "Image Processing — Resize then Output",
        "description": "Reads an image, runs the system Resize node, writes the result. Replace the resize node with any tenant Image-Processing node to adapt it.",
        "category": "Image Processing",
        "plan_required": "All plans",
        "preview_nodes": ["Input", "Resize", "Output"],
        "graph_json": {
            "nodes": [
                {"id": "input-1", "type": "input",
                 "position": {"x": 80, "y": 120},
                 "data": {"label": "Input"}},
                {"id": "process-1", "type": "process",
                 "position": {"x": 320, "y": 120},
                 "data": {"label": "Resize",
                          "nodeDefinitionName": "Resize"}},
                {"id": "output-1", "type": "output",
                 "position": {"x": 600, "y": 120},
                 "data": {"label": "Output"}},
            ],
            "edges": [
                {"id": "e1", "source": "input-1", "target": "process-1"},
                {"id": "e2", "source": "process-1", "target": "output-1"},
            ],
        },
    },
    {
        "name": "LLM Caption — Image to Text",
        "description": "Runs an image through a captioning LLM node, writes a JSON sidecar with the description. Pairs well with the Photo→3D template.",
        "category": "LLM",
        "plan_required": "Pro and above",
        "preview_nodes": ["Input", "ImageCaption", "Output"],
        "graph_json": {
            "nodes": [
                {"id": "input-1", "type": "input",
                 "position": {"x": 80, "y": 120},
                 "data": {"label": "Image"}},
                {"id": "process-1", "type": "process",
                 "position": {"x": 320, "y": 120},
                 "data": {"label": "ImageCaption",
                          "nodeDefinitionName": "ImageCaption"}},
                {"id": "output-1", "type": "output",
                 "position": {"x": 600, "y": 120},
                 "data": {"label": "Caption JSON"}},
            ],
            "edges": [
                {"id": "e1", "source": "input-1", "target": "process-1"},
                {"id": "e2", "source": "process-1", "target": "output-1"},
            ],
        },
    },
    {
        "name": "Branch on Status — If/Else",
        "description": "Demonstrates the If/Else logic node. Routes to one of two output nodes based on a JSON-Path predicate. Replace the predicate to fit your data.",
        "category": "Logic & Control",
        "plan_required": "All plans",
        "preview_nodes": ["Input", "If/Else", "OutputA", "OutputB"],
        "graph_json": {
            "nodes": [
                {"id": "input-1", "type": "input",
                 "position": {"x": 80, "y": 120},
                 "data": {"label": "Input"}},
                {"id": "ifelse-1", "type": "ifelse",
                 "position": {"x": 340, "y": 120},
                 "data": {"label": "If status==ok",
                          "predicate": "$.status == 'ok'"}},
                {"id": "output-ok", "type": "output",
                 "position": {"x": 620, "y": 60},
                 "data": {"label": "OK Branch"}},
                {"id": "output-fail", "type": "output",
                 "position": {"x": 620, "y": 200},
                 "data": {"label": "Fail Branch"}},
            ],
            "edges": [
                {"id": "e1", "source": "input-1", "target": "ifelse-1"},
                {"id": "e2", "source": "ifelse-1", "target": "output-ok",
                 "label": "true"},
                {"id": "e3", "source": "ifelse-1", "target": "output-fail",
                 "label": "false"},
            ],
        },
    },
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    session = _get_session()
    import models  # type: ignore

    upserts, inserts = 0, 0
    for spec in SEED_TEMPLATES:
        existing = session.query(models.WorkflowTemplate).filter(
            models.WorkflowTemplate.name == spec["name"],
            models.WorkflowTemplate.tenant_id.is_(None),
        ).first()
        if existing:
            print(f"{'WRITE' if args.commit else 'DRY  '}  update '{spec['name']}'")
            if args.commit:
                existing.description = spec["description"]
                existing.category = spec["category"]
                existing.plan_required = spec["plan_required"]
                existing.preview_nodes = spec["preview_nodes"]
                existing.graph_json = spec["graph_json"]
                existing.is_curated = True
                session.add(existing)
            upserts += 1
        else:
            print(f"{'WRITE' if args.commit else 'DRY  '}  insert '{spec['name']}'")
            if args.commit:
                session.add(models.WorkflowTemplate(
                    tenant_id=None,
                    name=spec["name"],
                    description=spec["description"],
                    category=spec["category"],
                    plan_required=spec["plan_required"],
                    preview_nodes=spec["preview_nodes"],
                    graph_json=spec["graph_json"],
                    is_curated=True,
                ))
            inserts += 1

    if args.commit:
        session.commit()
        print(f"Applied. inserted={inserts} updated={upserts}")
    else:
        print(f"(dry run) would insert={inserts} update={upserts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
