"""Generated HTTP connector plugin."""
import urllib.parse

URL = "https://httpbin.org/get"
METHOD = "GET"
TIMEOUT = 10
TOOL = {
    "name": "now",
    "description": "查询当前时间",
    "parameters": {
        "type": "object",
        "properties": {}
    }
}


def apply(ctx):
    def handler(args):
        url = URL
        for key, value in (args or {}).items():
            url = url.replace("{" + key + "}", urllib.parse.quote(str(value)))
        status, body = ctx.http_request(METHOD, url, timeout=TIMEOUT)
        return {"status": status, "body": body[:4000]}

    ctx.tools.register(TOOL["name"], TOOL["description"], TOOL["parameters"], handler,
                       aliases=(TOOL["name"],))
