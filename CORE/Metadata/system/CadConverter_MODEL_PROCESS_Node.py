# --- METADATA BLOCK ---
name = "CAD Converter"
description = "Converts STEP CAD files to GLB by tessellating them to triangle meshes for browser 3D viewing."
node_kind = "cad_convert_process"
category = "Model"
executable_path = "ghcr.io/modtim/cad-converter:latest"
input_schema = {"input_type": "model", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "model", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"TARGET_FORMAT": {"type": "string", "default": "glb", "enum": ["glb"], "description": "Output model format"}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["step", "stp"]
cost_in_credits = 2
tenant_id = None
image_pull_policy = "always"
container_timeout = 3600
# --- END METADATA BLOCK ---
