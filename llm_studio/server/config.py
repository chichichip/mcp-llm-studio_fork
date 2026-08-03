"""config.py

데이터 폴더 해석과 설정 파일 관리.

데이터 폴더는 인스톨러가 권한을 부여한 C:\\ProgramData\\LocalLLMStudio를 우선 사용하고,
쓰기가 불가능하면(개발 중, 미설치 환경) %LOCALAPPDATA%\\LocalLLMStudio로 내려간다.
어느 쪽이든 하위 구조는 동일하다:

    models/         GGUF 모델 파일을 넣는 곳
    conversations/  대화 기록 (JSON)
    uploads/        첨부 파일 원본 + 추출 텍스트
    logs/           llama-server 로그
    config.json     생성/서버 설정
    mcp_servers.json MCP 서버 연결 설정
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "LocalLLMStudio"
SUBDIRS = ("models", "conversations", "uploads", "logs")

DEFAULT_CONFIG = {
    # 생성 파라미터 (요청마다 반영, 재시작 불필요)
    "system_prompt": "당신은 사내망에서 동작하는 로컬 LLM 어시스턴트입니다. 한국어로 정확하고 간결하게 답하세요.",
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 64,
    "max_tokens": 4096,
    # 한 턴에서 모델이 도구를 부를 수 있는 최대 라운드 수 (무한 도구 루프 방지).
    # 초과하면 오류 이벤트로 중단한다. 도구를 많이 쓰는 작업이면 높인다.
    "max_tool_rounds": 8,
    # 서버 파라미터 (변경 시 llama-server 재시작 필요)
    "ctx": 32768,
    "kv_quant": False,
    "model_path": "",  # 비우면 models/ 폴더에서 가장 최근 .gguf를 자동 선택
    "model_alias": "gemma-12b-it-qat",
    "llama_port": 8000,
    # GPU 오프로드 레이어 수. 99=전 레이어(VRAM 넉넉한 서버용 기본값). VRAM이 작으면
    # 낮춰 일부만 올리고 나머지는 CPU로(예: 2GB dGPU면 16 안팎). 0이면 CPU 전용.
    "gpu_layers": 99,
    # 비우면 이 앱이 llama-server를 직접 띄운다(managed). 주소가 있으면 그 서버에
    # 붙기만 한다(external) — serve_llm.py로 LLM 서버를 따로 띄웠을 때 쓴다.
    # 예: "http://127.0.0.1:8000". app.py --llama-url 로도 설정된다.
    "llama_external_url": "",
    # 앱은 기본적으로 아무 로컬 모델도 서빙하지 않은 '유휴' 상태로 뜬다 (사용자가 UI에서
    # 모델을 골라 시작한다). 이 값을 True로 켜면 앱 시작 시 위 서버 설정으로 자동 서빙한다.
    # 자동 시작이 실패해도 앱은 유휴 상태로 계속 뜬다 (목 모드로 떨어지지 않음).
    "autostart_local": False,
    # 외부 LLM 연결 (OpenAI 호환 API — API 키로 접속)
    # 각 항목: {"name": "표시이름", "base_url": "https://.../v1", "api_key": "...", "model": "모델ID"}
    "providers": [],
    # 현재 선택된 모델: "local"(서빙 중인 로컬 LLM) 또는 providers의 name
    "active_provider": "local",
    # 장기 메모리 (대화 넘나드는 사실 기억 — memory.db). 전부 생성측이라 재시작 불필요.
    "memory_enabled": True,             # 끄면 회상 주입·자동요약을 건너뛴다 (채팅은 정상)
    "memory_recall_top_k": 5,           # 매 턴 주입할 관련 기억 최대 개수
    "memory_autosummary_enabled": True, # 대화에서 사실을 자동 추출해 저장할지
    "memory_autosummary_turn_interval": 25,   # 이 턴 수마다 자동요약 1회
    "memory_autosummary_char_threshold": 24000,  # 누적 이력이 이 문자수를 넘으면 자동요약
    # 계획-실행(작업 모드). 요청에 task_mode=True가 오면 다단계로 처리한다.
    "task_mode_enabled": True,   # 끄면 task_mode 요청도 일반 채팅으로 처리
    "task_max_steps": 10,        # 총 스텝 실행 상한 (무한 루프 방지)
    "task_max_replans": 2,       # 재계획 예산 (실패 시 남은 계획 재수립 횟수)
    # 위험 도구 승인 게이트: 모델이 confirm=true 인자(파괴적 동작의 실제 실행)로 도구를
    # 부르거나 approval_tools에 오른 도구를 부르면, 실행 전에 브라우저에 승인/거절
    # 버튼을 띄워 사용자의 결정을 기다린다. MCP 서버 쪽 confirm 게이트와 이중 안전장치 —
    # 모델이 사용자에게 묻지 않고 스스로 confirm=true를 넣는 사고를 막는다.
    "approval_enabled": True,
    "approval_timeout": 600,   # 승인 대기 제한(초). 0 이하 = 무제한 대기. 초과 시 실행하지 않음(거절과 동일)
    "approval_tools": [],      # 항상 승인이 필요한 도구 이름 목록 (send_email 또는 outlook__send_email)
    # ── 컨텍스트 관리 ──
    # 도구 결과는 하나가 최대 TOOL_RESULT_MAX(2만 자)까지 이력에 남고 매 턴 다시 보내진다.
    # 아무 것도 안 하면 read_excel_range 몇 번에 ctx가 차고, 넘치면 앞부분(=system
    # 프롬프트와 초반 지시)이 조용히 밀려나 응답 품질이 무너진다. 그래서 오래된 도구
    # 결과는 접어서 보낸다 — **접는 건 모델에 보내는 사본뿐이고 대화 기록 원문은 그대로다**.
    "context_aging_enabled": True,
    "context_tool_keep_turns": 2,     # 현재 턴 포함 최근 이 턴 수의 도구 결과는 원문 유지
    "context_tool_aged_chars": 200,   # 그보다 오래된 도구 결과는 이 길이로 접는다
    # ── 도구 스코프 ──
    # 서버를 여럿 붙이면 도구가 수십 개가 되고, 약한 로컬 모델은 그중에서 고르길 어려워한다.
    # 질문·첨부에서 어느 서버가 필요한지 확실할 때만 그 서버 도구만 노출한다.
    # 애매하면 전체를 보여준다 — 잘못 좁혀서 '못 한다'고 답하는 게 더 나쁘기 때문이다.
    "tool_scope_enabled": True,
    "tool_scope_keywords": {},        # 비우면 DEFAULT_TOOL_SCOPE_KEYWORDS 사용
    # ── 작업 대장(이 대화에서 읽고 고친 문서) ──
    # 매 턴 system에 주입해 (1) 같은 범위를 다시 읽는 낭비 (2) 저장하지 않은 변경을
    # 저장했다고 답하는 환각을 막는다.
    "worklog_enabled": True,
    "worklog_max_items": 12,
    # 첨부 처리. True면 Office 문서(docx/xlsx/pptx)를 서버가 바이트로 추출하지 않고
    # 저장 경로를 모델에 줘서 office MCP 도구(COM)가 읽게 한다. 사내 DRM처럼 파일이
    # 암호화돼 바이트 파싱은 암호문만 나오고 Word/Excel(COM)로 열어야만 복호화되는
    # 환경용 스위치. office_server가 연결돼 있어야 실제로 읽힌다. (업로드 시점 적용, 재시작 불필요)
    "attachment_com_office": False,
}

# 이 키들이 바뀌면 llama-server를 재시작해야 반영된다.
RESTART_KEYS = {"ctx", "kv_quant", "model_path", "model_alias", "llama_port",
                "llama_external_url", "gpu_layers"}

# --host 0.0.0.0으로 열었을 때 **원격 접속자**가 바꿀 수 있는 설정 키.
# 나머지(승인 게이트 on/off, 프로바이더·API 키, 서버 파라미터 등)는 이 PC에서만 바꾼다 —
# 원격에서 approval_enabled를 꺼 버리면 위험 도구 승인 게이트가 통째로 무력화된다.
# active_provider는 '지금 어느 모델로 답할지'라 원격 사용자에게도 필요해서 열어 둔다.
REMOTE_SETTABLE_KEYS = {"active_provider"}

# GET /api/settings가 API 키 대신 내려보내는 표식. 저장된 키가 있으면 이 값을 주고,
# PUT에서 이 값이 그대로 돌아오면 '바꾸지 않음'으로 보고 저장된 키를 유지한다.
# 키 입력란은 password 타입이라 사용자 눈에는 어차피 점으로 보인다 — UI는 그대로 두고
# 키 평문만 브라우저에 안 나가게 하는 장치다. (지우려면 입력란을 비우면 된다.)
API_KEY_PLACEHOLDER = "__saved__"


def redact_config(config: dict) -> dict:
    """설정 사본에서 API 키 평문을 표식으로 바꾼다 (브라우저로 내려보낼 용도)."""
    safe = dict(config)
    safe["providers"] = [
        {**p, "api_key": (API_KEY_PLACEHOLDER if p.get("api_key") else "")}
        for p in config.get("providers", [])
    ]
    return safe


def restore_api_keys(new_providers: list, old_providers: list) -> list:
    """표식이 그대로 돌아온 프로바이더는 저장돼 있던 키를 되살린다.

    이름으로 이전 항목을 찾는다 — 이름을 바꾸면서 키를 안 건드리면 짝을 못 찾아 키가
    비게 되므로, 그때는 키를 다시 입력해야 한다.
    """
    saved = {p.get("name"): p.get("api_key", "") for p in old_providers}
    out = []
    for p in new_providers:
        p = dict(p)
        if p.get("api_key") == API_KEY_PLACEHOLDER:
            p["api_key"] = saved.get(p.get("name"), "")
        out.append(p)
    return out

DEFAULT_MCP_CONFIG = {"mcpServers": {}}

# 서버 이름 → 그 서버가 필요하다는 신호가 되는 낱말. 질문에 이 낱말이 있으면 그 서버로
# 도구를 좁힌다(tool_scope). config의 tool_scope_keywords로 통째로 갈아끼울 수 있다.
#
# ⚠ **일반적인 낱말을 넣지 말 것.** '문서', '검색', '알려줘' 같은 걸 넣으면 거의 모든
# 질문이 그 서버에 걸려 다른 서버 도구가 사라진다 — 좁히지 않느니만 못하다. 특정 앱·형식을
# 콕 집는 낱말만 넣고, 애매하면 아무 것도 안 걸리게 두는 편이 안전하다(그러면 전체 노출).
DEFAULT_TOOL_SCOPE_KEYWORDS = {
    "office": ["엑셀", "excel", "xlsx", "xls", "워드", "word", "docx", "파워포인트",
               "powerpoint", "ppt", "pptx", "스프레드시트", "통합문서", "시트", "셀",
               "수식", "슬라이드"],
    "outlook": ["메일", "이메일", "e-mail", "mail", "outlook", "아웃룩", "받은편지함",
                "보낸편지함", "일정", "캘린더", "회의", "약속", "연락처", "주소록",
                "초안", "발송", "참조자"],
    "docs": ["규정", "지침", "매뉴얼", "사규", "사내문서", "사내 문서", "rag"],
    "pdf": ["pdf", "피디에프"],
    "catia": ["catia", "카티아", "스케치", "파트", "어셈블리", "모델링", "형상", "치수"],
    "ansys": ["ansys", "앤시스", "mapdl", "열해석", "해석", "메시", "시뮬레이션", "경계조건"],
}

# 같은 저장소의 mcp_server/ 서버들을 앱이 **자식 프로세스(stdio)로 직접 띄우기** 위한 목록.
# 이게 있으면 run_*.bat을 따로 실행하지 않아도 앱만 켜면 도구가 붙는다.
# COM 서버들이 요구하는 '사용자 로그인 세션' 제약도 자연히 지켜진다 — 앱이 그 세션에서
# 돌고 있고 자식 프로세스가 세션을 그대로 물려받기 때문이다.
#   (등록 이름, 스크립트 파일, 기본 활성 여부)
# 등록 이름이 도구 접두사가 된다(office__read_excel_range, docs__search_docs …).
# CATIA/ANSYS는 해당 제품이 깔린 PC에서만 의미가 있고, 켜 두면 도구 목록만 길어져
# 약한 로컬 모델의 도구 선택 정확도가 떨어진다 — 등록만 해 두고 기본은 꺼 둔다.
MCP_BUNDLED_SERVERS = (
    ("office", "office_server.py", True),
    ("outlook", "outlook_server.py", True),
    ("docs", "rag_server.py", True),
    ("pdf", "pdf_server.py", True),
    ("catia", "catia_server.py", False),
    ("ansys", "ansys_server.py", False),
)


def mcp_server_dir() -> Path | None:
    """같은 저장소의 mcp_server/ 폴더. 못 찾으면 None(자동 등록을 건너뛴다).

    소스 실행이면 저장소 루트 아래(../mcp_server), exe 설치본이면 앱 폴더 옆에
    같이 넣어 둔 경우를 본다. 둘 다 없으면 서버를 번들하지 않은 배포로 보고
    빈 설정을 쓴다(기존 동작 그대로).
    """
    for cand in (app_dir().parent / "mcp_server", app_dir() / "mcp_server"):
        if (cand / "office_server.py").is_file():
            return cand
    return None


def python_for_mcp() -> str:
    """MCP 서버를 띄울 파이썬 실행 파일 경로.

    루트 공용 venv > 지금 이 앱을 돌리는 파이썬 > PATH 순. bat들이 `..\\venv\\Scripts\\
    python.exe`를 먼저 보는 것과 같은 규약이다 — fastmcp/pywin32가 설치되는 곳이
    루트 requirements.txt를 받은 그 venv이기 때문이다.
    exe로 묶인 경우 sys.executable은 앱 자신이라 쓸 수 없다(자기 자신을 다시 띄운다).
    """
    root = app_dir().parent
    for cand in (root / "venv" / "Scripts" / "python.exe", root / "venv" / "bin" / "python"):
        if cand.is_file():
            return str(cand)
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("python3") or "python"


def default_mcp_config() -> dict:
    """번들 MCP 서버들을 stdio로 등록한 기본 설정을 만든다. 폴더가 없으면 빈 설정.

    - 스크립트는 **절대경로**로 적는다: 자식 프로세스의 작업 폴더는 앱 쪽이라
      상대경로면 못 찾는다. (파이썬이 스크립트가 있는 폴더를 sys.path에 넣어 주므로
      rag_core→office_server 같은 같은 폴더 import는 그대로 동작한다.)
    - `--transport stdio`를 명시한다: 기존에 bat으로 http를 쓰던 PC에 남아 있는
      OFFICE_MCP_TRANSPORT=http 같은 환경변수가 기본값을 뒤집어, 서버가 HTTP로 떠서
      stdio 응답을 영영 못 주는 사고를 막는다.
    """
    folder = mcp_server_dir()
    if folder is None:
        return {"mcpServers": {}}
    python = python_for_mcp()
    servers: dict = {}
    for name, script, enabled in MCP_BUNDLED_SERVERS:
        path = folder / script
        if not path.is_file():
            continue  # 저장소에서 뺀 서버는 등록하지 않는다
        spec: dict = {"command": python, "args": [str(path), "--transport", "stdio"]}
        if not enabled:
            spec["disabled"] = True
        servers[name] = spec
    return {"mcpServers": servers}


def app_dir() -> Path:
    """실행 파일(또는 소스 루트)이 있는 폴더. 동봉된 llama-server를 찾는 기준."""
    if getattr(sys, "frozen", False):  # PyInstaller로 묶인 경우
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    """웹 UI 정적 파일 폴더. PyInstaller 번들이면 _MEIPASS(_internal) 아래에 있다."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(app_dir()))) / "static"
    return app_dir() / "static"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_data_dir(override: str | None = None) -> Path:
    """쓰기 가능한 데이터 폴더를 정한다. 인자 > ProgramData > LocalAppData 순."""
    candidates = []
    if override:
        candidates.append(Path(override))
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        candidates.append(Path(program_data) / APP_NAME)
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / APP_NAME)
    candidates.append(Path.home() / f".{APP_NAME}")

    for cand in candidates:
        if _writable(cand):
            for sub in SUBDIRS:
                (cand / sub).mkdir(exist_ok=True)
            return cand
    raise RuntimeError("쓰기 가능한 데이터 폴더를 찾지 못했습니다.")


