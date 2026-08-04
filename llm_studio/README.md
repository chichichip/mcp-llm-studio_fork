# LocalLLM Studio

자체 호스팅 로컬 LLM 채팅 앱 — 브라우저 UI + 스트리밍 + MCP 도구 호출 + 외부 API 전환을
하나로 묶은 올인원. 원래 인터넷 없는 폐쇄망 Windows PC용으로 만들었지만 로컬 어디서나 돈다.

## 빠른 시작 (Windows + Python 3.10+)

```bat
pip install -r requirements.txt
python app.py            :: 유휴로 시작 → 브라우저 UI에서 GGUF 모델을 골라 서빙
python app.py --mock     :: 모델 없이 UI만 둘러보기 (목 응답)
```

> **llama-server 실행 파일과 `.gguf` 모델은 이 저장소에 포함돼 있지 않다**(용량 때문).
> llama.cpp의 `llama-server`를 따로 받아 `llama/` 폴더에 두거나 설정에서 경로를 지정하고,
> GGUF 모델은 로컬 어디든 두고 UI에서 고르면 된다. 모델·바이너리 없이도 `--mock`으로 UI는 확인된다.

아래는 원래 폐쇄망 반입을 전제로 쓴 상세 문서다.

---

폐쇄망 Windows PC에서 로컬 LLM(Gemma 12B IT QAT 등 GGUF 모델)을 서빙하고,
브라우저 채팅 UI로 사용하는 올인원 앱.

- **앱 시작과 모델 서빙이 분리됨** — 앱은 아무 모델도 서빙하지 않은 유휴 상태로 뜨고,
  UI에서 GGUF 모델과 옵션을 골라 서빙을 시작/중지한다 (외부 API도 그대로 선택 가능)
- 스트리밍 채팅 (마크다운 렌더링, 응답 중단)
- 대화 기록 저장/불러오기/이름변경/삭제
- 파일 첨부 질의 (텍스트/PDF/DOCX)
- 설정 화면 (시스템 프롬프트, 생성 파라미터, 로컬 서버 설정)
- **MCP 도구 연결** — mcp_servers.json에 등록하면 모델이 도구를 호출
- **외부 LLM 연결** — OpenAI/Claude/Gemini 등 OpenAI 호환 API를 키로 등록하고
  화면 상단 드롭다운으로 로컬 모델과 전환

> **저장 위치 원칙**: 대화기록·설정·API 키·첨부파일은 전부 서버 쪽 데이터 폴더
> (`C:\ProgramData\LocalLLMStudio`)의 파일로 저장된다. 브라우저 저장소
> (localStorage/쿠키/IndexedDB)는 일절 쓰지 않으므로, 보안 프로그램이 브라우저
> 데이터를 지워도 아무 것도 잃지 않는다.

## 구조

```
LocalLLMStudio.exe (통합 런처)
  ├─ 웹서버 실행 (기본 127.0.0.1:8080 — UI + API + 도구 호출 루프)
  ├─ 브라우저 자동 오픈
  └─ (유휴로 시작) UI에서 [서빙 시작] → llama-server 실행 (127.0.0.1:8000, 내부 전용)
        · '앱 시작 시 자동 서빙'을 켜두면 마지막 설정으로 자동 시작

C:\ProgramData\LocalLLMStudio\    ← 인스톨러가 Users 쓰기 권한 부여
  ├─ models\            GGUF 모델을 여기에 넣는다
  ├─ conversations\     대화 기록 (JSON)
  ├─ uploads\           첨부 파일
  ├─ logs\              llama-server 로그
  ├─ config.json        설정
  └─ mcp_servers.json   MCP 서버 등록
```

## 개발 실행 (소스로)

```bash
cd llm_studio
pip install -r requirements.txt
python app.py --mock        # 모델 없이 UI 확인 (목 응답)
python app.py               # 유휴로 시작 — UI에서 모델을 골라 [서빙 시작]
python app.py --host 0.0.0.0  # 같은 망의 다른 PC에서 접속 허용
python app.py --llama-url http://127.0.0.1:8000  # 시작 시 따로 띄운 서버에 붙기
```

앱은 기본적으로 아무 로컬 모델도 서빙하지 않고 뜬다. 헤더 아래 셋업 바 또는
설정(⚙) → "로컬 LLM 서버"에서 GGUF를 고르고 [서빙 시작]을 누른다. 마지막 설정은
`config.json`에서 그대로 불러와 미리 채워진다. '앱 시작 시 자동 서빙'을 켜두면
다음 실행부터 그 설정으로 자동 시작한다(실패해도 유휴로 뜬다).

개발 중에는 ProgramData에 쓰기 권한이 없으면 `%LOCALAPPDATA%\LocalLLMStudio`를 자동 사용한다.

