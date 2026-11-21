# --- METADATA BLOCK ---
name = "Return Assets Export"
description = "Export - Return processed assets to database with proper naming and permissions"
node_kind = "return_assets_export"
category = "Export"
executable_path = "ghcr.io/modtim/return-export:latest"
input_schema = {"input_type": "any", "file_mode": "multiple", "input_variables": "NONE"}
output_schema = {"output_type": "NONE", "file_mode": "single", "output_variables": "NONE"}
configuration_schema = {"extra_variables": "NONE"}
metadata = {"accepts_metadata": True, "metadata_required": False}
batch_mode = "batch"
cost_in_credits = 1
tenant_id = None
# --- END METADATA BLOCK ---
