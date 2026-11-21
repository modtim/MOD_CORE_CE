"""
MinIO Helper Utilities

Provides MinIO URI parsing, path manipulation, and validation helpers
used across multiple DAGs.

TENANT ISOLATION (TPN CS-1 Content Security):
Each tenant gets their own bucket (tenant-{id}-files) for complete data isolation.
Use get_tenant_bucket_name() to derive the correct bucket for any tenant.

Usage:
    from utils.minio_helpers import (
        parse_minio_uri,
        build_minio_uri,
        get_tenant_bucket_name
    )
"""

import os
import re
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import Session

# FX.96 (Q82=A): file names must never appear in MOD system logs — log the
# stable digest instead (utils.secret_redaction.hash_for_log).
from utils.secret_redaction import hash_for_log

logger = logging.getLogger(__name__)


def get_tenant_bucket_name(tenant_id: int, db: Optional[Session] = None) -> str:
    """
    Resolve the bucket name for a tenant.

    TPN CS-1 Compliance: Each tenant gets isolated storage in their own bucket.
    House-default format: tenant-{id}-files.

    remove-minio-versitygw Gate 5 (5.6) / BYO-S3: delegates to
    ``utils.minio_client.get_tenant_bucket_name`` — the single implementation —
    so a tenant with an ACTIVE BYO-S3 ``StorageConnection`` gets their own
    bucket back instead of the house default. Never raises (mirrors
    ``resolve_backend``'s never-raise contract).

    Args:
        tenant_id: The tenant's database ID
        db: Optional DB session to resolve an active BYO ``StorageConnection``
            through. Without one, falls back to the house-default string
            (resolve_backend will still try to open its own session if the
            module is available; see storage.resolve_backend).

    Returns:
        str: Bucket name (e.g., 'tenant-123-files', or the tenant's BYO bucket)

    Example:
        >>> get_tenant_bucket_name(1)
        'tenant-1-files'

        >>> get_tenant_bucket_name(456)
        'tenant-456-files'
    """
    from utils.minio_client import get_tenant_bucket_name as _resolve_tenant_bucket_name
    return _resolve_tenant_bucket_name(tenant_id, db)


def parse_minio_uri(uri: str, storage_scheme: str = None) -> str:
    """
    Parse a MinIO URI to extract just the object path (without bucket).

    Handles multiple formats:
    - minio://bucket-name/path/to/file.jpg → path/to/file.jpg
    - s3://bucket-name/path/to/file.jpg → path/to/file.jpg
    - /path/to/file.jpg → /path/to/file.jpg (passthrough)
    - bucket-name/path/to/file.jpg → path/to/file.jpg

    Args:
        uri: Full MinIO URI or path
        storage_scheme: Storage scheme to use (default: from env var STORAGE_SCHEME or 's3')

    Returns:
        Object path without bucket (e.g., 'tenant_2/uploads/file.jpg')

    Example:
        >>> parse_minio_uri('minio://cogs-files/tenant_1/image.png')
        'tenant_1/image.png'

        >>> parse_minio_uri('/path/to/file.jpg')
        '/path/to/file.jpg'
    """
    if not uri:
        return uri

    # Dual-read: accept the configured scheme, s3, and legacy minio regardless
    # of the configured default (a legacy minio:// row must still strip when
    # STORAGE_SCHEME=s3). Unknown schemes fall through unchanged.
    configured = storage_scheme or os.getenv('STORAGE_SCHEME', 's3')
    schemes = tuple(dict.fromkeys((configured, 's3', 'minio')))

    path = None
    for scheme in schemes:
        prefix = f'{scheme}://'
        if uri.startswith(prefix):
            path = uri[len(prefix):]
            break
    if path is None:
        # Not a storage URI (schemeless or unknown scheme) — return as-is
        return uri

    # Split into bucket and object path, return just the object path
    parts = path.split('/', 1)
    if len(parts) == 2:
        # Return just the path after bucket name
        return parts[1]
    return ''


def build_minio_uri(bucket: str, object_path: str, storage_scheme: str = None) -> str:
    """
    Build a MinIO URI from bucket and object path.

    Args:
        bucket: Bucket name (e.g., 'cogs-files')
        object_path: Object path (e.g., 'tenant_1/image.png')
        storage_scheme: Storage scheme (default: from env var STORAGE_SCHEME or 's3')

    Returns:
        Full storage URI (e.g., 's3://cogs-files/tenant_1/image.png')

    Example:
        >>> build_minio_uri('cogs-files', 'tenant_1/image.png')
        's3://cogs-files/tenant_1/image.png'

        >>> build_minio_uri('cogs-files', 'tenant_1/image.png', storage_scheme='minio')
        'minio://cogs-files/tenant_1/image.png'
    """
    storage_scheme = storage_scheme or os.getenv('STORAGE_SCHEME', 's3')

    # Remove leading slash from object path if present
    object_path = object_path.lstrip('/')

    return f"{storage_scheme}://{bucket}/{object_path}"


