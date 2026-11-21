# opendockit-xlsx-core

The native sheet reader of [OpenDocKit](https://github.com/hyeonsang010716/OpenDocKit)'s XLSX converter. It reads the
rows of a sheet part into exactly the cells OpenDocKit's Python reader gives, and hands a part back to that reader
whenever it meets anything it does not take on. OpenDocKit works without it, more slowly.

Not meant to be used on its own: its interface follows OpenDocKit's internals and may change in any release.
