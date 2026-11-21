# --- METADATA BLOCK ---
name = "Image Converter"
description = "Converts an image between formats using ffmpeg (Pillow fallback), and can also extract a single frame from a video file into an image."
node_kind = "image_convert_process"
category = "Image"
executable_path = "ghcr.io/modtim/image-converter:latest"
input_schema = {"input_type": "image", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "image", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"TARGET_FORMAT": {"type": "string", "default": "png", "enum": ["png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp"], "description": "Output image format"}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp", "mp4", "mov", "avi", "webm", "mkv"]
cost_in_credits = 1
tenant_id = None
image_pull_policy = "always"
container_timeout = 1800
# --- END METADATA BLOCK ---
