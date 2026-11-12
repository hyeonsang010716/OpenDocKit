# Release notes 2.4

## Added

- `--json` output for the command line tool
- Retry with backoff for uploads

## Fixed

| Issue | Area | Fix |
| --- | --- | --- |
| \#812 | Export | Dates lost their time zone |
| \#840 | Login | Session expired during upload |

```python
client.upload(path, retries=3)
```