def extract_bucket_and_path(uri: str, storage_scheme: str = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract both bucket and object path from a MinIO URI.

    Args:
        uri: Full MinIO URI
        storage_scheme: Storage scheme (default: auto-detect from URI)

    Returns:
        Tuple of (bucket_name, object_path) or (None, None) if invalid

    Example:
        >>> bucket, path = extract_bucket_and_path('minio://cogs-files/tenant_1/image.png')
        >>> print(bucket)  # 'cogs-files'
        >>> print(path)    # 'tenant_1/image.png'
    """
    if not uri:
        return None, None

    # Parse URL to extract components
    parsed = urlparse(uri)

    # Check if it's a valid storage URI
    if parsed.scheme not in ['minio', 's3', 'http', 'https', '']:
        return None, None

    # If it has a scheme, extract bucket from netloc
    if parsed.scheme:
        bucket = parsed.netloc
        object_path = parsed.path.lstrip('/')
    else:
        # No scheme, assume format: bucket/path
        parts = uri.split('/', 1)
        if len(parts) == 2:
            bucket, object_path = parts
        else:
            return None, None

    return bucket, object_path


def is_valid_minio_uri(uri: str) -> bool:
    """
    Check if a string is a valid MinIO URI.

    Args:
        uri: String to validate

    Returns:
        bool: True if valid MinIO URI, False otherwise

    Example:
        >>> is_valid_minio_uri('minio://bucket/path/file.png')
        True

        >>> is_valid_minio_uri('/local/path/file.png')
        False
    """
    if not uri or not isinstance(uri, str):
        return False

    bucket, path = extract_bucket_and_path(uri)
    return bucket is not None and path is not None and len(path) > 0


def normalize_path(path: str) -> str:
    """
    Normalize a file path by removing redundant slashes and dots.
    Also blocks path traversal attempts for security.

    Args:
        path: File path to normalize

    Returns:
        Normalized path

    Raises:
        ValueError: If path contains traversal attempts (../)

    Example:
        >>> normalize_path('tenant_1//uploads/./file.png')
        'tenant_1/uploads/file.png'
    """
    if not path:
        return path

    # SECURITY: Block path traversal attempts
    if '..' in path:
        logger.warning("SECURITY: Path traversal attempt blocked: %s",
                       hash_for_log(path))
        raise ValueError("Path traversal not allowed")

    # Remove multiple consecutive slashes
    while '//' in path:
        path = path.replace('//', '/')

    # Remove leading slash
    path = path.lstrip('/')

    # Remove trailing slash
    path = path.rstrip('/')

    # Remove single dots (current directory references)
    parts = path.split('/')
    parts = [p for p in parts if p and p != '.']
    path = '/'.join(parts)

    return path


def get_file_extension(path: str) -> Optional[str]:
    """
    Extract file extension from a path or URI.

    Args:
        path: File path or MinIO URI

    Returns:
        File extension (lowercase, without dot) or None if no extension

    Example:
        >>> get_file_extension('minio://bucket/file.PNG')
        'png'

        >>> get_file_extension('file.tar.gz')
        'gz'
    """
    if not path:
        return None

    # Extract just the filename if it's a URI
    if '://' in path:
        path = parse_minio_uri(path)

    # Get extension
    if '.' in path:
        return path.rsplit('.', 1)[-1].lower()

    return None


def is_image_file(path: str) -> bool:
    """
    Check if a file path represents an image based on extension.

    Args:
        path: File path or MinIO URI

    Returns:
        bool: True if image file, False otherwise

    Example:
        >>> is_image_file('minio://bucket/photo.jpg')
        True

        >>> is_image_file('document.pdf')
        False
    """
    image_extensions = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tif', 'tiff', 'webp', 'svg'}
    ext = get_file_extension(path)
    return ext in image_extensions if ext else False


# Regex for validating SHA256 hash format (64 hexadecimal characters)
SHA256_PATTERN = re.compile(r'^[a-f0-9]{64}$')


def is_valid_sha256_hash(content_hash: str) -> bool:
    """
    Validate that a string is a valid SHA256 hash format.

    Args:
        content_hash: String to validate

    Returns:
        True if valid SHA256 hash format (64 hex chars), False otherwise

    Example:
        >>> is_valid_sha256_hash('a' * 64)  # 64 hex chars
        True
        >>> is_valid_sha256_hash('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
        True
        >>> is_valid_sha256_hash('invalid')
        False
    """
    if not content_hash or not isinstance(content_hash, str):
        return False
    return bool(SHA256_PATTERN.match(content_hash.lower()))


def enrich_resources_with_extensions(resources: list, tenant_id: int, workflow_run_id: str = None) -> list:
    """
    Enrich a list of resource dicts with file_extension if missing.

    This is the main entry point for Traffic Cop to ensure all resources
    have file_extension before passing to DAGs. Does a single batch DB
    query for any resources missing the extension.

    Args:
        resources: List of resource dicts with 'location' keys
        tenant_id: Tenant ID for isolation
        workflow_run_id: Optional workflow run ID for audit logging

    Returns:
        Same list with file_extension added to any resources missing it

    Example:
        >>> resources = [
        ...     {'location': 'abc123...', 'type': 'image'},  # missing extension
        ...     {'location': 'def456...', 'type': 'image', 'file_extension': 'png'}  # has it
        ... ]
        >>> enriched = enrich_resources_with_extensions(resources, tenant_id=1)
        >>> # First resource now has file_extension from DB lookup
    """
    if not resources:
        return resources

    # Find resources missing file_extension
    missing = []
    for r in resources:
        if isinstance(r, dict) and not r.get('file_extension'):
            location = r.get('location', '')
            if location:
                # Extract hash from path
                content_hash = location.split('/')[-1] if '/' in location else location
                if '.' in content_hash:
                    content_hash = content_hash.rsplit('.', 1)[0]
                # Only lookup valid SHA256 hashes
                if is_valid_sha256_hash(content_hash):
                    missing.append((r, content_hash))
                else:
                    logger.debug(f"Skipping non-hash location: {location[:50]}...")

    if not missing:
        return resources  # All resources already have extensions

    # Batch lookup missing extensions
    hashes_to_lookup = [h for _, h in missing]

    # AUDIT: Log the enrichment request with context
    logger.info(
        f"AUDIT: Extension enrichment for {len(hashes_to_lookup)} resources "
        f"(tenant={tenant_id}, workflow_run={workflow_run_id or 'unknown'})"
    )
    extensions = batch_lookup_file_extensions(hashes_to_lookup, tenant_id)

    # Enrich resources with found extensions
    for resource, content_hash in missing:
        if content_hash in extensions:
            resource['file_extension'] = extensions[content_hash]

    return resources


def batch_lookup_file_extensions(content_hashes: list, tenant_id: int) -> dict:
    """
    Batch lookup file extensions from database for multiple content hashes.

    FALLBACK mechanism for when file_extension is missing from parent_outputs.
    Does a single DB query for all missing extensions - efficient at scale.

    Args:
        content_hashes: List of SHA256 hashes to look up
        tenant_id: Tenant ID for isolation

    Returns:
        Dict mapping content_hash -> file_extension (only includes found entries)

    Example:
        >>> extensions = batch_lookup_file_extensions(['abc123...', 'def456...'], tenant_id=1)
        >>> print(extensions)  # {'abc123...': 'png', 'def456...': 'jpg'}
    """
    from sqlalchemy import create_engine, text

    if not content_hashes:
        return {}

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set, cannot lookup file extensions")
        return {}

    try:
        logger.info(f"FALLBACK: Batch looking up {len(content_hashes)} file extensions from DB (tenant {tenant_id})")
        engine = create_engine(database_url)
        with engine.connect() as conn:
            # Use ANY for efficient batch lookup
            result = conn.execute(
                text("""
                    SELECT id, file_extension
                    FROM content_blobs
                    WHERE id = ANY(:content_hashes) AND tenant_id = :tenant_id
                      AND file_extension IS NOT NULL AND file_extension != ''
                """),
                {"content_hashes": content_hashes, "tenant_id": tenant_id}
            ).fetchall()

            extensions = {row[0]: row[1] for row in result}
            logger.info(f"FALLBACK: Found extensions for {len(extensions)}/{len(content_hashes)} hashes")
            return extensions
    except Exception as e:
        logger.error(f"FALLBACK: Failed to batch lookup file extensions: {e}")
        return {}


def lookup_file_extension_from_hash(content_hash: str, tenant_id: int) -> Optional[str]:
    """
    Look up file extension from database based on content hash.

    PURE HASH STORAGE: Files in MinIO are stored as pure SHA256 hashes without extensions.
    The file_extension is stored in the content_blobs table for processing tools that
    need to know the file type.

    NOTE: This is a FALLBACK mechanism. In normal operation, file_extension should be
    passed through parent_outputs from Traffic Cop. This DB lookup only happens if
    the extension is missing (edge cases, bugs, legacy data).

    Args:
        content_hash: SHA256 hash of file content (the MinIO object name)
        tenant_id: Tenant ID for isolation

    Returns:
        File extension (lowercase, no dot) or None if not found

    Example:
        >>> ext = lookup_file_extension_from_hash('abc123def456...', tenant_id=1)
        >>> print(ext)  # 'png'
    """
    from sqlalchemy import create_engine, text

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set, cannot lookup file extension")
        return None

    try:
        logger.info(f"FALLBACK: Looking up file_extension from DB for hash {content_hash[:12]}... (tenant {tenant_id})")
        engine = create_engine(database_url)
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT file_extension
                    FROM content_blobs
                    WHERE id = :content_hash AND tenant_id = :tenant_id
                    LIMIT 1
                """),
                {"content_hash": content_hash, "tenant_id": tenant_id}
            ).fetchone()

            if result and result[0]:
                logger.info(f"FALLBACK: Found extension '{result[0]}' for hash {content_hash[:12]}...")
                return result[0]
            logger.warning(f"FALLBACK: No extension found in DB for hash {content_hash[:12]}...")
            return None
    except Exception as e:
        logger.error(f"FALLBACK: Failed to lookup file extension for {content_hash}: {e}")
        return None


