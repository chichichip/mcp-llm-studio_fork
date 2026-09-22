"""standard_server.py

표준품 선정 **실행 시트(playbook)** 기반 선정 진행 MCP 서버.

왜 이 서버인가
--------------
선정 지침서를 RAG에 통째로 넣고 도구를 잔뜩 쥐여 주면, 모델이 매 턴 "지금 어떤 도구를
써야 하나 / 어떤 스펙 문서를 봐야 하나"를 다시 추론하다 헤맨다. 그래서 순서를 바꾼다.

    사내 지침서 → (사내 AI가 1회) → 선정 실행 시트(.md) → 이 서버가 단계별로 진행

시트를 `playbooks/` 폴더에 넣어 두면, 이 서버가 한 번에 **한 단계씩** 지시를 준다.
각 지시에는 그 단계에서 받을 입력, 쓸 도구, 볼 표, 열 엑셀, 기록할 항목, 그리고
**다음에 호출할 도구 한 줄**이 들어 있다. 모델은 고르지 않고 따라가기만 하면 된다.

- 분기("온도 -30~204 이내인가?")는 모델의 감이 아니라 서버가 **수집된 값으로 계산**한다.
- 필수 입력/기록이 비면 다음 단계로 **넘어가지 않는다**(확정 불가는 '미정'으로 명시).
- 끝나면 판단 근거가 붙은 **선정 결과서(.md)**가 파일로 남는다.

스펙 읽기는 여기서 하지 않는다. 표준품마다 자동화 도구가 있는 것도 아니고(특히
Dash No. 결정), 시트가 "이 도구로 이 문서를 찾아라"라고 지정만 하면 실제 읽기는
기존 서버들(docs__search_docs / office__* / pdf__read_pdf_text)이 한다 — 같은 일을
여기서 다시 구현하지 않는 건 의도적인 선택이다.

안전 등급 (outlook/office와 같은 3티어)
    🟢 읽기 — list_playbooks / show_playbook / lookup_table / current_step /
             selection_report / list_selections (시트와 세션을 바꾸지 않음)
    🟡 로컬 진행 — start_selection / submit_step / back_step. 세션 JSON과 결과서를
             이 서버의 데이터 폴더에만 쓴다. 사내 문서나 도면은 건드리지 않는다.
    🔴 파괴 — delete_selection. confirm=True 없이는 프리뷰만 돌려준다.

사용:
    python standard_server.py                      # MCP 서버 (stdio)
    python standard_server.py --transport http     # n8n 등 네트워크용, :8093
    python standard_server.py --list               # 시트 목록 (MCP 없이)
    python standard_server.py --lint               # 시트 전부 점검 — 시트를 새로 만들면 먼저 이것부터
    python standard_server.py --show 오링           # 시트 한 장 보기

llm_studio 장착 (코드 변경 불필요): 데이터 폴더의 mcp_servers.json에 등록만 하면
도구가 std__start_selection 등으로 모델에 노출된다.
    {"mcpServers": {"std": {"command": "python",
                            "args": ["C:\\\\경로\\\\mcp_server\\\\standard_server.py"]}}}

파싱·상태기계는 standard_core.py에 있고 여기는 MCP 도구 껍데기와 CLI만 둔다
(rag_core/rag_server와 같은 구성·서빙 분리).
"""

from __future__ import annotations

import argparse
import os
import sys
from functools import wraps

from fastmcp import FastMCP

import standard_core as core
from standard_core import StandardError

