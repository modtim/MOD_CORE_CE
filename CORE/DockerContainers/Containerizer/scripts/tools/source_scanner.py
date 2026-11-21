import os
from typing import Dict, Any

# Directories to skip when scanning
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs",
}

# Directory name suffixes to skip (can't use exact match for these)
SKIP_DIR_SUFFIXES = (".egg-info",)

# Binary file extensions to skip
BINARY_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll", ".exe",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
}

# Language detection by extension
LANG_MAP = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript (React)", ".jsx": "JavaScript (React)",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".cpp": "C++",
    ".c": "C", ".h": "C/C++ Header", ".swift": "Swift",
    ".sh": "Shell", ".bash": "Bash", ".zsh": "Zsh",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML", ".xml": "XML", ".sql": "SQL",
    ".r": "R", ".R": "R", ".lua": "Lua", ".pl": "Perl",
    ".dockerfile": "Dockerfile", ".tf": "Terraform",
}


async def scan_source(source_path: str, max_content_chars: int = 50000) -> Dict[str, Any]:
    """
    Scan a local file or directory and produce summary/tree/content
    in the same format as gitingest_tool, so create_container_tool
    can consume it identically.

    Args:
        source_path: Path to a single file or directory
        max_content_chars: Max characters of source content to include

    Returns:
        Dict with success, summary, tree, content keys
    """
    try:
        if not os.path.exists(source_path):
            return {"success": False, "error": f"Path does not exist: {source_path}"}

        if os.path.isfile(source_path):
            return _scan_single_file(source_path, max_content_chars)
        else:
            return _scan_directory(source_path, max_content_chars)

    except Exception as e:
        return {"success": False, "error": str(e)}


def _scan_single_file(file_path: str, max_content_chars: int) -> Dict[str, Any]:
    """Scan a single script file."""
    filename = os.path.basename(file_path)
    ext = os.path.splitext(filename)[1].lower()
    lang = LANG_MAP.get(ext, "Unknown")
    size = os.path.getsize(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_content_chars)
    except Exception as e:
        return {"success": False, "error": f"Failed to read {file_path}: {e}"}

    summary = f"Single {lang} script: {filename} ({size:,} bytes)"
    tree = filename
    file_content = f"--- {filename} ---\n{content}"

    return {
        "success": True,
        "summary": summary,
        "tree": tree,
        "content": file_content,
        "file_count": 1,
        "total_size": size,
    }


def _scan_directory(dir_path: str, max_content_chars: int) -> Dict[str, Any]:
    """Scan a directory of source files."""
    tree_lines = []
    file_contents = []
    total_chars = 0
    file_count = 0
    total_size = 0
    languages = set()
    truncated = False

    for root, dirs, files in os.walk(dir_path):
        # Filter out skipped directories
        dirs[:] = [
            d for d in sorted(dirs)
            if d not in SKIP_DIRS and not d.endswith(SKIP_DIR_SUFFIXES)
        ]
        files = sorted(files)

        rel_root = os.path.relpath(root, dir_path)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        indent = "  " * depth

        if rel_root != ".":
            tree_lines.append(f"{indent}{os.path.basename(root)}/")

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in BINARY_EXTENSIONS:
                continue

            file_path = os.path.join(root, fname)
            rel_path = os.path.relpath(file_path, dir_path)

            try:
                size = os.path.getsize(file_path)
            except OSError:
                continue

            total_size += size
            file_count += 1

            if ext in LANG_MAP:
                languages.add(LANG_MAP[ext])

            tree_lines.append(f"{indent}  {fname}")

            # Read content if we have budget
            if not truncated and total_chars < max_content_chars:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(max_content_chars - total_chars)
                    file_contents.append(f"--- {rel_path} ---\n{content}")
                    total_chars += len(content) + len(rel_path) + 10
                    if total_chars >= max_content_chars:
                        truncated = True
                except Exception:
                    continue

    if not file_count:
        return {"success": False, "error": f"No source files found in {dir_path}"}

    dir_name = os.path.basename(dir_path.rstrip("/"))
    lang_str = ", ".join(sorted(languages)) if languages else "Unknown"
    size_kb = total_size / 1024

    summary = (
        f"Project: {dir_name}\n"
        f"Languages: {lang_str}\n"
        f"Files: {file_count}, Size: {size_kb:.1f} KB"
    )

    tree = "\n".join(tree_lines)
    content = "\n\n".join(file_contents)
    if truncated:
        content += "\n\n... [Content truncated due to length] ..."

    return {
        "success": True,
        "summary": summary,
        "tree": tree,
        "content": content,
        "file_count": file_count,
        "total_size": total_size,
    }