def load_config(data_dir: Path) -> dict:
    """config.json을 읽어 기본값 위에 덮어쓴다. 파일이 없으면 기본값으로 만든다."""
    path = data_dir / "config.json"
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            config.update(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[주의] config.json을 읽지 못해 기본값을 씁니다: {e}")
    else:
        save_config(data_dir, config)
    return config


def save_config(data_dir: Path, config: dict) -> None:
    path = data_dir / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def mcp_config_path(data_dir: Path) -> Path:
    """mcp_servers.json 경로. 처음 실행이거나 비어 있으면 번들 서버를 자동 등록한다.

    **이미 사용자가 등록해 둔 서버가 하나라도 있으면 절대 건드리지 않는다** — 자동
    생성은 '처음 실행'과 '서버가 하나도 없는 설정'에만 개입한다. 손으로 고치다 JSON이
    깨진 파일도 덮어쓰지 않는다(사용자가 쓴 내용을 날리지 않는 쪽으로 물러선다 —
    MCPManager가 읽기 실패를 경고로 알린다).
    HTTP 주소로 등록해 쓰던 기존 설정을 stdio로 바꾸는 건 자동으로 하지 않는다.
    설정 → MCP의 [번들 서버 자동 설정] 버튼으로 사용자가 확인하고 바꾼다.
    """
    path = data_dir / "mcp_servers.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return path
        if existing.get("mcpServers"):
            return path
    config = default_mcp_config()
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    count = len(config["mcpServers"])
    if count:
        print(f"[정보] MCP 서버 {count}개를 자동 등록했습니다 ({mcp_server_dir()}).")
    return path
