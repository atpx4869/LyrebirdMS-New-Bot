import json
from pathlib import Path

from web_admin import _preflight_payload

if __name__ == '__main__':
    payload = _preflight_payload()
    Path('/tmp/lyrebird-preflight.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload.get('ok') else 1)
