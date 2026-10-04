# Evaluation

Validates the café dataset and can replay website scenarios on the development stack.

`validate` and `score` do not send chat. `run` sends chat only with `--allow-live-chat`. `transcripts` calls a model unless `--dry-run` is set.

```sh
python -m evaluate validate
python -m unittest discover -s evaluate/tests -v
```

Live runs: [Evaluation](../docs/evaluate/integration.md).