def get_download_rename_command(
    source_path: str,
    tenant_id: int,
    dest_dir: str = "/data/input",
    file_extension: Optional[str] = None
) -> str:
    """
    Generate a shell command that renames downloaded files with proper extension.

    PURE HASH STORAGE: Since files in MinIO are stored as pure hashes without
    extensions, processing tools (GIMP, FFmpeg, etc.) need files renamed with
    their actual extensions. This function generates the rename command.

    Args:
        source_path: MinIO path (may be a pure hash or path ending in hash)
        tenant_id: Tenant ID for database lookup (fallback only)
        dest_dir: Destination directory for downloaded file
        file_extension: File extension (fast path - passed from parent_outputs)
                       If None, falls back to DB lookup

    Returns:
        Shell command string for renaming downloaded files

    Example:
        >>> # Fast path - extension provided
        >>> cmd = get_download_rename_command('abc123...', tenant_id=1, file_extension='png')
        >>>
        >>> # Fallback path - DB lookup
        >>> cmd = get_download_rename_command('abc123...', tenant_id=1)
    """
    # Fast path: extension provided by caller (from parent_outputs)
    if not file_extension:
        # Fallback: Extract hash and lookup from database
        if '/' in source_path:
            content_hash = source_path.split('/')[-1]
        else:
            content_hash = source_path

        # Remove any existing extension (shouldn't have one, but be safe)
        if '.' in content_hash:
            content_hash = content_hash.rsplit('.', 1)[0]

        # DB lookup (logged as FALLBACK)
        file_extension = lookup_file_extension_from_hash(content_hash, tenant_id)

    if not file_extension:
        logger.warning(f"No file extension available, files will remain extensionless")
        return ""

    # Generate rename command that adds extension to files without one
    # This checks if the file has no extension (filename equals basename without extension)
    rename_cmd = f"""
        for f in {dest_dir}/*; do
            if [ -f "$f" ]; then
                basename=$(basename "$f")
                # If file has no dot (no extension), add the extension
                if [ "${{basename##*.}}" = "$basename" ]; then
                    mv "$f" "$f.{file_extension}"
                    echo "Renamed $basename -> $basename.{file_extension}"
                fi
            fi
        done
    """

    return rename_cmd


