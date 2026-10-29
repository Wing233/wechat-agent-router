"""SiliconFlow image generation client (Kolors and compatible models)."""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")


class ImageGenError(Exception):
    pass


class ImageGen:
    def __init__(self, base_url: str, api_key: str, model: str, *,
                 enabled: bool = True, size: str = "1024x1024", steps: int = 20,
                 guidance: float = 7.5, timeout: int = 120):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.enabled_flag = bool(enabled)
        self.size = size if SIZE_RE.match(str(size or "")) else "1024x1024"
        self.steps = int(steps)
        self.guidance = float(guidance)
        self.timeout = int(timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.enabled_flag and self.base_url and self.api_key and self.model)

    def _endpoint(self) -> str:
        if self.base_url.endswith("/images/generations"):
            return self.base_url
        return self.base_url + "/images/generations"

    def generate(self, prompt: str, size: str | None = None) -> tuple[bytes, str]:
        if not self.enabled:
            raise ImageGenError("未配置生图 API")
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ImageGenError("prompt 不能为空")
        if len(prompt) > 2000:
            prompt = prompt[:2000]
        image_size = size if SIZE_RE.match(str(size or "")) else self.size
        payload = {
            "model": self.model,
            "prompt": prompt,
            "image_size": image_size,
            "batch_size": 1,
            "num_inference_steps": self.steps,
            "guidance_scale": self.guidance,
        }
        request = urllib.request.Request(
            self._endpoint(), data=json.dumps(payload).encode("utf-8"), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ImageGenError(f"HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise ImageGenError(f"network: {exc.reason}") from None
        except Exception as exc:
            raise ImageGenError(str(exc)[:300]) from None

        items = data.get("images") or data.get("data") or []
        if not items:
            raise ImageGenError("接口没有返回图片")
        item = items[0] if isinstance(items[0], dict) else {}
        if item.get("b64_json"):
            try:
                raw = base64.b64decode(item["b64_json"])
            except Exception as exc:
                raise ImageGenError(f"图片解码失败：{exc}") from None
            return raw, "image/png"
        url = str(item.get("url") or "")
        if not url:
            raise ImageGenError("接口没有返回图片地址")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > 20 * 1024 * 1024:
                    raise ImageGenError("生成的图片超过 20MB")
                raw = response.read(20 * 1024 * 1024 + 1)
                if len(raw) > 20 * 1024 * 1024:
                    raise ImageGenError("生成的图片超过 20MB")
                return raw, response.headers.get("Content-Type", "") or ""
        except ImageGenError:
            raise
        except Exception as exc:
            raise ImageGenError(f"图片下载失败：{exc}") from None

    def generate_to_file(self, prompt: str, out_dir: str | Path, size: str | None = None) -> Path:
        data, content_type = self.generate(prompt, size)
        suffix = ".png"
        ctype = (content_type or "").split(";")[0].strip().lower()
        if "jpeg" in ctype or "jpg" in ctype:
            suffix = ".jpg"
        elif "webp" in ctype:
            suffix = ".webp"
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{suffix}"
        path.write_bytes(data)
        return path
