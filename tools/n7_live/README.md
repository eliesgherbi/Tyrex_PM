# N7 live tools

## Operator live one-shot (you run this)

From the repo root in Git Bash:

```bash
python tools/n7_live/run_n7_live_oneshot.py --live
```

Invoking with `--live` **is** the authorization. No phrase, envelope, or nonce.

## Read-only diagnosis

```bash
python tools/n7_live/run_n7_readonly_preflight.py --out-dir var/reporting/n7/readonly_diag
```

## Fake rehearsal (no venue)

```bash
python tools/n7_live/run_n7_live_oneshot.py --fake-rehearsal
```
