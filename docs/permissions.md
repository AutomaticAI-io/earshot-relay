# Minimal bucket permissions

The relay only writes. It never lists, reads or deletes objects (a test asserts that `PutObject` is the only S3 call it makes), so give it a key that can do exactly that.

Keep **two separate keys** for one bucket:

- **Writer** (this relay): `s3:PutObject` on the channel prefixes.
- **Phone** (the Earshot app): list and read on the same prefixes. See the [Earshot buyer guide](https://store.automaticai.io/earshot/guide/).

Writer policy intent (adapt the syntax to your provider; `BUCKET` is a placeholder):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": ["arn:aws:s3:::BUCKET/phone/*"]}
  ]
}
```

Add `arn:aws:s3:::BUCKET/alarm/*` only if you list `alarm` in `EARSHOT_RELAY_CHANNELS`. Every channel the relay may send to needs its prefix here; a channel in the relay's list but not in the policy fails with `502 storage_error` (AccessDenied).

`earshot-relay doctor` writes one inert `<default-channel>/relay-doctor-<timestamp>.txt` object to prove the key works. The phone ignores a lone `.txt`. The relay does not delete it (it cannot); remove it yourself if you care.

Provider notes:

- **MinIO / generic S3:** the policy above, attached to a service account for the writer.
- **Cloudflare R2:** R2 has no write-only token; use an Object Read & Write token scoped to the bucket and keep it out of anything but the relay.
- **Backblaze B2:** an application key with `writeFiles` restricted to the bucket (and prefix, if supported).

## Why not the phone's key?

The phone's key can read everything in the channel. Giving it to a relay that is reachable from the network turns a relay bug into a way to read your messages. Keep them apart.

## Other things the relay holds

- `EARSHOT_RELAY_TOKEN` guards the API. Rotate it by changing the variable and restarting.
- The relay's SQLite index in `/data` stores message ids, keys, channel, times, text length and beacons. It does not store message text.
- Objects in the bucket are plain text or audio. Anyone with read access to the bucket can read them, so treat agent output sent through a hook as data you are choosing to put in that bucket.
