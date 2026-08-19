# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

두 층으로 이루어져 있고, **둘의 성격이 완전히 다르다**.

1. **반입물 (repo가 추적하는 것)** — `mcp_server/`의 MCP 서버들과 그 실행용 `run_*.bat`, `llm_studio/`(앱 + `serve_llm.py`), RAG 학습 문서 투입 폴더 `rag_docs/`, 표준품 스펙 판독 모듈 `spec-reader/`, 루트의 `install_requirements.bat`. 사내 폐쇄망에서 실제로 돌릴 코드다. 여기가 이 저장소의 본체다. (폴더 규약: 서버 코드와 실행 bat은 `mcp_server/`에, LLM 서빙은 `llm_studio/`에, 오프라인 설치와 공용 `venv/`만 루트에 둔다.)
2. **강의 자료 (`Examples/`, `.gitignore`로 제외됨)** — LangChain/LangGraph 한국어 코스("AITF")의 랩 노트북과 봇 하니스. **git에 올라가지 않으므로 clone한 곳에는 존재하지 않는다.** 개발 PC에만 있는 참고 자료이고, 루트 코드가 여기에 의존하지 않는다.

`Examples/`를 수정하는 작업은 커밋되지 않는다는 점을 항상 염두에 둘 것.

## 배포 경로 (이 저장소의 존재 이유)

```
개인 PC (개발·커밋)  →  GitHub  →  회사 PC (clone)  →  폐쇄망 (실행)
```

폐쇄망에는 **인터넷이 없고**, 사내 PyPI 미러만 있다. 이 제약이 아래 모든 규칙의 근거다.

- **인터넷을 전제한 코드를 새로 넣지 말 것.** 런타임에 나가는 HTTP는 전부 localhost여야 한다. 패키지·모델·바이너리를 실행 중에 내려받는 코드는 폐쇄망에서 무조건 죽는다.
- **새 의존성은 사내 미러에 있는지 먼저 의심할 것.** 추가하면 반드시 `requirements.txt`에 명시하고, 없을 때 어떻게 되는지(우아한 저하 or 실패)를 주석에 적는다. `fastmcp`는 특히 위험한 축에 속한다 (개발 PC 검증 버전 3.4.4).
- **반입 보조 도구는 루트에 둔다** — `install_requirements.bat`(오프라인 설치), `iso_extract.py`(ISO 마운트가 실패하는 PC에서 내용물만 꺼냄, 표준 라이브러리만 사용).
- **반입물 목록은 루트 `반입목록.md`에 모아 둔다.** 패키지/실행파일/모델을 나눠 적고, 없을 때 무엇이 저하되는지까지 표로 정리한 문서다. 새 의존성을 추가하면 requirements.txt와 **이 문서 양쪽**에 반영할 것.
- **점(`.`)으로 시작하는 파일을 새로 만들지 말 것.** 사내 반입 검사가 dotfile을 막는다. 빈 폴더를 유지해야 하면 `.gitkeep` 대신 `README.md`를 둔다(실제로 `spec-reader/samples/.gitkeep`이 막혀 바꿨다). 이미 있는 `.gitignore` 둘은 폐쇄망에서 아무 역할이 없어 빼고 반입해도 된다 — 어느 파일이 없어도 되는지는 `반입목록.md`에 표로 있다.
- **대용량 파일은 git에 넣지 않는다.** `.gguf` 모델, `llama-server.exe`+CUDA DLL, PyInstaller 산출물은 `.gitignore`로 막혀 있다. GitHub는 파일당 100MB 제한이 있고, 이것들은 USB 등 별도 매체로 옮긴다.
- 폐쇄망에서는 소스 직접 실행과 exe 설치 **두 경로를 모두 지원한다** (아래 llm_studio 참고).

## 구성 요소

### `mcp_server/` — MCP 서버 모음

MCP 서버(office/outlook/catia/rag/ansys/pdf/intranet/std)와 스모크 테스트, **그리고 그 실행용 bat이 전부 이 폴더에 있다**. `run_office_server.bat`·`run_outlook_server.bat`·`run_catia_server.bat`·`run_rag_server.bat`·`run_ansys_server.bat`·`run_pdf_server.bat`이 각각을 띄우는 실행 파일이다 — **인자 없이(더블클릭) 실행하면 HTTP 트랜스포트**로 뜨고, 인자를 주면 그대로 전달한다(`run_office_server.bat --transport stdio`). RAG 인덱스 구성 CLI는 `run_rag_indexer.bat`이 따로 있다. bat들은 자기 폴더로 `cd`한 뒤 같은 폴더의 `*.py`를 부르고, 파이썬은 **루트의 공용 `venv/`를 `..\venv\Scripts\python.exe`로** 찾는다(없으면 시스템 python) — 이 상대경로(`..\venv`)를 깨지 말 것. `rag_core.py`는 `office_server.py`를, `test_outlook.py`는 `outlook_server.py`를 같은 폴더에서 import하므로 **파이썬 파일을 폴더 밖으로 따로 옮기면 깨진다**(RAG 코드가 mcp_server를 못 떠나는 이유 — 학습에 넣을 문서만 루트 `rag_docs/`에 둔다). bat 파일은 **CRLF 줄바꿈 + 영어 ASCII만** 유지할 것 — LF로 저장하면 cmd가 줄 경계를 잘못 잘라 주석 조각을 명령으로 실행하고, 한글은 콘솔 코드페이지(cp949)와 파일 인코딩(UTF-8)이 어긋나 깨진다.

### `mcp_server/office_server.py` / `outlook_server.py` — COM 기반 MCP 서버

pywin32(COM)로 **이미 로그인·실행 중인** Office/Outlook을 직접 조종하는 MCP 서버. 파일을 파싱하는 게 아니라 앱 자체를 붙잡으므로, 화면에 열려 있는 저장 전 문서나 현재 사용자의 사서함을 그대로 읽는다.

공통 규약:
- **트랜스포트 3종** — `--transport stdio`(기본, Claude 등 로컬 클라이언트가 프로세스를 직접 실행), `http`(n8n 등이 URL로 접속), `sse`(Streamable HTTP를 못 쓰는 구버전 n8n MCP 노드용). 포트는 office 8087 / outlook 8088.
- **반드시 사용자가 로그인한 그 세션에서 실행해야 한다.** COM 특성상 서비스나 다른 세션에서 띄우면 열린 문서/사서함이 보이지 않는다. 이건 우회할 수 없는 제약이다.
- **우아한 저하** — pywin32가 없으면(`COM_AVAILABLE=False`) 서버는 정상적으로 뜨고 모든 도구가 실패 사유를 담은 안내 메시지를 반환한다. import 에러로 죽지 않는다.
- `path=""` → 지금 활성화된 문서, `path` 지정 → 열려 있으면 그 세션, 아니면 백그라운드에서 읽기 전용으로 열었다 닫는다. 암호 걸린 문서는 `password` 인자로 넘긴다 (대화상자 대신 오류 메시지로 물러선다).