def build_download_with_extension_command(
    minio_bucket: str,
    source_path: str,
    tenant_id: int,
    dest_dir: str = "/data/input",
    file_extension: Optional[str] = None
) -> str:
    """
    Build a complete download command that fetches from MinIO and renames
    files with their proper extensions for processing tools.

    This is the recommended way for DAGs to download files with pure hash storage.

    Args:
        minio_bucket: MinIO bucket name
        source_path: Path within bucket (typically the content hash)
        tenant_id: Tenant ID for extension lookup (fallback only)
        dest_dir: Local destination directory
        file_extension: File extension (fast path - passed from parent_outputs)
                       If None, falls back to DB lookup

    Returns:
        Complete shell command for mc cp + rename

    Example:
        >>> # Fast path - extension provided from parent_outputs
        >>> cmd = build_download_with_extension_command(
        ...     'tenant-1-files',
        ...     'abc123def...',
        ...     tenant_id=1,
        ...     file_extension='png'
        ... )
        >>>
        >>> # Fallback path - DB lookup if extension not provided
        >>> cmd = build_download_with_extension_command(
        ...     'tenant-1-files',
        ...     'abc123def...',
        ...     tenant_id=1
        ... )
    """
    rename_cmd = get_download_rename_command(
        source_path, tenant_id, dest_dir, file_extension=file_extension
    )

    if rename_cmd:
        return f"""
            mkdir -p {dest_dir} && \\
            mc cp --recursive myminio/{minio_bucket}/{source_path} {dest_dir}/ && \\
            {rename_cmd} && \\
            ls -lh {dest_dir}/
        """
    else:
        # No rename needed (extension not found or already has extension)
        return f"""
            mkdir -p {dest_dir} && \\
            mc cp --recursive myminio/{minio_bucket}/{source_path} {dest_dir}/ && \\
            ls -lh {dest_dir}/
        """


# =============================================================================
# HASH-BASED OUTPUT UPLOAD (Pure Hash Storage for Outputs)
# =============================================================================

def compute_file_hash(file_path: str) -> str:
    """
    Compute SHA256 hash of a file.

    Args:
        file_path: Path to the file

    Returns:
        64-character lowercase hex SHA256 hash
    """
    import hashlib

    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def upload_output_as_hash(
    minio_client,
    bucket_name: str,
    local_file_path: str,
    logical_name: str,
    tenant_id: int,
    workflow_run_id: int,
    node_run_id: int,
    output_index: int = 0,
    is_terminal: bool = False,
    output_type: Optional[str] = None
) -> dict:
    """
    Upload a workflow output file using content-addressed storage (SHA256 hash).

    This function:
    1. Computes SHA256 hash of the file
    2. Checks if hash already exists in MinIO (deduplication)
    3. Uploads to MinIO using hash as object name
    4. Records in workflow_node_outputs table with full provenance

    Args:
        minio_client: Initialized MinIO client
        bucket_name: Target bucket (usually tenant-{id}-files)
        local_file_path: Path to the local file to upload
        logical_name: Original filename for reference (e.g., 'albedo_output.png')
        tenant_id: Tenant ID for isolation
        workflow_run_id: ID of the workflow run
        node_run_id: ID of the node run (database FK to node_runs table)
        output_index: Index for multi-output nodes (0, 1, 2...)
        is_terminal: True if this is a final workflow output
        output_type: Type of output ('albedo', 'normal', 'mesh', etc.)

    Returns:
        dict with upload result:
        {
            'content_hash': 'abc123...',
            'logical_name': 'albedo_output.png',
            'file_extension': 'png',
            'size_bytes': 12345,
            'deduplicated': True/False,
            'output_id': 123
        }

    Example:
        >>> result = upload_output_as_hash(
        ...     minio_client,
        ...     'tenant-1-files',
        ...     '/data/output/albedo.png',
        ...     'albedo_output.png',
        ...     tenant_id=1,
        ...     workflow_run_id=123,
        ...     node_run_id=456,
        ...     is_terminal=True,
        ...     output_type='albedo'
        ... )
    """
    from datetime import datetime, timedelta
    import os
    import mimetypes

    from database import SessionLocal
    from models import WorkflowNodeOutput, WorkflowOutputAuditLog, Tenant

    # Get file info
    file_size = os.path.getsize(local_file_path)
    file_extension = os.path.splitext(logical_name)[1].lstrip('.').lower()
    mime_type, _ = mimetypes.guess_type(logical_name)

    # Compute hash
    content_hash = compute_file_hash(local_file_path)
    logger.info("Computed hash for %s: %s...",
                hash_for_log(logical_name), content_hash[:12])

    # Check if already exists in MinIO (deduplication)
    deduplicated = False
    try:
        minio_client.stat_object(bucket_name, content_hash)
        deduplicated = True
        logger.info(f"Hash {content_hash[:12]}... already exists (deduplicated)")
    except Exception:
        # Object doesn't exist, need to upload
        logger.info("Uploading %s as %s...",
                    hash_for_log(logical_name), content_hash[:12])
        minio_client.fput_object(
            bucket_name,
            content_hash,
            local_file_path,
            content_type=mime_type or 'application/octet-stream'
        )

    # Record in database using ORM
    try:
        with SessionLocal() as session:
            # Get tenant's retention period
            tenant = session.query(Tenant).filter(Tenant.id == tenant_id).first()
            retention_days = tenant.default_retention_days if tenant and tenant.default_retention_days else 2555
            retention_expires_at = datetime.utcnow() + timedelta(days=retention_days)

            # Create output record using ORM
            output = WorkflowNodeOutput(
                content_hash=content_hash,
                tenant_id=tenant_id,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                output_index=output_index,
                logical_name=logical_name,
                file_extension=file_extension,
                size_bytes=file_size,
                mime_type=mime_type,
                is_terminal=is_terminal,
                output_type=output_type,
                retention_expires_at=retention_expires_at
            )
            session.add(output)
            session.flush()  # Get the ID without committing
            output_id = output.id

            # Create audit log entry using ORM
            audit_log = WorkflowOutputAuditLog(
                output_id=output_id,
                content_hash=content_hash,
                tenant_id=tenant_id,
                action='created',
                performed_by_system='metadata_upload',
                audit_metadata={
                    "logical_name": logical_name,
                    "workflow_run_id": workflow_run_id,
                    "node_run_id": node_run_id,
                    "deduplicated": deduplicated
                }
            )
            session.add(audit_log)
            session.commit()

            logger.info("Recorded output %s: %s... (%s)",
                        output_id, content_hash[:12],
                        hash_for_log(logical_name))

            return {
                'content_hash': content_hash,
                'logical_name': logical_name,
                'file_extension': file_extension,
                'size_bytes': file_size,
                'deduplicated': deduplicated,
                'output_id': output_id
            }

    except ImportError:
        # Fallback if ORM models not available (shouldn't happen in production)
        logger.warning("ORM models not available, skipping DB record")
        return {
            'content_hash': content_hash,
            'logical_name': logical_name,
            'file_extension': file_extension,
            'size_bytes': file_size,
            'deduplicated': deduplicated,
            'output_id': None
        }


