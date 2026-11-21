# --- METADATA BLOCK ---
name = "FFmpeg (CPU)"
description = "Runs a user-supplied ffmpeg operation on a single staged input file, producing one output file in the target container format. Without an H.264 encoder (CE image), mp4/mov/m4v output is VP9+AAC instead."
node_kind = "ffmpeg_cpu_process"
category = "Video"
executable_path = "ghcr.io/modtim/ffmpeg_cpu:latest"
input_schema = {"input_type": "video", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "video", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"FFMPEG_ARGS": {"type": "string", "default": "", "description": "Free-form ffmpeg arguments (filters, codecs, etc.) inserted between input and output."}, "TARGET_FORMAT": {"type": "string", "default": "mp4", "description": "Output container format / file extension.", "enum": ["mp4", "mov", "mkv", "avi", "webm", "m4v", "mp3", "wav", "aac", "flac", "m4a"]}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["mp4", "mov", "mkv", "avi", "webm", "m4v", "mp3", "wav", "aac", "flac", "m4a"]
cost_in_credits = 3
tenant_id = None
image_pull_policy = "always"
container_timeout = 3600
# --- END METADATA BLOCK ---