`office_server.py`는 Excel/Word/PowerPoint **모두 읽기+편집**을 지원한다(도구 30개, 3티어): 🟢 읽기(read_*/find_*/inspect_*/list_* 19개) / 🟡 메모리 수정(Excel `write_excel_cell`·`write_excel_range`, Word `replace_word_text`·`set_word_paragraph`·`write_word_paragraph`·`delete_word_paragraph`·`set_word_table_cell`, PPT `set_powerpoint_text` — **사용자 세션에 열려 있는** 문서만, 저장 안 함. Excel의 COM 수정은 Ctrl+Z에 안 쌓이므로 모든 쓰기 도구가 '바꾸기 전 값'을 응답으로 돌려준다) / 🔴 디스크 기록(`save_workbook`·`save_word_document`·`save_presentation` — confirm 게이트, 공용 `_save_open_document`). 쓰기 도구는 백그라운드 읽기 전용 인스턴스(`_document`)를 쓰지 않고 **`_writable(kind, path)`로 열린 문서만** 잡는다 — 이 구분을 깨지 말 것(안 열린 파일을 백그라운드로 열어 고치면 닫을 때 변경이 조용히 버려진다).
  - Word 편집의 COM 함정 둘: **단락 Range에는 끝의 ¶가 포함**돼 그냥 `Range.Text`에 대입하면 다음 단락과 합쳐진다(`_set_paragraph_text`가 `MoveEnd(wdCharacter, -1)`로 뺀다). `Find.Execute`는 **위치 인자 11개**로 넘긴다 — pywin32 동적 디스패치가 일부 메서드에서 키워드를 조용히 흘리는 문제(문서 열기의 `Password`와 같은 함정)를 피하기 위해서다. ⚠ 개발 PC에 Word가 없어 이 관용구들은 실기 미검증이다(`⚠ 실기 검증 대상` 주석 위치를 사내 PC에서 확인할 것).
  - 새 쓰기 도구를 만들면 `llm_studio/server/worklog.py`의 `_WRITE_HINTS`에 그 동사를 넣을 것. 빠지면 '읽음'으로 기록돼 미저장 경고가 안 뜬다(`replace`가 실제로 그랬다).

`outlook_server.py`는 쓰기가 가능해서 **3티어 안전 등급**을 따른다:
- 🟢 읽기 — 목록/검색/상세/첨부 저장/일정·연락처·작업 조회
- 🟡 로컬 생성(비파괴) — 초안·일정·연락처·작업 '만들기'만
- 🔴 외부 발송·파괴 — `send_email`, `send_draft`, `respond_message(send=True)`, `move_message`, `delete_message`, `create_meeting`, `respond_meeting`

**🔴 도구는 `confirm=True` 없이는 실행되지 않는다.** confirm 없이 부르면 "누구에게/무슨 제목/무슨 동작"을 요약한 프리뷰만 돌려준다(`_confirm_preview`). 이 게이트를 새 파괴적 도구에도 반드시 똑같이 적용할 것. 클라이언트 쪽에서 `HumanInTheLoopMiddleware`의 `INTERRUPT_ON`에 같은 이름을 올리면 이중 안전장치가 된다.

그 외 Outlook 고유 규약:
- **항목 참조는 EntryID 문자열**로 한다. 목록/검색이 `entry_id`를 주고 상세/후속 도구가 그걸 받는다.
- **푸시 트리거가 없다** (MCP는 요청-응답). 새 메일은 `poll_new_mail`로 폴링하고, 반환된 `checkpoint`를 다음 호출의 `since`로 넘겨 중복 없이 이어간다. n8n Schedule 트리거와 맞물리는 지점.
- Outlook의 Programmatic Access 경고창이 뜨면 COM 호출이 그 앞에서 멈춘다(회사 GPO에 좌우). 민감 속성은 우회 조회를 시도하고, 실패하면 대화상자를 띄우는 대신 빈 값/안내로 물러선다.

`test_outlook.py` — outlook_server 도구들을 실제 Outlook에 대고 한 번씩 호출하는 수동 스모크 테스트. **되돌리기 어려운 동작은 절대 실행하지 않는다** (어디에서도 `confirm=True`를 넘기지 않고, 🔴 도구는 프리뷰 경로만 확인한다). `--create` 플래그를 줘야 🟡 로컬 생성을 시도하고, 만든 항목은 곧바로 지운편지함으로 정리한다. 이 원칙을 깨는 수정을 하지 말 것.

### `mcp_server/rag_core.py` · `vision_ingest.py` · `rag_indexer.py` · `rag_server.py` — 문서 RAG (구성·서빙 분리)

폴더의 **.docx/.doc/.pdf/.xlsx**를 청킹·임베딩해 인덱싱하고 하이브리드 검색(벡터+FTS5 trigram, RRF 융합)을 제공한다. **구성과 서빙이 파일로 분리돼 있고, 이 구분을 유지할 것**:

- `rag_core.py` — 공용 코어(문서 읽기·청킹·임베딩·저장소). 실행 파일 아님. 설정(DB_PATH 등)은 CLI가 덮어쓰므로 다른 모듈에서는 `core.DB_PATH`처럼 **매번 속성으로** 읽는다(from-import 복사 금지).
- `vision_ingest.py` — **PDF 인제스트**(페이지 이미지 → VLM 전사). 아래 'Vision RAG' 절. 서버 없이 `python mcp_server\vision_ingest.py --probe <PDF>`로 진단.
- `rag_indexer.py` — **구성 CLI** (🟡 인덱싱 / 🔴 `--clear`는 `--yes` 없이 프리뷰만). `run_rag_indexer.bat`.
- `rag_server.py` — **서빙 MCP** (🟢 search_docs/list_sections/read_page/ask_page/rag_status **읽기 전용** — 모델이 인덱스를 못 건드린다). `run_rag_server.bat`, stdio 기본, http/sse는 :8090.

확장자→읽는 경로는 `DOC_KINDS` 한 곳에서 정한다: word=Word COM, excel=Excel COM, pdf=vision_ingest. 어느 경로든 결과는 **같은 모양의 청크 레코드**(`{heading, content, page, image}`)라 검색·임베딩은 원본 종류를 모른다 — 새 형식을 추가할 땐 `extract_chunks`에 분기 하나만 더하면 된다. 옛 `(heading, content)` 튜플도 `replace_file`이 받아 준다(하위 호환).

인덱스는 **원본 경로를 기억한다** — 검색·`read_page`는 저장된 전사문만 쓰므로 원본이 없어도 되지만, 재인덱싱과 `ask_page`의 이미지 재생성은 그 경로를 다시 본다. 문서는 한 폴더(예: `rag_docs/`)에 두고 옮기지 말 것. 폴더 정리(prune)는 **이번에 훑은 폴더 아래로 한정**한다(`remove_missing(under=)`) — 안 그러면 여러 폴더를 따로 인덱싱할 때 뒤에 돌린 폴더가 앞서 넣은 것을 통째로 지운다.

