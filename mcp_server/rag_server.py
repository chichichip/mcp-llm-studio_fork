"""rag_server.py

RAG **서빙** MCP 서버 — 읽기 전용(🟢). 인덱스 구성(인덱싱·삭제)은 rag_indexer.py가
담당한다 (구성·서빙 분리 — 이유는 rag_indexer.py docstring 참고). 저장소·검색
로직은 rag_core.py에 있고 여기는 MCP 도구 껍데기만 둔다.

도구:
    search_docs — 하이브리드 검색(벡터+키워드, RRF 융합). 임베딩 서버가 없으면
                  키워드 전용으로 우아하게 저하한다.
    read_page   — 특정 문서 특정 쪽의 전사 원문을 그대로 읽는다(검색 요약 말고 원문).
    ask_page    — **그 쪽의 원본 이미지를 VLM에 다시 보여 주며** 되묻는다.
                  텍스트로 한 번 접힌 표·도면을 원본 그림으로 되짚는 경로다.
    rag_status  — 인덱스 상태 조회 (파일/청크/벡터 수, 임베딩·VLM 서버 연결 여부).

⚠ 이 서버는 문서를 **찾아 읽어 주는** 도구다. 치수를 재어 부품을 고르는 판단에
직접 쓰지 말 것 — 표준품 선정은 spec-reader의 검증(verify.py)을 거친 판독값으로
결정론적으로 해야 한다(저장소 CLAUDE.md 'Vision RAG' 절).

사용:
    python rag_server.py                     # MCP 서버 (stdio)
    python rag_server.py --transport http    # n8n 등 네트워크용, :8090
    python rag_server.py --search "휴가 규정"  # 검색 시험 (CLI, MCP 없이)

llm_studio 장착 (코드 변경 불필요): 데이터 폴더의 mcp_servers.json에 등록만 하면
도구가 docs__search_docs 등으로 모델에 노출된다.
    {"mcpServers": {"docs": {"command": "python",
                             "args": ["C:\\경로\\mcp_server\\rag_server.py"]}}}

⚠ 이 서버가 떠 있는 동안 rag_indexer를 돌리면 Qdrant 로컬 잠금 때문에 인덱서가
시작을 거부한다 — 인덱싱할 때는 이 서버를 잠시 내릴 것. 반대로 인덱서가 도는 중에
이 서버를 띄우면 벡터 검색이 sqlite BLOB으로 저하한다(rag_status에 사유 표시).
"""

from __future__ import annotations

import argparse
import os
import sys

from fastmcp import FastMCP

import rag_core as core
from rag_core import MAX_RESULT_CHARS, RRF_K, RagError, rag_tool

mcp = FastMCP(
    name="docs",
    instructions=(
        "사내 문서(Word/PDF/Excel RAG 인덱스)를 검색하는 MCP 서버입니다. 문서 내용에 "
        "관한 질문을 받으면 search_docs로 관련 대목을 찾아 근거로 답하세요(출처 "
        "파일명과 쪽 번호를 함께 알려주세요). 검색 결과가 표나 도면에서 잘려 보이면 "
        "read_page로 그 쪽 전문을 읽고, 그래도 확실하지 않으면 ask_page로 그 쪽 "
        "그림을 직접 다시 보게 하세요. 인덱스가 비었거나 상태가 궁금하면 rag_status를 "
        "먼저 호출하세요. 이 서버는 읽기 전용입니다 — 인덱스 구성(문서 추가/삭제)은 "
        "관리자가 rag_indexer CLI로 합니다. 치수·부품번호처럼 틀리면 안 되는 값은 "
        "검색 결과를 요약하지 말고 원문 그대로 인용하고, 근거가 없으면 모른다고 "
        "답하세요."
    ),
)


@mcp.tool()
@rag_tool
def rag_status() -> str:
    """RAG 인덱스 상태를 조회합니다 — 파일/청크 수, 벡터 유무, 임베딩 서버 연결. (🟢 읽기)

    검색이 이상하거나 인덱스가 비어 보일 때 가장 먼저 호출하세요.
    """
    return core.status_text()


