from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from tools.conversion import convert_upload


class ConvertDocumentTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        file = tool_parameters["file"]
        # The format is detected from the content; the name is only the `source` of each document.
        name = file.filename or f"document{file.extension or ''}"
        converted = convert_upload(file.blob, name)

        yield self.create_text_message(converted.text)
        yield self.create_variable_message("documents", converted.documents)
        yield self.create_variable_message("warnings", converted.warnings)