mcp = FastMCP(
    name="std",
    instructions=(
        "사내 표준품(O-ring, 볼트 등) 선정을 '선정 실행 시트'에 따라 단계별로 진행하는 "
        "서버입니다. 사용자가 표준품 선정을 요청하면 절대 혼자 판단하지 말고 이렇게 하세요:\n"
        "1) list_playbooks로 해당 표준품의 시트가 있는지 확인합니다.\n"
        "2) start_selection으로 선정을 시작합니다. 돌려주는 지시문 하나가 한 단계입니다.\n"
        "3) 지시문의 '수집할 입력'은 사용자에게 물어서 채웁니다. 추측해서 채우지 마세요.\n"
        "   지시문이 도구를 지정하면(docs__search_docs 등) 그 도구만 쓰고, 다른 도구를 "
        "   찾아 헤매지 마세요. 참고 표가 있으면 lookup_table로 조회합니다.\n"
        "4) 모은 값을 submit_step으로 제출하면 서버가 조건을 계산해 다음 단계를 줍니다. "
        "   '기계 판정 조건'은 당신이 판단하지 말고 값만 넘기세요.\n"
        "5) 확정할 수 없는 항목은 '미정'으로 제출하세요 — 결과서에 미해결로 남습니다.\n"
        "6) 마지막 단계까지 마치면 선정 결과서가 나옵니다. 그대로 사용자에게 보여 주세요.\n"
        "시트가 없는 표준품은 이 서버로 진행할 수 없습니다 — 사용자에게 지침서로 시트를 "
        "먼저 만들어 달라고 알리세요(playbooks/README.md)."
    ),
)


