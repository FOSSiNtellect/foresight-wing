# Contributing to Foresight Wing

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

## Ground rules

- **Local-first stays local.** Nothing may send meeting audio, transcripts, notes or files off the machine. The only
  network calls are the ones the user starts (saving a web page, a calendar subscription, a model download).
- **No telemetry**, analytics or crash reporting.
- **No Google code, assets or prompts.** Foresight Wing is a clean-room project. Don't paste code, prompts or resources
  taken from Google AI Edge Foresight or any other closed app.

## Set up

Run `setup.bat` (see [Run from source](README.md#run-from-source) in the README), then run the tests:

```bash
python -m pytest tests -q
```

The tests run offline and need no microphone or Ollama. Please keep it that way: fake the model calls, as the existing
tests do.

## Pull requests

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Keep each pull request to one change, and match the style of the code around it.
3. Add or update tests for behaviour you change, and make sure `python -m pytest tests -q` passes.
4. Update the README or `docs/` when you change a feature, a setting or an environment variable.

## Reporting bugs

Open an issue with your Windows version, Python version, the Ollama models you use and the steps to reproduce.
Please leave out anything private from your meetings or files.

Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
