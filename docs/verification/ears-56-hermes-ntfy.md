# EARS-56 — isolated Hermes ntfy compat send and one-shot cron evidence

2026-11-24 23:31–23:34 UTC, Hermes checkout `d6525ab61b9ffde4121f12a03cbb772c860b4c01` (`v0.23.4-386-gd6525ab61b`), relay checkout `4bd761a12bd49187cbcf183cfb3a4a202f2810e6` (1.1.2), Earshot declared app version `0.1.12` (`version.properties`, `2af5a8187c6ba5a4c0c1f2e137157d870d186fc6`). **Device/Android: none; no attached device or emulator; no phone playback attempted.** This is a successful outbound **transport** spike, not a playback demo. Related Earshot-side report: `earshot/docs/verification/ears-56.md` in its separate candidate commit.

## Boundary and reproducible setup

Run only in a throwaway private directory under the active profile's scratch; export `TMPDIR=/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch`. The **reusable, exercised fixture** is [`examples/hermes-ntfy-spike.py`](../../examples/hermes-ntfy-spike.py) after `uv sync --frozen` in the relay checkout: `python3` should be the checkout's `.venv/bin/python`, then run `.venv/bin/python examples/hermes-ntfy-spike.py`. It creates fresh random tokens/ports and removes its runtime automatically, including on ordinary exceptions. In the recorded run the isolated runtime lived at `$TMPDIR/ears56-run`, chmod 0700, with a Moto S3 server bound to `127.0.0.1:43887` and a fresh private bucket `ears56-private-fixture`. Relay bound **only** `127.0.0.1:33179` main and `127.0.0.1:50737` compat, with its own private SQLite directory. Fixture writer ID/secret were generated randomly, explicitly supplied with `EARSHOT_S3_ENDPOINT=http://127.0.0.1:43887`, `EARSHOT_BUCKET=ears56-private-fixture`, `EARSHOT_REGION=us-east-1`, `EARSHOT_RELAY_ALLOW_HTTP_S3=1` (only for Moto), `EARSHOT_RELAY_DATA_DIR=$TMPDIR/ears56-run/relay-data`, `EARSHOT_RELAY_HOST=127.0.0.1`, `EARSHOT_RELAY_PORT=33179`, `EARSHOT_RELAY_COMPAT=ntfy`, `EARSHOT_RELAY_COMPAT_PORT=50737`, `EARSHOT_RELAY_COMPAT_NTFY_TOPICS=phone=phone`; no alarm or external port/tunnel. The relay used *different* generated main, beacon and send-only compat tokens. No environment from any production profile or cloud credentials was passed to the fixture process. There was no handset fixture READ credential, since no handset was run. The private bucket contained only two generated one-line nonces and was destroyed afterward.

`HERMES_HOME=$TMPDIR/ears56-run/hermes` (also isolated HOME/XDG/TMPDIR) was a new throwaway home. Its mode-0600 `.env` held `NTFY_SERVER_URL=http://127.0.0.1:50737` and `NTFY_TOKEN=<generated EARSHOT_RELAY_COMPAT_TOKEN>`; the main relay token was **not** given to Hermes. `NTFY_TOPIC` was unset. Its `config.yaml` enabled the ntfy platform with `extra.server` set to that loopback compat URL and `extra.token: "${NTFY_TOKEN}"`. Only the standalone outbound sender was called; no gateway/subscriber was started. The installed launcher `/home/delorenj/.local/bin/hermes` resolved to this Hermes checkout. Each command below ran with the isolated environment. Re-create fresh random tokens and ports for reproduction; do **not** copy these vanished local addresses to a production config or use a real bucket. A no-agent job's script must be placed under `<HERMES_HOME>/scripts/` and specified by filename, not absolute path.

## Sanitized terminal and HTTP output (actual results)

