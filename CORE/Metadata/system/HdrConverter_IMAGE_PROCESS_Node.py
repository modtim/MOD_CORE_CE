# --- METADATA BLOCK ---
name = "HDR Converter"
description = "Tone-maps HDR and EXR images to standard PNG or JPG using ACES filmic tone mapping with auto-exposure and sRGB gamma correction."
node_kind = "hdr_convert_process"
category = "Image"
executable_path = "ghcr.io/modtim/hdr-converter:latest"
input_schema = {"input_type": "image", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "image", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"TARGET_FORMAT": {"type": "string", "default": "png", "enum": ["png", "jpg", "jpeg"], "description": "Output image format"}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["hdr", "exr"]
cost_in_credits = 1
tenant_id = None
image_pull_policy = "always"
container_timeout = 1800
# --- END METADATA BLOCK ---
