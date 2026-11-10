# Amazon S3 and S3-compatible storage

S3 is an explicit provider, separate from Cloudflare R2 and MinIO. It uses the
existing encrypted Dimension keyset and Restic Room Store. It does not introduce
a second backup format. Use a dedicated, existing bucket and an explicit region.
A custom S3-compatible endpoint must use HTTPS, including on loopback. The local
HTTP exception remains specific to MinIO.

Without a custom endpoint, the controller resolves the regional endpoint from
the installed AWS SDK's local endpoint metadata for commercial AWS, GovCloud,
and China regions. Unknown regions require an explicit HTTPS endpoint. Custom
S3-compatible regions need not appear in the AWS region list. Endpoint URLs
cannot contain userinfo, queries, or fragments, and TLS verification cannot be
disabled. Path-style selection applies to both the SDK and Restic. Dotted bucket
names use path-style HTTPS addressing in both transports to avoid a wildcard
certificate mismatch, including with custom endpoints.

## Credentials and selection

Select **Amazon S3 / S3-compatible** in the native storage picker. Supply the
region, an existing bucket, and either the default AWS endpoint or an explicit
HTTPS endpoint. Bucket selection must remain explicit; setup does not need to
enumerate an AWS account. Access key, secret key, and optional session token are
private credentials. The native extension passes credentials through its existing
private handoff and stores them in SecretStorage and the controller keyring.
Do not put them in a Room, command arguments, catalog, screenshot, or issue.
Expired temporary credentials must be replaced through reconnect.

Give each connection a recognizable label. **New S3 Connection** creates a
separate opaque connection and keyring profile even when another account uses
the same regional endpoint. Reuse an existing connection only when its bucket
authority is the one you intend. CLI creation with an occupied explicit
connection ID is rejected; use update or reconnect to change that connection.
Connection labels and regions are not proof of AWS account identity.

SDK failures returned by S3 operations use bounded error codes such as
`credentials-invalid`, `region-or-endpoint-mismatch`, `access-denied`, and
`bucket-unavailable`. Raw provider messages are not included. A missing object
or a conditional publication conflict still follows the existing Room Store
recovery behavior.

Provider credentials and encryption recovery material remain separate. An S3
bucket credential is an enrollment-capable trust decision for a fixed Dimension
keyset, just as for MinIO. Recovery private identities are never uploaded to the
bucket. Initialization does not automatically create or change bucket policies.

## Acceptance and operational limits

Local fixture tests establish only the cases they exercise. Real AWS acceptance
requires synthetic data, a disposable bucket, and separately supplied private
credentials. Never use a private project for public evidence. Qualify the exact
packaged VSIX through native VS Code commands independently of controller tests.

The real-provider acceptance sequence is:

1. Connect the disposable bucket in its explicit region and initialize encryption.
2. Save a synthetic Room, repeat Save without edits, and verify no recovery point
   or content was added. Make one deterministic edit and verify an incremental Save.
3. List and inspect history; Enter and hydrate selected recovery points into a
   different disposable directory and compare the restored bytes.
4. Remove one disposable recovery point and verify the remaining history. Inject
   a failure before catalog publication and verify prior history stays authoritative.
5. Check temporary/session credentials, expired or missing credentials, wrong
   region, access denial, and a nonexistent bucket. Exercise independent bucket
   policies denying list, write, and delete. Check versioned buckets, multipart
   transfers, and clock/signature failures where available.
6. Inspect bounded diagnostics and receipts for credential and private-identifier
   leakage. Capture synthetic native UI evidence for connection, Save/history,
   no-op/incremental Save, and restore/Enter.

AWS/provider checks, packaged-VSIX qualification, and screenshots remain **NOT RUN**
until receipts for the named source and package are recorded. Offline tests must
not require AWS or a network connection.