def build_hash_upload_command(
    source_dir: str = "/data/output",
    minio_bucket: str = None,
    tenant_id: int = None
) -> str:
    """
    Build a shell command that uploads files using hash-based storage.

    This command:
    1. Computes SHA256 hash of each file
    2. Uploads using hash as object name
    3. Outputs a mapping file (hash,original_name,size,extension)

    The mapping file can be parsed by the worker to record in database.

    Args:
        source_dir: Directory containing output files
        minio_bucket: Target bucket
        tenant_id: For bucket name generation if not provided

    Returns:
        Shell command string

    Example:
        >>> cmd = build_hash_upload_command(
        ...     source_dir='/data/output',
        ...     minio_bucket='tenant-1-files'
        ... )
    """
    if not minio_bucket and tenant_id:
        minio_bucket = get_tenant_bucket_name(tenant_id)

    return f"""
        echo "Uploading files with hash-based storage..."
        for file in {source_dir}/*; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                extension="${{filename##*.}}"
                hash=$(sha256sum "$file" | cut -d' ' -f1)
                size=$(stat -c%s "$file")

                # Check if hash already exists
                if mc stat myminio/{minio_bucket}/$hash > /dev/null 2>&1; then
                    echo "DEDUP,$hash,$filename,$size,$extension"
                else
                    mc cp "$file" myminio/{minio_bucket}/$hash
                    echo "UPLOAD,$hash,$filename,$size,$extension"
                fi
            fi
        done > /data/upload_manifest.csv
        cat /data/upload_manifest.csv
    """


def parse_upload_manifest(manifest_content: str) -> list:
    """
    Parse the upload manifest from build_hash_upload_command.

    Args:
        manifest_content: Output from the upload command

    Returns:
        List of dicts with upload info
    """
    results = []
    for line in manifest_content.strip().split('\n'):
        if ',' not in line:
            continue
        parts = line.split(',')
        if len(parts) >= 5:
            results.append({
                'action': parts[0],  # UPLOAD or DEDUP
                'content_hash': parts[1],
                'logical_name': parts[2],
                'size_bytes': int(parts[3]),
                'file_extension': parts[4],
                'deduplicated': parts[0] == 'DEDUP'
            })
    return results


