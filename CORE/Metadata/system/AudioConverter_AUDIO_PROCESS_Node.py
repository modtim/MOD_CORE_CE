# --- METADATA BLOCK ---
name = "Audio Converter"
description = "Converts an input audio file to a target audio format using ffmpeg."
node_kind = "audio_convert_process"
category = "Audio"
executable_path = "ghcr.io/modtim/audio-converter:latest"
input_schema = {"input_type": "audio", "file_mode": "both", "input_variables": "NONE"}
output_schema = {"output_type": "audio", "file_mode": "both", "paired": True, "output_variables": "NONE"}
configuration_schema = {"extra_variables": {"TARGET_FORMAT": {"type": "string", "default": "mp3", "enum": ["mp3", "wav", "flac", "ogg", "aac", "m4a"], "description": "Output audio format"}}}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
accepted_formats = ["mp3", "wav", "flac", "ogg", "aac", "m4a"]
cost_in_credits = 1
tenant_id = None
image_pull_policy = "always"
container_timeout = 1800
# --- END METADATA BLOCK ---