@mcp.tool()
@rag_tool
def search_docs(query: str, top_k: int = 5) -> str:
    """인덱싱된 사내 문서(Word/PDF/Excel)에서 질의와 관련된 내용을 찾습니다. (🟢 읽기)

    벡터(의미) 검색과 키워드 검색을 함께 수행해 RRF로 합치고, 리랭커가 있으면 상위
    후보를 관련도로 재정렬합니다. 임베딩·리랭커 서버가 없으면 각각 우아하게 저하합니다
    (키워드 전용 / RRF 순서). 결과에는 출처 파일명·섹션 경로·쪽 번호가 붙고, 매칭 청크의
    같은 섹션 이웃을 함께 보여줍니다 — 답변할 때 출처를 함께 알려주세요.

    표가 잘려 보이거나 값이 확실하지 않으면 결과의 쪽 번호로 read_page(전사 원문) →
    ask_page(원본 그림 재판독) 순으로 더 들어가세요.

    Args:
        query: 찾을 내용(자연어 질문 그대로도 됩니다).
        top_k: 돌려줄 청크 수(기본 5, 최대 20).
    """
    q = core._nfc(query)
    if not q:
        raise RagError("query가 비어 있습니다. 찾을 내용을 지정하세요.")
    k = max(1, min(int(top_k), 20))
    store = core.get_store()
    if store.stats()["chunks"] == 0:
        return "인덱스가 비어 있습니다. rag_indexer.py(run_rag_indexer.bat)로 문서 폴더를 먼저 인덱싱하세요."

    # 두 경로에서 넉넉히(k*3) 뽑아 RRF로 합친다.
    pool = k * 3
    vec_hits: list[tuple[int, float]] = []
    qv = core._embed_texts([core._embed_query_text(q)])  # 질의 프리픽스(모델 포맷) 부착
    mode = "키워드 전용 (임베딩 서버 없음)"
    if qv:
        vec_hits = store.vector_search(qv[0], pool)
        mode = "하이브리드 (벡터+키워드)"
    kw_hits = store.keyword_search(q, pool)

    fused: dict[int, float] = {}
    for hits in (vec_hits, kw_hits):
        for rank, (cid, _score) in enumerate(hits):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    if not fused:
        return f"'{q}'와 관련된 내용을 찾지 못했습니다. 다른 표현으로 다시 시도해 보세요."

    # 리랭크 후보: 융합 상위 N개(k보다 넉넉히)를 뽑아 본문을 가져온다. 리랭커가 있으면
    # 질의-청크 관련도로 재정렬하고, 없으면 RRF 순서를 그대로 쓴다(우아한 저하).
    rerank_pool = max(k * 4, 20)
    ranked = sorted(fused.items(), key=lambda t: -t[1])[:rerank_pool]
    chunk_map = store.fetch_chunks([cid for cid, _ in ranked])
    present = [(cid, chunk_map[cid]) for cid, _ in ranked if cid in chunk_map]
    rr_docs = [
        (c["heading"] + "\n" + c["content"]) if c.get("heading") else c["content"]
        for _, c in present
    ]
    rr = core._rerank(q, rr_docs)
    if rr is not None:
        order = [present[idx] for idx, _s in rr if idx < len(present)]
        mode += " + 리랭크"
    else:
        order = present

    # 이웃 확장을 켜면 인접 청크가 같은 블록으로 겹칠 수 있다 — 이미 낸 블록과 같은
    # 내용은 건너뛰고 다음 후보로 채워 서로 다른 결과 k개를 유지한다.
    cap = core.MAX_CONTEXT_CHARS if core.CONTEXT_WINDOW > 0 else MAX_RESULT_CHARS
    out = [f"검색: {q}  [{mode}]", ""]
    seen: set[str] = set()
    i = 0
    for _cid, c in order:
        if i >= k:
            break
        content = _expand_context(store, c)
        if content in seen:
            continue
        seen.add(content)
        if len(content) > cap:
            content = content[:cap] + " …(생략)"
        i += 1
        out.append(f"[{i}] {_cite(c)}")
        out.append(content)
        out.append("")
    return "\n".join(out).rstrip()


def _cite(c: dict) -> str:
    """출처 한 줄 — 파일명 › 섹션 (쪽/청크). 쪽이 있으면 read_page/ask_page의 열쇠가 된다."""
    heading = c.get("heading")
    loc = f" › {heading}" if heading else ""
    page = int(c.get("page") or 0)
    where = f"{page}쪽, 청크 {c['seq']}" if page else f"청크 {c['seq']}"
    return f"{os.path.basename(c['path'])}{loc} ({where})"


# ─────────────────────────────── 쪽 단위 되읽기 ───────────────────────────────
# search_docs는 근거를 '찾는' 도구라 청크 경계에서 표가 잘릴 수 있다. 아래 둘은 찾은
# 뒤 그 쪽을 **원문 그대로 / 원본 그림 그대로** 다시 보게 하는 경로다.


