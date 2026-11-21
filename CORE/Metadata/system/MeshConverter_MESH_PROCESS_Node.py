# --- METADATA BLOCK ---
name = "Mesh Converter"
description = "Converts a 3D mesh from one format to another via trimesh, loading any material or texture companion files automatically."
node_kind = "mesh_convert_process"
category = "Mesh"
executable_path = "ghcr.io/modtim/mesh-converter:latest"
input_schema = {"input_type": "mesh", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "mesh", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"TARGET_FORMAT": {"type": "string", "default": "glb", "enum": ["glb", "gltf", "obj", "ply", "stl", "dae", "off", "3mf"], "description": "Output mesh format"}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["obj", "glb", "gltf", "stl", "ply", "fbx", "dae", "3ds", "off", "3mf", "x3d", "usd", "usdz", "usda", "usdc"]
cost_in_credits = 1
tenant_id = None
image_pull_policy = "always"
container_timeout = 1800
# --- END METADATA BLOCK ---