저장: 청크 본문·키워드 인덱스는 sqlite(rag_index.db), 벡터는 **Qdrant 로컬(파일) 모드**(rag_vectors/ — 서버 프로세스 없음, qdrant-client는 사내 미러 등록됨). qdrant-client가 없으면 sqlite BLOB 벡터로 우아하게 저하한다. ⚠ Qdrant 로컬은 단일 프로세스 잠금 — **rag_indexer는 서빙이 잠금을 쥐고 있으면 시작을 거부한다(exit 2)**. 조용히 sqlite로 저하해 인덱싱하면 서빙과 다른 저장소에 벡터가 쌓여 검색이 어긋나기 때문이다. 인덱싱할 때는 서빙을 잠시 내릴 것. llm_studio는 `docs`라는 이름으로 rag_server를 **자동 등록**한다(도구는 `docs__search_docs` 등) — 앱이 떠 있는 동안 잠금을 쥐므로 인덱싱하려면 앱을 끄거나 설정에서 `docs`를 꺼야 한다.

- **문서 읽기는 office_server의 `_document`를 import해 재사용**한다 — Word COM이 여는 것이라 사내 DRM 문서도 읽힌다. 같은 제약(사용자 세션, Windows+Office)을 물려받는다.
- **임베딩은 llama-server `--embeddings`**(기본 `http://127.0.0.1:8001/v1`, EmbeddingGemma 등 GGUF를 CPU `-ngl 0` 상주 권장). **서버가 없으면 키워드 인덱스만 만들고 검색도 키워드 전용으로 우아하게 저하** — 나중에 서버를 켜고 `--reindex`로 돌리면 벡터가 붙는다. 이 저하 경로 덕에 임베딩 모델 반입 전에도 개발·검증이 가능하다.
- 임베딩 모델을 바꿔 벡터 차원이 달라지면 Qdrant 컬렉션을 자동 재생성한다(stderr 경고) — 이후 `--embed-only`로 벡터만 다시 채우면 된다.
- 임베딩 요청은 **배치가 크면 서버가 통째로 거절한다**(`input is too large to process`) — 청크 하나가 500~700토큰이라 몇 개만 묶어도 llama-server 기본 배치(-b/-ub)를 넘는다. `_embed_batched`가 실패하면 배치를 절반씩 줄여 재시도하고(8→4→2→1), 1까지 줄여도 안 되면 사유를 남기고 포기한다. 서버는 `-c 4096 -b 4096 -ub 4096`처럼 크게 잡는 편이 빠르다.
- **임베딩 서버를 나중에 확보했을 때 전체 `--reindex`를 돌리지 말 것** — PDF/Word를 VLM으로 다시 전사하느라 이미 끝낸 판독을 통째로 버린다. `--embed-only`가 문서를 열지 않고 저장된 청크에 벡터만 붙인다(`store.set_vectors`). 프리픽스(`EMBED_QUERY_PREFIX`/`DOC_TEMPLATE`)는 모델마다 달라 `local_settings.py`에서 바꾼다 — 안 맞으면 오류 없이 품질만 떨어진다.
- 키워드 검색은 llm_studio `memory.py`와 같은 패턴(FTS5 trigram + 조사 제거 LIKE 저하)이다 — 한쪽 휴리스틱을 고치면 다른 쪽도 확인할 것.
- **임베딩 서버가 없으면 낱말이 다를 때 아무것도 못 찾는다**(질문 '체결두께' ↔ 문서 '그립'). 키워드 기법으로는 메울 수 없는 간극이라, 그 사이의 통로로 `list_sections`(쪽별 제목 = 목차)를 둔다 — 모델이 목차에서 문서가 실제로 쓰는 용어를 확인하고 다시 검색한다. 검색 0건일 때 응답이 이 도구를 직접 가리킨다.

#### Vision RAG — 표·도면이 많은 PDF (`vision_ingest.py`)

표준품 선정 지침서·스펙 도면·부품 스펙처럼 **표와 그림이 본문인 PDF**는 텍스트 레이어만 뽑으면 내용이 사라진다(표가 좌우 그룹으로 쪼개져 추출 순서가 엉키고, 스캔본은 텍스트가 아예 없다). 그래서 **페이지를 그림으로 렌더링해 VLM에게 전사시키고**, 그 전사문을 기존 청킹·임베딩·검색 파이프라인에 태운다. spec-reader에서 VLM 판독이 실제로 잘 되는 걸 확인한 뒤 그 경로를 RAG 인제스트로 옮긴 것이다.

- **저하 체인(쪽 하나 기준)**: ① VLM 전사 → ② PyMuPDF 텍스트 레이어 → ③ `pdf_server._extract`(DRM PDF를 Word COM으로; 쪽 구분이 없어 `page=0`). 전부 실패하면 예외 대신 그 쪽을 건너뛰고 사유를 알림으로 남긴다. VLM이 아예 없어도 `--no-vlm`으로 텍스트 인덱스 뼈대를 먼저 만들고, 나중에 VLM을 켜고 `--reindex`하면 전사가 붙는다.
- **Word 지침서도 이 경로를 탈 수 있다 — `--word-vision`.** 표·그림이 본문인 문서는 Word COM 텍스트 추출로는 표가 뭉개지고 그림이 통째로 빠진다. 그래서 `_word_as_pdf`가 Word에게 PDF로 내보내게 한 뒤(`ExportAsFixedFormat`, **위치 인자 14개** — pywin32 키워드 함정 회피) 그 PDF를 같은 vision 경로에 태운다. Word가 여는 것이라 DRM 문서도 통과한다. 임시 PDF는 지우고, **페이지 이미지 캐시는 원본 .docx 경로를 키로**(`image_key`) 남긴다 — 임시 파일 이름으로 캐시하면 다음 실행 때 되짚기가 끊긴다. 기본값이 off인 이유: 텍스트 추출이 훨씬 빠르고 정확하며 PyMuPDF·VLM 없이도 되기 때문. 표·그림이 본문인 폴더에만 켤 것.
- **청크 경계 = 쪽 경계.** 쪽 번호와 이미지가 청크마다 정확히 하나로 대응해야 "몇 쪽을 보라"고 답할 수 있다. 한 쪽이 길면 그 쪽 안에서만 나누고 page/image를 물려준다. 전사 첫 줄의 `# 제목`(PAGE_PROMPT가 요구)이 섹션 경로가 된다.
- **되짚어 가는 경로가 핵심이다.** 페이지 이미지를 `rag_pages/`에 남겨 두고, `read_page`(전사 원문 그대로)와 `ask_page`(**그 쪽 그림을 VLM에 다시 보여 주며 질문**)로 검색 결과에서 원본까지 내려갈 수 있게 했다. 텍스트로 한 번 접힌 표를 원본으로 되짚는 이 경로가 없으면 vision RAG는 그냥 OCR RAG다. 캐시가 지워졌으면 `ensure_page_image`가 즉석 렌더링한다.
- **DPI 기본 150 + 긴 변 2000px.** 300 DPI 전면 페이지는 사내 게이트웨이 요청 크기 제한에 걸려 413이 났다(spec-reader 실측). 413이 나면 `--dpi`를 더 낮출 것. Pillow가 없으면 축소를 못 해 DPI로만 조절한다.
- VLM 주소는 **`mcp_server/local_settings.py`**에 적는다(`local_settings.example.py`를 복사). 우선순위는 **CLI > 환경변수 > 이 파일 > 기본값**이고 `settings.py`가 한 곳에서 읽는다. 이 파일이 필요한 진짜 이유는 편의가 아니라 **llm_studio가 자동으로 띄우는 rag_server에는 인자를 줄 수 없다는 것** — 없으면 `ask_page`가 기본값(localhost)을 보고 동작하지 않는다. 저장소가 공개라 사내 주소는 `.gitignore`로 빠지고 예시만 커밋한다(`install_requirements.bat`의 미러 주소, `spec-reader/config.py`와 같은 방침). `--vlm-url`은 `/v1`도 `/v1/chat/completions`도 받는다(`_base_url`) — spec-reader `config.py`가 후자라 그대로 붙여넣는 실수가 잦았다.
- **VLM 연결 실패는 사유를 화면에 남긴다**(`vlm_check`). 사내망은 로그를 반출할 수 없어 화면 한 줄로 원인을 알아야 한다: HTTP 상태 해석(401 인증/404 경로/413 DPI), 프록시 환경변수 경고, 주소 표기 오류 지적. `/models`가 404여도 **실제 chat 요청으로 재확인**한다 — 게이트웨이가 `/models`를 안 열어도 전사는 되기 때문이다.

