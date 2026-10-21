from __future__ import annotations

import base64
import json
import re

import requests

from .config import env
from .utils import CostTracker, log, parse_json_text, retry

URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOKENS_CEILING = 8000


class TruncatedError(ValueError):
    """The model ran out of output tokens before finishing (thinking models can burn the budget)."""


def image_part(jpeg: bytes) -> dict:
    b64 = base64.b64encode(jpeg).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def loose_parse(text: str) -> dict:
    """Parse JSON from messy model output: fences, prose around it, trailing commas, raw newlines."""
    try:
        return parse_json_text(text)
    except ValueError:
        pass
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    if start == -1:
        raise ValueError("No JSON object found")
    s = s[start:]
    end = s.rfind("}")
    if end != -1:
        s = s[: end + 1]
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing commas
    try:
        obj = json.loads(s, strict=False)  # strict=False allows raw newlines inside strings
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    try:
        from json_repair import repair_json  # optional: pip install json-repair

        obj = repair_json(s, return_objects=True)
        if isinstance(obj, dict) and obj:
            return obj
    except ImportError:
        pass
    raise ValueError("Could not parse JSON from model output")


class LLM:
    def __init__(self, cfg: dict, cost: CostTracker) -> None:
        self.key = env("OPENROUTER_API_KEY")
        self.models = cfg["models"]
        self.cost = cost

    def chat(self, model: str, messages: list[dict], max_tokens: int = 2000, temperature: float = 0.7) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "usage": {"include": True},
        }

        def call():
            r = requests.post(
                URL,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
            r.raise_for_status()
            return r.json()

        data = retry(call)
        self.cost.add("openrouter", float((data.get("usage") or {}).get("cost") or 0.0))
        choice = data["choices"][0]
        content = (choice.get("message") or {}).get("content") or ""
        if choice.get("finish_reason") == "length":
            raise TruncatedError(f"Output cut off at max_tokens={max_tokens} (got {len(content)} chars)")
        if not content.strip():
            raise ValueError("Empty response from model")
        return content

    def json(self, model: str, messages: list[dict], fallback: str | None = None, **kw) -> dict:
        """Ask for JSON; retry, grow the token budget if truncated, then fall back and repair."""
        last_err: Exception | None = None
        last_text = ""
        kw = dict(kw)
        for model_id in [model, model, fallback]:
            if not model_id:
                continue
            text = ""
            try:
                text = self.chat(model_id, messages, **kw)
                return loose_parse(text)
            except TruncatedError as e:
                last_err = e
                kw["max_tokens"] = min(int(kw.get("max_tokens", 2000)) * 2, MAX_TOKENS_CEILING)
                log.warning("LLM output truncated on %s: %s; retrying with max_tokens=%d", model_id, e, kw["max_tokens"])
            except (ValueError, KeyError, requests.RequestException) as e:
                last_err = e
                if text:
                    last_text = text
                    kw["temperature"] = min(float(kw.get("temperature", 0.7)), 0.4)
                    log.warning("LLM JSON call failed on %s: %s | head=%r tail=%r", model_id, e, text[:100], text[-100:])
                else:
                    log.warning("LLM JSON call failed on %s: %s", model_id, e)

        # Repair pass: text-only (no image resent), and only if some output exists to repair.
        if last_text:
            try:
                fixed = self.chat(
                    model,
                    [
                        {
                            "role": "user",
                            "content": "Convert the following into valid minified JSON only. Keep all content. "
                            "No markdown, no explanations, no text outside the JSON object.\n\n" + last_text,
                        }
                    ],
                    max_tokens=kw.get("max_tokens", 2000),
                    temperature=0.0,
                )
                return loose_parse(fixed)
            except (ValueError, KeyError, requests.RequestException) as e:
                last_err = e
                log.warning("LLM JSON repair failed on %s: %s", model, e)

        raise RuntimeError(f"LLM JSON call failed: {last_err}")
