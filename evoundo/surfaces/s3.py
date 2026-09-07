"""Amazon S3 mutation surface driver for EvoUndo.

Provides versioning-aware object recovery, non-versioned fallback,
multipart upload cleanup, conflict refusal, and physical S3 verification.
"""

from __future__ import annotations
import base64
import hashlib
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from evoundo.recovery.driver_registry import DriverRegistry, _decode_b64

logger = logging.getLogger("evoundo.surfaces.s3")


class S3SurfaceDriver:
    """Production Amazon S3 mutation driver with versioning and conflict recovery."""

    DRIVER_TYPE = "s3"
    _client_cache: Dict[str, Any] = {}

    @classmethod
    def get_client(
        cls,
        endpoint_url: Optional[str] = None,
        region_name: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ) -> Any:
        """Construct or retrieve cached boto3 S3 client."""
        cache_key = f"{endpoint_url}:{region_name}"
        if cache_key in cls._client_cache:
            return cls._client_cache[cache_key]

        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or os.environ.get("EVOUNDO_S3_ENDPOINT_URL"),
            region_name=region_name or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=aws_access_key_id or os.environ.get("AWS_ACCESS_KEY_ID", "testing"),
            aws_secret_access_key=aws_secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "testing"),
        )
        cls._client_cache[cache_key] = client
        return client

    @classmethod
    def register(cls) -> None:
        """Register this driver with the central DriverRegistry."""
        DriverRegistry.register_driver(
            driver_type=cls.DRIVER_TYPE,
            executor=cls.execute_recovery,
            verifier=cls.verify_recovery,
        )
        logger.info("Registered '%s' surface driver with DriverRegistry", cls.DRIVER_TYPE)

    @classmethod
    def capture_witness(
        cls,
        bucket: str,
        key: str,
        client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Capture pre-mutation physical witness state for an S3 object."""
        s3 = client or cls.get_client()
        witness: Dict[str, Any] = {
            "bucket": bucket,
            "key": key,
            "existed": False,
            "version_id": None,
            "etag": None,
            "content_b64": None,
            "content_length": 0,
            "metadata": {},
            "is_versioned": False,
        }

        # Check versioning status
        try:
            v_res = s3.get_bucket_versioning(Bucket=bucket)
            witness["is_versioned"] = (v_res.get("Status") == "Enabled")
        except Exception:
            witness["is_versioned"] = False

        # Read object metadata and content
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            witness["existed"] = True
            witness["etag"] = head.get("ETag", "").strip('"')
            witness["version_id"] = head.get("VersionId")
            witness["metadata"] = head.get("Metadata", {})
            witness["content_length"] = head.get("ContentLength", 0)

            # For objects under 10MB, capture content
            if witness["content_length"] <= 10 * 1024 * 1024:
                obj = s3.get_object(Bucket=bucket, Key=key)
                body = obj["Body"].read()
                witness["content_b64"] = base64.b64encode(body).decode("ascii")
        except Exception as e:
            # Object does not exist
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code in ("404", "NoSuchKey", "NotFound"):
                witness["existed"] = False
            else:
                witness["existed"] = False

        return witness

    @classmethod
    def execute_recovery(cls, op: Any, witness: Any) -> None:
        """Execute physical rollback of an S3 mutation."""
        params = op.parameters or {}
        bucket = params.get("bucket")
        key = params.get("key")
        operation = (op.operation or params.get("operation", "PUT_OBJECT")).upper()
        s3 = cls.get_client(endpoint_url=params.get("endpoint_url"), region_name=params.get("region_name", "us-east-1"))

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        if not bucket or not key:
            raise ValueError("S3 recovery operation requires 'bucket' and 'key' parameters")

        created_version_id = params.get("created_version_id")
        created_upload_id = params.get("upload_id")

        if operation == "PUT_OBJECT":
            existed = w.get("existed", False)
            is_versioned = w.get("is_versioned", False)

            # Check for downstream concurrent mutation conflict
            try:
                curr_head = s3.head_object(Bucket=bucket, Key=key)
                curr_etag = curr_head.get("ETag", "").strip('"')
                curr_version = curr_head.get("VersionId")

                # If a specific version was created by this mutation, check if a newer version was added
                if created_version_id and curr_version and curr_version != created_version_id:
                    raise ValueError(
                        f"CONFLICT_DETECTED: S3 object '{bucket}/{key}' has newer version '{curr_version}' "
                        f"overwriting mutated version '{created_version_id}'"
                    )
            except Exception as e:
                if "CONFLICT_DETECTED" in str(e):
                    raise

            if is_versioned and created_version_id:
                # Versioned recovery: explicitly delete the created version ID
                s3.delete_object(Bucket=bucket, Key=key, VersionId=created_version_id)
                logger.info("Deleted created S3 version '%s' for '%s/%s'", created_version_id, bucket, key)
            else:
                # Non-versioned bucket
                if not existed:
                    s3.delete_object(Bucket=bucket, Key=key)
                    logger.info("Deleted non-versioned S3 object '%s/%s'", bucket, key)
                else:
                    # Restore previous content
                    raw_b64 = w.get("content_b64")
                    if raw_b64 is not None:
                        body_bytes = base64.b64decode(raw_b64)
                        s3.put_object(
                            Bucket=bucket,
                            Key=key,
                            Body=body_bytes,
                            Metadata=w.get("metadata", {}),
                        )
                        logger.info("Restored previous S3 object '%s/%s'", bucket, key)

        elif operation == "DELETE_OBJECT":
            is_versioned = w.get("is_versioned", False)
            delete_marker_version_id = params.get("delete_marker_version_id")

            if is_versioned and delete_marker_version_id:
                s3.delete_object(Bucket=bucket, Key=key, VersionId=delete_marker_version_id)
                logger.info("Removed S3 delete marker '%s' for '%s/%s'", delete_marker_version_id, bucket, key)
            else:
                raw_b64 = w.get("content_b64")
                if raw_b64 is not None:
                    body_bytes = base64.b64decode(raw_b64)
                    s3.put_object(
                        Bucket=bucket,
                        Key=key,
                        Body=body_bytes,
                        Metadata=w.get("metadata", {}),
                    )
                    logger.info("Restored deleted S3 object '%s/%s' from witness", bucket, key)

        elif operation in ("MULTIPART_UPLOAD", "ABORT_MULTIPART"):
            upload_id = created_upload_id or params.get("upload_id")
            if upload_id:
                try:
                    s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                    logger.info("Aborted orphaned S3 multipart upload '%s' for '%s/%s'", upload_id, bucket, key)
                except Exception as e:
                    logger.warning("Could not abort multipart upload: %s", e)

        else:
            raise NotImplementedError(f"Unsupported S3 recovery operation: {operation}")

    @classmethod
    def verify_recovery(cls, op: Any, witness: Any) -> bool:
        """Physically verify external S3 object state matches post-recovery condition."""
        params = op.parameters or {}
        bucket = params.get("bucket")
        key = params.get("key")
        operation = (op.operation or params.get("operation", "PUT_OBJECT")).upper()
        s3 = cls.get_client(endpoint_url=params.get("endpoint_url"), region_name=params.get("region_name", "us-east-1"))

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        if not bucket or not key:
            return False

        existed = w.get("existed", False)

        try:
            if operation == "PUT_OBJECT":
                if not existed:
                    try:
                        s3.head_object(Bucket=bucket, Key=key)
                        return False
                    except Exception:
                        return True
                else:
                    head = s3.head_object(Bucket=bucket, Key=key)
                    expected_etag = w.get("etag")
                    if expected_etag:
                        curr_etag = head.get("ETag", "").strip('"')
                        return curr_etag == expected_etag
                    return True

            elif operation == "DELETE_OBJECT":
                head = s3.head_object(Bucket=bucket, Key=key)
                expected_etag = w.get("etag")
                if expected_etag:
                    curr_etag = head.get("ETag", "").strip('"')
                    return curr_etag == expected_etag
                return True

            elif operation in ("MULTIPART_UPLOAD", "ABORT_MULTIPART"):
                upload_id = params.get("upload_id")
                if upload_id:
                    res = s3.list_multipart_uploads(Bucket=bucket, Prefix=key)
                    uploads = res.get("Uploads", [])
                    return not any(u.get("UploadId") == upload_id for u in uploads)
                return True

            return True
        except Exception as e:
            logger.error("Failed S3 physical recovery verification: %s", e)
            return False
