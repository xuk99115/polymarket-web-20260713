import aiohttp
import json
import re
import logging
from typing import Optional, Dict, Any
from ..core.config import Config

logger = logging.getLogger("ai_decision")

SYSTEM_PROMPT = """你是一个专注于 Polymarket 二元预测市场的交易决策助手。

你的任务是根据市场问题、到期时间、最新成交价、可成交参考价和盘口结构，判断是否值得买入某一边。

硬性要求：
1. 只能在存在明确方向优势时返回 BUY，否则必须返回 SKIP。
2. 不要被远离最新成交价的极端挂单误导；如果提示里说明某些挂单应忽略，就按“无效流动性”处理。
3. 如果市场接近 50/50、信息不足、到期太近、流动性差、价差偏大，默认返回 SKIP。
4. 对 BTC 15m 这类超短周期市场，只有在“价格方向 + 定价偏差 + 可成交成本”三者同时支持时才返回 BUY。
5. reason 必须简短直接，只写最关键的 1 个原因，不要复述整段上下文。

请严格按照以下 JSON 格式返回，不要输出任何其他文字：
{
  "action": "BUY" | "SKIP",
  "outcome_index": 0 | 1 | null,
  "outcome_label": "YES/NO 等标签，无法确定时可为 null",
  "confidence": 0.0~1.0,
  "reason": "一句话说明理由（50字以内）"
}
"""


def _strip_think_block(text: str) -> str:
    """去掉推理模型可能输出的 <think>...</think> 块,带嵌套 fallback。

    未闭合的 `` 取后半段（答案在推理后）。
    """
    for _ in range(5):
        new_text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        if new_text == text:
            break
        text = new_text
    # 未闭合的 ``: 取之后的部分（答案通常跟在推理后）
    if '<think>' in text:
        text = text.split('<think>', 1)[1]
    return text.strip()


def _strip_markdown_fence(text: str) -> str:
    """去掉 ```json ... ``` 这种 markdown 代码块。"""
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, flags=re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def _find_balanced_json(text: str) -> Optional[str]:
    """括号配对提取第一个完整 { ... } JSON 对象,容忍嵌套。"""
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\' and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_json(text: str) -> Optional[str]:
    """从 AI 响应中提取 JSON 对象,容忍:
    - <think>...</think> 推理块（带嵌套）
    - ```json ... ``` markdown 围栏
    - 文本前后有其他内容（解释/代码块外溢）
    - JSON 内的嵌套 { }
    """
    if not text:
        return None
    text = _strip_think_block(text)
    text = _strip_markdown_fence(text)
    try:
        json.loads(text)
        return text
    except Exception:
        pass
    candidate = _find_balanced_json(text)
    if candidate:
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass
    # 备选: 正则 (旧逻辑, 处理无嵌套的简单情况)
    match = re.search(r'\{[^{}]*(?:"action"|"prediction")[^{}]*\}', text, re.DOTALL)
    if match:
        return match.group(0)
    return None


class AIProvider:
    """单个 AI 提供商的配置和调用，支持多模型自动切换"""

    def __init__(self, prefix: str, name: str):
        """prefix: 'AI' (主) 或 'AI_BACKUP' (备用)"""
        self.name = name
        self.enabled = Config.get_bool(f"{prefix}_ENABLED", "true")
        self.base_url = Config.get(f"{prefix}_BASE_URL", "https://api.openai.com/v1")
        if self.base_url:
            self.base_url = self.base_url.rstrip("/")
        self.api_key = Config.get(f"{prefix}_API_KEY", "")
        models_raw = Config.get(f"{prefix}_MODELS", Config.get(f"{prefix}_MODEL", "gpt-4o-mini"))
        self.models = [m.strip() for m in models_raw.split(",") if m.strip()]
        self.temperature = Config.get_float(f"{prefix}_TEMPERATURE", "0.1")
        self.max_tokens = Config.get_int(f"{prefix}_MAX_TOKENS", "300")

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.api_key) and len(self.models) > 0

    async def call(self, prompt: str, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """依次尝试本提供商下的各模型，返回第一个成功的结果或 None"""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for model in self.models:
            logger.info(f"[{self.name}] 尝试模型: {model}")
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }

            try:
                async with session.post(
                    url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(f"[{self.name}/{model}] API 返回 {resp.status}: {error_text[:200]}")
                        continue

                    data = await resp.json()
                    raw_content = data["choices"][0]["message"]["content"]
                    logger.debug(f"[{self.name}/{model}] AI 原始返回: {raw_content[:300]}")

                    json_str = _extract_json(raw_content)
                    if not json_str:
                        logger.warning(f"[{self.name}/{model}] 无法从 AI 响应中提取 JSON: {raw_content[:200]}")
                        continue

                    result = json.loads(json_str)

                    action = str(result.get("action", "")).upper()
                    if action not in ("BUY", "SKIP"):
                        prediction = str(result.get("prediction", "SKIP")).upper()
                        if prediction in ("UP", "YES"):
                            action = "BUY"
                            result.setdefault("outcome_index", 0)
                        elif prediction in ("DOWN", "NO"):
                            action = "BUY"
                            result.setdefault("outcome_index", 1)
                        else:
                            action = "SKIP"
                    result["action"] = action

                    outcome_index = result.get("outcome_index")
                    try:
                        result["outcome_index"] = int(outcome_index) if outcome_index is not None else None
                    except Exception:
                        result["outcome_index"] = None

                    outcome_label = result.get("outcome_label")
                    result["outcome_label"] = str(outcome_label).upper() if outcome_label not in (None, "") else None
                    result.setdefault("confidence", 0.5)
                    result.setdefault("reason", "AI 未提供理由")

                    logger.info(
                        "[%s/%s] AI 决策: %s outcome=%s | 信心: %.0f%% | %s",
                        self.name, model, result["action"],
                        result.get("outcome_index"),
                        float(result["confidence"]) * 100,
                        result["reason"],
                    )
                    return result

            except Exception as e:
                logger.warning(f"[{self.name}/{model}] AI 调用异常: {type(e).__name__}: {e}")
                continue

        logger.warning(f"[{self.name}] 所有模型均失败: {self.models}")
        return None


class AIDecisionEngine:
    """封装 AI 决策逻辑，支持主备多提供商自动切换"""

    def __init__(self):
        self.enabled = Config.get_bool("AI_ENABLED", "true")
        self.primary = AIProvider("AI", "主")
        self.backup = AIProvider("AI_BACKUP", "备")
        self._providers: list[AIProvider] = []
        if self.primary.ready:
            self._providers.append(self.primary)
        if self.backup.ready:
            self._providers.append(self.backup)

    async def get_prediction(self, prompt: str) -> Optional[Dict[str, Any]]:
        """依次尝试各 AI 提供商，主失败自动切备用，全部失败返回 None"""
        if not self.enabled:
            logger.warning("AI 未启用（AI_ENABLED=false）")
            return None

        if not self._providers:
            logger.warning("没有可用的 AI 提供商（无 API Key 或未启用）")
            return None

        async with aiohttp.ClientSession() as session:
            errors = []
            for provider in self._providers:
                result = await provider.call(prompt, session)
                if result is not None:
                    return result
                errors.append(provider.name)
                continue  # 自动切下一个

            logger.error(f"所有 AI 提供商({', '.join(errors)})均失败")
            return None
