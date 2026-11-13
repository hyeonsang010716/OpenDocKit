# Security policy

OpenDocKit parses files from untrusted sources, so we treat robustness problems as security problems.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on the repository page
(<https://github.com/hyeonsang010716/OpenDocKit/security/advisories/new>).

Include the OpenDocKit version, the Python version and platform, and — if you can share it — a minimal file that
reproduces the problem. A script that *builds* the file is even better than the file itself.

You can expect an acknowledgement within a week. Fixes are released as a new patch version and credited to the
reporter unless you prefer otherwise.

## What counts

- A crafted document that makes `convert` use unbounded memory or time, crash the interpreter, read or write files,
  or reach the network.
- A way for one conversion inside `IsolatedConverter` to affect the host process or another conversion.
- Exception messages or warnings that leak document content or parser internals.

## Supported versions

Security fixes go into the latest released version.

## Deployment advice

`convert` bounds the inputs it knows about (file size, decompressed size, merged-cell counts, image pixels, PDF
drawing operations), but a parser library can always surprise you. When the files come from the public, run
conversions through `IsolatedConverter`, which gives every file its own worker process with a memory and time limit.
See [docs/server.md](docs/server.md).

Legacy `.doc` and `.ppt` files are converted by LibreOffice, started as a local process when it is installed. Macros,
linked content and network access are switched off through its settings, but that is configuration, not a sandbox:
when such files come from the public, run conversions in a container without network access and keep LibreOffice
patched, or switch the bridge off by setting `OPENDOCKIT_SOFFICE=` (empty). See
[docs/limitations.md](docs/limitations.md#word-97-2003-doc-and-powerpoint-97-2003-ppt).
