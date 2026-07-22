---
name: env-password-quoting
symptom: ClickHouse "Code 516 AUTHENTICATION_FAILED" though the password looks correct
area: ops
status: active
updated: 2026-07-21
---

## Symptom

ClickHouse rejects login (`Code: 516 ... Authentication failed: password is incorrect`)
even though the password in `.env` is right. Wrapping/unwrapping it in quotes seems to make
no consistent difference.

## Root cause

`CLICKHOUSE_PASSWORD='...'` or `="..."` in `.env`: Docker Compose's `env_file` passes the
surrounding quotes as **literal password characters** (a 40-char secret becomes 42 bytes).
The app does not strip them either (`settings.py`). So the quoted value never matches.

## Fix / correct pattern

Write secrets in `.env` **unquoted**, no surrounding quotes and no trailing spaces:
`CLICKHOUSE_PASSWORD=actualsecret`. Only quote if the value truly contains `#` or leading/
trailing whitespace. Verify structurally without printing the secret:
`awk -F= '/^CLICKHOUSE_PASSWORD=/{v=substr($0,index($0,"=")+1); print length(v), substr(v,1,1)}' .env`
(first char should be the real first char, not a quote).

## How to avoid / detect

If auth fails, first check for surrounding quotes. To confirm the credential itself,
connect with the quotes stripped in Python — if that authenticates, the quotes are the bug.
Real error is masked by [[cli-hides-real-db-error]]. Consider adding a `field_validator`
on `clickhouse_password` in `settings.py` to strip surrounding quotes defensively.
