# Encrypted database backups

WAL-G can encrypt every base backup and every change-log (WAL) file before it
leaves the server, using a PGP public key. The server holds only the public
key, so it can write encrypted backups but cannot read them back. The private
key, which decrypts them, stays offline with the owner and is used only for a
restore. Clashers see no difference.

Encryption is off until the owner places a public key and turns it on. The
rest of [PostgreSQL backups and recovery](deployment.md#postgresql-backups-and-recovery)
applies unchanged: schedule, retention, `backup-status` and alerts.

## How ops keeps encrypted and plain backups apart

WAL-G reads a whole storage prefix either with a key or without one. A plain
file read with a key fails with `openpgp: incorrect key`, and an encrypted file
read without one fails to unpack. So one prefix must hold only one kind, or a
restore that crosses the change (for example a delta built on a plain full
backup) could not be read at all.

`ops` enforces this: a backup prefix ending in `-pgp` must be used exactly
when `CLASHLENS_BACKUP_PGP_PUBLIC_KEY_FILE` is set. Turning encryption on or
off therefore always starts a new prefix with its own full backup, and every
older prefix keeps restoring with the settings it was written with.

`ops up` stops before changing anything when encryption is on but the key file
is missing, is not mode 600, is not an armored PGP public key, or also holds a
private key. `ops` checks only the file's form; a damaged key, or a mounted key
that disappears from a running release, makes WAL-G fail the upload rather than
send it unencrypted, and the existing backup alerts fire.

With encryption on, `ops` turns off WAL-G's change-log overwrite check
(`WALG_PREVENT_WAL_OVERWRITE`). That check reads the stored copy back to compare
it, which needs the private key, so after a crash PostgreSQL's retry of an
already-uploaded file would fail forever and change log would pile up on disk.
Without it, a retry uploads the same file again. The check mainly guarded
against two databases writing to one prefix, which the deployment rules
already forbid: every database gets its own prefix, never shared with a
scratch database.

## Make the key (owner, offline)

On the owner's own computer, not on rogue:

```sh
gpg --quick-gen-key 'Clash Lens backups' ed25519 cert never
gpg --list-keys 'Clash Lens backups'      # note the fingerprint
gpg --quick-add-key FINGERPRINT cv25519 encr never
gpg --armor --export FINGERPRINT > clashlens-backup-pgp.asc
gpg --armor --export-secret-keys FINGERPRINT > clashlens-backup-pgp-private.asc
```

Protect the key with a strong passphrase when asked. `never` means the key
does not expire: an expired key would make every upload fail. Keep the private
key file and its passphrase in two separate offline places (for example the
password manager and an encrypted USB drive). Losing both makes every encrypted
backup unreadable. Only `clashlens-backup-pgp.asc` (the public key) ever goes
to the server.

## Turn it on (needs owner approval)

1. Copy the public key to rogue as
   `/srv/clashlens-secrets/clashlens-backup-pgp.asc`, owned by the service
   account, mode 600.
2. In private `app.env`, add the key and switch to a new prefix ending in
   `-pgp`:

   ```ini
   CLASHLENS_BACKUP_PGP_PUBLIC_KEY_FILE=/srv/clashlens-secrets/clashlens-backup-pgp.asc
   CLASHLENS_BACKUP_PREFIX=s3://clashlens-pg-backup/rogue-pg18-pgp
   ```

3. Run the approved `./ops up`. It mounts the public key read-only into
   PostgreSQL at `/run/secrets/walg-pgp.asc` (uid/gid 70, mode 0400), next to
   the existing settings secret, and PostgreSQL restarts with WAL going to the
   new prefix, encrypted.
4. Straight away run `./ops backup`. The first backup in a new prefix is always
   a full copy. Then `./ops backup-status`.
5. Run the [restore drill](#restore) below for a recent point in the new
   prefix before relying on it. Repeat it at the seven-day boundary once the
   prefix holds seven days.

The old prefix covers up to the last change log it received before the
restart, at most about five minutes earlier (`archive_timeout=300`). The new
prefix covers from the end of its first full backup. Points between those two
cannot be restored from either; running step 4 at once keeps that gap short.
`backup-status` reports the seven-day window as warming up for the first seven
days. Choose the prefix by the point you need: up to the old prefix's last
change log, the old prefix; from the end of the first encrypted full backup,
the new one.

Retention runs only on the active prefix, so the old plain prefix keeps its
last backups until removed. Once the new prefix has a full seven days and a
restore drill from it has passed, delete the old prefix with the owner's
approval (this deletes data): with a temporary WAL-G settings secret naming the
old prefix, run `wal-g --config /run/secrets/walg.json delete everything FORCE --confirm`.

## Restore

Follow [Restore into a separate database](deployment.md#restore-into-a-separate-database)
with these changes for a `-pgp` prefix:

- Run it on a throwaway machine, not rogue, so the private key never touches
  the production host. For a real recovery onto a new host, place the key there
  only for the restore.
- Mount the private key as a second temporary Podman secret, for example at
  `/run/secrets/walg-private.asc` with uid/gid 70 and mode 0400.
- In the temporary settings secret, add
  `"WALG_PGP_KEY_PATH":"/run/secrets/walg-private.asc"` and
  `"WALG_PGP_KEY_PASSPHRASE":"..."`. Do not print it.
- Delete both temporary secrets and the key file when finished.

For a point up to the old prefix's last change log (see above), restore from
the old plain prefix exactly as before, without any key settings. Points after
that and before the end of the first encrypted full backup cannot be restored.

With the wrong settings for a prefix, WAL-G does not stop: it logs
`DecryptAndDecompressTar: decrypt failed` and retries the download about 15
times with growing waits. Stop it and correct the settings.

## Turn it off (rollback)

1. Remove `CLASHLENS_BACKUP_PGP_PUBLIC_KEY_FILE` from `app.env` and set a new
   prefix that does not end in `-pgp`, for example
   `s3://clashlens-pg-backup/rogue-pg18-plain`.
2. Run the approved `./ops up`, then `./ops backup` and `./ops backup-status`.

Do step 1 before deploying any release older than this change. Older `ops`
ignores the key setting, so with a `-pgp` prefix still in `app.env` it would
write plain files into the encrypted prefix.

The encrypted prefix still restores with the private key; keep the key until
that prefix is deleted. Replacing the key works the same way: a new key goes
with a new `-pgp` prefix, and the old private key stays until its prefix is gone.
