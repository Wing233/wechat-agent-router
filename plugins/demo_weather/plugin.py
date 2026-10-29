"""Generated HTTP connector plugin."""
import urllib.parse

URL = "https://api.open-meteo.com/v1/forecast?latitude=39.9&longitude=116.4&current=temperature_2m"
METHOD = "GET"
TIMEOUT = 10
TOOL = {
    "name": "now",
    "description": "查询北京当前温度",
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
