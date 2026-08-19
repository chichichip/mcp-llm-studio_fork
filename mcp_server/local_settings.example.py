# -*- coding: utf-8 -*-
"""local_settings.py 로 **복사해서** 사내 값을 채운다.
   (이 예시 파일은 저장소에 있고, local_settings.py 는 .gitignore 로 빠진다.)

       copy mcp_server\\local_settings.example.py mcp_server\\local_settings.py

⚠ 저장소는 공개다. 사내 호스트·IP·계정을 **local_settings.py 밖으로 내보내지 말 것.**
   (install_requirements.bat 의 미러 주소, spec-reader/config.py 와 같은 방침)

여기 적어 두면 CLI 인자 없이도 인덱서·서버가 같은 주소를 본다. 특히 **llm_studio가
자동으로 띄우는 rag_server에는 인자를 줄 수 없어**, ask_page(원본 쪽 VLM 재판독)를
쓰려면 이 파일이 필요하다.

우선순위: CLI 인자 > 환경변수 > 이 파일 > 코드 기본값
"""

# ─────────────────────────── VLM (PDF 쪽 전사 / ask_page 재판독) ───────────────────────────
# `/v1` 까지만 적어도 되고, spec-reader/config.py 처럼 `/v1/chat/completions` 전체를
# 붙여넣어도 코드가 알아서 잘라 쓴다.
RAG_VLM_URL = "http://<사내VLM주소>/v1"
RAG_VLM_MODEL = "<모델명>"

# 게이트웨이가 인증을 요구할 때만. 없으면 빈 문자열로 두거나 지운다.
RAG_VLM_API_KEY = ""

# 페이지 렌더링 해상도. 413(요청 과대)이 나면 낮춘다. 표가 흐리면 조금 올린다.
RAG_VLM_DPI = "150"

# ─────────────────────────── 임베딩 / 리랭커 (의미 검색) ───────────────────────────
# 없으면 키워드 검색만으로 우아하게 저하한다 — 비워 둬도 RAG는 동작하지만, 질문과
# 문서의 낱말이 다르면 못 찾는다("체결두께" ↔ "그립").
# 사내 게이트웨이가 임베딩도 서빙한다면 VLM과 같은 주소를 적으면 된다.
RAG_EMBED_URL = "http://127.0.0.1:8001/v1"
RAG_RERANK_URL = "http://127.0.0.1:8002/v1"

# 임베딩 요청에 실을 모델 이름. llama-server는 무시하지만 **사내 게이트웨이(vLLM 등)는
# 등록된 이름이 아니면 400으로 거절한다** — /v1/models 에 뜨는 이름을 그대로 적을 것.
RAG_EMBED_MODEL = "embedding"
# 게이트웨이가 인증을 요구할 때만.
RAG_EMBED_API_KEY = ""

# ⚠ 임베딩 모델마다 **질의/문서에 붙이는 프리픽스 형식이 다르다.** 안 맞으면 오류 없이
#    검색 품질만 조용히 떨어진다. 쓰는 모델에 맞춰 아래 둘을 고칠 것.
#
#   EmbeddingGemma (기본값 — 아무것도 안 적으면 이 형식):
#       RAG_EMBED_QUERY_PREFIX = "task: search result | query: "
#       RAG_EMBED_DOC_TEMPLATE = "title: {title} | text: {text}"
#   bge-m3 / bge-large (프리픽스 없음):
#       RAG_EMBED_QUERY_PREFIX = ""
#       RAG_EMBED_DOC_TEMPLATE = "{text}"
#   multilingual-e5:
#       RAG_EMBED_QUERY_PREFIX = "query: "
#       RAG_EMBED_DOC_TEMPLATE = "passage: {text}"
#
# RAG_EMBED_QUERY_PREFIX = ""
# RAG_EMBED_DOC_TEMPLATE = "{text}"

# ─────────────────────────── 검색 결과 크기 (컨텍스트가 빠듯할 때) ───────────────────────────
# 매칭 청크에 붙일 이웃 청크 수(앞뒤 각각). 근거가 청크 경계에서 잘리는 걸 막지만
# 그만큼 결과가 길어진다. 사내 게이트웨이 컨텍스트가 빠듯하면 0으로 줄인다.
# RAG_CONTEXT_WINDOW = "1"

# 엑셀 한 시트에서 인덱싱할 최대 행 수.
# RAG_EXCEL_MAX_ROWS = "20000"

# 임베딩 요청 한 번에 보낼 청크 수. 서버가 거절하면 코드가 절반씩 줄여 재시도하지만,
# llama-server를 -b/-ub 작게 띄웠다면 처음부터 낮춰 두는 편이 빠르다.
# RAG_EMBED_BATCH = "8"

# ─────────────────────────── 표준품 찾기 (standard_part_server) ───────────────────────────
# 엔진 적용 표준품 목록 엑셀. 없으면 계열 후보는 나오지만 도면번호가 안 붙는다.
STD_CATALOG_PATH = r"C:\경로\엔진 적용 표준품 목록.xlsx"
# 스펙 도면 PDF 폴더. 도구의 spec_dir 인자로 그때그때 넘겨도 되고, 여기 적어 두면
# 인자 없이도 쓴다(하위 폴더까지 훑는다). 파일 이름은 도면번호든 품명이든 상관없다 —
# 둘 다로 찾아본다.
STD_SPEC_DIR = r"C:\경로\스펙"
# 판독 결과 캐시 위치. VLM 판독은 쪽당 수 초라 같은 도면을 두 번 읽지 않게 저장한다.
# STD_SPEC_CACHE = r"C:\ProgramData\LocalLLMStudio\spec_cache"
# 표를 찾을 때 앞에서부터 훑을 쪽 수(치수표는 대개 앞쪽에 있다).
# STD_SPEC_MAX_PAGES = "6"

# ─────────────────────────── 인덱스 저장 위치 (보통 그대로 둔다) ───────────────────────────
# RAG_DB_PATH = r"C:\ProgramData\LocalLLMStudio\rag_index.db"
# RAG_QDRANT_PATH = r"C:\ProgramData\LocalLLMStudio\rag_vectors"
# RAG_PAGE_IMAGES = r"C:\ProgramData\LocalLLMStudio\rag_pages"
