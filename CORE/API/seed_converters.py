"""Seed converter containers as selectable workflow nodes.

Run on startup to register image-converter, video-converter, etc.
as node definitions users can drag into workflows.

open-core-tier-structure §4.1 — this is part of MOD's bundled node catalog.
A fresh install no longer seeds it by default; existing deployments are
untouched (this only gates the INSERT path, never deletes/modifies rows
already in the DB). Set MOD_SEED_BUNDLED_NODES=false to skip. Defaults to
true so current/dev behavior is unchanged until an operator opts out.
"""

import logging
import os
from sqlalchemy.orm import Session
from models import NodeDefinition

logger = logging.getLogger(__name__)

CONVERTER_NODES = [
    {
        "name": "Image Converter",
        "description": "Convert images between formats (PNG, JPG, TIFF, BMP, WebP, GIF). Set TARGET_FORMAT in configuration.",
        "type": "Converter",
        "executable_path": "ghcr.io/modtim/image-converter:latest",
        "process_type": "CPU",
        "node_kind": "image_converter",
        "batch_mode": "batch",
        "input_schema": {"input_type": "image"},
        "output_schema": {"output_type": "image"},
        "configuration_schema": {
            "TARGET_FORMAT": {"type": "string", "default": "png", "description": "Output format (png, jpg, tiff, bmp, webp)"},
        },
        "accepted_formats": ["png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp", "gif", "exr", "hdr", "tga"],
        "cost_in_credits": 1,
    },
    {
        "name": "Video Converter",
        "description": "Convert videos between formats (MP4, MOV, AVI, WebM, MKV). Set TARGET_FORMAT in configuration.",
        "type": "Converter",
        "executable_path": "ghcr.io/modtim/video-converter:latest",
        "process_type": "CPU",
        "node_kind": "video_converter",
        "batch_mode": "batch",
        "input_schema": {"input_type": "video"},
        "output_schema": {"output_type": "video"},
        "configuration_schema": {
            "TARGET_FORMAT": {"type": "string", "default": "mp4", "description": "Output format (mp4, mov, avi, webm, mkv)"},
        },
        "accepted_formats": ["mp4", "mov", "avi", "webm", "mkv"],
        "cost_in_credits": 2,
    },
    {
        "name": "Mesh Converter",
        "description": "Convert 3D meshes between formats (GLB, OBJ, FBX, PLY, STL). Set TARGET_FORMAT in configuration.",
        "type": "Converter",
        "executable_path": "ghcr.io/modtim/mesh-converter:latest",
        "process_type": "CPU",
        "node_kind": "mesh_converter",
        "batch_mode": "batch",
        "input_schema": {"input_type": "3d_model"},
        "output_schema": {"output_type": "3d_model"},
        "configuration_schema": {
            "TARGET_FORMAT": {"type": "string", "default": "glb", "description": "Output format (glb, obj, ply, stl)"},
        },
        "accepted_formats": ["obj", "fbx", "glb", "gltf", "ply", "stl", "dae"],
        "cost_in_credits": 1,
    },
    {
        "name": "Audio Converter",
        "description": "Convert audio between formats (MP3, WAV, FLAC, OGG, AAC). Set TARGET_FORMAT in configuration.",
        "type": "Converter",
        "executable_path": "ghcr.io/modtim/audio-converter:latest",
        "process_type": "CPU",
        "node_kind": "audio_converter",
        "batch_mode": "batch",
        "input_schema": {"input_type": "audio"},
        "output_schema": {"output_type": "audio"},
        "configuration_schema": {
            "TARGET_FORMAT": {"type": "string", "default": "mp3", "description": "Output format (mp3, wav, flac, ogg, aac)"},
        },
        "accepted_formats": ["mp3", "wav", "flac", "ogg", "aac", "m4a"],
        "cost_in_credits": 1,
    },
    {
        "name": "CAD Converter",
        "description": "Convert CAD files (STEP, STP) to 3D mesh formats (GLB) for viewing and processing.",
        "type": "Converter",
        "executable_path": "ghcr.io/modtim/cad-converter:latest",
        "process_type": "CPU",
        "node_kind": "cad_converter",
        "batch_mode": "batch",
        "input_schema": {"input_type": "cad"},
        "output_schema": {"output_type": "3d_model"},
        "configuration_schema": {
            "TARGET_FORMAT": {"type": "string", "default": "glb", "description": "Output format (glb)"},
        },
        "accepted_formats": ["step", "stp"],
        "cost_in_credits": 2,
    },
    {
        "name": "USD Converter",
        "description": "Convert USD scene files to GLB for browser viewing.",
        "type": "Converter",
        "executable_path": "ghcr.io/modtim/usd-converter:latest",
        "process_type": "CPU",
        "node_kind": "usd_converter",
        "batch_mode": "batch",
        "input_schema": {"input_type": "3d_model"},
        "output_schema": {"output_type": "3d_model"},
        "configuration_schema": {
            "TARGET_FORMAT": {"type": "string", "default": "glb", "description": "Output format (glb)"},
        },
        "accepted_formats": ["usd", "usda", "usdc", "usdz"],
        "cost_in_credits": 2,
    },
    {
        "name": "HDR Converter",
        "description": "Tone-map HDR/EXR images to standard formats (PNG) for viewing.",
        "type": "Converter",
        "executable_path": "ghcr.io/modtim/hdr-converter:latest",
        "process_type": "CPU",
        "node_kind": "hdr_converter",
        "batch_mode": "batch",
        "input_schema": {"input_type": "image"},
        "output_schema": {"output_type": "image"},
        "configuration_schema": {
            "TARGET_FORMAT": {"type": "string", "default": "png", "description": "Output format (png, jpg)"},
        },
        "accepted_formats": ["exr", "hdr"],
        "cost_in_credits": 1,
    },
]


def seed_converter_nodes(db: Session) -> int:
    """Register converter containers as system node definitions. Returns count of new nodes created.

    open-core-tier-structure §4.1: skipped entirely when MOD_SEED_BUNDLED_NODES
    is false — a fresh install then starts with no bundled converter nodes.
    Existing rows already in the DB are never touched by this check; it only
    guards the INSERT path below.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping bundled converter "
            "node catalog seed"
        )
        return 0

    created = 0
    for node_data in CONVERTER_NODES:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == node_data["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            continue

        nd = NodeDefinition(
            name=node_data["name"],
            tenant_id=None,  # System node — available to all tenants
            description=node_data["description"],
            type=node_data["type"],
            executable_path=node_data["executable_path"],
            process_type=node_data["process_type"],
            node_kind=node_data["node_kind"],
            batch_mode=node_data["batch_mode"],
            input_schema=node_data["input_schema"],
            output_schema=node_data["output_schema"],
            configuration_schema=node_data["configuration_schema"],
            accepted_formats=node_data["accepted_formats"],
            cost_in_credits=node_data["cost_in_credits"],
        )
        db.add(nd)
        created += 1

    if created > 0:
        db.commit()

    return created