⚠ **치수를 RAG로 재지 말 것.** 이 경로로 넣은 치수표는 사람이 찾아보는 **참고용**이다. 판독값을 실제 부품 선정에 쓰려면 `spec-reader/read_spec.py` + `verify.py`(L−K_max 계열 상수 등 자동 검증)를 거쳐야 한다 — RAG는 청크 경계에서 숫자가 잘릴 수 있고 판독 검증이 없다. 같은 이유로 **엑셀 표준품 목록의 정확 조회는 `catalog.py`가 엑셀을 직접 읽어서** 한다. RAG에 넣은 엑셀 텍스트는 "이런 계열이 있더라"를 찾는 용도다. 이 분업(RAG=찾기 / 결정론적 함수=고르기)이 `spec-reader/CLAUDE.md`의 "VLM은 판독만, 선정 판단은 결정론적 함수로"와 같은 원칙이다.

### `mcp_server/ansys_server.py` — ANSYS MAPDL 열해석 MCP 서버

PyMAPDL(`ansys-mapdl-core`, gRPC)로 MAPDL을 조종해 열해석(정상상태·과도)을 수행한다. COM이 아니므로 **사용자 세션 제약이 없고** 원격 인스턴스 접속도 된다. 대신 **ANSYS 본체 + 라이선스**가 필요하고, 서버가 MAPDL 세션 하나를 전역으로 상주시킨다(도구 호출은 락으로 직렬화).

- 워크플로: `launch_ansys` → 형상(create_block/cylinder) → `set_thermal_element` → `define_thermal_material`(과도면 밀도·비열 필수) → `mesh_model` → 경계조건(apply_temperature/convection/heat_flux/heat_generation — 면 번호는 `list_areas`) → `solve_steady`/`solve_transient` → `result_*`/`capture_plot`.
- 3티어: 🟢 상태·결과 조회 / 🟡 세션·모델 구축·solve·`run_apdl`(단 /CLEAR·/EXIT는 차단) / 🔴 `clear_model`·`shutdown_ansys`(confirm 게이트).
- `capture_plot`은 pyvista 없이 **MAPDL 자체 렌더러(/SHOW,PNG)**를 쓴다 — 폐쇄망 의도적 선택. 기존 파일은 덮어쓰지 않는다.
- ⚠ 개발 PC에 ANSYS가 없어 APDL 시퀀스·*GET 조회는 실기 미검증 — `⚠ 실기 검증 대상` 주석 위치를 실기에서 확인할 것. `ansys-mapdl-core`는 사내 미러 등록 여부 미확인이라 requirements.txt에 주석으로만 있다.

### `mcp_server/pdf_server.py` — DRM PDF 텍스트 추출 MCP 서버

사내 보안프로그램(DRM)이 암호화한 PDF의 **텍스트를 읽어오는** 읽기 전용(🟢) 서버. 핵심 전제: DRM은 **인증된 SW에만 실시간 복호화**를 해주므로(Python `open`·cmd `copy`는 둘 다 암호화 바이트만 읽힘 — 실측 확인됨), DRM을 우회하는 게 아니라 **인증 앱을 통과**해야 한다. office_server가 Word COM으로 DRM Word 문서를 읽는 것과 같은 원리다. (notepad로 읽어 재구성하는 접근은 불가 — 일반 프로세스엔 복호화를 안 해주고 PDF는 바이너리라 텍스트로 읽으면 손상.)

- **추출 백엔드 3종을 순서대로 시도(우아한 저하 체인)**: ① `direct` — 앞부분이 `%PDF`면(DRM 미적용) pypdf로 바로 추출, 암호화면 조용히 다음으로 ② `word_com` — Word를 백그라운드(DispatchEx)로 띄워 PDF를 열고(Word가 자동 변환) `doc.Content.Text` 추출. **DRM PDF의 실질 경로.** office_server와 같은 제약(사용자 세션·Windows+Word) 상속 ③ `reader_print` — (실험적) Acrobat Reader로 'Microsoft Print to PDF' 인쇄 후 pypdf. 조용한 파일 출력이 환경에 의존해 신뢰도 낮음. 어느 것도 실패하면 예외 대신 안내 문자열.
- 도구(전부 🟢): `read_pdf_text(path, pages, max_chars)`, `read_pdf_metadata(path)`, `pdf_status(path)`. `pypdf`/`pywin32`가 없어도 import에서 죽지 않고 도구가 안내로 저하. http/sse는 :8092.
- **word_com은 hang-safe**다: Word의 'PDF를 편집 가능한 문서로 변환' 확인창은 `DisplayAlerts=0`으로 안 꺼지므로(개발 PC 재현), Open을 데몬 스레드에서 돌리고 워치독이 그 대화상자를 자동 확인하며 `WORD_TIMEOUT`(기본 90초) 초과 시 **우리가 띄운 Word PID만**(생성 전후 차집합) taskkill한다 — 사용자 Word는 건드리지 않고, 막혀도 MCP 서버가 얼지 않는다.
- ⚠ 실기 검증 대상: DRM이 **Word.exe에 .pdf 복호화까지 허용하는지**(확장자 스코프 DRM이면 막힐 수 있음), 대화상자 자동 확인이 실기에서 실제로 통하는지. **서버 없이 `python mcp_server\pdf_server.py --probe <PDF경로>`로 각 백엔드를 진단**할 것. 개발 PC엔 실제 DRM이 없어(nProtect만 상주) word_com의 성공 여부는 사내 PC에서만 확정된다. `pypdf`는 `llm_studio`가 이미 쓰던 것을 루트 requirements.txt에 추가했다.

