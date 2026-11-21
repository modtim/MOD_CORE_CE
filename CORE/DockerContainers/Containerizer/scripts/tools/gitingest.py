import os
from gitingest import ingest_async
from typing import Dict, Any


async def gitingest_tool(local_repo_path: str) -> Dict[str, Any]:
    """Analyze a local repository using gitingest."""
    try:
        if not os.path.isdir(local_repo_path):
            raise FileNotFoundError(f"Not a directory: {local_repo_path}")

        summary, tree, content = await ingest_async(
            source=local_repo_path,
            max_file_size=5 * 1024 * 1024,
        )

        return {
            "success": True,
            "summary": summary,
            "tree": tree,
            "content": content,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}