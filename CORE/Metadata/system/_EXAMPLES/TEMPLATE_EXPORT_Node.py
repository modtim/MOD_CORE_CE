# --- METADATA BLOCK ---
# TEMPLATE: Copy this file and replace all values marked with [REPLACE]
name = "[REPLACE] Your Export Name"  # e.g., "Custom Platform Export", "S3 Bucket Export"
description = "Export - [REPLACE] Description of what this export does"
node_kind = "[REPLACE]_export"  # e.g., "custom_platform_export", "s3_bucket_export" (lowercase, underscores, MUST end with _export)
category = "Export"
executable_path = "none"
input_schema = {"input_type": "any", "file_mode": "multiple", "input_variables": "NONE"}
output_schema = {"output_type": "NONE", "file_mode": "single", "output_variables": "NONE"}
configuration_schema = {"extra_variables": "NONE"}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
cost_in_credits = 1  # [REPLACE] Adjust cost based on export complexity
tenant_id = None
# --- END METADATA BLOCK ---
