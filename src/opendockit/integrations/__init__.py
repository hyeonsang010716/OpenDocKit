"""Adapters that hand OpenDocKit's result to other frameworks. Each one needs its framework installed:

    pip install "opendockit[langchain]"     # opendockit.integrations.langchain: OpenDocKitLoader, OpenDocKitParser
    pip install "opendockit[llamaindex]"    # opendockit.integrations.llama_index: OpenDocKitReader
    pip install "opendockit[mcp]"           # the `opendockit-mcp` server (opendockit.mcp_server)

Nothing here is imported by `opendockit` itself, so the default install stays as small as it is.
"""
