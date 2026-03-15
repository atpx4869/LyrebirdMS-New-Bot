import re
import textwrap
from typing import Callable, List, Tuple

from app_config import config
from http_client import post
from logger.logger import write_log

ProgressCallback = Callable[[str], None] | None


def _parse_srt(content: str) -> List[dict]:
    blocks = re.split(r'\n\s*\n', content.strip(), flags=re.MULTILINE)
    entries = []
    for block in blocks:
        lines = [line.rstrip('\r') for line in block.splitlines() if line.strip() != '']
        if len(lines) < 3:
            continue
        idx = lines[0]
        ts = lines[1]
        text = '\n'.join(lines[2:])
        entries.append({'index': idx, 'timestamp': ts, 'text': text})
    return entries


def _render_srt(entries: List[dict]) -> str:
    return '\n\n'.join(f"{e['index']}\n{e['timestamp']}\n{e['text']}" for e in entries) + '\n'


def _chunk_entries(entries: List[dict], max_chars: int = 2400) -> List[List[dict]]:
    chunks: List[List[dict]] = []
    current: List[dict] = []
    current_size = 0
    for e in entries:
        addition = len(e['text']) + 40
        if current and current_size + addition > max_chars:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(e)
        current_size += addition
    if current:
        chunks.append(current)
    return chunks


def _build_prompt(chunk: List[dict], target_language: str) -> str:
    payload = '\n\n'.join(f"[{i}]\n{e['text']}" for i, e in enumerate(chunk, start=1))
    return textwrap.dedent(
        f"""
        你是专业字幕翻译助手。请把下面的字幕文本翻译成{target_language}。
        规则：
        1. 仅返回翻译结果，不要解释。
        2. 保持段落编号 [1] [2] ... 原样。
        3. 不要合并、拆分或遗漏任何编号。
        4. 保留专有名词，必要时可音译或意译。
        5. 输出格式必须与输入编号一一对应。

        {payload}
        """
    ).strip()


def _parse_numbered_translation(content: str, expected_count: int) -> List[str]:
    parts = re.split(r'\[(\d+)\]\s*\n?', content)
    result = {}
    for i in range(1, len(parts), 2):
        num = int(parts[i])
        text = parts[i + 1].strip()
        result[num] = text
    translated = [result.get(i, '').strip() for i in range(1, expected_count + 1)]
    if any(not item for item in translated):
        raise ValueError('AI 返回的编号翻译结果不完整')
    return translated


def _translate_chunk_openai(chunk: List[dict], progress_callback: ProgressCallback = None) -> List[str]:
    base_url = str(config.get('ai_base_url') or '').rstrip('/')
    api_key = str(config.get('ai_api_key') or '')
    model = str(config.get('ai_model') or 'gpt-4o-mini')
    if not base_url or not api_key:
        raise ValueError('OpenAI 兼容模式缺少 ai_base_url 或 ai_api_key')

    response = post(
        f'{base_url}/chat/completions',
        json={
            'model': model,
            'temperature': 0.2,
            'messages': [
                {'role': 'system', 'content': 'You are a subtitle translation engine. Return only numbered translations.'},
                {'role': 'user', 'content': _build_prompt(chunk, '简体中文')},
            ],
        },
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        timeout=int(config.get('request_timeout', 30)) * 4,
    )
    if response.status_code >= 300:
        raise ValueError(f'OpenAI 兼容接口调用失败，状态码: {response.status_code}, body={response.text[:300]}')
    content = (((response.json().get('choices') or [{}])[0].get('message') or {}).get('content') or '').strip()
    if progress_callback:
        progress_callback(f'OpenAI compatible provider chunk ok ({len(chunk)} lines)')
    return _parse_numbered_translation(content, len(chunk))


def _translate_chunk_gemini(chunk: List[dict], progress_callback: ProgressCallback = None) -> List[str]:
    api_key = str(config.get('gemini_api_key') or config.get('ai_api_key') or '')
    model = str(config.get('gemini_model') or config.get('ai_model') or 'gemini-2.5-flash')
    if not api_key:
        raise ValueError('Gemini 模式缺少 gemini_api_key')
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    response = post(
        url,
        json={
            'contents': [{'parts': [{'text': _build_prompt(chunk, '简体中文')}]}],
            'generationConfig': {'temperature': 0.2},
        },
        headers={'Content-Type': 'application/json'},
        timeout=int(config.get('request_timeout', 30)) * 4,
    )
    if response.status_code >= 300:
        raise ValueError(f'Gemini 接口调用失败，状态码: {response.status_code}, body={response.text[:300]}')
    candidates = response.json().get('candidates') or []
    text = ''
    if candidates:
        parts = (((candidates[0].get('content') or {}).get('parts')) or [])
        text = ''.join(part.get('text', '') for part in parts)
    if not text.strip():
        raise ValueError('Gemini 返回空内容')
    if progress_callback:
        progress_callback(f'Gemini provider chunk ok ({len(chunk)} lines)')
    return _parse_numbered_translation(text, len(chunk))


def translate_srt_file(input_file: str, output_file: str, progress_callback: ProgressCallback = None) -> Tuple[str, int]:
    provider = str(config.get('ai_provider') or 'gemini').strip().lower()
    entries = _parse_srt(open(input_file, 'r', encoding='utf-8', errors='ignore').read())
    if not entries:
        raise ValueError('字幕文件为空或不是有效的 SRT 格式')
    chunks = _chunk_entries(entries, max_chars=int(config.get('ai_chunk_chars', 2400)))
    translated_entries: List[dict] = []
    for idx, chunk in enumerate(chunks, start=1):
        if progress_callback:
            progress_callback(f'Translating: chunk {idx}/{len(chunks)}')
        if provider == 'openai_compatible':
            translated_lines = _translate_chunk_openai(chunk, progress_callback=progress_callback)
        elif provider == 'gemini_api':
            translated_lines = _translate_chunk_gemini(chunk, progress_callback=progress_callback)
        elif provider == 'gemini':
            # 兼容旧配置：优先走 gemini_srt_translator 风格外部流程的调用者；当前直接走 API
            translated_lines = _translate_chunk_gemini(chunk, progress_callback=progress_callback)
        else:
            raise ValueError(f'不支持的 AI provider: {provider}')
        for entry, translated in zip(chunk, translated_lines):
            translated_entries.append({**entry, 'text': translated})
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(_render_srt(translated_entries))
    write_log(f'字幕翻译完成 provider={provider} chunks={len(chunks)} lines={len(entries)}')
    return provider, len(entries)