def run_hash_upload_in_container(
    docker_client,
    volume_name: str,
    minio_bucket: str,
    minio_env: dict,
    docker_network: str,
    minio_extra_hosts: dict,
    s3_client_image: str,
    tenant_id: int,
    workflow_run_id: int,
    node_run_id: int,
    node_kind: str,
    is_terminal: bool = True,
    output_type: Optional[str] = None,
    source_dir: str = "/data/output",
    bandwidth_limit: Optional[str] = None,
) -> dict:
    """
    Complete hash-based upload workflow in a single function call.

    This replaces the old hierarchical folder upload with content-addressed storage.
    Call this instead of the old `mc cp --recursive` pattern.

    Args:
        docker_client: Initialized Docker client
        volume_name: Name of the ephemeral Docker volume with output files
        minio_bucket: Target bucket name
        minio_env: MinIO environment variables (from get_minio_container_config)
        docker_network: Docker network name
        minio_extra_hosts: Extra hosts mapping for distributed workers
        s3_client_image: S3 client Docker image
        tenant_id: Tenant ID
        workflow_run_id: Workflow run ID
        node_run_id: Node run ID (database FK to node_runs table)
        node_kind: Node-type identifier (NodeDefinition.node_kind)
        is_terminal: True if outputs are final/visible to user
        output_type: Type of output ('albedo', 'normal', etc.)
        source_dir: Directory in container with output files
        bandwidth_limit: Optional bandwidth cap for mc (e.g. "100MB" for 100MB/s).
                         Falls back to MC_BANDWIDTH_LIMIT env var if not provided.

    Returns:
        dict with:
        - outputs: List of output dicts for Traffic Cop
        - upload_results: Raw upload manifest data
        - recorded_outputs: Database records with output_ids
        - total_bytes: Total bytes uploaded
        - upload_count: Number of files uploaded
        - dedup_count: Number of files deduplicated
        - duration_seconds: Upload duration

    Example:
        >>> result = run_hash_upload_in_container(
        ...     docker_client=client,
        ...     volume_name=volume_name,
        ...     minio_bucket=minio_bucket,
        ...     minio_env=minio_env,
        ...     docker_network=docker_network,
        ...     minio_extra_hosts=minio_extra_hosts,
        ...     s3_client_image=s3_client_image,
        ...     tenant_id=tenant_id,
        ...     workflow_run_id=workflow_run_id,
        ...     node_run_id=node_run_id,
        ...     node_kind=node_kind,
        ...     output_type='albedo'
        ... )
        >>> outputs = result['outputs']  # For Traffic Cop
    """
    import time

    # SECURITY: Validate bucket name before use in shell commands
    if not _validate_bucket_name(minio_bucket):
        logger.error(f"SECURITY: Invalid bucket name rejected: {minio_bucket[:50]}")
        return {
            'outputs': [],
            'upload_results': [],
            'recorded_outputs': [],
            'total_bytes': 0,
            'upload_count': 0,
            'dedup_count': 0,
            'duration_seconds': 0,
            'manifest': '',
            'error': 'Invalid bucket name'
        }

    # SECURITY: Validate source_dir is a safe path (no traversal)
    if '..' in source_dir or not source_dir.startswith('/data'):
        logger.error(f"SECURITY: Invalid source_dir rejected: {source_dir}")
        return {
            'outputs': [],
            'upload_results': [],
            'recorded_outputs': [],
            'total_bytes': 0,
            'upload_count': 0,
            'dedup_count': 0,
            'duration_seconds': 0,
            'manifest': '',
            'error': 'Invalid source directory'
        }

    logger.info(f"Starting hash-based upload: bucket={minio_bucket}, type={output_type}")

    # Resolve bandwidth limit: explicit param > env var > no limit
    bw = bandwidth_limit or os.getenv('MC_BANDWIDTH_LIMIT', '')
    # Validate format (e.g. "100MB", "1GB", "500KB")
    if bw and not re.match(r'^\d+[KMG]B$', bw, re.IGNORECASE):
        logger.warning(f"Invalid MC_BANDWIDTH_LIMIT value '{bw}', ignoring")
        bw = ''

    bw_flag = f' --limit-upload {bw}' if bw else ''
    large_file_threshold = 1 * 1024 * 1024 * 1024  # 1GB

    # Build the hash upload shell command
    # NOTE: Files are iterated safely - filenames are shell-escaped via $file variable
    # Hash is computed by sha256sum (guaranteed to be valid 64-char hex)
    # Build upload commands
    hash_upload_cmd = f"""
        for file in {source_dir}/*; do
            if [ -f "$file" ]; then
                filename=$(basename "$file")
                extension="${{filename##*.}}"
                hash=$(sha256sum "$file" | cut -d' ' -f1)
                size=$(stat -c%s "$file")

                if mc stat myminio/{minio_bucket}/$hash > /dev/null 2>&1; then
                    echo "DEDUP,$hash,$filename,$size,$extension"
                else
                    mc cp{bw_flag} "$file" myminio/{minio_bucket}/$hash
                    echo "UPLOAD,$hash,$filename,$size,$extension"
                fi
            fi
        done
    """

    upload_start = time.perf_counter()

    upload_result = docker_client.containers.run(
        image=s3_client_image,
        entrypoint="/bin/sh",
        command=["-c", hash_upload_cmd],
        environment=minio_env,
        volumes={volume_name: {'bind': '/data', 'mode': 'rw'}},
        network=docker_network,
        extra_hosts=minio_extra_hosts,
        remove=True,
        detach=False
    )

    duration = time.perf_counter() - upload_start

    # Parse manifest
    manifest_output = upload_result.decode('utf-8') if upload_result else ''
    upload_results = parse_upload_manifest(manifest_output)

    # Calculate stats
    total_bytes = sum(r.get('size_bytes', 0) for r in upload_results)
    dedup_count = sum(1 for r in upload_results if r.get('deduplicated'))
    upload_count = len(upload_results) - dedup_count

    logger.info(f"Upload complete: {upload_count} uploaded, {dedup_count} deduplicated, {total_bytes} bytes in {duration:.2f}s")

    # FAIL if container produced no output files
    if not upload_results:
        raise RuntimeError(
            f"Container produced no output files. "
            f"Check container logs - the processing may have failed silently or received incorrect input data type."
        )

    # Record in database
    recorded_outputs = record_hash_outputs(
        upload_results=upload_results,
        tenant_id=tenant_id,
        workflow_run_id=workflow_run_id,
        node_run_id=node_run_id,
        node_kind=node_kind,
        is_terminal=is_terminal,
        output_type=output_type
    )

    # Build Traffic Cop output format with full storage path
    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    outputs = []
    for r in recorded_outputs:
        content_hash = r['content_hash']
        # Build full storage path: s3://tenant-{id}-files/{hash}
        full_path = f"{storage_scheme}://{minio_bucket}/{content_hash}"
        outputs.append({
            "type": "image",
            "location": full_path,
            "file_path": full_path,
            "content_hash": content_hash,  # Also include raw hash for reference
            "file_extension": r.get('file_extension', ''),
            "logical_name": r.get('logical_name'),
            "output_id": r.get('output_id'),
            "node_run_id": node_run_id
        })

    return {
        'outputs': outputs,
        'upload_results': upload_results,
        'recorded_outputs': recorded_outputs,
        'total_bytes': total_bytes,
        'upload_count': upload_count,
        'dedup_count': dedup_count,
        'duration_seconds': duration,
        'manifest': manifest_output
    }