def standard_tool(fn):
    """예외를 안내 문자열로 바꾼다 — 도구는 항상 문자열을 돌려준다(우아한 저하)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StandardError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"작업에 실패했습니다: {type(e).__name__}: {e}"

    return wrapper


def _preview(action: str, details: str, retry: str) -> str:
    """🔴 도구가 confirm 없이 호출됐을 때 돌려줄 '실행 전 확인' 프리뷰.

    outlook/office/catia 서버와 같은 게이트다. 사용자가 내용을 확인한 뒤 같은 도구를
    confirm=True로 다시 부르면 그때 실행된다.
    """
    return (
        f"[확인 필요] {action}\n{details}\n\n"
        "이대로 진행하려면 같은 도구를 confirm=true 로 다시 호출하세요. "
        f"{retry}"
    )


# ════════════════════════ A. 🟢 시트 조회 (읽기 전용) ════════════════════════


@mcp.tool()
@standard_tool
def list_playbooks() -> str:
    """어떤 표준품의 선정 실행 시트가 있는지 목록을 봅니다. (🟢 읽기)

    표준품 선정 요청을 받으면 **가장 먼저** 호출하세요. 여기 없는 표준품은 이 서버로
    진행할 수 없습니다(사용자에게 시트를 먼저 만들어 달라고 알리세요).
    """
    files = core.playbook_files()
    if not files:
        return (
            f"선정 실행 시트가 없습니다. 시트(.md)를 {core.PLAYBOOK_DIR} 폴더에 넣으세요.\n"
            "만드는 법: 사내 AI에게 선정 지침서를 읽히고 실행 시트를 만들게 한 뒤 이 폴더에 "
            "저장합니다 (형식과 프롬프트는 playbooks/README.md)."
        )
    out = [f"선정 실행 시트 {len(files)}개 ({core.PLAYBOOK_DIR})", ""]
    for f in files:
        try:
            pb = core._read_playbook(f)
        except OSError as e:
            out.append(f"  · {f.stem} — ⚠ 읽기 실패: {e}")
            continue
        alias = f"  (별칭: {', '.join(pb.aliases())})" if pb.aliases() else ""
        out.append(f"  · {pb.pid} — {pb.title} / {len(pb.steps)}단계{alias}")
        if pb.meta.get("지침서"):
            out.append(f"      근거 지침서: {pb.meta['지침서']}")
    out += ["", "선정을 시작하려면 start_selection(playbook='<위 이름 중 하나>', request='<무엇에 쓸 부품인지>')"]
    return "\n".join(out)


@mcp.tool()
@standard_tool
def show_playbook(playbook: str, lint: bool = False) -> str:
    """선정 실행 시트 전문을 봅니다 — 전체 흐름을 미리 훑을 때만 쓰세요. (🟢 읽기)

    실제 선정은 이걸로 하지 말고 start_selection → submit_step으로 진행하세요.
    시트 전문을 보고 혼자 진행하면 단계를 건너뛰거나 판정을 틀리기 쉽습니다.

    Args:
        playbook: 시트 이름/제목/별칭 (예: 'rubber_oring', '오링').
        lint: True면 시트 본문 대신 기계 판독 가능 여부 점검 결과를 봅니다(시트 작성자용).
    """
    pb = core.load_playbook(playbook)
    if lint:
        return core.lint(pb)
    try:
        body = open(pb.path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as e:
        raise StandardError(f"시트 파일을 읽지 못했습니다: {e}") from e
    return f"[선정 실행 시트] {pb.title} ({pb.pid}) — {len(pb.steps)}단계\n파일: {pb.path}\n\n{body}"


@mcp.tool()
@standard_tool
def lookup_table(playbook: str, table: str = "", query: str = "") -> str:
    """시트에 실린 참조표(대체규격표, 치수표 등)를 조회합니다. (🟢 읽기)

    단계 지시문의 '참고 표'에 이름이 나오면 이 도구로 보세요. 시트 안에 답이 있는
    내용을 굳이 밖에서(RAG/PDF) 찾지 마세요.

    Args:
        playbook: 시트 이름/제목/별칭.
        table: 표 이름. 비우면 시트의 표 목록을 돌려줍니다.
        query: 찾을 조건(공백으로 여러 낱말). 비우면 표 전체를 보여 줍니다.
    """
    pb = core.load_playbook(playbook)
    tables = [(st, t) for st in pb.steps for t in st.tables]
    if not tables:
        return f"시트 '{pb.pid}'에는 표가 없습니다."
    if not core._nfc(table):
        out = [f"'{pb.title}' 시트의 표 {len(tables)}개", ""]
        out += [f"  · {t.name} (Step {st.no}, {len(t.rows)}행) — 열: {', '.join(t.headers)}"
                for st, t in tables]
        return "\n".join(out)

    key = str(core.CIStr(table)).casefold().replace(" ", "")
    hit = None
    for st, t in tables:
        name = str(core.CIStr(t.name)).casefold().replace(" ", "")
        if name == key or key in name:
            hit = (st, t)
            break
    if hit is None:
        return (f"표 '{table}'을(를) 찾지 못했습니다. 있는 표: "
                + ", ".join(t.name for _st, t in tables))
    st, t = hit
    rows = t.search(query)
    if not rows:
        return (f"[{t.name}] '{query}'에 맞는 행이 없습니다 ({len(t.rows)}행 중 0건).\n"
                "낱말을 줄여서 다시 조회하거나, query를 비워 표 전체를 보세요.")
    sub = core.Table(name=t.name, headers=t.headers, rows=rows)
    return (f"[{t.name}] (Step {st.no})"
            + (f" — '{query}' 검색 결과 {len(rows)}행" if query else f" — 전체 {len(rows)}행")
            + "\n\n" + sub.render())


# ════════════════════════ B. 🟡 선정 진행 (세션 파일만 씀) ════════════════════════


@mcp.tool()
@standard_tool
def start_selection(playbook: str, request: str = "") -> str:
    """표준품 선정을 시작합니다 — 시트의 1단계 지시를 돌려줍니다. (🟡 로컬)

    돌려주는 지시문 그대로 따라가세요. 필요한 입력은 사용자에게 묻고, 지정된 도구만
    쓰고, 모은 값을 submit_step으로 제출하면 다음 단계가 나옵니다.

    Args:
        playbook: 시트 이름/제목/별칭 (예: 'rubber_oring', '오링'). 모르면 list_playbooks.
        request: 무엇에 쓸 부품인지 한 줄 (결과서에 남습니다). 예: '연소기 케이싱 플랜지 실링용'.
    """
    pb = core.load_playbook(playbook)
    s = core.new_session(pb, request)
    warn = ""
    if all(not (st.fields or st.branch or st.records or st.checks or st.excel) for st in pb.steps):
        warn = ("ℹ 이 시트에는 기계 판독 블록이 없어 각 단계의 산문만 안내됩니다. "
                "판단 근거를 note에 꼭 남기세요.\n")
    return warn + core.render_step(pb, s)


@mcp.tool()
@standard_tool
def submit_step(session_id: str, values: str = "", note: str = "") -> str:
    """현재 단계에서 모은 값을 제출하고 다음 단계 지시를 받습니다. (🟡 로컬)

    서버가 값을 검증하고(타입·선택지·단위 자동 환산), 시트에 조건식이 있으면 그
    조건을 **직접 계산해** 분기를 정합니다. 조건 판정을 당신이 하지 마세요 — 값만
    정확히 넘기면 됩니다. 필수 항목이 비면 다음 단계로 넘어가지 않고 무엇이 없는지
    알려 줍니다.

    Args:
        session_id: start_selection이 돌려준 세션 id.
        values: 수집한 값. `이름=값; 이름=값` 형식(줄바꿈도 됨) 또는 JSON 객체.
            예: "작동유체=Air; 작동온도_max=260; 작동압력=150 psi"
            단위를 함께 적으면 시트가 선언한 단위로 환산해 기록합니다(psi→bar 등).
            확정할 수 없는 항목은 '미정'으로 넘기세요.
        note: 판단 사유·검증 결과·참고한 문서 등. 결과서에 그대로 남습니다.
            검증 체크리스트가 있는 단계에서는 필수입니다.
    """
    s = core.load_session(session_id)
    pb = core.load_playbook(s.playbook)
    return core.submit(pb, s, values, note)


@mcp.tool()
@standard_tool
def current_step(session_id: str) -> str:
    """지금 어느 단계인지, 무엇을 해야 하는지 다시 봅니다. (🟢 읽기)

    대화가 길어져 흐름을 놓쳤으면 추측하지 말고 이 도구로 현재 지시를 다시 받으세요.
    """
    s = core.load_session(session_id)
    pb = core.load_playbook(s.playbook)
    return core.render_step(pb, s)


@mcp.tool()
@standard_tool
def back_step(session_id: str) -> str:
    """직전 단계로 되돌립니다 — 그 시점의 값으로 복원됩니다. (🟡 로컬)

    잘못된 입력으로 분기가 틀렸거나, 최종 검증에서 앞 단계를 다시 해야 할 때 쓰세요.
    """
    s = core.load_session(session_id)
    pb = core.load_playbook(s.playbook)
    return core.back(pb, s)


@mcp.tool()
@standard_tool
def list_selections(only_active: bool = False) -> str:
    """진행 중이거나 끝난 선정 세션 목록을 봅니다. (🟢 읽기)

    Args:
        only_active: True면 아직 끝나지 않은 세션만 봅니다.
    """
    sessions = core.list_sessions()
    if only_active:
        sessions = [s for s in sessions if not s.done]
    if not sessions:
        return "선정 세션이 없습니다. start_selection으로 시작하세요."
    out = [f"선정 세션 {len(sessions)}개 (최근 순)", ""]
    for s in sessions[:30]:
        state = "완료" if s.done else f"진행 중 — Step {s.step}"
        out.append(f"  · {s.sid}  [{state}]  시트={s.playbook}  갱신={s.updated}")
        if s.request:
            out.append(f"      요청: {s.request}")
    return "\n".join(out)


@mcp.tool()
@standard_tool
def selection_report(session_id: str) -> str:
    """선정 결과서를 봅니다 — 입력 조건·단계별 판단 근거·선정 결과·미해결 항목. (🟢 읽기)

    진행 중인 세션도 그때까지의 내용으로 볼 수 있습니다. 사용자에게 결과를 보고할 때
    이 내용을 그대로 보여 주세요.
    """
    s = core.load_session(session_id)
    pb = core.load_playbook(s.playbook)
    loc = f"\n(파일: {s.report_path})\n" if s.report_path else "\n"
    return core.report_markdown(pb, s) + loc


# ════════════════════════ C. 🔴 삭제 (confirm 게이트) ════════════════════════


@mcp.tool()
@standard_tool
def delete_selection(session_id: str, confirm: bool = False) -> str:
    """🔴 선정 세션을 삭제합니다. (confirm=True 필요)

    confirm 없이 부르면 무엇을 지우는지 프리뷰만 돌려줍니다. 되돌릴 수 없습니다
    (이미 저장된 결과서 .md 파일은 남습니다).

    Args:
        session_id: 지울 세션 id.
        confirm: 실제로 지우려면 True. 없으면 프리뷰만.
    """
    s = core.load_session(session_id)
    if not confirm:
        state = "완료" if s.done else f"진행 중 (Step {s.step})"
        return _preview(
            "선정 세션 삭제",
            f"  세션: {s.sid}\n  시트: {s.playbook}\n  상태: {state}\n"
            f"  수집된 값 {len(s.values)}개 / 진행 기록 {len(s.log)}건이 사라집니다.",
            "(delete_selection ... confirm=true)",
        )
    return core.delete_session(session_id)


# ─────────────────────────────── CLI / 서버 기동 ───────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="표준품 선정 실행 시트 기반 진행 MCP 서버"
    )
    parser.add_argument("--list", action="store_true", help="시트 목록을 보고 종료")
    parser.add_argument("--show", metavar="NAME", help="시트 한 장을 보고 종료")
    parser.add_argument("--lint", nargs="?", const="*", metavar="NAME",
                        help="시트가 기계 판독 가능한지 점검하고 종료 (이름 생략 시 전부)")
    parser.add_argument("--playbooks", default=None,
                        help=f"실행 시트 폴더 (기본 {core.PLAYBOOK_DIR})")
    parser.add_argument("--state", default=None,
                        help=f"선정 세션 폴더 (기본 {core.STATE_DIR})")
    parser.add_argument("--reports", default=None, help="선정 결과서 폴더 (기본 <state>/reports)")
    parser.add_argument(
        "--transport", choices=["stdio", "http", "sse"],
        default=os.getenv("STD_MCP_TRANSPORT", "stdio"),
        help="stdio(기본): 로컬 클라이언트가 직접 실행. http/sse: n8n 등 네트워크 접속.",
    )
    parser.add_argument("--host", default=os.getenv("STD_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("STD_MCP_PORT", "8093")))
    args = parser.parse_args()

    if args.playbooks:
        core.PLAYBOOK_DIR = os.path.abspath(os.path.expanduser(args.playbooks))
    if args.state:
        core.STATE_DIR = os.path.abspath(os.path.expanduser(args.state))
    if args.reports:
        core.REPORT_DIR = os.path.abspath(os.path.expanduser(args.reports))

    if args.list:
        print(list_playbooks())
        sys.exit(0)
    if args.show:
        print(show_playbook(args.show))
        sys.exit(0)
    if args.lint:
        files = core.playbook_files()
        if not files:
            print(f"점검할 시트가 없습니다 ({core.PLAYBOOK_DIR}).")
            sys.exit(1)
        targets = files if args.lint == "*" else None
        if targets is None:
            print(show_playbook(args.lint, lint=True))
        else:
            for f in targets:
                print(core.lint(core._read_playbook(f)))
                print()
        sys.exit(0)

    if args.transport in ("http", "sse"):
        path = "/mcp/" if args.transport == "http" else "/sse/"
        print(f"표준품 선정 MCP 서버 시작 ({args.transport}) — http://{args.host}:{args.port}{path}",
              file=sys.stderr)
        print(f"  실행 시트: {core.PLAYBOOK_DIR}", file=sys.stderr)
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    else:
        # stdio: stdout은 MCP 프로토콜 채널 — 로그는 stderr로.
        print(f"표준품 선정 MCP 서버 시작 (stdio) — 실행 시트: {core.PLAYBOOK_DIR}", file=sys.stderr)
        mcp.run(transport="stdio")
