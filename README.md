# Bexs

Recovered Bexs Teach-Memory application source: the existing October 18 candidate with the October 27 offline-startup fix. This restores source to this repository; it does not deploy or replace a running Telegram bot.

## Run now without credentials

Requires Python 3.10 or newer and the standard library. No package installation is needed.

```sh
python3 teach_demo.py --interactive --data-dir ./data/demo
```

This uses the existing synthetic demonstration seed. It does not contain the owner's original private memory. Commands include `/remember topic = value`, `/recall topic`, `/correct topic = value`, `/teach phrase = reply`, `/chat phrase`, `/forget topic`, `/unteach phrase`, `/restart`, and `/quit`.

For a scripted demonstration of persistence, correction, teaching and forgetting:

```sh
python3 teach_demo.py --json
```

## Tests and recovery evidence

On October 28, 2026, all 74 recovered tests passed locally with the exact original private seed fixture. The original seed's checksum matched the existing recovery receipt. The memory lifecycle demonstration passed. Outbound socket connections were blocked during verification; zero network attempts occurred. See `verification.json` for the result.

The private fixture is not published. These archived synthetic test modules run without it:

```sh
python3 -m unittest discover -s tests -p test_teach_memory.py -v
python3 -m unittest discover -s tests -p test_teach_lab.py -v
```

The full archived suite requires the original authorized fixture at `seed/original-memory.json`:

```sh
python3 -m unittest discover -s tests -v
```

Other original modes in `bot.py` also expect that seed unless a valid seed is supplied with `--seed`. Use `teach_demo.py` for the ready-to-run synthetic experience.

## Scope

The application provides per-chat Markov learning, explicit remembered values, taught replies, inspection and cloning tools. It is not neural-model training. The recovered code and tests retain their existing behavior; the offline-startup fix ignores incomplete Telegram-only configuration only in local chat mode.

Current live-bot entrypoints were not in this candidate. No private corpus, credentials, runtime databases, wallet, automatic trader, scheduler or hosted service is included or activated.

Telegram operation is separate from this offline restoration and requires owner configuration. Do not use this saved candidate to overwrite newer live code or data.