def record_hash_outputs(
    upload_results: list,
    tenant_id: int,
    workflow_run_id: int,
    node_run_id: int,
    node_kind: str,
    is_terminal: bool = False,
    output_type: Optional[str] = None
) -> list:
    """
    Record uploaded hash outputs in the workflow_node_outputs table.

    This function is called AFTER the container-based hash upload completes.
    It parses the upload manifest and records each output with full provenance.

    Args:
        upload_results: List of dicts from parse_upload_manifest()
        tenant_id: Tenant ID for isolation
        workflow_run_id: ID of the workflow run
        node_run_id: ID of the node run (database FK to node_runs table)
        node_kind: Node-type identifier (NodeDefinition.node_kind)
        is_terminal: True if these are final workflow outputs
        output_type: Type of output ('albedo', 'normal', 'mesh', etc.)

    Returns:
        List of recorded output dicts with output_id

    Example:
        >>> manifest_output = container_result.decode('utf-8')
        >>> upload_results = parse_upload_manifest(manifest_output)
        >>> recorded = record_hash_outputs(
        ...     upload_results,
        ...     tenant_id=1,
        ...     workflow_run_id=123,
        ...     node_run_id=456,
        ...     node_kind='albedo_texture_process',
        ...     is_terminal=True,
        ...     output_type='albedo'
        ... )
    """
    from datetime import datetime, timedelta

    from database import SessionLocal
    from models import WorkflowNodeOutput, WorkflowOutputAuditLog, Tenant

    if not upload_results:
        logger.warning("No upload results to record")
        return []

    recorded_outputs = []

    try:
        with SessionLocal() as session:
            # Get tenant's retention period
            tenant = session.query(Tenant).filter(Tenant.id == tenant_id).first()
            retention_days = tenant.default_retention_days if tenant and tenant.default_retention_days else 2555
            retention_expires_at = datetime.utcnow() + timedelta(days=retention_days)

            # Record each output
            for idx, result in enumerate(upload_results):
                content_hash = result['content_hash']
                logical_name = result['logical_name']
                file_extension = result.get('file_extension', '')
                size_bytes = result.get('size_bytes', 0)
                deduplicated = result.get('deduplicated', False)

                try:
                    # Create output record using ORM
                    output = WorkflowNodeOutput(
                        content_hash=content_hash,
                        tenant_id=tenant_id,
                        workflow_run_id=workflow_run_id,
                        node_run_id=node_run_id,
                        output_index=idx,
                        logical_name=logical_name,
                        file_extension=file_extension,
                        size_bytes=size_bytes,
                        is_terminal=is_terminal,
                        output_type=output_type,
                        retention_expires_at=retention_expires_at
                    )
                    session.add(output)
                    session.flush()  # Get the ID without committing
                    output_id = output.id

                    # Create audit log entry using ORM
                    audit_log = WorkflowOutputAuditLog(
                        output_id=output_id,
                        content_hash=content_hash,
                        tenant_id=tenant_id,
                        action='created',
                        performed_by_system=node_kind,
                        audit_metadata={
                            "logical_name": logical_name,
                            "workflow_run_id": workflow_run_id,
                            "node_run_id": node_run_id,
                            "deduplicated": deduplicated,
                            "output_index": idx
                        }
                    )
                    session.add(audit_log)

                    logger.info("Recorded output %s: %s... (%s)",
                        output_id, content_hash[:12],
                        hash_for_log(logical_name))

                    recorded_outputs.append({
                        **result,
                        'output_id': output_id
                    })

                except Exception as e:
                    logger.error(f"Failed to record output {content_hash[:12]}...: {e}")
                    recorded_outputs.append({
                        **result,
                        'output_id': None,
                        'error': str(e)
                    })

            # Commit all records at once
            session.commit()

    except ImportError:
        # Fallback if ORM models not available (shouldn't happen in production)
        logger.warning("ORM models not available, skipping DB record")
        return upload_results

    return recorded_outputs


# =============================================================================
# HASH-BASED DOWNLOAD (Pure Hash Storage for Inputs)
# =============================================================================

def _validate_hash(content_hash: str) -> bool:
    """Validate that a string is a safe SHA256 hash (64 hex chars only)."""
    if not content_hash or not isinstance(content_hash, str):
        return False
    # Must be exactly 64 lowercase hex characters
    return bool(re.match(r'^[a-f0-9]{64}$', content_hash.lower()))


def _validate_extension(ext: str) -> str:
    """Sanitize file extension - only allow alphanumeric chars."""
    if not ext:
        return ''
    # Remove leading dot if present
    ext = ext.lstrip('.')
    # Only allow alphanumeric characters (a-z, A-Z, 0-9)
    if re.match(r'^[a-zA-Z0-9]+$', ext):
        return ext.lower()
    logger.warning(f"SECURITY: Invalid file extension blocked: {ext[:20]}")
    return ''


def _validate_bucket_name(bucket: str) -> bool:
    """Validate bucket name - only allow safe characters."""
    if not bucket or not isinstance(bucket, str):
        return False
    # Bucket names: lowercase, numbers, hyphens, 3-63 chars
    return bool(re.match(r'^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$', bucket))


