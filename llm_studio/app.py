"""app.py — LocalLLM Studio 진입점.

하는 일:
1. 데이터 폴더(ProgramData 우선)와 설정을 준비한다.
2. FastAPI 웹서버(UI + API)를 실행하고 브라우저를 자동으로 연다.

**LLM 연결은 앱 시작과 분리돼 있다.** 앱은 기본적으로 아무 로컬 모델도 서빙하지 않은
'유휴' 상태로 뜨고, 사용자가 UI에서 GGUF 모델과 옵션을 골라 [서빙 시작]을 누르거나
외부 API를 선택해 쓴다 (마지막 설정은 config.json에서 그대로 불러와 미리 채워진다).

자동으로 서빙을 시작하는 경우는 둘뿐이다:
    - config의 autostart_local=True (UI 체크박스 '앱 시작 시 자동 서빙')
    - --llama-url 로 이번 실행에 붙을 외부 서버를 명시했을 때
어느 쪽이든 시작에 실패해도 앱은 유휴 상태로 계속 뜬다 (목 모드로 떨어지지 않는다).

로컬 서버를 얻는 방식은 두 가지다 — 이 앱이 직접 띄우거나(managed),
serve_llm.py 등으로 이미 떠 있는 서버에 붙거나(external). 후자는 LLM 서버와
인터페이스(UI)를 따로 실행해 두고 연결하는 구성으로, 여러 클라이언트가 한 서버를
공유하고 앱을 껐다 켜도 모델이 유지된다.

UI 포트(기본 8080)가 이미 쓰이고 있으면 **다음 빈 포트로 옮겨서** 뜬다. 8080은 Windows에서
Docker/Jenkins/Tomcat/사내 에이전트 등과 자주 겹치는데, 예전에는 uvicorn이 bind 단계에서
죽으면서 ExceptionGroup 스택만 남아 원인을 알아보기 어려웠다. 옮기지 않고 오류로 끝내려면
--strict-port.

사용:
    python app.py                    # 기본 (UI 포트 8080, 유휴 상태로 시작 — UI에서 모델 선택)
    python app.py --host 0.0.0.0     # 같은 망의 다른 PC에서 접속 허용
    python app.py --mock             # 모델 없이 UI 개발/시험 (목 응답)
    python app.py --no-browser
    python app.py --llama-url http://127.0.0.1:8000   # 시작 시 따로 띄운 서버에 붙기
PyInstaller로 묶으면 이 파일이 exe의 진입점이 된다.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from types import SimpleNamespace

from server.config import load_config, mcp_config_path, resolve_data_dir
from server.conversations import ConversationStore
from server.files import UploadStore
from server.llama_proc import LlamaServer, LlamaServerError
from server.mcp_client import MCPManager
from server.memory import MemoryStore


def build_state(args) -> SimpleNamespace:
    data_dir = resolve_data_dir(args.data_dir)
    config = load_config(data_dir)
    print(f"[정보] 데이터 폴더: {data_dir}")

    # --llama-url을 주면 이번 실행에 한해 external 모드로 강제한다 (config에는 저장하지
    # 않는다 — CLI로 넘긴 일회성 지정이 파일 설정을 영구히 덮어쓰지 않도록).
    if args.llama_url:
        config["llama_external_url"] = args.llama_url

    state = SimpleNamespace(
        data_dir=data_dir,
        config=config,
        llama=LlamaServer(data_dir),
        mcp=MCPManager(mcp_config_path(data_dir)),
        store=ConversationStore(data_dir),
        memory=MemoryStore(data_dir),
        uploads=UploadStore(data_dir),
        mock=False,
        mock_reason=None,
    )

    if args.mock:
        state.mock = True
        state.mock_reason = "--mock 옵션으로 실행됨"
        return state

    # 기본은 유휴(모델 미서빙). 아래 조건일 때만 시작하면서 자동으로 서빙을 건다.
    #   - --llama-url: 이번 실행에 한해 외부 서버에 붙으라는 명시적 지시
    #   - autostart_local: 사용자가 UI에서 '앱 시작 시 자동 서빙'을 켜둠
    # 자동 시작이 실패해도 목 모드로 떨어지지 않는다 — 유휴로 두고 UI에서 다시 시작하게 한다.
    should_autostart = bool(args.llama_url) or config.get("autostart_local", False)
    if should_autostart:
        try:
            state.llama.start(config)
        except LlamaServerError as e:
            print(f"[주의] 자동 서빙 시작 실패, 유휴 상태로 시작합니다: {e}")
    else:
        print("[정보] 유휴 상태로 시작합니다. UI에서 모델을 골라 서빙을 시작하세요.")
    return state


PORT_SCAN_LIMIT = 20  # 요청한 포트부터 몇 개까지 훑어볼지


def _port_available(host: str, port: int) -> bool:
    """그 주소에 실제로 bind해 보고 되돌린다. 되면 True."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # SO_REUSEADDR을 일부러 켜지 않는다 — Windows에서는 이 옵션이 '이미 쓰는 포트를
        # 빼앗기'를 허용해서, 켜면 실제로는 물려 있는 포트를 비어 있다고 오판한다.
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _resolve_port(host: str, port: int, strict: bool) -> int:
    """쓸 수 있는 UI 포트를 정한다. 요청한 포트가 물려 있으면 다음 빈 포트로 옮긴다.

    8080은 Windows에서 가장 많이 겹치는 포트다(Docker/Jenkins/Tomcat/사내 에이전트…).
    이 확인이 없으면 uvicorn이 bind 단계에서 ExceptionGroup 스택을 뱉고 죽는데, 그 와중에
    이미 떠 있는 MCP 자식 프로세스 로그까지 섞여서 원인을 알아보기가 매우 어렵다.
    그래서 **아무것도 띄우기 전에** 여기서 먼저 확인하고 사람이 읽을 수 있게 알린다.

    (미세한 경합은 있다 — 확인과 uvicorn의 실제 bind 사이에 누가 가져갈 수 있다.
     그건 어차피 uvicorn이 오류를 내므로 여기서는 흔한 경우만 잡는다.)
    """
    if _port_available(host, port):
        return port

    hint = (
        f"       무엇이 쓰는지 확인:  netstat -ano | findstr :{port}\n"
        "       아무것도 안 나오면 Windows가 예약한 대역일 수 있습니다:\n"
        "         netsh int ipv4 show excludedportrange protocol=tcp"
    )
    if strict:
        sys.exit(
            f"[오류] 포트 {port}이(가) 이미 사용 중입니다 (--strict-port).\n{hint}\n"
            f"       다른 포트로 실행하려면: --port <번호>"
        )
    for cand in range(port + 1, port + PORT_SCAN_LIMIT + 1):
        if _port_available(host, cand):
            print(
                f"[주의] 포트 {port}이(가) 이미 사용 중이라 {cand}로 옮겨서 실행합니다.\n"
                f"{hint}\n"
                f"       계속 이 포트를 쓰려면 run_app.bat에 --port {cand} 를 넣으세요."
            )
            return cand
    sys.exit(
        f"[오류] {port}~{port + PORT_SCAN_LIMIT} 사이에 쓸 수 있는 포트가 없습니다.\n{hint}\n"
        f"       --port <번호>로 다른 대역을 지정하세요."
    )