### `mcp_server/standard_part_server.py` — 항공 표준품 찾기 MCP 서버

"무엇이 필요한가"를 받아 **어느 계열(중분류·도면번호)을 보면 되는지**까지 좁히는 읽기 전용(🟢) 서버. 도구 3개: `find_standard`(요구사항 → 계열 후보 + 지침서 근거) / `list_parts`(계열의 실제 부품번호 조회) / `std_status`(진단). http/sse는 :8094. llm_studio에는 `std`로 등록되지만 **기본 `disabled`** — `STD_CATALOG_PATH`를 설정하기 전에는 도구만 늘기 때문이다.

**일을 위험도로 가른 것이 이 서버의 설계다.** ① 어느 계열을 쓰나 — 틀리면 엉뚱한 표를 보고 사람이 알아챈다 → 여기서 한다. ② 그 계열의 어느 부품번호 — 틀리면 잘못된 부품이 조립되고 못 알아챈다 → **여기서 안 한다**(spec-reader의 판독+`verify.py`가 할 일). 중분류가 60개인데 계열마다 선정 기준이 달라(오링은 홈 치수, 클램프는 튜브 외경, 볼트는 그립) ②를 한꺼번에 만들 수 없다 — ①만 전 계열에 일반화되고, ②는 규칙이 확정되는 계열부터 하나씩 붙인다.

- **계열 후보는 세 경로에서 모으고 근거가 센 것부터 보여준다**: 지침서 본문에 나온 계열 > 요구사항에 영문 계열명이 그대로 있음 > 한국어 낱말 유추(`TERMS` 사전). 순서가 중요하다 — 약한 모델은 맨 위 후보를 잡으므로, '배관' 같은 넓은 낱말로 유추한 것이 지침서가 직접 지시한 계열보다 위에 오면 안 된다. 같은 등급 안에서는 **엑셀에 실제로 있는 계열**을 앞세운다.
- **엑셀에 없는 계열도 후보에서 지우지 않는다.** 조용히 빼면 모델이 '그 계열은 존재하지 않는다'고 잘못 결론짓는다 — '목록에 없음'으로 표시해 뒤로 보낼 뿐이다.
- `TERMS`는 한국어↔영문 계열명 사전이다. 여기 없는 낱말이어도 지침서 본문에서 계열명을 줍는 경로가 따로 있어 못 찾는 게 아니다 — 빠른 길일 뿐이라 새 낱말은 한 줄 추가하면 된다.
- 지침서 검색은 **rag_core를 그대로 재사용**한다(같은 인덱스를 본다 — 따로 만들지 않는다). RAG가 없거나 인덱스가 비어도 죽지 않고 엑셀만으로 후보를 낸다.
- `spec-reader/`는 폴더명에 하이픈이 있어 패키지 import가 안 된다 — 이 파일 기준 절대경로를 `sys.path`에 얹어 `catalog`를 가져온다(앱이 절대경로로 띄워 cwd가 달라도 되게).
- ⚠ **응답에 '부품번호는 정하지 않는다'는 경고를 코드가 직접 박아 넣는다.** 프롬프트로 부탁하는 것보다 세다 — 모델이 요약하며 떨어뜨리지 못한다.

### `mcp_server/intranet_server.py` — 사내 포털(SharePoint) 검색 MCP 서버

사내 SharePoint/그룹웨어를 검색해 읽어오는 읽기 전용(🟢) 서버. 도구 3개: `search_intranet`(검색 API 질의) / `read_intranet_page`(페이지 본문) / `intranet_status`(진단). http/sse는 :8093.

설계 선택 세 가지가 핵심이다:
- **인덱싱이 아니라 실시간 조회.** SharePoint에는 검색 API(`/_api/search/query`)가 내장돼 있어 관련도 랭킹까지 서버가 해 준다 — rag_server처럼 임베딩·인덱싱할 이유가 없다(사내 포털은 자주 바뀌어 인덱스가 금방 낡기도 한다). 나중에 '자주 보는 문서만 인덱싱'을 얹고 싶으면 rag_core를 재사용하면 된다.
- **인증에 새 패키지를 쓰지 않는다.** requests+requests-ntlm은 사내 미러에 없을 위험이 큰데, 이미 의존성인 pywin32의 **WinHTTP COM**(`WinHttp.WinHttpRequest.5.1`)이 NTLM/Negotiate/Basic을 다 처리한다. 계정을 안 주면 `SetAutoLogonPolicy(0)`으로 Windows 통합 인증(SSO)이 된다. office_server가 Word COM으로 DRM 문서를 읽는 것과 같은 발상 — OS가 이미 할 줄 아는 일을 빌려 쓴다. pywin32가 없으면 urllib로 저하한다(인증 없는 사이트만).
- **HTML 파싱도 표준 라이브러리**(`html.parser`)로 한다. beautifulsoup4 불필요. `_TextExtractor`가 script/style/nav/footer를 건너뛰고 표는 셀을 탭으로 이어 한 행이 한 줄이 되게 한다(office_server의 Word 표 처리와 같은 방침). 인라인 태그(`<b>`) 앞뒤 공백을 살리지 않으면 '연차는입사일'처럼 낱말이 붙으니 주의.

- 자격 증명은 **Windows 자격 증명 관리자**(win32cred)에 저장한다 — `--save-credential`. 평문 파일에 두지 않는다. 환경변수 `INTRANET_USER`/`INTRANET_PASSWORD`는 임시 시험용 폴백.
- `read_intranet_page`는 **SITE_URL과 호스트가 다르면 거부**한다 — 인터넷 의존 금지 규약을 코드로 지킨다.
- SSL 검증을 끄는 옵션은 **일부러 두지 않았다.** WinHTTP는 Windows 인증서 저장소를 쓰므로 사내 CA가 GPO로 배포된 도메인 PC에서는 그냥 통과한다. 오류가 나면 CA를 신뢰 저장소에 넣는 게 옳은 해결이다.
- ⚠ 실기 검증 대상: SharePoint 버전별 검색 응답 껍데기(`_search_rows`가 nometadata/verbose 양쪽을 받게 해 뒀다), WinHTTP 인증 협상. **서버 없이 `python mcp_server\intranet_server.py --probe "질의"`로 단계별 진단**할 것.
- llm_studio에는 `intranet`으로 등록되지만 **기본 `disabled`** — 사이트 주소·계정을 설정하기 전에는 도구만 늘기 때문이다.

