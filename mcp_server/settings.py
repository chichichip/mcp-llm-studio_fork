"""settings.py — 사내 주소 같은 로컬 설정을 한 곳에서 읽는다.

**우선순위: CLI 인자 > 환경변수 > local_settings.py > 코드 기본값**

왜 필요한가:
    사내 VLM/임베딩 주소를 매번 `--vlm-url ...`로 넘기는 건 번거롭고, 무엇보다
    **llm_studio가 자동으로 띄우는 rag_server에는 인자를 못 준다** — 그러면 ask_page의
    VLM 재판독이 기본값(localhost)을 보게 돼 동작하지 않는다. 파일에 한 번 적어 두면
    CLI로 띄우든 앱이 띄우든 같은 주소를 본다.

왜 코드에 직접 안 박는가:
    이 저장소는 **공개**다. 사내 호스트·IP·계정은 커밋되면 안 된다
    (`install_requirements.bat`의 미러 주소, `spec-reader/config.py`와 같은 방침).
    그래서 `local_settings.py`는 `.gitignore`에 있고, 저장소에는 예시만 둔다.

쓰는 법:
    mcp_server\\local_settings.example.py 를 local_settings.py 로 복사해 값을 채운다.
    (spec-reader/config.py 를 이미 채워 두었다면 거기 URL/MODEL 을 그대로 옮기면 된다.)
"""

from __future__ import annotations

import os
import sys

# local_settings.py 는 이 파일과 같은 폴더에 있다. 파이썬이 **실행 스크립트의 폴더**를
# sys.path[0]에 넣으므로, 앱이 절대경로로 띄워 cwd가 다른 곳이어도 import된다
# (rag_core가 office_server를 import하는 것과 같은 원리).
try:
    import local_settings as _local  # type: ignore[import-not-found]

    LOCAL_ERROR = ""
except ImportError:
    _local = None
    LOCAL_ERROR = "없음"
except Exception as e:  # noqa: BLE001 — 문법 오류 등. 죽지 말고 알리기만.
    _local = None
    LOCAL_ERROR = f"불러오지 못함({type(e).__name__}: {e})"
    print(f"[주의] local_settings.py를 {LOCAL_ERROR} — 기본값으로 계속합니다.",
          file=sys.stderr)


def get(name: str, default: str = "") -> str:
    """설정값 하나. 환경변수 > local_settings.py > default."""
    v = os.getenv(name)
    if v:
        return v
    if _local is not None:
        v = getattr(_local, name, None)
        if v:
            return str(v)
    return default


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def get_float(name: str, default: float) -> float:
    try:
        return float(get(name, str(default)))
    except ValueError:
        return default


def source_of(name: str) -> str:
    """이 값이 어디서 왔는지 — 진단용(사내망에서 '왜 그 주소를 보나'를 알려면 필요)."""
    if os.getenv(name):
        return "환경변수"
    if _local is not None and getattr(_local, name, None):
        return "local_settings.py"
    return "기본값"


def status_line() -> str:
    path = getattr(_local, "__file__", "") if _local is not None else ""
    return f"local_settings.py: {path or LOCAL_ERROR or '없음'}"
