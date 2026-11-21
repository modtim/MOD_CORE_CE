import asyncio
import os
import shutil
from urllib.parse import urlparse
from typing import Dict, Any


async def clone_repo_tool(github_url: str, target_dir: str = "repos") -> Dict[str, Any]:
    """
    Clone a Git repository locally for analysis.

    Supports private repos via (checked in priority order):
      - GITHUB_TOKEN env var (injected into HTTPS URL)
      - GIT_USERNAME + GIT_PASSWORD env vars (basic HTTPS auth)
      - SSH key via GIT_SSH_KEY_PATH env var (uses GIT_SSH_COMMAND)

    Args:
        github_url: The repository URL to clone
        target_dir: Directory where to clone (default: "repos")

    Returns:
        Dict with clone results and local path
    """
    try:
        parsed_url = urlparse(github_url)

        # Extract owner/repo from URL
        path_parts = parsed_url.path.strip('/').split('/')
        if len(path_parts) < 2:
            return {"success": False, "error": "Invalid repo URL format. Need owner/repo.", "url": github_url}

        owner, repo_name = path_parts[0], path_parts[1]
        if repo_name.endswith('.git'):
            repo_name = repo_name[:-4]

        os.makedirs(target_dir, exist_ok=True)
        local_path = os.path.join(target_dir, f"{owner}_{repo_name}")

        # Clean up existing clone
        if os.path.exists(local_path):
            shutil.rmtree(local_path)

        # Build the clone URL and env
        clone_url = github_url
        env = os.environ.copy()

        github_token = os.getenv("GITHUB_TOKEN", "").strip()
        git_username = os.getenv("GIT_USERNAME", "").strip()
        git_password = os.getenv("GIT_PASSWORD", "").strip()
        ssh_key_path = os.getenv("GIT_SSH_KEY_PATH", "").strip()

        if github_token and parsed_url.scheme in ("https", "http", ""):
            # Inject token into HTTPS URL: https://{token}@github.com/owner/repo.git
            host = parsed_url.netloc or "github.com"
            path = parsed_url.path
            if not path.endswith('.git'):
                path = path.rstrip('/') + '.git'
            clone_url = f"https://{github_token}@{host}{path}"
        elif git_username and git_password and parsed_url.scheme in ("https", "http", ""):
            # Basic auth: https://username:password@host/path.git
            from urllib.parse import quote
            host = parsed_url.netloc or "github.com"
            path = parsed_url.path
            if not path.endswith('.git'):
                path = path.rstrip('/') + '.git'
            clone_url = f"https://{quote(git_username, safe='')}:{quote(git_password, safe='')}@{host}{path}"
        elif ssh_key_path:
            # Use SSH key for authentication
            if not os.path.exists(ssh_key_path):
                return {"success": False, "error": f"SSH key not found: {ssh_key_path}", "url": github_url}
            env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            # Convert HTTPS URL to SSH if needed
            if parsed_url.scheme in ("https", "http"):
                host = parsed_url.netloc or "github.com"
                path = parsed_url.path.lstrip('/')
                if not path.endswith('.git'):
                    path = path + '.git'
                clone_url = f"git@{host}:{path}"

        # GSEC R-100 — the clone is a bounded operation:
        #   - `--depth 1 --filter=blob:none` (shallow + partial): only the
        #     tip commit, blobs fetched on demand — a large upstream repo
        #     can't exhaust build-container disk with full history + blobs.
        #   - `GIT_LFS_SKIP_SMUDGE=1`: LFS pointers are NOT downloaded, so a
        #     repo full of LFS objects can't balloon the clone.
        #   - CLONE_TIMEOUT_S / CLONE_MAX_MB (DB platform settings
        #     wizard.clone_timeout_s / wizard.clone_max_mb, passed by the
        #     generate stage): a hard subprocess timeout and a working-tree
        #     size cap. Fail-closed to 900s / 2048MB when unset.
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        try:
            clone_timeout = int(float(os.getenv("CLONE_TIMEOUT_S", "900")))
        except (TypeError, ValueError):
            clone_timeout = 900
        clone_timeout = max(30, min(clone_timeout, 3600))
        try:
            clone_max_mb = int(float(os.getenv("CLONE_MAX_MB", "2048")))
        except (TypeError, ValueError):
            clone_max_mb = 2048
        clone_max_mb = max(1, min(clone_max_mb, 32768))

        clone_command = (
            f"git clone --depth 1 --filter=blob:none {clone_url} {local_path}"
        )

        process = await asyncio.create_subprocess_shell(
            clone_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=clone_timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {
                "success": False,
                "error": (
                    f"Clone exceeded the {clone_timeout}s bound "
                    f"(CLONE_TIMEOUT_S / wizard.clone_timeout_s) and was "
                    f"killed. The upstream repo is too large or too slow "
                    f"for the wizard's bounded shallow clone — pick a "
                    f"smaller repo or a different install strategy."
                ),
                "url": github_url,
            }

        if process.returncode != 0:
            error_msg = stderr.decode('utf-8') if stderr else "Clone failed"
            # Scrub credentials from error messages
            if github_token:
                error_msg = error_msg.replace(github_token, "***")
            if git_password:
                error_msg = error_msg.replace(git_password, "***")
            if git_username:
                error_msg = error_msg.replace(git_username, "***")
            return {"success": False, "error": error_msg, "url": github_url}

        repo_size = _get_directory_size(local_path)
        file_count = _count_files(local_path)

        # GSEC R-100 — working-tree size cap (wizard.clone_max_mb). A
        # shallow partial clone of a hostile repo can still carry an
        # absurd number of small files; refuse to analyze past the cap.
        if repo_size > clone_max_mb * 1024 * 1024:
            shutil.rmtree(local_path, ignore_errors=True)
            return {
                "success": False,
                "error": (
                    f"Clone working tree is {repo_size / (1024 * 1024):.0f} MB, "
                    f"over the {clone_max_mb} MB bound (CLONE_MAX_MB / "
                    f"wizard.clone_max_mb). The repo is too large for the "
                    f"wizard's bounded clone — pick a smaller repo or a "
                    f"different install strategy."
                ),
                "url": github_url,
            }

        return {
            "success": True,
            "local_path": local_path,
            "repo_name": f"{owner}/{repo_name}",
            "repo_size_mb": round(repo_size / (1024 * 1024), 2),
            "file_count": file_count,
            "url": github_url,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "url": github_url}


def _get_directory_size(path: str) -> int:
    """Get total size of directory in bytes."""
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
    except Exception:
        pass
    return total


def _count_files(path: str) -> int:
    """Count total number of files in directory."""
    count = 0
    try:
        for _, _, filenames in os.walk(path):
            count += len(filenames)
    except Exception:
        pass
    return count