def _resolve_file(store, document: str) -> str:
    """문서 이름 조각을 인덱싱된 절대경로 하나로 좁힌다. 애매하면 RagError로 되묻는다."""
    found = store.find_file(document)
    if not found:
        raise RagError(
            f"'{document}' 문서를 인덱스에서 찾지 못했습니다. rag_status로 인덱싱된 "
            "파일 목록을 확인하거나 파일명을 더 정확히 지정하세요."
        )
    if len(found) > 1:
        names = ", ".join(os.path.basename(f["path"]) for f in found[:8])
        raise RagError(f"'{document}'에 해당하는 문서가 여러 개입니다: {names}. 더 정확히 지정하세요.")
    return found[0]["path"]


@mcp.tool()
@rag_tool
def read_page(document: str, page: int) -> str:
    """인덱싱된 문서의 특정 쪽 **전사 원문**을 그대로 돌려줍니다. (🟢 읽기)

    search_docs 결과에 '12쪽'처럼 쪽 번호가 붙어 있으면, 표가 잘려 보이거나 값이
    확실하지 않을 때 이 도구로 그 쪽 전체를 읽으세요. 검색 요약이 아니라 인덱싱할 때
    저장한 원문입니다.

    Args:
        document: 파일명 또는 경로 일부 (예: "표준품 선정 지침서.pdf").
        page: 쪽 번호 (1부터).
    """
    store = core.get_store()
    path = _resolve_file(store, document)
    rows = store.page_chunks(path, int(page))
    if not rows:
        raise RagError(
            f"'{os.path.basename(path)}'의 {page}쪽을 찾지 못했습니다. "
            "쪽 번호를 확인하세요(쪽 개념이 없는 Word/Excel 문서일 수도 있습니다)."
        )
    body = core._merge_overlapping([r["content"] for r in rows])
    head = rows[0].get("heading") or ""
    img = rows[0].get("image") or ""
    out = [f"{os.path.basename(path)} {page}쪽" + (f" — {head}" if head else "")]
    if img:
        out.append(f"(원본 이미지: {img} — 더 확인이 필요하면 ask_page로 되물으세요)")
    out.append("")
    out.append(body)
    return "\n".join(out)


@mcp.tool()
@rag_tool
def ask_page(document: str, page: int, question: str) -> str:
    """문서의 특정 쪽 **원본 그림을 다시 보고** 질문에 답하게 합니다 (VLM 재판독). (🟢 읽기)

    인덱싱할 때의 전사는 페이지를 텍스트로 한 번 옮긴 것이라, 표가 복잡하거나 도면의
    지시선을 봐야 하는 질문은 전사만으로 부족할 수 있습니다. 이 도구는 그 쪽 이미지를
    VLM에 다시 보내 질문에 맞춰 읽게 합니다 — read_page로도 확실하지 않을 때 쓰세요.

    ⚠ 돌아오는 값은 **판독 결과이지 검증된 값이 아닙니다.** 부품 선정처럼 틀리면 안 되는
    판단의 근거로 쓸 때는 사람이 원본을 확인해야 합니다.

    Args:
        document: 파일명 또는 경로 일부.
        page: 쪽 번호 (1부터).
        question: 그 쪽에서 확인할 내용 (예: "dash -12 행의 L 값은?").
    """
    if core.vision_ingest is None:
        raise RagError(f"VLM 재판독을 쓸 수 없습니다: {core.VISION_IMPORT_ERROR}")
    q = core._nfc(question)
    if not q:
        raise RagError("question이 비어 있습니다. 그 쪽에서 확인할 내용을 지정하세요.")

    store = core.get_store()
    path = _resolve_file(store, document)
    rows = store.page_chunks(path, int(page))
    img = next((r["image"] for r in rows if r.get("image")), "")
    if not img or not os.path.isfile(img):
        # 캐시가 지워졌거나 인덱싱 때 이미지를 못 남긴 경우 — 즉석 렌더링으로 복구한다
        # (Word는 다시 PDF로 내보내 렌더링한다).
        img = core.ensure_page_image(path, int(page))
    if not img:
        raise RagError(
            f"'{os.path.basename(path)}' {page}쪽의 이미지를 얻지 못했습니다. "
            "쪽 이미지가 없는 문서(텍스트로만 인덱싱된 Word/Excel)이거나 PyMuPDF가 "
            "없을 수 있습니다 — read_page로 전사 원문을 읽어 보세요."
        )
    prompt = (
        "이 페이지를 보고 아래 질문에 답하세요. 페이지에 있는 내용만 근거로 삼고, "
        "값은 인쇄된 그대로 옮기세요(반올림·단위 추가 금지). 페이지에서 확인할 수 없으면 "
        "지어내지 말고 '이 페이지에서는 확인할 수 없습니다'라고 답하세요.\n\n"
        f"질문: {q}"
    )
    try:
        with open(img, "rb") as f:
            png = f.read()
        answer = core.vision_ingest.ask_image(png, prompt)
    except OSError as e:
        raise RagError(f"페이지 이미지를 읽지 못했습니다: {e}") from e
    if not answer:
        raise RagError(
            f"VLM 서버({core.vision_ingest.VLM_URL})에서 응답을 받지 못했습니다. "
            "rag_status로 연결을 확인하거나 read_page로 전사 원문을 읽으세요."
        )
    return (
        f"{os.path.basename(path)} {page}쪽 재판독 (VLM — 검증되지 않은 판독값)\n"
        f"원본 이미지: {img}\n\n{answer.strip()}"
    )