### `llm_studio/serve_llm.py` — 헤드리스 LLM 서빙

로컬 GGUF 모델을 llama.cpp의 `llama-server`로 띄워 OpenAI 호환 API(`/v1/chat/completions`)를 여는 CLI 스크립트. LangChain·n8n·HTML 페이지 등이 `base_url`만 바꿔 붙는 용도다. 표준 라이브러리만 쓰므로 pip 의존성이 없고, 대신 `llama-server` 실행 파일과 `.gguf`를 별도 반입해야 한다. (LLM 서빙 관련 코드를 한곳에 모으려고 앱과 같은 `llm_studio/`에 둔다.)

같은 폴더의 `llm_studio/server/llama_proc.py`와 **의도적으로 공존한다** (후자가 전자의 로직을 앱 내장 클래스로 옮긴 것). 갈라진 지점:

| | `serve_llm.py` | `llama_proc.py` |
|---|---|---|
| 형태 | CLI 스크립트 | 앱 내장 클래스 (`start`/`stop`/`restart`) |
| 바인딩 | `--host`로 `0.0.0.0` 공개 가능 | 항상 `127.0.0.1` (외부 공개는 UI 서버 담당) |
| **`--jinja`** | **없음 → 도구 호출 불가** | 항상 켬 |
| 동시 슬롯 | `--parallel`(`-np`) 지원 | 없음 (슬롯 1, 순차 처리) |

**`--jinja` 차이가 중요하다.** 이 플래그가 chat template 기반 function calling을 켠다. `serve_llm.py`로 띄운 서버에 도구 호출을 붙이면 동작하지 않으므로, 도구가 필요하면 `--extra --jinja`로 넘기거나 llm_studio를 쓸 것.

Gemma 3 계열 권장 샘플링(`GEMMA_SAMPLING`: temp 1.0 / top-p 0.95 / top-k 64 / min-p 0.0)은 양쪽에 중복 정의돼 있다. 직접 서빙에서는 이 값이 응답 품질을 좌우하므로 서버 기본값에 맡기지 않는다 — 한쪽을 바꾸면 다른 쪽도 확인할 것.

### `llm_studio/` — 폐쇄망용 올인원 로컬 LLM 앱

FastAPI 서버 + 브라우저 채팅 UI + llama-server 프로세스 관리를 하나로 묶은 앱. 스트리밍 채팅, 대화 기록, 파일 첨부, MCP 도구 연결, 외부 LLM(OpenAI 호환) 전환을 지원한다. 자세한 건 `llm_studio/README.md` (폐쇄망 반입 체크리스트 포함).

- **저장 위치 원칙**: 대화기록·설정·API 키·첨부는 **전부 서버 쪽 데이터 폴더**(`C:\ProgramData\LocalLLMStudio`, 권한 없으면 `%LOCALAPPDATA%`로 폴백)에 파일로 저장한다. **브라우저 저장소(localStorage/쿠키/IndexedDB)는 일절 쓰지 않는다** — 보안 프로그램이 브라우저 데이터를 지워도 아무것도 잃지 않게 하기 위한 의도적 설계다. 이 원칙을 깨지 말 것.
- **실행 경로 두 가지 모두 유지한다**:
  - 소스 직접 실행 — `pip install -r llm_studio/requirements.txt && python app.py` (`--mock`으로 모델 없이 UI만 확인 가능). 폐쇄망에 미러가 있으니 이쪽이 기본.
  - exe 배포 — `build_exe.bat`(PyInstaller) → `installer.iss`(Inno Setup) → `Setup.exe` 하나로 앱+llama-server 반입. 빌드는 개인 PC에서 한다.
