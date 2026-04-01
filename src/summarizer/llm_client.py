"""
LLM client for the on-cluster vLLM instance serving Qwen3.5-397B.

Uses the OpenAI-compatible API exposed by vLLM at localhost:30000.
The openai Python library works out of the box — just point base_url
at the vLLM endpoint instead of api.openai.com.
"""

import logging
import time
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(
        self,
        base_url: str = "http://localhost:30000/v1",
        model: str = "Qwen/Qwen3.5-397B-A17B",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        api_key: str = "not-needed",
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = OpenAI(base_url=base_url, api_key=api_key)

    def generate_summary(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        """Send a prompt to the LLM and return the response with metadata.

        Returns:
            dict with keys: content, model, prompt_tokens, completion_tokens,
                            total_tokens, latency_seconds
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        t0 = time.time()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens or self.max_tokens,
                temperature=temperature if temperature is not None else self.temperature,
            )
        except Exception as e:
            logger.error("LLM request failed: %s", e)
            raise

        latency = time.time() - t0

        choice = response.choices[0]
        content = choice.message.content or ""
        # Qwen3.5 may return reasoning in a separate field; append if present
        reasoning = getattr(choice.message, "reasoning", None) or \
                    getattr(choice.message, "reasoning_content", None)
        if reasoning and not content:
            content = reasoning

        usage = response.usage

        result = {
            "content": content,
            "model": response.model,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "latency_seconds": round(latency, 2),
            "finish_reason": choice.finish_reason,
        }

        logger.info(
            "LLM response: %d prompt + %d completion tokens in %.1fs",
            result["prompt_tokens"] or 0,
            result["completion_tokens"] or 0,
            latency,
        )
        return result

    def health_check(self) -> bool:
        """Verify the vLLM endpoint is reachable and serving the expected model."""
        try:
            models = self.client.models.list()
            available = [m.id for m in models.data]
            if self.model in available:
                logger.info("LLM health check passed: %s available", self.model)
                return True
            else:
                logger.warning("Model %s not found. Available: %s", self.model, available)
                return False
        except Exception as e:
            logger.error("LLM health check failed: %s", e)
            return False
