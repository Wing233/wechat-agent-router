"""Generated HTTP connector plugin."""
import urllib.parse

URL = "https://api.deepseek.com/user/balance"
METHOD = "GET"
TIMEOUT = 15
TOOL = {
    "name": "deepseek_balance",
    "description": "查询 DeepSeek 账户余额：返回 is_available 与各币种的 total_balance、granted_balance、topped_up_balance。",
    "parameters": {
        "properties": {},
        "type": "object"
    }
}


def apply(ctx):
    api_key = ctx.secret("llm_api_key")

    def handler(args):
        url = URL
        for key, value in (args or {}).items():
            url = url.replace("{" + key + "}", urllib.parse.quote(str(value)))
        status, body = ctx.http_request(
            METHOD, url, headers={"Authorization": "Bearer " + api_key}, timeout=TIMEOUT
        )
        return {"status": status, "body": body[:4000]}

    ctx.tools.register(TOOL["name"], TOOL["description"], TOOL["parameters"], handler,
                       aliases=(TOOL["name"],))
