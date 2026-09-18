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
from pathlib import Path

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


def get_obj(name: str, default=None):
    """dict·list 같은 **문자열이 아닌** 설정값. 환경변수로는 못 주므로 파일만 본다.

    get()은 환경변수를 먼저 보고 str()로 바꾸기 때문에 RAG_DOC_ROLES 같은 dict에는
    쓸 수 없다. 형식이 어긋나면(문자열을 적었다든가) default로 물러선다 — 설정 하나
    때문에 서버가 죽으면 안 된다.
    """
    if _local is None:
        return default
    v = getattr(_local, name, None)
    return default if v is None else v


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


# ─────────────────── 임베딩 서버 실행 경로 (run_embed_server.bat 전용) ───────────────────
# bat이 파이썬을 한 번 불러 경로를 받아 가는 자리다. 사내 경로를 bat에 직접 박으면
# **공개 저장소에 들어가고**, 저장소를 다시 받을 때마다 고친 게 날아간다 — VLM 주소를
# local_settings.py에 두는 것과 같은 이유다.
#
# 포트는 RAG_EMBED_URL에서 뽑는다. 서버를 8001에 띄웠는데 검색이 8002를 보면 오류 없이
# **조용히 키워드 전용으로 저하**해 원인을 찾기 어렵다 — 한 곳에서 파생시켜 어긋날 수 없게 한다.

_GUESS_SUBDIRS = ("", "models", "llama", "llama.cpp", "embedding", "embed")


def _guess(roots, patterns) -> str:
    """흔한 자리에서만 찾는다. 드라이브 전체 스캔은 느려서 하지 않는다."""
    for root in roots:
        for sub in _GUESS_SUBDIRS:
            d = (root / sub) if sub else root
            for pat in patterns:
                try:
                    hit = sorted(d.glob(pat))
                except OSError:
                    continue
                if hit:
                    return str(hit[0])
    return ""


def embed_launch() -> list[str]:
    """bat이 `set` 으로 그대로 실행할 수 있는 줄들을 만든다.

    값이 비면 bat이 안내를 띄우고 멈춘다(엉뚱한 명령을 실행하지 않는다).
    """
    here = Path(__file__).resolve().parent
    roots = [here.parent, here.parent.parent, Path("C:/"), Path.home()]

    gguf = get("RAG_EMBED_GGUF", "")
    if not gguf:
        # 임베딩 모델을 먼저 찾고(이름에 embed/bge/e5), 없으면 아무 gguf나 집지 않는다 —
        # 채팅 모델을 임베딩 서버로 띄우면 차원이 안 맞아 인덱스가 통째로 어긋난다.
        gguf = _guess(roots, ("*embed*.gguf", "*bge*.gguf", "*e5*.gguf"))
    exe = get("RAG_LLAMA_SERVER", "")
    if not exe:
        exe = _guess(roots, ("llama-server.exe", "server.exe"))

    url = get("RAG_EMBED_URL", "http://127.0.0.1:8001/v1")
    port = "8001"
    for part in url.split("/"):
        if ":" in part and part.rsplit(":", 1)[-1].isdigit():
            port = part.rsplit(":", 1)[-1]
            break

    note = ""
    if not exe:
        note = "llama-server.exe not found near the repo, C:\\ or your home folder."
    elif not gguf:
        note = "No *embed*.gguf found. A chat model must NOT be used here."
    return [
        f'"EMBED_GGUF={gguf}"',
        f'"EMBED_EXE={exe}"',
        f'"EMBED_PORT={port}"',
        f'"EMBED_NOTE={note}"',
        # 마지막 표식 — bat이 '파이썬이 아예 못 돌았다'와 '경로를 못 찾았다'를 구분한다.
        # 없으면 파이썬이 없거나 settings.py를 못 읽은 것인데, 그 둘은 고치는 법이 다르다.
        '"EMBED_OK=1"',
    ]


if __name__ == "__main__":
    # run_embed_server.bat 이 부르는 유일한 진입점.
    if len(sys.argv) > 1 and sys.argv[1] == "embed-launch":
        print("\n".join(embed_launch()))
    else:
        print(status_line())