def _expand_context(store, c: dict) -> str:
    """매칭 청크에 같은 섹션의 이웃 청크(앞뒤)를 붙여 근거가 청크 경계에서 잘리지 않게 한다.

    이웃은 같은 섹션 경로(heading)만 골라 seq 순으로 겹침을 제거하며 잇는다. 컨텍스트
    확장이 꺼져 있거나(CONTEXT_WINDOW=0) 이웃이 없으면 매칭 청크 본문만 돌려준다.
    """
    if core.CONTEXT_WINDOW <= 0:
        return c["content"]
    neigh = store.fetch_context(c["file_id"], c["seq"], core.CONTEXT_WINDOW)
    same = [
        n["content"] for n in neigh
        if (n.get("heading") or None) == (c.get("heading") or None)
    ]
    return core._merge_overlapping(same) if same else c["content"]


# ─────────────────────────────── CLI / 서버 기동 ───────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Word 문서 RAG 검색 MCP 서버 (서빙 전용 — 인덱싱은 rag_indexer.py)"
    )
    parser.add_argument("--search", metavar="QUERY", help="검색을 시험하고 종료 (MCP 서버를 띄우지 않음)")
    parser.add_argument("--top-k", type=int, default=5, help="--search 결과 수 (기본 5)")
    parser.add_argument("--db", default=None, help=f"인덱스 파일 경로 (기본 {core.DB_PATH})")
    parser.add_argument("--qdrant", default=None,
                        help=f"Qdrant 로컬 데이터 폴더 (기본 {core.QDRANT_PATH})")
    parser.add_argument("--embed-url", default=None,
                        help=f"임베딩 서버 /v1 베이스 URL (기본 {core.EMBED_URL})")
    parser.add_argument("--rerank-url", default=None,
                        help=f"리랭커 서버 /v1 베이스 URL (기본 {core.RERANK_URL}, 없으면 리랭크 건너뜀)")
    parser.add_argument("--vlm-url", default=None,
                        help="ask_page 재판독용 VLM 서버 /v1 베이스 URL (사내 게이트웨이 주소)")
    parser.add_argument("--vlm-model", default=None, help="VLM 모델 이름")
    parser.add_argument(
        "--transport", choices=["stdio", "http", "sse"],
        default=os.getenv("RAG_MCP_TRANSPORT", "stdio"),
        help="stdio(기본): 로컬 클라이언트가 직접 실행. http/sse: n8n 등 네트워크 접속.",
    )
    parser.add_argument("--host", default=os.getenv("RAG_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("RAG_MCP_PORT", "8090")))
    args = parser.parse_args()

    if args.db:
        core.DB_PATH = os.path.abspath(os.path.expanduser(args.db))
    if args.qdrant:
        core.QDRANT_PATH = os.path.abspath(os.path.expanduser(args.qdrant))
    if args.embed_url:
        core.EMBED_URL = args.embed_url
    if args.rerank_url:
        core.RERANK_URL = args.rerank_url
    if core.vision_ingest is not None:
        if args.vlm_url:
            core.vision_ingest.VLM_URL = args.vlm_url
        if args.vlm_model:
            core.vision_ingest.VLM_MODEL = args.vlm_model

    if args.search:
        # CLI 모드: 도구 함수를 직접 호출한다 (rag_tool 데코레이터가 예외를 처리).
        print(search_docs(args.search, top_k=args.top_k))
        sys.exit(0)

    if args.transport in ("http", "sse"):
        path = "/mcp/" if args.transport == "http" else "/sse/"
        print(f"RAG MCP 서버 시작 ({args.transport}) — http://{args.host}:{args.port}{path}",
              file=sys.stderr)
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    else:
        # stdio: stdout은 MCP 프로토콜 채널 — 로그는 stderr로.
        print("RAG MCP 서버 시작 (stdio)", file=sys.stderr)
        mcp.run(transport="stdio")
