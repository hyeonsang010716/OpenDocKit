from typing import Any

from dify_plugin import ToolProvider


class OpenDocKitProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        """Nothing to check: the conversion runs locally and needs no account or key."""
