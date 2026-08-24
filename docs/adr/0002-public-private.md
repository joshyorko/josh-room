# ADR 0002 — Public platform and private instance

The repository is a strongly Josh-opinionated public platform. Public history
contains code, schemas, synthetic fixtures, and intentionally selected public
age recipients only. Real catalogs, private identities, R2 credentials,
private paths, timestamps, repositories, employer/customer data, and snapshots
remain outside Git under `$XDG_CONFIG_HOME/josh-room/`.

Normal snapshot, hydrate, and enter operations are non-interactive with respect
to Cloudflare. R2 credentials are imported once into the host OS Secret
Service/keyring and retrieved only at operation time. The private XDG file
contains endpoint, bucket, and an opaque keyring profile, not credentials. A
dedicated, bucket-scoped Josh Room token is preferred; broader Fizzy/Kamal
credentials are not a runtime dependency.
