"""Geracao de variantes de legenda via LLM (uma chamada por campanha).

Cada vendedora escreve a mensagem dela; aqui geramos N variantes que mantem o
tom e o significado, variando abertura, ordem das frases, sinonimos e emoji.
O scheduler distribui deterministicamente por destinatario (hash do telefone),
entao o mesmo cliente sempre recebe a mesma variante entre campanhas.

Regra de seguranca: numeros (precos, %, telefones) sao substituidos por
placeholders antes de ir pra LLM e restaurados depois — a LLM nunca "ve" um
valor pra alucinar em cima dele.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Modelos baratos atuais (confirmados nas docs oficiais em ago/2026).
# Override por env: LLM_MODEL.
DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-haiku-4-5-20251001",
}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")  # {nome}, spintax {a|b} ficam intactos
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

PROMPT_TEMPLATE = """Voce adapta mensagens de WhatsApp de vendedoras brasileiras para clientes que JA sao clientes da loja.

Mensagem original da vendedora:
---
{original}
---

Gere {n} variantes dessa mensagem. Regras obrigatorias:
- Portugues brasileiro informal, tom de vendedora de loja falando com cliente conhecido.
- Mantenha EXATAMENTE os mesmos significados, ofertas e informacoes; so mude a forma.
- Varie: abertura/saudacao, ordem das frases, sinonimos, pontuacao e emojis (pode trocar ou remover alguns).
- Cada variante deve ter tamanho parecido com a original (no maximo 30% maior).
- NAO altere, NAO remova e NAO mova os tokens especiais: qualquer coisa entre chaves (ex.: {{nome}}) e qualquer token NUMxx (ex.: NUM0, NUM12). Copie-os literalmente.
- NAO invente precos, condicoes, links ou promessas que nao estao na original.

Responda APENAS com um JSON array de strings, sem markdown, sem explicacao. Exemplo de formato: ["variante 1", "variante 2"]"""


@dataclass(frozen=True)
class LLMConfig:
    provider: str = ""  # "", "gemini", "openai", "anthropic"
    api_key: str = ""
    model: str = ""     # "" = default do provider
    variants: int = 8

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.api_key)

    @property
    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "")


class LLMError(RuntimeError):
    pass


def mask_numbers(text: str) -> tuple[str, List[str]]:
    """Troca numeros por NUMxx na ordem de aparecimento; retorna (mascarado, valores)."""
    values: List[str] = []

    def _sub(match: re.Match) -> str:
        values.append(match.group(0))
        return f"NUM{len(values) - 1}"

    return _NUMBER_RE.sub(_sub, text), values


def unmask_numbers(text: str, values: List[str]) -> str:
    """Restaura os NUMxx na ordem original."""
    def _sub(match: re.Match) -> str:
        idx = int(match.group(1))
        return values[idx] if idx < len(values) else match.group(0)

    return re.sub(r"NUM(\d+)", _sub, text)


def _extract_json_array(raw: str) -> List[str]:
    """Tolerante a LLM que devolve markdown ou texto em volta do JSON."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"Resposta da LLM sem JSON array: {raw[:120]!r}")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON invalido da LLM: {exc}") from exc
    if not isinstance(data, list):
        raise LLMError("Resposta da LLM nao e uma lista")
    return [str(item).strip() for item in data if str(item).strip()]


async def _post_json(url: str, headers: dict, payload: dict) -> dict:
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise LLMError(f"LLM HTTP {resp.status}: {body[:200]}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Resposta nao-JSON da LLM: {body[:200]}") from exc


async def _call_gemini(config: LLMConfig, prompt: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.resolved_model}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9},
    }
    data = await _post_json(url, {"x-goog-api-key": config.api_key}, payload)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Resposta Gemini inesperada: {str(data)[:200]}") from exc


async def _call_openai(config: LLMConfig, prompt: str) -> str:
    payload = {
        "model": config.resolved_model,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = await _post_json(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {config.api_key}"},
        payload,
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Resposta OpenAI inesperada: {str(data)[:200]}") from exc


async def _call_anthropic(config: LLMConfig, prompt: str) -> str:
    payload = {
        "model": config.resolved_model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = await _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
        },
        payload,
    )
    try:
        return "".join(
            block["text"] for block in data["content"] if block.get("type") == "text"
        )
    except (KeyError, TypeError) as exc:
        raise LLMError(f"Resposta Anthropic inesperada: {str(data)[:200]}") from exc


_PROVIDERS = {
    "gemini": _call_gemini,
    "openai": _call_openai,
    "anthropic": _call_anthropic,
}


async def generate_variants(original: str, config: LLMConfig) -> List[str]:
    """Gera variantes da legenda. Falha -> LLMError (chamador decide o fallback).

    Retorna lista possivelmente menor que config.variants se a LLM devolver
    menos; o chamador sempre inclui a original como variante 0.
    """
    if not config.enabled:
        return []
    caller = _PROVIDERS.get(config.provider)
    if caller is None:
        raise LLMError(f"LLM_PROVIDER desconhecido: {config.provider!r}")

    masked, values = mask_numbers(original)
    prompt = PROMPT_TEMPLATE.format(original=masked, n=config.variants)
    raw = await caller(config, prompt)

    variants: List[str] = []
    seen = set()
    for candidate in _extract_json_array(raw):
        restored = unmask_numbers(candidate, values).strip()
        if not restored or restored == original or restored in seen:
            continue
        # Seguranca: a LLM nao pode ter inventado numero novo nem perdido os de
        # verdade (eles sao placeholders na ida e restauracao na volta).
        if sorted(_NUMBER_RE.findall(restored)) != sorted(values):
            logger.warning("Variante descartada (numeros divergem da original): %r", restored[:80])
            continue
        # Placeholders {nome} / spintax precisam sobreviver.
        if sorted(_PLACEHOLDER_RE.findall(restored)) != sorted(_PLACEHOLDER_RE.findall(original)):
            logger.warning("Variante descartada (placeholders divergem): %r", restored[:80])
            continue
        seen.add(restored)
        variants.append(restored)

    if not variants:
        raise LLMError("LLM devolveu zero variantes utilizaveis")
    return variants
