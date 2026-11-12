**Title:** Release notes 2.4
**Language:** C
**Date:** 2026-11-12T05:10:03Z
**Identifier:** urn:uuid:5550ce4e-9e40-46ec-94f2-f9d4a63d4f1b

# Release notes 2.4

# Release notes 2.4

## Added

* `--json` output for the command line tool
* Retry with backoff for uploads

## Fixed

| Issue | Area | Fix |
| --- | --- | --- |
| #812 | Export | Dates lost their time zone |
| #840 | Login | Session expired during upload |

```
client.upload(path, retries=3)
```