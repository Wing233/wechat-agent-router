import importlib.util

spec = importlib.util.spec_from_file_location("candidate", "plugin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Tools:
    def register(self, *args, **kwargs):
        assert args and isinstance(args[0], str)


class Ctx:
    tools = Tools()

    def http_request(self, *args, **kwargs):
        return 200, "{}"


module.apply(Ctx())
print("ok")
