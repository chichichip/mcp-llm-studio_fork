r"""rag_indexer.py

RAG 인덱스 **구성** CLI — 서빙(rag_server.py, MCP)과 의도적으로 분리돼 있다.
Word/Excel은 COM으로, **PDF는 페이지 이미지 → VLM 전사(Vision RAG)**로 읽는다
(vision_ingest.py). 어느 경로든 결과는 같은 모양의 청크라 검색은 원본 종류를 모른다.
폴더/파일 인덱싱(🟡: 원본 문서는 읽기만 하고 인덱스 파일에만 씀)과
인덱스 삭제(🔴: --clear는 --yes 없이는 프리뷰만)를 담당한다.

구성·서빙 분리 이유:
    1. Qdrant 로컬(파일) 모드는 단일 프로세스 잠금 — 서빙 중 인덱싱하면 잠금을 못
       잡아 sqlite로 저하한 채 벡터가 서빙과 **다른 저장소에** 쌓인다(검색 어긋남).
       그래서 이 인덱서는 Qdrant를 못 잡으면 시작을 거부한다: 인덱싱 전에
       rag_server(MCP)를 내리고 실행할 것.
    2. 서빙 MCP는 읽기 전용(🟢)만 노출돼 모델이 인덱스를 건드릴 수 없다.

사용 (mcp_server의 run_rag_indexer.bat 이 이 스크립트를 부른다):
    python rag_indexer.py C:\docs               # 폴더 인덱싱 (증분 — 변경된 파일만)
    python rag_indexer.py C:\docs --reindex     # 전부 다시 (임베딩 포함)
    python rag_indexer.py --file C:\docs\a.pdf  # 파일 하나만 다시
    python rag_indexer.py C:\specs --vlm-url http://<사내VLM>/v1 --vlm-model gemma-3-27b
    python rag_indexer.py C:\docs --no-vlm      # PDF도 텍스트 레이어만 (VLM 없이 뼈대부터)
    python rag_indexer.py C:\guide --word-vision  # Word 지침서도 표·그림째로 (PDF 변환 후 전사)
    python rag_indexer.py --embed-only          # 벡터만 다시 (전사 안 함 — 임베딩 서버 확보 후)
    python rag_indexer.py --status              # 인덱스 상태 확인
    python rag_indexer.py --clear               # 삭제 프리뷰 (실행 안 함)
    python rag_indexer.py --clear --yes         # 인덱스 전체 삭제

Word/Excel은 COM을 쓰므로 office_server와 같은 제약: Windows + Office, 사용자가
로그인한 세션에서 실행(PDF만 넣을 때는 필요 없다). 임베딩 서버(llama-server
--embeddings)나 VLM 서버가 꺼져 있으면 각각 우아하게 저하하고, 나중에 서버를 켜고
--reindex 하면 벡터와 전사가 붙는다.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import rag_core as core
from rag_core import DOC_PATTERNS, EMBED_BATCH, OfficeError, RagError, RagStore


# ─────────────────────────────── 인덱싱 파이프라인 ───────────────────────────────


def _iter_doc_files(folder: str) -> list[str]:
    """폴더(재귀)에서 인덱싱 대상 파일 목록(.docx/.doc/.pdf/.xlsx…). 임시 파일(~$)은 제외."""
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if name.startswith("~$") or name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in DOC_PATTERNS:
                found.append(os.path.abspath(os.path.join(root, name)))
    return sorted(found)


def _index_one_file(store: RagStore, path: str, password: str = "",
                    reindex: bool = False, use_embed: bool | None = None,
                    use_vlm: bool | None = None, word_vision: bool = False) -> str:
    """파일 하나를 인덱싱한다. 반환: 한 줄 결과 요약."""
    st = os.stat(path)
    if not reindex and store.file_unchanged(path, st.st_mtime, st.st_size):
        return "변경 없음 — 건너뜀"
    kind = core.doc_kind(path)
    chunks, source, notes = core.extract_chunks(path, password, use_vlm=use_vlm,
                                                word_vision=word_vision)
    if not chunks:
        store.replace_file(path, st.st_mtime, st.st_size, [], None, kind, source)
        return "본문 없음 — 청크 0개"
    # 임베딩에는 섹션 경로를 제목(title)으로 붙여, 하위 청크에도 상위 제목 맥락이 벡터에
    # 담기게 한다(본문엔 그 섹션 제목 줄만 있고 상위 대제목은 없을 수 있으므로).
    # EmbeddingGemma 등은 문서 프롬프트 포맷이 정해져 있어 core 헬퍼로 생성한다.
    embed_texts = [core._embed_doc_text(c["content"], c["heading"]) for c in chunks]
    vectors: list[list[float]] | None = None
    if use_embed is not False:
        # 배치가 크면 서버가 거절하므로 _embed_batched가 줄여 가며 맞춘다.
        vectors = core._embed_batched(embed_texts)  # 실패하면 None → 키워드 전용으로 저장
    store.replace_file(path, st.st_mtime, st.st_size, chunks, vectors, kind, source)
    vec_note = f"벡터 {len(vectors)}개" if vectors else "벡터 없음(키워드 전용)"
    pages = {c["page"] for c in chunks if c["page"]}
    page_note = f", {len(pages)}쪽" if pages else ""
    note = f"[{source}] 청크 {len(chunks)}개{page_note}, {vec_note}"
    if notes:
        note += f" (알림 {len(notes)}건: {notes[0][:60]})"
    return note


def index_folder(folder: str, reindex: bool = False, prune: bool = True,
                 password: str = "", use_vlm: bool | None = None,
                 word_vision: bool = False) -> str:
    """폴더(하위 포함)의 문서를 모두 인덱싱하고 결과 요약을 돌려준다.

    Word/Excel은 COM으로, PDF는 페이지 이미지 → VLM 전사로 읽는다. 원본 문서는 읽기만
    하고 인덱스 파일에만 쓴다. 수정 시각·크기가 같은 파일은 건너뛴다(증분).
    prune=True면 폴더에서 사라진 파일을 인덱스에서도 정리한다.
    """
    root = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(root):
        raise RagError(f"'{root}' 폴더가 없습니다. 경로를 확인하세요.")
    files = _iter_doc_files(root)
    if not files:
        return (f"'{root}' 아래에 인덱싱할 문서가 없습니다 "
                f"(대상 확장자: {', '.join(sorted(DOC_PATTERNS))}).")
    # Office COM은 Word/Excel 파일이 있을 때만 필요하다 — PDF만 넣는 PC에서 Office가
    # 없다고 거부하면 안 되므로 여기서 조건부로 확인한다.
    if any(core.doc_kind(f) in ("word", "excel") for f in files):
        core._require_word()

    _preflight_vision(files, word_vision)
    store = core.get_store()
    embed_ok = core._embed_available()
    needs_vlm = any(core.doc_kind(f) == "pdf" for f in files) or (
        word_vision and any(core.doc_kind(f) == "word" for f in files))
    if use_vlm is None and needs_vlm:
        # 파일마다 서버를 찔러 보지 않도록 한 번만 확인해 전체에 적용한다.
        use_vlm = core.vision_ingest is not None and core.vision_ingest.vlm_available()
    results: list[str] = []
    ok = skipped = failed = 0
    start = time.time()
    for path in files:
        try:
            note = _index_one_file(store, path, password, reindex, use_embed=embed_ok,
                                   use_vlm=use_vlm, word_vision=word_vision)
            if note.startswith("변경 없음"):
                skipped += 1
            else:
                ok += 1
                results.append(f"  - {os.path.basename(path)}: {note}")
        except (RagError, OfficeError) as e:
            failed += 1
            results.append(f"  ✗ {os.path.basename(path)}: {e}")
        except Exception as e:  # noqa: BLE001 — 한 파일 실패가 전체를 멈추지 않게
            failed += 1
            results.append(f"  ✗ {os.path.basename(path)}: {type(e).__name__}: {e}")
    # 정리는 **이번에 훑은 폴더 안**으로 한정한다 — 다른 폴더를 따로 인덱싱해 둔
    # 것을 이번 실행이 지우면 안 된다.
    pruned = (store.remove_missing({os.path.abspath(f) for f in files}, under=root)
              if prune else 0)

    s = store.stats()
    head = [
        f"인덱싱 완료: {root}  ({time.time() - start:.1f}초)",
        f"  처리 {ok}개 / 건너뜀(변경 없음) {skipped}개 / 실패 {failed}개"
        + (f" / 정리(삭제된 파일) {pruned}개" if pruned else ""),
        f"  임베딩: {'벡터 생성함' if embed_ok else '서버 없음 — 키워드 인덱스만 생성'}",
        f"  누적: 파일 {s['files']}개, 청크 {s['chunks']}개 (벡터 {s['with_vector']}개)",
    ]
    if use_vlm is not None:
        head.insert(3, f"  PDF 판독: {'VLM 전사' if use_vlm else '텍스트 레이어만 (VLM 서버 없음)'}")
    body = results[:100]
    if len(results) > 100:
        body.append(f"  … (이하 {len(results) - 100}개 생략)")
    return "\n".join(head + ([""] + body if body else []))


def index_file(path: str, password: str = "", use_vlm: bool | None = None,
               word_vision: bool = False) -> str:
    """문서 하나를 (다시) 인덱싱하고 결과 요약을 돌려준다."""
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        raise RagError(f"'{p}' 파일이 없습니다. 경로를 확인하세요.")
    kind = core.doc_kind(p)
    if not kind:
        raise RagError(
            f"인덱싱 대상이 아닌 확장자입니다: {os.path.basename(p)} "
            f"(지원: {', '.join(sorted(DOC_PATTERNS))})"
        )
    if kind in ("word", "excel"):
        core._require_word()
    _preflight_vision([p], word_vision)
    store = core.get_store()
    note = _index_one_file(store, p, password, reindex=True, use_embed=None,
                           use_vlm=use_vlm, word_vision=word_vision)
    return f"인덱싱 완료: {os.path.basename(p)} — {note}"


def _preflight_vision(files: list[str], word_vision: bool) -> None:
    """Vision 경로를 쓰겠다고 했는데 못 쓰는 상태면 **시작 전에** 크게 알린다.

    저하 자체는 설계대로지만, "인덱싱 완료"만 보고 표·그림이 들어간 줄 알면 안 된다.
    끝나고 나오는 알림 한 줄로는 놓치기 쉬워서 앞에서 한 번 더 짚는다.
    """
    wants = [f for f in files
             if core.doc_kind(f) == "pdf" or (word_vision and core.doc_kind(f) == "word")]
    if not wants:
        return
    vi = core.vision_ingest
    if vi is None:
        print(f"[경고] Vision 경로를 쓸 수 없습니다: {core.VISION_IMPORT_ERROR}", file=sys.stderr)
        return
    if not vi.FITZ_AVAILABLE:
        print(
            f"[경고] PyMuPDF가 없어({vi.FITZ_IMPORT_ERROR}) **쪽 이미지를 만들 수 없습니다**.\n"
            f"        {len(wants)}개 파일이 텍스트 추출로 저하됩니다 — 표·그림은 들어가지 않고\n"
            "        쪽 번호와 ask_page 되짚기도 안 됩니다.\n"
            "        지금 쓰는 파이썬에 설치하세요:\n"
            "          venv\\Scripts\\python.exe -m pip install --no-index "
            "--find-links wheelhouse PyMuPDF Pillow",
            file=sys.stderr,
        )


def embed_only() -> str:
    """이미 저장된 청크에 **벡터만** 다시 붙인다. 원본 문서는 열지 않는다.

    임베딩 서버를 나중에 확보했을 때 쓰는 경로다. 전체 재인덱싱은 PDF/Word를 VLM으로
    다시 전사하므로(쪽당 수 초) 이미 끝낸 판독을 통째로 버리게 된다 — 그게 아까워서
    벡터 생성만 떼어 놓았다. 청크·쪽·이미지는 그대로 두고 벡터만 갈아 끼운다.
    """
    if not core._embed_available():
        raise RagError(
            f"임베딩 서버에 연결하지 못했습니다({core.EMBED_URL}). "
            "llama-server --embeddings 를 띄웠는지, local_settings.py의 "
            "RAG_EMBED_URL 이 맞는지 확인하세요."
        )
    store = core.get_store()
    chunks = store.all_chunks()
    if not chunks:
        return "인덱스가 비어 있습니다. 먼저 문서를 인덱싱하세요."

    texts = [core._embed_doc_text(c["content"], c["heading"] or "") for c in chunks]
    ids = [int(c["id"]) for c in chunks]
    done = 0
    start = time.time()
    step = max(1, core.EMBED_BATCH) * 8   # 진행 표시 단위 (내부에서 더 잘게 나눠 보낸다)
    for i in range(0, len(texts), step):
        batch = core._embed_batched(texts[i:i + step])
        if batch is None:
            raise RagError(
                f"임베딩 도중 서버 응답이 끊겼습니다. {done}/{len(ids)}개까지 붙였습니다.\n"
                f"  사유: {core._embed_reason or '불명'}\n"
                "  배치를 1까지 줄여도 안 되면 청크 하나가 모델 한도를 넘는 것입니다 — "
                "llama-server를 `-c 4096 -b 4096 -ub 4096`처럼 크게 잡고 다시 실행하세요."
            )
        done += store.set_vectors(ids[i:i + len(batch)], batch)
        print(f"  임베딩 {done}/{len(ids)}", end="\r", file=sys.stderr)
    print("", file=sys.stderr)

    s = store.stats()
    return (
        f"벡터 생성 완료 ({time.time() - start:.1f}초) — 문서는 다시 읽지 않았습니다.\n"
        f"  청크 {done}개에 벡터를 붙였습니다.\n"
        f"  누적: 파일 {s['files']}개, 청크 {s['chunks']}개 (벡터 {s['with_vector']}개)\n"
        f"  임베딩 서버: {core.EMBED_URL}"
    )


def _require_qdrant_or_exit(store: RagStore) -> None:
    """qdrant-client가 있는데 백엔드를 못 열었다면(대개 서빙이 잠금 보유) 중단한다.

    저하한 채 인덱싱하면 벡터가 서빙(Qdrant)과 다른 곳(sqlite BLOB)에 쌓여
    검색이 어긋난다 — 조용한 불일치보다 명시적 거부가 안전하다.
    qdrant-client 자체가 없는 환경은 양쪽 다 sqlite라 일관되므로 그대로 진행한다.
    """
    if core.QDRANT_AVAILABLE and store.vec_kind != "qdrant":
        print(f"[중단] Qdrant 벡터 저장소를 열지 못했습니다: {store.vec_error}", file=sys.stderr)
        print(
            "rag_server(MCP 서빙)가 떠 있으면 종료한 뒤 다시 실행하세요. "
            "(저하한 채 인덱싱하면 서빙과 다른 저장소에 벡터가 쌓여 검색이 어긋납니다)",
            file=sys.stderr,
        )
        sys.exit(2)


# ─────────────────────────────── CLI ───────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Word 문서 RAG 인덱스 구성 CLI (서빙은 rag_server.py)"
    )
    parser.add_argument("folder", nargs="?", help="인덱싱할 폴더 (하위 폴더 포함)")
    parser.add_argument("--file", metavar="DOCX", help="파일 하나만 (다시) 인덱싱")
    parser.add_argument("--reindex", action="store_true", help="변경 없어도 전부 다시 (임베딩 포함)")
    parser.add_argument("--no-prune", action="store_true", help="사라진 파일을 인덱스에서 정리하지 않음")
    parser.add_argument("--password", default="", help="문서 공통 열기 암호")
    parser.add_argument("--status", action="store_true", help="인덱스 상태만 출력하고 종료")
    parser.add_argument("--embed-only", action="store_true",
                        help="이미 인덱싱된 청크에 벡터만 다시 붙인다 (문서를 다시 읽지 않음 — "
                             "임베딩 서버를 나중에 확보했을 때)")
    parser.add_argument("--clear", action="store_true", help="인덱스 전체 삭제 (--yes 없으면 프리뷰만)")
    parser.add_argument("--yes", action="store_true", help="--clear를 실제로 실행")
    parser.add_argument("--db", default=None, help=f"인덱스 파일 경로 (기본 {core.DB_PATH})")
    parser.add_argument("--qdrant", default=None,
                        help=f"Qdrant 로컬 데이터 폴더 (기본 {core.QDRANT_PATH})")
    parser.add_argument("--embed-url", default=None,
                        help=f"임베딩 서버 /v1 베이스 URL (기본 {core.EMBED_URL})")
    parser.add_argument("--vlm-url", default=None,
                        help="PDF 전사용 VLM 서버 /v1 베이스 URL (사내 게이트웨이 주소)")
    parser.add_argument("--vlm-model", default=None, help="VLM 모델 이름")
    parser.add_argument("--dpi", type=int, default=None,
                        help="PDF 렌더링 DPI (기본 150 — 413 오류가 나면 낮출 것)")
    parser.add_argument("--no-vlm", action="store_true",
                        help="PDF를 VLM 없이 텍스트 레이어만으로 인덱싱")
    parser.add_argument("--word-vision", action="store_true",
                        help="Word 문서도 PDF로 내보내 VLM으로 전사 (표·그림이 본문인 지침서용)")
    args = parser.parse_args()

    if args.db:
        core.DB_PATH = os.path.abspath(os.path.expanduser(args.db))
    if args.qdrant:
        core.QDRANT_PATH = os.path.abspath(os.path.expanduser(args.qdrant))
    if args.embed_url:
        core.EMBED_URL = args.embed_url
    if core.vision_ingest is not None:
        if args.vlm_url:
            core.vision_ingest.VLM_URL = args.vlm_url
        if args.vlm_model:
            core.vision_ingest.VLM_MODEL = args.vlm_model
        if args.dpi:
            core.vision_ingest.VLM_DPI = args.dpi
    use_vlm = False if args.no_vlm else None

    if core.pythoncom is not None:  # CLI 메인 스레드 COM 초기화 (Word 읽기용)
        core.pythoncom.CoInitialize()

    try:
        if args.status:
            print(core.status_text())
            return

        if args.embed_only:
            _require_qdrant_or_exit(core.get_store())
            print(embed_only())
            return

        if args.clear:
            store = core.get_store()
            s = store.stats()
            if not args.yes:
                print(
                    f"⚠️ 프리뷰 — 아직 삭제하지 않았습니다: 인덱스 전체 삭제\n"
                    f"  인덱스 파일: {core.DB_PATH}\n"
                    f"  삭제 대상: 파일 {s['files']}개, 청크 {s['chunks']}개 (원본 문서는 안전)\n"
                    "실제로 삭제하려면 --yes 를 함께 지정하세요."
                )
                return
            _require_qdrant_or_exit(store)
            nf, nc = store.clear()
            print(f"인덱스를 비웠습니다 (파일 {nf}개, 청크 {nc}개 삭제). 원본 문서는 그대로입니다.")
            return

        if args.file:
            _require_qdrant_or_exit(core.get_store())
            print(index_file(args.file, password=args.password, use_vlm=use_vlm,
                             word_vision=args.word_vision))
            return

        if args.folder:
            _require_qdrant_or_exit(core.get_store())
            print(index_folder(args.folder, reindex=args.reindex,
                               prune=not args.no_prune, password=args.password,
                               use_vlm=use_vlm, word_vision=args.word_vision))
            return

        parser.print_help()
    except (RagError, OfficeError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Qdrant 로컬 저장소를 명시적으로 닫는다. 안 닫으면 인터프리터가 내려간 뒤 GC가
        # 소멸자를 불러 `sys.meta_path is None`이 뜨고, 그 시점엔 flush 보장이 없다.
        core.close_store()
        if core.pythoncom is not None:
            core.pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
