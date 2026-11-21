# --- METADATA BLOCK ---
name = "3D Thumbnail Generator"
description = "Mesh Process - Renders a thumbnail/preview image for a 3D model (mesh, USD, or CAD) using headless OSMesa OpenGL."
node_kind = "thumbnail_3d_mesh_process"
category = "Mesh Processing"
executable_path = "ghcr.io/modtim/3d-thumbnail-gen:latest"
input_schema = {"input_type": "mesh", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "image", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {
    "thumbnail_size": {
        "type": "integer",
        "default": 512,
        "enum": [256, 512, 1024, 2048],
        "description": "Output thumbnail width and height in pixels"
    },
    "background_color": {
        "type": "string",
        "default": "transparent",
        "enum": ["transparent", "white", "black", "gray"],
        "description": "Background fill color for the rendered thumbnail"
    }
}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["obj", "stl", "ply", "glb", "gltf", "fbx", "usd", "usda", "usdc", "usdz", "step", "stp"]
cost_in_credits = 1
tenant_id = None
image_pull_policy = "always"
container_timeout = 600
container_gpu_required = False
# --- END METADATA BLOCK ---
