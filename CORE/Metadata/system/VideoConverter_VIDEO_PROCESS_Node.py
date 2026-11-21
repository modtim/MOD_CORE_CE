# --- METADATA BLOCK ---
name = "Video Converter"
description = "Converts a video file from one format to another via ffmpeg, targeting the format set by TARGET_FORMAT."
node_kind = "video_convert_process"
category = "Video"
executable_path = "ghcr.io/modtim/video-converter:latest"
input_schema = {"input_type": "video", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "video", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"TARGET_FORMAT": {"type": "string", "default": "mp4", "enum": ["mp4", "mov", "webm", "avi", "mkv", "flv", "wmv", "m4v", "mpg", "mpeg", "ts", "3gp"], "description": "Output video format"}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["mp4", "mov", "webm", "avi", "mkv", "flv", "wmv", "m4v", "mpg", "mpeg", "ts", "3gp"]
cost_in_credits = 2
tenant_id = None
image_pull_policy = "always"
container_timeout = 3600
# --- END METADATA BLOCK ---