- **MCP 클라이언트** (`server/mcp_client.py`) — 데이터 폴더의 `mcp_servers.json`을 Claude Desktop과 같은 `mcpServers` 규격으로 읽는다. `url`→streamable_http, `command`→stdio. 서버마다 전용 워커 태스크를 두고 큐로 요청을 전달하는데, 이건 MCP 세션을 **열었던 태스크에서 닫아야 한다는 anyio cancel scope 제약** 때문이다 — 구조를 단순화하려다 이 제약을 깨지 말 것. 연결 실패한 서버는 비활성 표시만 하고 나머지로 계속 동작한다(기동을 30초까지만 기다린다 — 서버 하나가 멈춰 앱 자체가 못 뜨는 걸 막는다). 도구 이름은 `<서버이름>__<도구이름>`으로 모델에 노출된다.
- **번들 MCP 서버 자동 등록** (`server/config.py`의 `MCP_BUNDLED_SERVERS`/`default_mcp_config`) — `mcp_servers.json`이 **없거나 서버가 하나도 없을 때만** 같은 저장소의 `mcp_server/*.py`를 stdio(`command`)로 등록해 써 넣는다. 그래서 `run_*.bat`을 따로 열지 않아도 앱만 켜면 도구가 붙는다(앱이 서버의 부모 프로세스라 앱을 끄면 같이 정리된다). office/outlook/docs/pdf는 켜고 catia/ansys는 `disabled`로 둔다 — 제품이 없는 PC에서 도구 목록만 늘리면 약한 모델의 도구 선택이 나빠지기 때문이다. **이미 사용자가 등록해 둔 설정은 절대 덮어쓰지 않는다**(깨진 JSON도 보존); HTTP로 쓰던 사람이 옮겨오는 건 설정 UI의 [번들 서버 자동 설정] 버튼(`GET /api/mcp/default-config`)이 담당하고, 저장은 사용자가 누른다. 스크립트는 절대경로로, `--transport stdio`를 명시해 적는다(자식의 cwd가 앱 쪽이고, 예전 bat 운용으로 남은 `*_MCP_TRANSPORT=http` 환경변수가 기본값을 뒤집을 수 있어서). 서버를 띄울 파이썬은 bat과 같은 규약으로 루트 공용 `venv`를 먼저 찾는다. bat들은 n8n 등 외부 클라이언트용 HTTP 경로로 계속 유지한다.
- **모델 서빙은 앱 시작과 분리돼 있다.** `app.py`는 llama-server를 자동으로 띄우지 않고 **유휴 상태로 뜬다**. 사용자가 UI(헤더 아래 셋업 바 / 설정 → 로컬 LLM 서버)에서 GGUF와 옵션을 골라 `POST /api/server/start`로 서빙을 켜고 `/stop`·`/restart`로 관리한다. `state.mock`은 이제 **오직 `--mock`**일 때만 참이다(개발용 canned 응답) — "모델 없음"과 혼용하지 말 것. 로컬을 골랐는데 서빙 중이 아니면 `/api/chat`이 실제 호출 대신 안내 error 이벤트를 흘린다. `config.autostart_local`(기본 False)을 켜거나 `--llama-url`을 주면 시작 시 자동 서빙하되, **실패해도 목 모드로 떨어지지 않고 유휴로 뜬다**.
- **인증이 없다 — 로컬/원격 구분이 유일한 방어선.** `--host 0.0.0.0`으로 열면 같은 망의 누구나 UI에 닿는다. 그래서 **이 PC를 건드리는 API는 `_require_local`로 막는다**: MCP 설정 쓰기(`command`가 그대로 실행되므로 원격 쓰기 = 임의 명령 실행), 서버 시작/중지/재시작, 앱 종료, 파일 선택 대화상자, 승인 응답, 장기 기억 조회·삭제. 설정 쓰기는 원격이면 `REMOTE_SETTABLE_KEYS`(현재 `active_provider`뿐) 안에서만 허용한다 — 원격에서 `approval_enabled`를 끄면 승인 게이트가 통째로 무너지기 때문이다. 판정은 소켓 상대 주소로만 하고 `X-Forwarded-For`는 **의도적으로 무시한다**(헤더는 위조된다). 새 엔드포인트를 추가할 때 이 분류를 반드시 따를 것. API 키는 `GET /api/settings`에서 `API_KEY_PLACEHOLDER`로 가려 내보내고, 그 표식이 그대로 돌아오면 저장된 키를 유지한다(`restore_api_keys`) — 마스킹만 하고 이 복원을 빼면 설정을 저장할 때마다 키가 지워진다.
- **컨텍스트 관리** (`server/context.py`) — 이 앱은 매 턴 이력 전체를 보내는데 도구 결과 하나가 최대 2만 자(`TOOL_RESULT_MAX`)라, 두면 `ctx`가 차고 **앞쪽의 system 프롬프트가 밀려나 조용히 망가진다**. `age_messages`가 오래된 `role:tool` 메시지를 앞부분만 남기고 접는다 — **접는 건 모델에 보내는 사본뿐이고 `conv["messages"]` 원문은 절대 안 건드린다**(대화를 다시 열면 전부 있어야 하고, 접힌 자리의 안내를 보고 모델이 도구를 다시 부를 수 있어야 한다). `pick_tool_servers`는 질문·첨부로 서버를 좁히되 **애매하면 None(전체)을 돌려준다** — 잘못 좁혀 "못 한다"고 답하는 게 도구가 많은 것보다 나쁘다. 스코프 낱말(`DEFAULT_TOOL_SCOPE_KEYWORDS`)에 '문서'·'검색' 같은 일반어를 넣지 말 것: 거의 모든 질문이 한 서버에 걸려 나머지 도구가 사라진다. `slim_tools`는 도구 설명을 첫 문단만 남겨 스키마를 줄이되 이름·`required`·`type`·`default`·`enum`은 건드리지 않는다 — 이건 매 요청 고정 비용을 깎는 것이고(이력 접기는 과거를 깎는다), 컨텍스트가 빠듯한 사내 게이트웨이(vLLM 등) 대응이다. ⚠ 측정해 보면 **설명은 스키마의 32%뿐이고 64%가 JSON 구조**라 다이어트만으로는 13%밖에 안 줄어든다 — 도구 개수를 줄이는 게 유일한 큰 지렛대다(67개 12.9k토큰 → 스코프 적중 시 30개 5.1k토큰). 못 정했을 때 전체 대신 기본 세트만 쓰려면 `tool_scope_fallback`.
- **작업 대장 = 중기 기억** (`server/worklog.py`) — 단기(메시지 이력)와 장기(`memory.db`) 사이의 층. 이 대화에서 **무엇을 읽고 무엇을 고쳤는지**를 `conv["worklog"]`에 쌓아 매 턴 system에 결정적으로 주입한다(회상을 모델의 도구 호출에 맡기지 않는 건 memory.py와 같은 방침). 노리는 건 둘: 몇 턴 전에 읽은 걸 또 읽는 낭비, 그리고 **Excel 쓰기가 메모리만 바꾸는데 "저장했다"고 답하는 환각**(🟡/🔴 분리가 모델에겐 안 보인다). 기록은 `agent.run_chat`의 `observer` 훅에서 하는데, 일반 채팅과 작업 모드가 그 지점에서 만나기 때문이다 — 이벤트 스트림 쪽에 두면 planner가 도구 이벤트를 step_token으로 감싸서 놓친다. **실행된 호출만** 기록할 것(거절된 걸 넣으면 대장이 하지 않은 일을 했다고 거짓말한다). 같은 대상·동작·범위는 갱신하되 **`saved`는 물려받지 않는다** — 저장 후 같은 셀을 또 고치면 그건 다시 미저장이다.
- **위험 도구 승인 게이트** (`server/approvals.py`) — 모델이 `confirm=true` 인자로 도구를 부르거나 config `approval_tools`에 오른 도구를 부르면, 실행 전에 SSE `approval_request` 이벤트로 브라우저에 승인/거절 버튼을 띄우고 `POST /api/chat/approve` 응답을 기다린다(시간 초과·거절이면 실행하지 않고 그 사실을 도구 결과로 모델에 알림). 상태는 전부 RAM(asyncio Future) — 저장 위치 원칙과 무관. **MCP 서버 쪽 confirm 게이트와 이중 안전장치**로, 모델이 사용자에게 묻지 않고 스스로 confirm=true를 넣는 사고를 막는다. `approval_enabled`로 켜고 끈다(기본 켬).
- llama-server는 `--jinja`로 실행돼 Gemma의 chat template 기반 함수 호출을 쓴다. Gemma는 공식 tool-use 학습이 약한 편이라 도구 호출 정확도가 모델에 따라 갈린다.
- 외부 LLM 프리셋(OpenAI/Anthropic/Gemini)은 폐쇄망에선 쓸 수 없다. 그 자리에 **사내 프록시/게이트웨이 주소를 등록하는 용도**로 남겨둔 것이다.

### `spec-reader/` — 항공 표준품 스펙 판독 모듈 (별도 프로젝트에서 합류)

항공 가스터빈 체결용 표준품(볼트/너트/워셔) **선정 에이전트**의 1단계. 스펙 PDF의 치수표를 Gemma VLM(사내 호스팅, OpenAI 호환 `/v1/chat/completions`)으로 판독하고 **결정론적 규칙으로 자동 검증**한다. 원래 독립 저장소였고 커밋 5개 분량의 이력은 원본 zip에 있다(여기엔 파일만 합쳤다).

**이 폴더는 아직 MCP 서버가 아니다** — CLI(`read_spec.py`)다. 에이전트 도구로 만드는 게 다음 작업이고, 그때 `mcp_server/` 규약(3티어·우아한 저하·stdio stdout 금지)을 따라 감싸면 된다.