## exe 빌드 → 인스톨러

```bash
pip install pyinstaller
# 1) llama.cpp 릴리스에서 llama-bXXXX-bin-win-cuda-x64.zip을 받아
#    llm_studio\llama\ 폴더에 풀어둔다 (llama-server.exe + DLL들)
build_exe.bat               # → dist\LocalLLMStudio\
# 2) Inno Setup 6 설치 후 installer.iss 컴파일 → Output\LocalLLMStudio-Setup-1.0.0.exe
```

## 폐쇄망 반입 체크리스트

| 반입물 | 비고 |
|---|---|
| `LocalLLMStudio-Setup-x.x.x.exe` | 인스톨러 하나에 앱+llama-server 포함 |
| Gemma 12B IT QAT `.gguf` | 설치 후 `C:\ProgramData\LocalLLMStudio\models\`에 복사 |
| NVIDIA 드라이버 | 서버 PC에 설치돼 있어야 함 (CUDA 툴킷은 불필요, DLL 동봉됨) |

설치 → models에 GGUF 복사 → 바탕화면 아이콘 실행 → 브라우저가 자동으로 열림.

## MCP 서버 등록

### 같은 저장소의 서버는 자동으로 붙는다 (bat 실행 불필요)

**처음 실행할 때** `mcp_servers.json`이 없으면, 같은 저장소의 `mcp_server/` 서버들을
앱이 **자식 프로세스(stdio)로 직접 띄우도록** 자동 등록한다. `run_office_server.bat`
같은 걸 따로 열어 둘 필요가 없다 — 앱만 켜면 도구가 붙고, 앱을 끄면 같이 정리된다.

| 등록 이름 | 서버 | 기본 |
|---|---|---|
| `office` | office_server.py | 켬 |
| `outlook` | outlook_server.py | 켬 |
| `docs` | rag_server.py | 켬 |
| `pdf` | pdf_server.py | 켬 |
| `catia` | catia_server.py | **끔** (CATIA 설치 PC에서만 켤 것) |
| `ansys` | ansys_server.py | **끔** (ANSYS 설치 PC에서만 켤 것) |

- CATIA/ANSYS를 기본으로 꺼 두는 이유: 그 제품이 없는 PC에서는 도구 목록만 길어지고,
  도구가 많아질수록 약한 로컬 모델의 도구 선택 정확도가 떨어진다. 설정 → MCP에서 켠다.
- 서버를 띄우는 파이썬은 **루트 공용 `venv\Scripts\python.exe`**를 먼저 찾고, 없으면
  지금 앱을 돌리는 파이썬을 쓴다 (bat들의 `..\venv` 규약과 같다).
- COM 서버(office/outlook)의 '사용자 로그인 세션' 제약은 그대로 지켜진다 — 앱이 그
  세션에서 돌고 자식 프로세스가 세션을 물려받기 때문이다.
- ⚠ `docs`(rag_server)가 떠 있는 동안은 Qdrant 잠금 때문에 `run_rag_indexer.bat`이
  시작을 거부한다. 인덱싱할 때는 앱을 잠시 끄거나 설정에서 `docs`를 꺼 둘 것.

**이미 쓰던 설정이 있으면 자동 등록은 개입하지 않는다.** HTTP 주소(`url`)로 등록해
쓰던 사람이 옮겨오려면 설정(⚙) → MCP → **[🔌 번들 서버 자동 설정]**을 누르면 편집기에
자동 설정이 채워진다. 확인한 뒤 [MCP 저장 + 재연결]을 눌러야 실제로 바뀐다.

### 직접 등록

설정(⚙) → MCP 항목에서 편집하거나 `mcp_servers.json`을 직접 수정:

```json
{
  "mcpServers": {
    "사내검색": { "url": "http://10.x.x.x:8082/mcp" },
    "파일도구": { "command": "python", "args": ["C:/tools/file_server.py"] },
    "꺼둔서버": { "url": "http://...", "disabled": true }
  }
}
```

- `url` → streamable_http, `command` → stdio 방식으로 연결
- 연결 실패한 서버는 건너뛰고 나머지로 동작 (앱이 죽지 않음). 30초 안에 응답하지
  않는 서버도 그 서버만 포기하고 넘어간다 — 하나가 멈춰서 앱이 못 뜨는 일은 없다.
- 도구 이름은 `서버이름__도구이름`으로 모델에 노출됨
- n8n 등 **다른 클라이언트**가 붙어야 하면 그때는 여전히 `run_*.bat`으로 HTTP 서버를
  띄운다 (bat은 그 용도로 남아 있다). 같은 서버를 앱과 n8n이 동시에 쓸 일이 있으면
  bat으로 HTTP를 띄우고 앱에는 `url`로 등록하는 편이 낫다.

## 컨텍스트와 기억 관리

기억을 세 층으로 나눠 다룬다. MCP 도구를 많이 쓸수록 이 관리가 응답 품질을 좌우한다.

| 층 | 범위 | 어디에 |
|---|---|---|
| 단기 | 이번 턴의 도구 결과 | 메시지 이력 (오래된 건 접어서 보낸다) |
| 중기 | **이 대화에서 읽고 고친 문서** | 대화 JSON의 `worklog` |
| 장기 | 대화를 넘는 사용자·프로젝트 사실 | `memory.db` |

### 이력 접기 (`context_aging_enabled`)

도구 결과는 하나가 최대 2만 자까지 이력에 남고 매 턴 다시 보내진다. 그냥 두면
`read_excel_range` 몇 번에 `ctx`(기본 32768)가 차고, 넘치는 순간 앞부분 — 즉 system
프롬프트와 초반 지시 — 이 조용히 밀려나 대화가 망가진다.

그래서 **모델에 보내는 사본에서만** 오래된 도구 결과를 앞부분만 남기고 접는다.
대화 기록 원문은 그대로다(다시 열면 전부 보인다). 접힌 자리에는 "같은 도구를 다시
호출하면 된다"고 적어 둬서 모델이 스스로 복구할 수 있다.

- `context_tool_keep_turns` (기본 2) — 현재 턴 포함 최근 몇 턴을 원문으로 둘지
- `context_tool_aged_chars` (기본 200) — 그보다 오래된 결과를 남길 길이

### 도구 스코프 (`tool_scope_enabled`)

번들 서버를 다 켜면 도구가 58개다. 약한 로컬 모델은 그중에서 고르길 어려워한다.
질문·첨부에서 필요한 서버가 분명하면 그 서버 도구만 노출한다("엑셀 …" → office만 21개).

**애매하면 좁히지 않는다** — 잘못 좁혀 "할 수 없다"고 답하는 게 도구가 많아 헷갈리는
것보다 나쁘기 때문이다. 첨부 파일이 요구하는 서버는 무조건 살린다.
낱말은 `tool_scope_keywords`로 갈아끼울 수 있다(비우면 기본값). ⚠ '문서', '검색' 같은
일반적인 낱말은 넣지 말 것 — 거의 모든 질문이 걸려 다른 서버가 사라진다.

### 작업 대장 (`worklog_enabled`)

이 대화에서 다룬 문서를 추적해 매 턴 system에 넣는다:

```
[작업 중인 문서] … 같은 내용을 다시 읽지 마라 …
- 예산.xlsx  읽음 Sheet1!A1:D50  (2턴 전)
- 예산.xlsx  수정 Sheet1!C7  (방금)  ⚠ 아직 저장되지 않음
⚠ 저장되지 않은 변경이 있다 … '저장했다'고 말하지 마라 …
```

두 가지를 잡는다: (1) 세 턴 전에 읽은 걸 또 읽는 낭비, (2) **Excel 쓰기는 메모리만
바꾸고 `save_workbook`이 따로인데 "저장했습니다"라고 답하는 환각.** 저장하면 ⚠가
사라지고, 저장한 뒤 같은 셀을 또 고치면 다시 ⚠가 붙는다.

- 미저장 경고는 확장자를 보고 **어느 저장 도구를 불러야 하는지** 콕 집어 준다
  (.docx → `save_word_document`). 저장 도구가 셋이라 뭉뚱그리면 틀린 걸 부른다.
- 기록은 실제로 **실행된** 도구만 (승인 거절된 호출은 기록하지 않는다)
- `worklog_max_items` (기본 12)를 넘으면 오래된 것부터 버리되 **미저장 수정은 남긴다**
- ⚠ 작업 모드의 스텝 프롬프트에는 이 블록이 안 들어간다(스텝은 좁은 컨텍스트만 준다는
  planner 설계). 계획 수립과 최종 종합은 본다.

## 외부 LLM 연결 (API 키)

설정(⚙) → "외부 LLM 연결"에서 추가한다. 프리셋: OpenAI / Anthropic(Claude) /
Google Gemini / 직접 입력(사내 게이트웨이 등). OpenAI 호환 `chat/completions`
규격이면 무엇이든 등록 가능하다.

| 프로바이더 | base_url | 모델 예시 |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-5` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash` |

- 화면 상단 드롭다운으로 로컬 ↔ 외부를 전환한다. 선택값도 서버에 저장되어
  브라우저를 바꿔도 유지된다.
- API 키는 `config.json`(데이터 폴더)에만 저장되고, 브라우저나 상태 API로는
  내려가지 않는다 (`has_key` 여부만 표시).
- 외부 모델 선택 시에는 로컬 모델이 없어도(목 모드) 실제 API가 호출된다.
- MCP 도구는 외부 모델에도 동일하게 노출된다 — GPT/Claude는 도구 호출
  정확도가 높아 MCP 활용에는 오히려 유리하다.
- 폐쇄망에서는 외부 API 대신 사내 프록시/게이트웨이 주소를 등록하는 용도로 쓴다.

## 포트가 물려 있을 때

UI 기본 포트는 8080인데, Windows에서 가장 많이 겹치는 포트다(Docker Desktop, Jenkins,
Tomcat, Oracle XE, 사내 보안/자산관리 에이전트…). 이미 쓰이고 있으면 앱이 **다음 빈
포트로 옮겨서** 뜨고 콘솔에 이렇게 알린다:

```
[주의] 포트 8080이(가) 이미 사용 중이라 8081로 옮겨서 실행합니다.
       무엇이 쓰는지 확인:  netstat -ano | findstr :8080
       ...
       계속 이 포트를 쓰려면 run_app.bat에 --port 8081 를 넣으세요.
