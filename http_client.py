import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app_config import config

DEFAULT_TIMEOUT = config.get('request_timeout', 30)
RETRIES = config.get('request_retries', 2)

_session = requests.Session()
retry = Retry(
    total=RETRIES,
    connect=RETRIES,
    read=RETRIES,
    status=RETRIES,
    backoff_factor=1,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS']),
    raise_on_status=False,
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
_session.mount('http://', adapter)
_session.mount('https://', adapter)

# requests 默认就会使用 HTTP(S)_PROXY / NO_PROXY；这里保留 trust_env=True
_session.trust_env = True


def _merge_timeout(timeout: int | tuple[int, int] | None) -> int | tuple[int, int]:
    return timeout if timeout is not None else DEFAULT_TIMEOUT


def get(url: str, **kwargs: Any) -> requests.Response:
    kwargs.setdefault('timeout', _merge_timeout(kwargs.get('timeout')))
    return _session.get(url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    kwargs.setdefault('timeout', _merge_timeout(kwargs.get('timeout')))
    return _session.post(url, **kwargs)
