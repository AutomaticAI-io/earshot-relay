# EARS-56 — isolated Hermes ntfy compat send and one-shot cron evidence

2026-11-24 23:31–23:34 UTC initial **transport-only** run, Hermes checkout `d6525ab61b9ffde4121f12a03cbb772c860b4c01` (`v0.23.4-386-gd6525ab61b`), relay checkout `4bd761a12bd49187cbcf183cfb3a4a202f2810e6` (1.1.2), Earshot declared app version `0.1.12` (`version.properties`, `2af5a8187c6ba5a4c0c1f2e137157d870d186fc6`). **No device was attached in this historical run.** A 2026-11-25 selected Android 16 emulator first failed TTS initialization, then a fresh eSpeak run completed playback for both real Hermes entrypoints; see below and the [Earshot EARS-56 device report](https://github.com/delorenj/earshot/blob/main/docs/verification/ears-56.md) (link resolves after parent landing). This is not physical-phone, audible, locked-screen, SigV4 or automatic wall-clock proof.

## Boundary and reproducible setup

Run only in a throwaway private directory under the active profile's scratch. Set `HERMES_HOME` to the **invoking profile** (not a production profile) and `TMPDIR="$HERMES_HOME/cache/scratch"`; the fixture validates their relationship, then creates a *new* disposable child `HERMES_HOME` under that scratch. The **reusable, exercised fixture** is [`examples/hermes-ntfy-spike.py`](../../examples/hermes-ntfy-spike.py) after `uv sync --frozen` in the relay checkout: use the checkout's `.venv/bin/python -B examples/hermes-ntfy-spike.py`. It creates fresh random tokens/ports, supervises CLI child process groups and removes its runtime with a bounded deletion retry; it verifies absence after deletion and refuses success on failed assertions/cleanup. On this host, the invoking profile was `/home/delorenj/.hermes/profiles/earshot-pm` and its scratch `/home/delorenj/.hermes/profiles/earshot-pm/cache/scratch`. In the recorded run the isolated runtime lived at `$TMPDIR/ears56-run`, chmod 0700, with a Moto S3 server bound to `127.0.0.1:43887` and a fresh private bucket `ears56-private-fixture`. Relay bound **only** `127.0.0.1:33179` main and `127.0.0.1:50737` compat, with its own private SQLite directory. Fixture writer ID/secret were generated randomly, explicitly supplied with `EARSHOT_S3_ENDPOINT=http://127.0.0.1:43887`, `EARSHOT_BUCKET=ears56-private-fixture`, `EARSHOT_REGION=us-east-1`, `EARSHOT_RELAY_ALLOW_HTTP_S3=1` (only for Moto), `EARSHOT_RELAY_DATA_DIR=$TMPDIR/ears56-run/relay-data`, `EARSHOT_RELAY_HOST=127.0.0.1`, `EARSHOT_RELAY_PORT=33179`, `EARSHOT_RELAY_COMPAT=ntfy`, `EARSHOT_RELAY_COMPAT_PORT=50737`, `EARSHOT_RELAY_COMPAT_NTFY_TOPICS=phone=phone`; no alarm or external port/tunnel. The relay used *different* generated main, beacon and send-only compat tokens. No environment from any production profile or cloud credentials was passed to the fixture process. There was no handset fixture READ credential, since no handset was run. The private bucket contained only two generated one-line nonces and was destroyed afterward.

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

## Independent-review fixture repair (2026-11-25 UTC)

The preceding 2026-11-24 IDs and commands remain **historical transport evidence**; do not relabel them as device proof. Independent review found that the first fixture could print security status mismatches and wrong object bodies yet return 0, and that a `cron remove` timeout skipped relay/Moto shutdown; one real review repetition printed cleanup then failed with `ENOTEMPTY` from a late Hermes helper writer. The repaired fixture now validates the exact denial status **and body**, mapping, successful manual JSON fields, existing-receipt 401 with the compat token and 200 with the separate main token, both unique nonce bytes and message ID/key/receipt/status/device joins. It creates the cron nonce **before** dispatch and requires a completed durable fire for the created job. Every CLI is supervised as an owned process group. Nested `finally` closes relay and Moto even when job removal throws. The runtime is deleted with bounded retry and absence is checked **after** deletion, before a cleanup-complete line; a failed removal reports `partial` and exits nonzero. The fixture accepts an explicit invoking `HERMES_HOME` and matching `TMPDIR`, rather than hard-coding one user's profile name.

Actual repaired run: `HERMES_HOME=/home/delorenj/.hermes/profiles/earshot-pm TMPDIR=$HERMES_HOME/cache/scratch .venv/bin/python -B examples/hermes-ntfy-spike.py` exited **0**. Manual `msg_b29323d686b34b0784a1aaafd0d80a87`, nonce `EARS56-manual-20261124T235949Z`; independent cron `msg_da80ec7d038a43b2a13d47bff3afe49c`, nonce `EARS56-cron-99b59b01e6f95bc26d6802c8`, fire `74014cb239d04e18a745049be5844102 completed` and `Ran now: succeeded`. Both exact objects in the isolated bucket and main-token receipts joined; both `stored`, `devices=[]`, not playback. `cron remove` exit 0; final `cron list --all` said no jobs; `{"cleanup":"complete","job_removed":true,"runtime_absent":true}` printed after runtime deletion. The raw sanitized output is at active-profile scratch `ears56-repaired-success.stdout.txt` and Moto stderr at `ears56-repaired-success.stderr.txt` (not committed; no tokens).

Final unmodified fixture rerun (after the last assertion/cleanup edits): `ears56-repaired-final.stdout.txt` / `.stderr.txt` in the same scratch, exit **0**. Manual `msg_6211c8bc5fd4468e8d393b49aac1cc17` and cron `msg_743a79e574d34dc3a498b1a9b2fec3c3` were distinct, with exact private object text and `stored`/`devices=[]` receipts; direct fire `51cb26038df947268cf48dc49c2471b3 completed`, final job list empty, `{"cleanup":"complete","job_removed":true,"runtime_absent":true}`. This is still transport-only evidence.

Fault probes against the real local Moto/relay and real Hermes CLI (not separate device tests) were run from active-profile scratch using `ears56-fault-check.py`: forced security-status 200 exited **1** with `unexpected security/subscription status`; overwritten cron body exited **1** with `cron body or identity differs from precommitted nonce`; injected `TimeoutExpired` on job removal exited **1** with `job_removed:false`, `runtime_absent:true`, `cleanup:partial: job removal failed; throwaway home erased`. Each fault run used its own disposable runtime and cleaned it; none of these probes claims successful delivery. Raw outputs: `ears56-repaired-{security-fault,wrong-body,cleanup-fault}.{stdout,stderr}.txt`. **These transport fixture runs** did not supply an Android device or an automatic wall-clock wakeup; the independent Android device run below supplied a selected-device failure, not `finished` playback.

## Independent device follow-up — actual failure after device detection (2026-11-25 00:19–00:21 UTC)

Read the [Earshot EARS-56 device report](https://github.com/delorenj/earshot/blob/main/docs/verification/ears-56.md) for the raw-evidence locations, exact setup and device teardown. It used a frozen relay source archive of `e1021e801301d0c8c183ba701c180aac0aaaf180`, Hermes `d6525ab61b9ffde4121f12a03cbb772c860b4c01`, beta-debug Earshot `0.1.12`, and dedicated `EARS56_Isolated_API36` Android **16** emulator `emulator-5604` through private ADB server `5076`, **not S26**. A fresh private Moto bucket, loopback HTTPS localhost random-path reader, private writer and send-only compat tokens, separate main read token, and isolated beacon allowed actual app service polling and receipts. Settings were seeded with `run-as` rather than the app's UI; the test did **not** establish SigV4 reader authentication or retail-device behavior. The app completed an initial empty `phone/` listing before the sends. No release build, production storage/credentials, full ntfy gateway subscription, recurring clock or agent LLM run was involved.

For each device receipt, `device_run.py` set a 70-second monotonic observation deadline and polled the main-token endpoint with `?wait=2`, sleeping two seconds between nonterminal polls. It stopped on a top-level terminal status (`finished`, `failed`, `missed`, or `skipped`); in both cases it returned the `failed` receipt containing the selected-device failure rather than waiting out the deadline.

| Actual entrypoint | Object and exact text | Selected-device result |
| --- | --- | --- |
| `hermes send --to ntfy:phone 'EARS56-device-manual-20261125T001957Z' --json` | `msg_915bcbbc69ef4d45ac79e9843ccf4904`; `phone/915bcbbc69ef4d45ac79e9843ccf4904.say` containing exactly `EARS56-device-manual-20261125T001957Z` | App beacon `detected` → **`failed`**, error `TTS synthesis failed: TTS initialization failed (-1) [step=tts]`. |
| One-shot no-agent cron `--deliver ntfy:phone`, **explicit** `hermes cron run 375e0d795e79` | Durable fire `c95db6e1575f44ac8e123118ad7fb7f2 completed source=direct`; `msg_65414fa83c6f4d61b95ac92e3c43a862`; `phone/65414fa83c6f4d61b95ac92e3c43a862.say` containing exactly `EARS56-device-cron-2d2774c8eb44bd3e41b8` | Same selected device, `detected` → **`failed`**, same exact TTS error. **Automatic wall-clock firing remains untested.** |

The selected device was beta-debug Earshot 0.1.12 (Android 16 / SDK 36), ID `cecc363c-b194-40a9-bac3-aa4cb1d96f8a`. The retained app logcat independently recorded both TTS initialization failures. In this historical run the diagnosis that no service/default synth was available is not supported by retained command output. This is not evidence of a product defect. Both paths reached device detection, exact `.say` fetch and a real failure beacon, **not completed playback in this run**. A fresh eSpeak remedy and successful follow-up are recorded below.

## Fresh eSpeak emulator follow-up — playback completion (2026-11-25 00:34–00:35 UTC)

On the same **dedicated** stopped AVD, cold-launched `emulator-5604` under private ADB server `5076`, the worker installed the beta-debug Earshot 0.1.12 APK and applied the documented eSpeak remedy. Android 16 / API 36, Hermes `d6525ab61b9ffde4121f12a03cbb772c860b4c01`, frozen relay `e1021e801301d0c8c183ba701c180aac0aaaf180` (1.1.2), same APK hash `aa228e7e8274f3e7f58429d544d294c26b0d05c0e1dc2e5c20e3e7c4ca6bf600`. Before eSpeak install `tts-setup.txt` recorded `No services found` and `tts_default_synth=null`; after installing F-Droid eSpeak NG 1.52.0, setting it as default and starting its voice-data unpack activity, the enabled `com.reecedunn.espeak.TtsService` and default were recorded. App logcat recorded a successful eSpeak binding. APK SHA-256 `0a7822fec54d7f7ae759ffedc9afeaa77f809587b171fb2101998787821ec790` matched F-Droid's HTTPS-delivered index and the hash prefix/suffix previously recorded in Earshot's EARS-7. Java `jarsigner` treated that SHA1-signed index as unsigned because the algorithm is disabled; this is **not** independently verified signed-index evidence. The Earshot report details provenance, commands, raw proof and limitations.

A fresh isolated Moto bucket, relay state, random-path HTTPS reader, credentials, throwaway Hermes profile and message IDs were used. An empty `phone/` listing returned 200 before either message. The random-path reader was anonymous (not a SigV4 auth test), app settings were seeded via `run-as` (not UI proof), the gateway did not start and `NTFY_TOPIC` stayed unset. Both main-token receipts were polled with `?wait=2` and a 70-second monotonic deadline; exact objects, nonce bodies, receipt IDs and app beacons were joined. The send-only compat token returned 401 for the actual manual native receipt and `/mcp`; separate main test token returned 200 for that receipt.

| Real entrypoint | Object and exact body | Selected-device result |
| --- | --- | --- |
| `hermes send --to ntfy:phone 'EARS56-device-manual-20261125T003451Z' --json` | `msg_6e0e746418b0428786cbc8f4700f3fb3`; `phone/6e0e746418b0428786cbc8f4700f3fb3.say`; exact body `EARS56-device-manual-20261125T003451Z` | Device `b67d884e-2076-40a0-ab79-a656b17dff52`: `detected → started → finished`; last beacon received **00:35:12.083 UTC**. |
| No-agent one-shot `--deliver ntfy:phone`, direct `hermes cron run b3d93b9f831c` | Fire `cb5818ad86e5492a9915108f774de14f completed source=direct`; `msg_e3d5cb29ed6d4c7081a38c617e664f7d`; `phone/e3d5cb29ed6d4c7081a38c617e664f7d.say`; exact body `EARS56-device-cron-d5715f2f61bc5f1bb0f9` | Same device, `detected → started → finished`; last beacon received **00:35:23.013 UTC**. |

Both rows are **Tested for playback completion on this no-audio isolated emulator**, not for physical-phone behavior, a person hearing audio, lock screen, provider SigV4 or unattended wall-clock firing. The cron command was run explicitly before its nominal ISO time, without a gateway/ticker or agent LLM. The README rows link to this evidence.

## Cleanup evidence and remaining gate

The **historical 2026-11-24 transport run** stopped both fixture listeners at `2026-11-24T23:33:45Z`; private credentials file, throwaway `HERMES_HOME` (including `.env`, job and script), private Moto bucket/process and relay SQLite directory were deleted. `hermes cron list --all` showed zero jobs **before** deleting that home. A separate `ANDROID_ADB_SERVER_PORT=5069 adb devices -l` showed zero devices then; no S26 or production Hermes profile/settings/credentials/storage were touched. The repaired transport fixture also reported `runtime_absent:true` after its final run. The **00:19 device failure run** removed job `375e0d795e79`, confirmed an empty cron list, cleared/uninstalled the beta debug app, removed ADB reverse, stopped the dedicated emulator, ADB server, relay and Moto, and deleted its runtime and credentials. In the **00:34 eSpeak run**, `cron remove b3d93b9f831c` succeeded and `cron list --all` was empty; both apps were force-stopped, data-cleared and uninstalled, default synth restored to `null`, reverse removed, emulator/ADB/relay/Moto/HTTPS fixture stopped, throwaway runtime and credentials deleted. `ss` showed no listeners on the selected rig and fixture ports. A disposable AVD image remains inert in active-profile scratch. Raw evidence remains private at `$TMPDIR/ears56-tts/` (see Earshot report for individual files). Neither run used production or S26; neither emulator result proves human hearing or locked-phone playback. Independent review of the new completion evidence remains for the parent.
