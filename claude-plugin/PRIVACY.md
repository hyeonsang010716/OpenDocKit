# Privacy

The OpenDocKit plugin converts the document files Claude asks it to read, on your own computer.

- **No outside service.** The conversion makes no network requests. The only network access is `uv` downloading
  OpenDocKit and its dependencies from PyPI the first time the server starts (and Python, if the computer has no
  3.12 or newer). That download carries nothing about you or your files.
- **Nothing is shared.** No data is sent to the plugin author or to any third party. There is no analytics or tracking.
- **Nothing is kept.** The server keeps the results of its last few conversions in memory while the session lasts, so
  that reading a long document in parts does not convert it again; they are gone when the session ends. No file is
  written.
- **No personal data is collected.** A document may contain personal data; its text goes only into Claude Code's
  conversation, as the answer to the tool call.
- **Your files, your choice.** The server reads the files Claude names in its tool calls. To limit it to some folders,
  set `OPENDOCKIT_MCP_ROOTS` (see the README).

Questions: <https://github.com/hyeonsang010716/OpenDocKit/issues>.