def run_hash_download_in_container(
    docker_client,
    volume_name: str,
    minio_bucket: str,
    minio_env: dict,
    docker_network: str,
    minio_extra_hosts: dict,
    s3_client_image: str,
    inputs: list,
    dest_dir: str = "/data/input",
    bandwidth_limit: Optional[str] = None,
) -> dict:
    """
    Complete hash-based download workflow in a single function call.

    Downloads one or more files from MinIO using content hashes and renames
    them with proper extensions for processing tools.

    This replaces inline `mc cp` commands with a single helper call.

    SECURITY: All inputs are validated before use in shell commands:
    - content_hash: Must be valid 64-char hex SHA256
    - file_extension: Only alphanumeric characters allowed
    - bucket_name: Must match S3 bucket naming rules

    Args:
        docker_client: Initialized Docker client
        volume_name: Name of the ephemeral Docker volume for input files
        minio_bucket: Source bucket name
        minio_env: MinIO environment variables (from get_minio_container_config)
        docker_network: Docker network name
        minio_extra_hosts: Extra hosts mapping for distributed workers
        s3_client_image: S3 client Docker image
        inputs: List of input dicts, each with:
            - content_hash: SHA256 hash (object name in S3)
            - file_extension: Extension to restore (e.g., 'png', 'obj')
            - logical_name: (optional) Original filename for logging
        dest_dir: Directory in container for downloaded files
        bandwidth_limit: Optional bandwidth cap for mc (e.g. "100MB" for 100MB/s).
                         Falls back to MC_BANDWIDTH_LIMIT env var if not provided.

    Returns:
        dict with:
        - success: True if all downloads succeeded
        - downloaded_files: List of local file paths
        - total_bytes: Total bytes downloaded
        - duration_seconds: Download duration
        - errors: List of any errors

    Example:
        >>> result = run_hash_download_in_container(
        ...     docker_client=client,
        ...     volume_name=volume_name,
        ...     minio_bucket=minio_bucket,
        ...     minio_env=minio_env,
        ...     docker_network=docker_network,
        ...     minio_extra_hosts=minio_extra_hosts,
        ...     s3_client_image=s3_client_image,
        ...     inputs=[
        ...         {'content_hash': 'abc123...', 'file_extension': 'png'},
        ...         {'content_hash': 'def456...', 'file_extension': 'obj'}
        ...     ]
        ... )
        >>> if result['success']:
        ...     print(f"Downloaded {len(result['downloaded_files'])} files")
    """
    import time

    if not inputs:
        return {
            'success': True,
            'downloaded_files': [],
            'total_bytes': 0,
            'duration_seconds': 0,
            'errors': []
        }

    # SECURITY: Validate bucket name
    if not _validate_bucket_name(minio_bucket):
        logger.error(f"SECURITY: Invalid bucket name rejected: {minio_bucket[:50]}")
        return {
            'success': False,
            'downloaded_files': [],
            'total_bytes': 0,
            'duration_seconds': 0,
            'errors': ['Invalid bucket name']
        }

    # SECURITY: Validate dest_dir is a safe path (no traversal, must be in /data)
    if '..' in dest_dir or not dest_dir.startswith('/data'):
        logger.error(f"SECURITY: Invalid dest_dir rejected: {dest_dir}")
        return {
            'success': False,
            'downloaded_files': [],
            'total_bytes': 0,
            'duration_seconds': 0,
            'errors': ['Invalid destination directory']
        }

    logger.info(f"Starting hash-based download: {len(inputs)} files from {minio_bucket}")

    # Resolve bandwidth limit: explicit param > env var > no limit
    bw = bandwidth_limit or os.getenv('MC_BANDWIDTH_LIMIT', '')
    if bw and not re.match(r'^\d+[KMG]B$', bw, re.IGNORECASE):
        logger.warning(f"Invalid MC_BANDWIDTH_LIMIT value '{bw}', ignoring")
        bw = ''
    bw_flag = f' --limit-download {bw}' if bw else ''

    # Build download commands for all inputs
    download_commands = []
    for inp in inputs:
        content_hash_or_path = inp.get('content_hash') or inp.get('location')  # Support both formats
        file_extension = inp.get('file_extension', '')
        logical_name = inp.get('logical_name', '')

        if not content_hash_or_path:
            logger.warning("Skipping input without content_hash: %s",
                           hash_for_log(inp))
            continue

        # Normalize and strip any leading/trailing whitespace
        content_hash_or_path = content_hash_or_path.strip()

        # Determine if this is a pure hash or a path-based location
        # Pure hash: exactly 64 lowercase hex characters
        # Path-based: contains slashes or is not a valid 64-char hash
        is_pure_hash = _validate_hash(content_hash_or_path.lower())

        if is_pure_hash:
            # Pure hash storage: object key is just the hash
            minio_object_key = content_hash_or_path.lower()
            content_hash = content_hash_or_path.lower()
            logger.info(f"Using pure hash storage: {content_hash[:12]}...")
        else:
            # Path-based storage: object key is the full path
            # SECURITY: Validate path doesn't contain dangerous characters
            # Allow alphanumeric, slashes, underscores, hyphens, dots
            if not re.match(r'^[a-zA-Z0-9/_\-\.]+$', content_hash_or_path):
                logger.error("SECURITY: Invalid path rejected: %s",
                             hash_for_log(content_hash_or_path))
                continue
            # Prevent directory traversal
            if '..' in content_hash_or_path:
                logger.error("SECURITY: Path traversal rejected: %s",
                             hash_for_log(content_hash_or_path))
                continue
            minio_object_key = content_hash_or_path
            # Extract the last component as the "hash" for filename purposes
            content_hash = content_hash_or_path.split('/')[-1]
            logger.info("Using path-based storage: %s",
                        hash_for_log(minio_object_key))

        # SECURITY: Sanitize file extension
        file_extension = _validate_extension(file_extension)

        # Safe logical name for logging (truncate and sanitize)
        safe_logical_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', logical_name[:50]) if logical_name else content_hash[:12]

        # Destination filename: use hash/basename with extension
        if file_extension:
            dest_filename = f"{content_hash}.{file_extension}"
        else:
            dest_filename = content_hash

        # Sanitize dest_filename for shell safety
        safe_dest_filename = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', dest_filename)

        # Build mc cp command with validated values only
        download_commands.append(f"""
            echo "Downloading {safe_logical_name} ({content_hash[:12]}...)"
            mc cp{bw_flag} myminio/{minio_bucket}/{minio_object_key} {dest_dir}/{safe_dest_filename}
            if [ $? -eq 0 ]; then
                size=$(stat -c%s {dest_dir}/{safe_dest_filename} 2>/dev/null || echo 0)
                echo "OK,{content_hash},{safe_dest_filename},$size"
            else
                echo "FAIL,{content_hash},{safe_dest_filename},0"
            fi
        """)

    if not download_commands:
        return {
            'success': True,
            'downloaded_files': [],
            'total_bytes': 0,
            'duration_seconds': 0,
            'errors': ['No valid inputs to download']
        }

    # Combine all commands
    full_cmd = f"""
        mkdir -p {dest_dir}
        {"".join(download_commands)}
        echo "---DOWNLOAD_COMPLETE---"
        ls -lh {dest_dir}/
    """

    start_time = time.perf_counter()

    try:
        result = docker_client.containers.run(
            image=s3_client_image,
            entrypoint="/bin/sh",
            command=["-c", full_cmd],
            environment=minio_env,
            volumes={volume_name: {'bind': '/data', 'mode': 'rw'}},
            network=docker_network,
            extra_hosts=minio_extra_hosts,
            remove=True,
            detach=False
        )

        duration = time.perf_counter() - start_time
        output = result.decode('utf-8') if result else ''

        # Parse results
        downloaded_files = []
        total_bytes = 0
        errors = []

        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('OK,'):
                parts = line.split(',')
                if len(parts) >= 4:
                    downloaded_files.append(f"{dest_dir}/{parts[2]}")
                    total_bytes += int(parts[3])
            elif line.startswith('FAIL,'):
                parts = line.split(',')
                if len(parts) >= 3:
                    errors.append(f"Failed to download {parts[1][:12]}...")

        logger.info(f"Download complete: {len(downloaded_files)} files, {total_bytes} bytes in {duration:.2f}s")

        return {
            'success': len(errors) == 0,
            'downloaded_files': downloaded_files,
            'total_bytes': total_bytes,
            'duration_seconds': duration,
            'errors': errors
        }

    except Exception as e:
        duration = time.perf_counter() - start_time
        logger.error(f"Download failed: {e}")
        return {
            'success': False,
            'downloaded_files': [],
            'total_bytes': 0,
            'duration_seconds': duration,
            'errors': [str(e)]
        }
