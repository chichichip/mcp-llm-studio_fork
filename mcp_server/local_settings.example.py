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
# 없으면 키워드 검색만으로 우아하게 저하한다 — 비워 둬도 RAG는 동작한다.
# 사내 게이트웨이가 임베딩도 서빙한다면 VLM과 같은 주소를 적으면 된다.
RAG_EMBED_URL = "http://127.0.0.1:8001/v1"
RAG_RERANK_URL = "http://127.0.0.1:8002/v1"

# ─────────────────────────── 인덱스 저장 위치 (보통 그대로 둔다) ───────────────────────────
# RAG_DB_PATH = r"C:\ProgramData\LocalLLMStudio\rag_index.db"
# RAG_QDRANT_PATH = r"C:\ProgramData\LocalLLMStudio\rag_vectors"
# RAG_PAGE_IMAGES = r"C:\ProgramData\LocalLLMStudio\rag_pages"