def _start_key_listener(url: str) -> None:
    """콘솔에서 'o'=브라우저 다시 열기, 'q'=종료 (n8n 스타일 편의 기능).

    서버 수명은 브라우저와 분리돼 있어(탭을 닫아도 서버는 유지), 실수로 창을 닫았을 때
    URL을 다시 칠 필요 없이 콘솔에서 'o' 한 번으로 재접속할 수 있게 한다. 'q'는 Ctrl+C와
    같은 우아한 종료 경로를 탄다.

    데몬 스레드에서 msvcrt.kbhit()로 폴링한다. 콘솔이 없거나(창 없는 exe, stdin
    리다이렉트) 다른 OS면 조용히 아무 것도 하지 않는다 — 우아한 저하.
    Ctrl+C가 키 입력('\\x03')으로 넘어오면 메인 스레드에 인터럽트를 전달해
    기존 종료 경로(Ctrl+C)를 깨지 않는다.
    """
    try:
        import msvcrt  # Windows 전용
    except ImportError:
        return  # 다른 OS에서는 이 편의 기능을 건너뛴다

    # stdin이 실제 콘솔에 붙어 있지 않으면(리다이렉트/창 없는 exe) 감시하지 않는다
    if not sys.stdin or not sys.stdin.isatty():
        return

    def _loop() -> None:
        import _thread
        while True:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\x03":  # Ctrl+C: getwch가 가로채면 SIGINT가 안 뜨므로 직접 전달
                        _thread.interrupt_main()
                        return
                    if ch in ("o", "O"):
                        webbrowser.open(url)
                        print(f"[정보] 브라우저를 다시 엽니다: {url}")
                    elif ch in ("q", "Q"):
                        # Ctrl+C와 같은 우아한 종료 경로를 탄다 (server.run이 받아
                        # lifespan shutdown → llama.stop()까지 돈다). external 모드면
                        # 따로 띄운 LLM 서버는 유지된다.
                        print("[정보] 종료합니다...")
                        _thread.interrupt_main()
                        return
                time.sleep(0.1)
            except Exception:
                return  # 콘솔이 사라지는 등 어떤 이유로든 실패하면 조용히 감시를 접는다

    threading.Thread(target=_loop, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="LocalLLM Studio — 로컬 LLM 채팅 스튜디오")
    parser.add_argument("--host", default="127.0.0.1",
                        help="UI 바인딩 주소. 다른 PC에서 접속하려면 0.0.0.0 (기본 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="UI 포트 (기본 8080). 이미 쓰이고 있으면 다음 빈 포트로 옮긴다")
    parser.add_argument("--strict-port", action="store_true",
                        help="포트가 물려 있어도 다른 포트로 옮기지 않고 오류로 끝낸다")
    parser.add_argument("--data-dir", default=None, help="데이터 폴더 직접 지정")
    parser.add_argument("--mock", action="store_true", help="모델 없이 UI만 실행")
    parser.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 끄기")
    parser.add_argument("--llama-url", default=None,
                        help="이미 떠 있는 llama-server에 붙는다 (예: serve_llm.py로 띄운 "
                             "http://127.0.0.1:8000). 지정하면 자체 llama-server를 띄우지 않고 "
                             "그 서버를 공유하며, 앱을 껐다 켜도 그 서버는 유지된다")
    args = parser.parse_args()

    # 포트 확인은 **아무것도 띄우기 전에** 한다 — 여기서 물러설 거면 llama-server나
    # MCP 자식 프로세스를 괜히 띄웠다 정리하는 일이 없도록.
    args.port = _resolve_port(args.host, args.port, args.strict_port)

    state = build_state(args)

    from server.main import create_app
    app = create_app(state)

    open_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{open_host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    # 콘솔에서 'o'로 브라우저 재열기 (탭을 닫아도 서버는 살아 있으므로 재접속용)
    _start_key_listener(url)

    print(f"[정보] 웹 UI: {url}")
    print("[정보] 콘솔에서 'o'=브라우저 다시 열기, 'q'=종료. (Ctrl+C 또는 페이지의 [호스팅 종료] 버튼도 종료)")
    import uvicorn
    # Server를 직접 만들어 참조를 state에 둔다 — /api/shutdown이 should_exit로
    # 우아하게 내릴 수 있게. (uvicorn.run()은 핸들을 돌려주지 않아 종료를 못 건다.)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    state.server = server
    try:
        server.run()
    finally:
        state.llama.stop()


if __name__ == "__main__":
    main()