**Vision RAG와의 분업**(위 rag 절 참고): 지침서·스펙을 통째로 RAG에 넣어 "어디에 뭐라고 쓰여 있나"를 찾는 것은 `vision_ingest.py`가, **판독값을 검증해 부품을 고르는 것**은 여기가 한다. 같은 PDF가 양쪽에 들어가지만 쓰임이 다르다 — RAG 결과를 치수 근거로 삼지 말 것.

핵심 원칙 (항공 부품이라 타협 불가 — `spec-reader/CLAUDE.md`·`HANDOFF.md`에 상세):
- **VLM은 판독만, 선정 판단은 결정론적 함수로.** LLM이 부품을 "고르면" 환각이 곧 비행 안전 문제가 된다.
- **판독값은 자동 검증 통과 후에만 쓴다.** 검증 규칙은 데이터에서 찾은 불변식이다 — `L − K_max`가 계열 상수(MS9555=0.578, MS9556=0.630), `L`이 1/16″ 격자 위, dash 중복·누락 없음. 새 표준을 추가할 땐 **그 계열의 불변식을 먼저 찾을 것.**
- **모르면 모른다고 출력.** "MS9555에 맞는 너트가 없다"를 잡아낸 것이 이 시스템의 가치였다.
- **출력은 짧게.** 사내망에서 파일 반출이 안 돼 사람이 눈으로 보고 구두로 옮긴다.

구조: `read_spec.py`(CLI: preview/read/meta) · `verify.py`(검증 — VLM 없이 동작해 외부망에서 개발 가능) · `merge.py`(다중 페이지 병합 — 행 분할/컬럼 분할 둘 다) · `prompts.py`(판독이 안 맞으면 여기만 고친다) · `selftest.py`(fixture 회귀 — 오독 4종 주입 검출) · `fixtures/MS9555.json`(공개 표준 정답 29행).

⚠ 제약이 이 저장소와 다르다: 사내 PC **파이썬 3.10**(휠은 `cp310`+`win_amd64`를 받아야 한다 — 다만 `HANDOFF.md`의 'Pillow는 10.4.0이 상한'은 사실이 아니다: Pillow 12에도 cp310 휠이 있다), VLM 요청 크기 제한으로 300 DPI 전체 페이지는 413 → `--dpi 150` 또는 `--crop` 필요. `config.py`(사내 URL/모델명)는 `.gitignore`에 있고 `config.example.py`를 복사해 채운다.

### `Examples/` — 강의 자료 (추적 안 됨)

LangChain/LangGraph 한국어 코스. 번호순 랩 노트북(01 tool calling → 11 multi-agent)과 그 결과물인 `.py` 모듈들(에이전트 팩토리 `build_agent.py`, Slack/Discord 봇 하니스, MCP 서버들, skills/memory/summarization 확장). 모든 코드·주석·프롬프트가 한국어다.

여기 있는 패턴 몇 가지가 루트 코드의 설계 배경이다:
- `build_agent.py`의 `VLLM_DECODING` ↔ `serve_llm.py`의 `GEMMA_SAMPLING` (자체 서빙은 샘플링을 명시한다는 같은 취지)
- `HumanInTheLoopMiddleware`의 `INTERRUPT_ON` ↔ `outlook_server.py`의 `confirm` 게이트
- `test_tools.py` ↔ `test_outlook.py` (수동 스모크 테스트 형식)

빌드 시스템·패키지 매니페스트·테스트 스위트가 없다. Jupyter로 대화식 실행하거나 스크립트를 직접 돌린다. 자세한 실행법은 `Examples/` 안의 노트북과 `.md` 파일들을 볼 것.

## 실행

```bash
# MCP 서버 (사용자 세션에서, Office/Outlook이 켜진 상태로)
pip install -r requirements.txt
python mcp_server\office_server.py                    # stdio
python mcp_server\office_server.py --transport http   # n8n용, :8087
python mcp_server\outlook_server.py --transport http  # n8n용, :8088 (catia :8089, rag :8090, ansys :8091, pdf :8092)
python mcp_server\test_outlook.py                     # 읽기 전용 스모크 테스트
mcp_server\run_office_server.bat                      # 위 http 실행의 더블클릭용 (서버별, mcp_server 안)
mcp_server\run_rag_indexer.bat ..\rag_docs            # RAG 인덱스 구성 (rag_docs 투입, 서빙은 내리고 실행)
python mcp_server\rag_indexer.py C:\specs --vlm-url http://<사내VLM>/v1   # PDF를 VLM으로 전사해 인덱싱
python mcp_server\vision_ingest.py --probe C:\specs\MS9555.pdf --page 3    # 그 쪽만 전사해 진단

# 로컬 LLM
python llm_studio\serve_llm.py --model C:/models/gemma-12b-it-qat.gguf
cd llm_studio && python app.py             # 유휴로 시작 — UI에서 모델을 골라 서빙 (run_app.bat 동일)
cd llm_studio && python app.py --mock      # 모델 없이 UI 확인 (목 응답)
```

린트/테스트 커맨드가 따로 없다. 변경은 해당 스크립트를 직접 돌려서 검증한다.

## 전반적 규약

- **모든 코드·주석·docstring·프롬프트는 한국어로 쓴다.** (예외: `.bat`은 전부 영어 ASCII — cmd 인코딩 문제로 한글이 깨진다. 위 mcp_server 절 참고.)
- **예외보다 우아한 저하** — 라이브러리 없음 → 안내 메시지 반환하는 스텁, MCP 서버 연결 실패 → 그 도구만 비활성, 확장 모듈 로드 실패 → 그것만 건너뜀. 선택적/외부 설정 때문에 프로세스가 죽는 경로를 만들지 말 것.
- **stdio 트랜스포트에서 stdout은 MCP 프로토콜 채널이다.** 로그는 반드시 stderr로 보낼 것 (`print(..., file=sys.stderr)`).
- **Windows 전제** — COM 서버들은 Windows + Office 없이는 의미가 없다. pywin32는 `sys_platform == "win32"` 마커로 걸려 있다.
- **requirements.txt는 UTF-8 BOM 포함으로 유지할 것** — 한국어 주석이 있는데 구버전 pip는 BOM이 없으면 로케일(cp949)로 읽어 `UnicodeDecodeError`가 난다. 파일을 다시 쓸 때 BOM을 떨어뜨리지 말 것. (파이썬 코드의 파일 I/O는 항상 `encoding="utf-8"` 명시 — 이미 전부 그렇게 돼 있다.)
- TLS/CA 번들: 외부 HTTPS를 호출하는 모듈은 네트워킹 라이브러리 import 전에 `SSL_CERT_FILE`을 `certifi.where()`로 설정한다 (사내망의 비표준 CA 체인 대응). 폐쇄망 코드에는 해당 없음.
