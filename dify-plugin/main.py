from dify_plugin import DifyPluginEnv, Plugin


# The plugin is made only when this file is run. Each conversion runs in a worker process (tools/conversion.py), and a
# worker imports this module again under another name: a plugin made at import time would start a second one there
# (Pinned by: test_dify_plugin.py::TestDeclarations::test_the_plugin_is_made_only_when_main_is_run).
if __name__ == "__main__":
    # Longer than the time limit of one conversion, so a file that runs over it ends with its own error.
    Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=180)).run()
