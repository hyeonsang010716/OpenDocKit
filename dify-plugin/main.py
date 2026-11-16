# The Dify SDK is imported, and the plugin made, only when this file is run. Each conversion runs in a worker process
# (tools/conversion.py), and a worker imports this module again under another name. The SDK's import patches the standard
# library for gevent: in a worker that turned the thread that ends it once the plugin is gone into a greenlet that never
# ran, so a worker outlived a killed plugin, with nothing left to enforce its time limit
# (Pinned by: test_dify_plugin.py::TestDeclarations::test_the_sdk_is_imported_and_the_plugin_made_only_when_main_is_run).
if __name__ == "__main__":
    from dify_plugin import DifyPluginEnv, Plugin

    Plugin(DifyPluginEnv()).run()