```text
$ GET http://127.0.0.1:50737/phone/json
HTTP 404 {"code":40401,"http":404,"error":"page not found"}
$ GET http://127.0.0.1:50737/v1/status  [send-only compat token]
HTTP 404 {"code":40401,"http":404,"error":"page not found"}
$ GET http://127.0.0.1:33179/v1/status  [send-only compat token]
HTTP 401 {"error":{"code":"unauthorized","message":"missing or invalid bearer token"}}
$ GET http://127.0.0.1:33179/mcp  [send-only compat token]
HTTP 401 {"error":{"code":"unauthorized","message":"missing or invalid bearer token"}}
$ GET http://127.0.0.1:33179/v1/status  [separate main TEST token]
HTTP 200; compat.enabled=["ntfy"], compat.ntfy_topics={"phone":"phone"},
          compat.send_token=true, receipts.configured=true, receipts.devices=[]

$ hermes send ntfy:phone "EARS56-position-syntax-check"
exit=2; hermes: error: unrecognized arguments: EARS56-position-syntax-check
# The positional syntax in the earlier strategy draft is NOT supported. Use --to.
$ hermes send --to ntfy:phone "EARS56-manual-20261124T233125Z" --json
exit=0
{"success":true,"platform":"ntfy","chat_id":"phone",
 "message_id":"msg_37d237343e9e4810a09378b407cef844"}
$ GET http://127.0.0.1:33179/v1/messages/msg_37d237343e9e4810a09378b407cef844?wait=2  [main TEST token]
HTTP 200; id=msg_37d237343e9e4810a09378b407cef844;
key=phone/37d237343e9e4810a09378b407cef844.say;
status=stored; devices=[];
hint="no beacon has reached this relay; set the phone's beacon endpoint (see GET /v1/status)"

# The throwaway <HERMES_HOME>/scripts/ears56-once.py printed the sole line
# EARS56-cron-<UTC timestamp> on stdout. No LLM was called.
$ hermes cron create "2026-11-24T23:36:11+00:00" \
    "EARS56 one-shot no-agent test" --name "EARS56 isolated one shot" \
    --script ears56-once.py --no-agent --deliver ntfy:phone --no-wrap-response
exit=0; Created job: cfab27cdb894; Schedule: once at 2026-11-24 23:36;
Mode: no-agent (script stdout delivered directly)
warning: Scheduler is not ready for profile 'default': no gateway serves it and no fresh ticker heartbeat.
$ hermes cron list --all
exit=0; cfab27cdb894 [active]; Repeat: 0/1; Deliver: ntfy:phone;
Script: ears56-once.py; Mode: no-agent
$ hermes cron run cfab27cdb894
exit=0; Triggered job: EARS56 isolated one shot (cfab27cdb894);
Next run: 2026-11-24T23:36:11+00:00; Ran now: succeeded.
$ hermes cron runs cfab27cdb894
exit=0; cea8b2f9d4634be3b83e6c3579c9e508  completed
job=cfab27cdb894 source=direct 2026-11-24T18:32:13.276064-05:00
$ GET http://127.0.0.1:33179/v1/messages/msg_fffbe858e9924c799bae48dbc738b65f?wait=2  [main TEST token]
HTTP 200; id=msg_fffbe858e9924c799bae48dbc738b65f;
key=phone/fffbe858e9924c799bae48dbc738b65f.say;
status=stored; devices=[]; same no-beacon hint
$ hermes cron remove cfab27cdb894
exit=0; Removed job: EARS56 isolated one shot (cfab27cdb894)
$ hermes cron list --all
exit=0; No scheduled jobs in profile 'default'.
```

Private Moto object GETs independently returned exact bytes `EARS56-manual-20261124T233125Z` at `phone/37d237343e9e4810a09378b407cef844.say` and `EARS56-cron-20261124T233213Z` at `phone/fffbe858e9924c799bae48dbc738b65f.say`. This correlates the Hermes manual message ID, completed scheduler fire and two distinct objects/receipts. Both receipts **remained `stored`** after the bounded `wait=2`; `devices=[]`, no beacon. Neither is playback Tested. A stored/HTTP success is not a finished receipt. Even a real `finished` receipt means device completion, not a person heard it or the screen was locked.

Other bounded attempts: initial absolute `--script /.../ears56-once.py` was refused (`exit=1`, script path must be relative to `<HERMES_HOME>/scripts/`); corrected above. `hermes --version` in the empty throwaway home exited 1 (`no dependency environment is committed for this install; run hermes pm repair`), so the version above comes from `git describe --tags --always` on the installed source checkout; the actual `hermes send` and `hermes cron` commands nevertheless ran successfully. The one-shot was triggered manually **before its nominal ISO time** because this throwaway profile intentionally had no gateway/ticker; its completed scheduler execution and delivery are proven, automatic wall-clock wakeup is **not**. No independent recurring clock was created; any future recurring instruction must use Candybar.

## Cleanup evidence and remaining gate

At `2026-11-24T23:33:45Z`, both fixture listeners stopped; private credentials file, throwaway `HERMES_HOME` (including `.env`, job and script), private Moto bucket/process and relay SQLite directory were deleted. `hermes cron list --all` showed zero jobs **before** deleting the home. A separate `ANDROID_ADB_SERVER_PORT=5069 adb devices -l` showed zero devices; no S26 or production Hermes profile/settings/credentials/storage were touched. The run's raw sanitized transcripts remained only in the active-profile scratch evidence directory; this page reproduces the relevant results and IDs without secret values. Full Android playback/locked-screen validation needs a newly authorized emulator/spare, explicitly isolated READ credentials and beacon, and a selected-device `finished` receipt. Do not call this a working voice demo until then.
