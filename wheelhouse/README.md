# wheelhouse — 폐쇄망 오프라인 설치용 휠 모음

**사내 PC 파이썬 3.10 / 64비트 윈도우(`cp310` + `win_amd64`) 기준**으로 받아 둔
`.whl` 모음이다. 인터넷도, 사내 PyPI 미러도 없이 여기 있는 파일만으로 설치된다.

이 저장소는 **git도 pip 다운로드도 안 되는 PC**로 옮기는 것을 전제한다. GitHub 웹에서
저장소를 ZIP으로 받으면 이 폴더가 통째로 따라오므로, USB로 옮겨 압축만 풀면 된다.

## 설치

```cmd
install_requirements.bat
```

`MIRROR_INDEX`를 비워 두면 `--no-index`로 **이 폴더에서만** 설치한다(네트워크 안 씀).
`pip` 명령이 없다고 하면 `python -m pip` 으로 부를 것 — 대개 PATH 문제다.
직접 하려면:

```cmd
pip install --no-index --find-links=wheelhouse -r requirements.txt
pip install --no-index --find-links=wheelhouse -r llm_studio\requirements.txt
pip install --no-index --find-links=wheelhouse -r spec-reader\requirements.txt
```

## 들어 있는 것

| | 무엇에 쓰나 |
|---|---|
| `fastmcp`, `mcp` | MCP 서버·클라이언트. **2.x면 서버가 import에서 죽는다** |
| `pywin32` | Office/Outlook/CATIA COM 전부 |
| `PyMuPDF`, `Pillow` | Vision RAG — PDF를 그림으로 렌더링 |
| `qdrant-client` | RAG 벡터 저장소(로컬 파일 모드) |
| `fastapi`, `uvicorn`, `openai` | llm_studio 앱 |
| `pypdf`, `python-docx`, `openpyxl` | PDF/Word/엑셀 읽기 |
| `requests` | spec-reader의 VLM 판독 |
| `numpy` | 선택 — sqlite 벡터 코사인 가속 |
| `pip`, `setuptools`, `wheel` | 파이썬에 pip이 안 딸려 왔을 때 부트스트랩용 |

나머지는 위 패키지들의 의존성이다(pydantic, httpx, starlette 등).

## 갱신 — 인터넷 되는 PC에서

```cmd
pip download -r requirements.txt -d wheelhouse ^
    --python-version 310 --only-binary=:all: --platform win_amd64
pip download -r llm_studio\requirements.txt -d wheelhouse ^
    --python-version 310 --only-binary=:all: --platform win_amd64
pip download -r spec-reader\requirements.txt -d wheelhouse ^
    --python-version 310 --only-binary=:all: --platform win_amd64
```

`--platform`/`--python-version`을 주면 **이 PC가 아니라 대상 PC용** 휠을 받는다
(리눅스에서 받아도 윈도우용이 떨어진다). `--only-binary=:all:`이 없으면 소스
배포(.tar.gz)가 섞여 폐쇄망에서 빌드하려다 실패한다.

받은 뒤 파일명을 확인할 것: 바이너리 휠은 `cp310-...-win_amd64.whl`,
순수 파이썬은 `py3-none-any.whl`이어야 한다. `cp311`이나 `manylinux`가 보이면
잘못 받은 것이다. 3.10 호환인지는 각 휠의 METADATA에서 `Requires-Python`을 읽어
`>=3.11` 같은 하한이 있는지 보면 한 번에 확인된다.

## 여기 없는 것

`.gguf` 모델과 `llama-server.exe`는 개당 100MB(GitHub 제한)를 넘어 git에 못 넣는다.
USB 등 별도 매체로 옮긴다 — 루트 `반입목록.md` 참고.
