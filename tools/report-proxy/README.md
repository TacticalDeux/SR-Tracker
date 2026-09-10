# Report proxy setup

A small public front for bug reports. The app sends plain report JSON
(recent activity / comment-only) or one multipart call (metadata JSON
plus the joined session file) here; the worker writes one row to its
bound D1 database and stores whole-file bytes in a private file host
under an unguessable key. The row keeps the capped excerpt plus that
key. No secret exists anywhere in this path — D1 needs none (binding
only) and the file host is reached through a binding too — and no
secret ever ships with the app.

Whole-file uploads ride one multipart POST: a `metadata` part with
the usual flat fields (the inline log stays under the ~1MB bound) and
a `file` part with the full session text.

## 1. Log in (dev machine only, never commit anything from this step)

```sh
npx wrangler login
```

## 2. Create the database (dev machine only)

```sh
npx wrangler d1 create sr-tracker-reports
```

## 3. Create the table (dev machine only)

```sh
npx wrangler d1 execute sr-tracker-reports --remote --file=schema.sql
```

Existing databases predate the file-key column — run once:

```sh
npx wrangler d1 execute sr-tracker-reports --remote --command="ALTER TABLE issues ADD COLUMN log_url TEXT"
```

(If the column is already there this errors; ignore it.)

## 4. Create the file host (dev machine only)

```sh
npx wrangler r2 bucket create sr-tracker-report-logs
```

The bucket stays private: no public serving is configured, on
purpose. Fetch a stored file with:

```sh
npx wrangler r2 object get sr-tracker-report-logs/<key> ./report.log
```

or find the key (the issue row's file-key field) and download it
through the dashboard bucket browser.

## 5. Fill in the binding id (repo file, safe to commit — an identifier, not a secret)

Paste the id from the `d1 create` output into `wrangler.toml`:

```toml
database_id = "<the id from the create output>"
```

## 6. Deploy

```sh
npx wrangler deploy
```

## 7. Rebuild the app (dev machine only, separate step)

The client side ships in the exe: rebuild it after these changes so
the dialog offers the full-file choice and posts the multipart form.
Until both sides ship, older clients keep working — the plain JSON
path is unchanged, and the worker still accepts it.

## 8. Clean up the Turso era (dev machine only)

The worker no longer reads them; remove the sealed secrets and then
delete or retain the old database at your discretion:

```sh
npx wrangler secret delete TURSO_URL
npx wrangler secret delete TURSO_TOKEN
```