```

- `netstat`에 **아무것도 안 나오는데도** 막히면 Windows가 예약한 대역일 수 있다:
  `netsh int ipv4 show excludedportrange protocol=tcp` (Hyper-V/WSL/Docker가 예약한다.
  재부팅해도 안 풀린다.)
- 옮기지 않고 오류로 끝내려면 `--strict-port`.
- 매번 같은 포트를 쓰려면 `run_app.bat`의 마지막 줄을 `"%PY%" app.py --port 8090 %*`처럼 고친다.

⚠ 포트가 물려 죽으면 그 시점에 MCP 자식 프로세스는 이미 떠 있어서 로그가 뒤엉킨다.
그때 UI가 보인다면 **예전에 죽다 만 인스턴스**일 수 있다 — 그 인스턴스는 기동 시점의
낡은 `mcp_servers.json`을 들고 있어서 MCP가 전부 연결 실패로 보인다. 이럴 땐 8080을
잡고 있는 프로세스를 먼저 정리할 것(`netstat -ano | findstr :8080` → `taskkill /PID <번호> /F`).

## 참고/제약

- llama-server는 `--jinja`로 실행되어 Gemma의 chat template 기반 함수 호출을 쓴다.
  모델에 따라 도구 호출 정확도가 다를 수 있다 (Gemma는 공식 tool-use 학습이 약한 편).
- llama-server는 127.0.0.1에만 바인딩된다. 외부 공개는 UI 서버(`--host 0.0.0.0`)가 담당하므로
  모델 API가 직접 노출되지 않는다.

### `--host 0.0.0.0`으로 열었을 때 (인증이 없다)

이 앱에는 로그인이 없다. 같은 망의 누구나 UI에 닿을 수 있으므로, **이 PC를 건드리는
기능은 앱이 돌고 있는 PC(127.0.0.1)에서만** 동작하도록 막아 뒀다.

| 원격에서 가능 | 로컬(이 PC)에서만 |
|---|---|
| 채팅, 대화 기록, 파일 업로드, 상태 조회, 모델 전환(`active_provider`) | MCP 설정 쓰기, 서버 시작·중지·재시작, 앱 종료, 파일 선택 대화상자, 위험 도구 승인, 장기 기억 조회·삭제, 나머지 모든 설정 변경 |

- MCP 설정(`command`/`args`)은 그대로 프로세스로 실행되므로, 원격 쓰기를 허용하면
  곧 이 PC에서의 임의 명령 실행이 된다 — 그래서 로컬 전용이다.
- 원격에서 `approval_enabled`를 못 끄게 막는다. 못 막으면 위험 도구 승인 게이트가
  통째로 무력화된다.
- 판정은 **소켓 상대 주소**로만 한다. `X-Forwarded-For` 같은 헤더는 일부러 믿지 않는다
  (클라이언트가 마음대로 붙일 수 있다). 앞에 리버스 프록시를 두면 모든 요청이 로컬로
  보이므로, 그런 구성이면 **프록시 쪽에서 인증을 따로 걸어야 한다.**
- 대화 기록은 원격에서도 읽고 지울 수 있다(사용자 구분이 없는 공용 앱이다). 민감한
  대화를 다룬다면 `--host 0.0.0.0`으로 열지 말 것.
- 여러 명이 동시에 쓰려면 llama-server의 병렬 슬롯이 1이라 순차 처리된다.
  동시성이 필요하면 `llama_proc.py`에 `-np` 옵션을 추가할 것 (컨텍스트가 슬롯 수로 나뉜다).
