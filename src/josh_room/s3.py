"""Provider-neutral S3 bucket operations used by storage adapters."""

import re

from botocore.exceptions import ClientError

_BUCKET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?$")


class BucketListForbidden(PermissionError):
    def __init__(self, message: str, provider: str, context=None):
        self.result = {"error_code": "bucket-list-forbidden", "recoverable": True, "provider": provider}
        if context is not None:
            self.result["connection_id"] = context
        super().__init__(message)


class BucketAccessDenied(PermissionError):
    def __init__(self, message: str, provider: str, context=None):
        self.result = {"error_code": "bucket-access-denied", "recoverable": True, "provider": provider}
        if context is not None:
            self.result["connection_id"] = context
        super().__init__(message)


def validate_bucket_name(bucket: str) -> str:
    if not isinstance(bucket, str) or not _BUCKET_NAME.fullmatch(bucket):
        raise ValueError("bucket name must be 3-63 lowercase letters, numbers, dots, or hyphens")
    if ".." in bucket or ".-" in bucket or "-." in bucket:
        raise ValueError("bucket name contains an invalid separator")
    return bucket


def list_buckets(client, provider: str, *, error_type=BucketListForbidden, context=None) -> list[str]:
    try:
        response = client.list_buckets()
    except ClientError as error:
        details = error.response.get("Error", {})
        code = str(details.get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code.lower() in {"accessdenied", "forbidden", "unauthorized"} or status == 403:
            raise error_type(f"{provider} bucket listing is forbidden", provider=provider, context=context) from error
        raise
    return sorted(bucket["Name"] for bucket in response.get("Buckets", []) if isinstance(bucket, dict) and bucket.get("Name"))


def create_bucket(client, bucket: str, *, region: str = "us-east-1") -> str:
    bucket = validate_bucket_name(bucket)
    kwargs = {"Bucket": bucket}
    if region not in {"auto", "us-east-1", None}:
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**kwargs)
    return bucket


def check_bucket_access(client, bucket: str, provider: str, *, error_type=BucketAccessDenied, context=None) -> str:
    bucket = validate_bucket_name(bucket)
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as error:
        details = error.response.get("Error", {})
        code = str(details.get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code.lower() in {"accessdenied", "forbidden", "unauthorized", "nosuchbucket"} or status in {403, 404}:
            raise error_type(f"{provider} bucket is unavailable or access is denied", provider=provider, context=context) from error
        raise
    return bucket
