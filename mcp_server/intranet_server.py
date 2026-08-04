r"""intranet_server.py

사내 SharePoint / 그룹웨어의 내용을 **검색해서 읽어오는** MCP 서버입니다.
읽기 전용(🟢) — 아무 것도 쓰지 않습니다.

설계 배경
    - SharePoint에는 검색 API(`/_api/search/query`)가 내장돼 있다. 크롤링 없이 질의를
      던지면 관련도순 결과가 JSON으로 온다 — 관련도 랭킹을 SharePoint가 해주므로
      rag_server처럼 우리가 임베딩·인덱싱을 할 필요가 없다(사내 포털은 내용이 자주
      바뀌어서 인덱싱은 금방 낡기도 한다).
    - **인증에 새 패키지를 쓰지 않는다.** requests+requests-ntlm 조합은 사내 미러에
      없을 위험이 큰데, 이미 의존성인 pywin32의 WinHTTP COM이 NTLM/Negotiate/Basic을
      전부 처리한다. office_server가 Word COM으로 DRM 문서를 읽는 것과 같은 발상 —
      OS가 이미 할 줄 아는 일을 그대로 빌려 쓴다.
    - HTML 파싱도 표준 라이브러리(html.parser)만 쓴다. beautifulsoup4 불필요.

⚠ 사내망 HTTP를 쓰는 첫 코드다. 저장소 규약은 '런타임 HTTP는 전부 localhost'인데,
   그 취지는 **인터넷 의존 금지**다. 사내 포털 접속은 그 취지에 어긋나지 않지만,
   외부 인터넷으로는 절대 나가지 않도록 SITE_URL을 사내 주소로만 둘 것.

자격 증명 (평문 저장을 피한다 — 우선순위 순)
    1. Windows 자격 증명 관리자 — `--save-credential`로 한 번 저장해 두면 그 사용자
       계정으로 암호화돼 보관된다(pywin32의 win32cred). 권장.
    2. 환경 변수 INTRANET_USER / INTRANET_PASSWORD — 임시 시험용.
    3. 아무 것도 없으면 Windows 통합 인증(SSO)으로 시도한다.

사용:
    # 1) 사이트 주소와 계정을 한 번 등록
    python intranet_server.py --site https://portal.company.com/sites/team --save-credential
    # 2) 연결 진단 (서버를 띄우지 않음 — 무엇이 막히는지 단계별로 보여준다)
    python intranet_server.py --probe "휴가 규정"
    # 3) MCP 서버로 실행
    python intranet_server.py                     # stdio (기본)
    python intranet_server.py --transport http    # n8n 등, :8093

llm_studio 장착: 설정 → MCP에서 `intranet` 서버의 disabled를 풀면 된다(기본은 꺼져
있다 — 사이트 주소·계정 설정 전에는 도구만 늘어나기 때문).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from functools import wraps
from html.parser import HTMLParser

from fastmcp import FastMCP

# pywin32 — WinHTTP(인증) / 자격 증명 관리자에 쓴다. 없으면 인증 없는 사이트만
# urllib로 읽는 저하 모드로 동작한다 (서버는 정상 기동).
try:
    import pythoncom
    import win32com.client

    COM_AVAILABLE = True
    COM_IMPORT_ERROR = ""
except ImportError as e:
    COM_AVAILABLE = False
    COM_IMPORT_ERROR = str(e)

try:
    import win32cred

    CRED_AVAILABLE = True
except ImportError:
    CRED_AVAILABLE = False

mcp = FastMCP(
    name="intranet",
    instructions=(
        "사내 포털(SharePoint/그룹웨어)을 검색해 내용을 읽어오는 읽기 전용 서버입니다. "
        "사내 규정·공지·업무 문서에 관한 질문을 받으면 search_intranet으로 먼저 찾고, "
        "본문이 더 필요하면 결과의 url을 read_intranet_page에 넘겨 읽으세요. "
        "답할 때 출처(제목과 주소)를 함께 알려주세요. 접속이 안 되거나 결과가 이상하면 "
        "intranet_status로 진단하세요."
    ),
)

# 사내 사이트 주소 (CLI/환경변수로 지정). 예: https://portal.company.com/sites/team
SITE_URL = os.getenv("INTRANET_SITE_URL", "").rstrip("/")
CRED_TARGET = os.getenv("INTRANET_CRED_TARGET", "LocalLLMStudio:intranet")
HTTP_TIMEOUT = int(os.getenv("INTRANET_TIMEOUT", "30"))

MAX_CHARS = 20000
MAX_RESULTS = 20


class IntranetError(Exception):
    """도구가 사용자에게 그대로 돌려줄 안내 메시지를 담은 예외."""


def intranet_tool(fn):
    """예외를 안내 문자열로 바꾸고 COM 스레드를 초기화한다 (다른 서버들과 같은 규약)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if COM_AVAILABLE:
            pythoncom.CoInitialize()
        try:
            return fn(*args, **kwargs)
        except IntranetError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 — 도구는 항상 문자열을 돌려준다
            return f"작업에 실패했습니다: {type(e).__name__}: {e}"
        finally:
            if COM_AVAILABLE:
                pythoncom.CoUninitialize()

    return wrapper


# ─────────────────────────────── 자격 증명 ───────────────────────────────


def _load_credential() -> tuple[str, str]:
    """(사용자, 암호). 없으면 ('', '') — 그 경우 Windows 통합 인증(SSO)으로 시도한다."""
    user = os.getenv("INTRANET_USER", "")
    password = os.getenv("INTRANET_PASSWORD", "")
    if user:
        return user, password
    if CRED_AVAILABLE:
        try:
            cred = win32cred.CredRead(CRED_TARGET, win32cred.CRED_TYPE_GENERIC)
            blob = cred.get("CredentialBlob") or b""
            # CredWrite에 UTF-16LE로 넣으므로 같은 방식으로 되돌린다
            return cred.get("UserName", ""), blob.decode("utf-16-le")
        except Exception:  # noqa: BLE001 — 저장된 게 없으면 SSO로 넘어간다
            pass
    return "", ""


def _save_credential(user: str, password: str) -> str:
    """Windows 자격 증명 관리자에 저장한다 (그 사용자 계정으로 암호화돼 보관)."""
    if not CRED_AVAILABLE:
        raise IntranetError(
            "win32cred를 쓸 수 없어 자격 증명을 저장하지 못합니다(pywin32 필요). "
            "대신 환경 변수 INTRANET_USER / INTRANET_PASSWORD를 쓰세요."
        )
    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": CRED_TARGET,
            "UserName": user,
            "CredentialBlob": password.encode("utf-16-le"),
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        },
        0,
    )
    return f"자격 증명을 저장했습니다 (대상: {CRED_TARGET}, 사용자: {user})."


# ─────────────────────────────── HTTP ───────────────────────────────


def _require_site() -> str:
    if not SITE_URL:
        raise IntranetError(
            "사내 사이트 주소가 설정되지 않았습니다. 환경 변수 INTRANET_SITE_URL을 "
            "지정하거나 `--site https://portal.company.com/sites/team`으로 실행하세요."
        )
    return SITE_URL


def _http_get(url: str, accept: str = "application/json;odata=nometadata") -> tuple[int, str]:
    """GET 한 번. (상태코드, 본문). 인증이 필요하면 WinHTTP COM으로 처리한다.

    WinHTTP를 쓰는 이유: NTLM/Negotiate 협상을 OS가 대신 해 준다. 파이썬 쪽에
    requests-ntlm 같은 패키지를 새로 들이지 않아도 되고, 계정을 안 주면
    SetAutoLogonPolicy(0)으로 지금 로그인한 Windows 계정(SSO)으로 붙는다.
    """
    if COM_AVAILABLE:
        return _http_get_winhttp(url, accept)
    return _http_get_urllib(url, accept)


def _http_get_winhttp(url: str, accept: str) -> tuple[int, str]:
    req = win32com.client.Dispatch("WinHttp.WinHttpRequest.5.1")
    ms = HTTP_TIMEOUT * 1000
    req.SetTimeouts(ms, ms, ms, ms)
    req.Open("GET", url, False)  # False = 동기 호출
    user, password = _load_credential()
    if user:
        # 0 = HTTPREQUEST_SETCREDENTIALS_FOR_SERVER. Open 뒤에 불러야 한다.
        req.SetCredentials(user, password, 0)
    else:
        try:
            req.SetAutoLogonPolicy(0)  # 0 = Always — 현재 Windows 계정으로 SSO
        except Exception:  # noqa: BLE001
            pass
    req.SetRequestHeader("Accept", accept)
    # SSL 검증을 끄는 옵션은 일부러 넣지 않는다. WinHTTP는 **Windows 인증서 저장소**를
    # 쓰므로, 사내 사설 CA가 GPO로 배포된 도메인 PC에서는 그냥 검증에 통과한다.
    # 그래도 인증서 오류가 나면 그 CA를 신뢰 저장소에 넣는 게 옳은 해결이다 —
    # 검증을 끄면 사내망이라도 중간자 공격에 무방비가 된다.
    req.Send()
    return int(req.Status), str(req.ResponseText or "")


def _http_get_urllib(url: str, accept: str) -> tuple[int, str]:
    """pywin32가 없을 때의 저하 경로 — 인증이 필요 없는 사이트만 읽힌다."""
    request = urllib.request.Request(url, headers={"Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# ─────────────────────────────── HTML → 텍스트 ───────────────────────────────


class _TextExtractor(HTMLParser):
    """HTML에서 사람이 읽을 본문만 뽑는다 (beautifulsoup4 없이 표준 라이브러리로).

    script/style/nav 같은 껍데기는 통째로 건너뛰고, 블록 요소 경계마다 줄을 바꾼다.
    표는 셀을 탭으로 이어 한 행이 한 줄이 되게 한다(office_server의 Word 표 처리와 같은
    방침 — 청킹·요약 때 표가 흩어지지 않게).
    """

    SKIP = {"script", "style", "noscript", "head", "nav", "footer", "svg", "form"}
    BLOCK = {
        "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "section", "article", "header", "blockquote", "pre", "ul", "ol", "table",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def _nl(self):
        """줄바꿈을 하나만 넣는다. 블록의 시작과 끝에서 각각 넣으면 빈 줄이 줄줄이
        생겨(표 행 사이가 특히) 읽기도 나쁘고 컨텍스트도 낭비된다."""
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif self._skip_depth == 0:
            if tag in ("td", "th"):
                self.parts.append("\t")
            elif tag in self.BLOCK:
                self._nl()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth == 0 and tag in self.BLOCK:
            self._nl()

    def handle_data(self, data):
        if self._skip_depth or not data.strip():
            return
        # 원문에 있던 앞뒤 공백은 한 칸으로 살린다. 그냥 strip하면 <b>·<a> 같은 인라인
        # 태그 앞뒤 낱말이 '연차는입사일'처럼 붙어 버린다.
        lead = " " if data[:1].isspace() else ""
        trail = " " if data[-1:].isspace() else ""
        self.parts.append(lead + data.strip() + trail)

    def text(self) -> str:
        raw = "".join(self.parts)
        out: list[str] = []
        for line in raw.split("\n"):
            # 인라인 태그 때문에 생긴 연속 공백을 한 칸으로 (탭은 표 구분이라 남긴다)
            line = " ".join(line.split(" ")).strip()
            while "  " in line:
                line = line.replace("  ", " ")
            line = line.strip(" ")
            if line:
                out.append(line)
            elif out and out[-1] != "":
                out.append("")
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    """HTML 문자열에서 본문 텍스트를 뽑는다. 파싱이 실패해도 예외를 내지 않는다."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — 깨진 HTML이어도 거기까지 모은 걸 돌려준다
        pass
    return parser.text()


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…(전체 {len(text):,}자 중 앞 {limit:,}자. 더 필요하면 max_chars를 늘리세요)"


# ─────────────────────────────── SharePoint 검색 ───────────────────────────────


def _search_rows(data: dict) -> list[dict]:
    """SharePoint 검색 응답(JSON)에서 결과 행들을 꺼낸다.

    ⚠ 실기 검증 대상: SharePoint 버전에 따라 응답 껍데기가 다르다. odata=nometadata와
    verbose(‘d’로 한 겹 더 감싸는 형태) 양쪽을 모두 받아 본다.
    """
    node = data.get("d", data)  # verbose면 d 아래에 들어 있다
    query = node.get("query", node.get("PrimaryQueryResult", {}))
    if "PrimaryQueryResult" in query:
        query = query["PrimaryQueryResult"]
    table = (query.get("RelevantResults") or {}).get("Table") or {}
    rows = table.get("Rows")
    if isinstance(rows, dict):  # verbose는 {"results": [...]}
        rows = rows.get("results", [])
    out = []
    for row in rows or []:
        cells = row.get("Cells")
        if isinstance(cells, dict):
            cells = cells.get("results", [])
        item = {}
        for cell in cells or []:
            key, value = cell.get("Key"), cell.get("Value")
            if key:
                item[key] = value
        out.append(item)
    return out


# ─────────────────────────── MCP 도구 (🟢 읽기 전용) ───────────────────────────


@mcp.tool()
@intranet_tool
def search_intranet(query: str, limit: int = 5) -> str:
    """사내 포털(SharePoint)을 검색해 관련 문서·페이지를 찾습니다. (🟢 읽기 전용)

    사내 규정·공지·업무 문서에 관한 질문이면 먼저 이걸 부르세요. 결과의 url을
    read_intranet_page에 넘기면 본문 전체를 읽을 수 있습니다.

    Args:
        query: 찾을 내용(자연어 그대로도 됩니다).
        limit: 결과 개수(기본 5, 최대 20).
    """
    site = _require_site()
    q = (query or "").strip()
    if not q:
        raise IntranetError("query가 비어 있습니다. 찾을 내용을 지정하세요.")
    k = max(1, min(int(limit), MAX_RESULTS))

    select = "Title,Path,HitHighlightedSummary,LastModifiedTime,Author"
    url = (
        f"{site}/_api/search/query"
        f"?querytext='{urllib.parse.quote(q)}'"
        f"&rowlimit={k}"
        f"&selectproperties='{urllib.parse.quote(select)}'"
    )
    status, body = _http_get(url)
    if status == 401 or status == 403:
        raise IntranetError(
            f"사내 포털 인증에 실패했습니다 (HTTP {status}). "
            "`python intranet_server.py --save-credential`로 계정을 등록했는지, "
            "그 계정에 이 사이트 접근 권한이 있는지 확인하세요."
        )
    if status != 200:
        raise IntranetError(
            f"검색 요청이 실패했습니다 (HTTP {status}). 사이트 주소가 맞는지 "
            f"확인하세요: {site}\n`--probe`로 진단할 수 있습니다."
        )
    try:
        rows = _search_rows(json.loads(body))
    except json.JSONDecodeError:
        raise IntranetError(
            "검색 응답을 이해하지 못했습니다(JSON이 아님). SharePoint가 아니거나 "
            "로그인 페이지로 넘어갔을 수 있습니다 — `--probe`로 확인하세요."
        )
    if not rows:
        return f"'{q}'와 관련된 사내 문서를 찾지 못했습니다. 다른 표현으로 시도해 보세요."

    out = [f"사내 검색: {q}  |  {len(rows)}건", ""]
    for i, row in enumerate(rows, start=1):
        title = row.get("Title") or "(제목 없음)"
        path = row.get("Path") or ""
        summary = html_to_text(row.get("HitHighlightedSummary") or "").replace("\n", " ")
        modified = (row.get("LastModifiedTime") or "")[:10]
        author = row.get("Author") or ""
        meta = "  ".join(x for x in (modified, author) if x)
        out.append(f"[{i}] {title}" + (f"   ({meta})" if meta else ""))
        if summary:
            out.append(f"    {summary[:300]}")
        out.append(f"    url: {path}")
        out.append("")
    out.append("본문 전체가 필요하면 read_intranet_page(url=...)로 읽으세요.")
    return "\n".join(out)


@mcp.tool()
@intranet_tool
def read_intranet_page(url: str, max_chars: int = MAX_CHARS) -> str:
    """사내 포털의 페이지/문서 하나를 열어 본문 텍스트를 읽습니다. (🟢 읽기 전용)

    search_intranet이 돌려준 url을 그대로 넘기면 됩니다.

    Args:
        url: 읽을 페이지 주소(사내 주소만 허용).
        max_chars: 반환 최대 글자 수(기본 20000).
    """
    site = _require_site()
    target = (url or "").strip()
    if not target:
        raise IntranetError("url이 비어 있습니다.")
    if not target.lower().startswith(("http://", "https://")):
        target = f"{site}/{target.lstrip('/')}"
    # 사내 사이트 밖으로 나가지 않게 막는다 (인터넷 의존 금지 규약).
    site_host = urllib.parse.urlparse(site).netloc.lower()
    target_host = urllib.parse.urlparse(target).netloc.lower()
    if target_host != site_host:
        raise IntranetError(
            f"사내 사이트({site_host}) 밖의 주소는 읽지 않습니다: {target_host}"
        )

    status, body = _http_get(target, accept="text/html,application/xhtml+xml")
    if status in (401, 403):
        raise IntranetError(f"이 페이지를 볼 권한이 없습니다 (HTTP {status}): {target}")
    if status != 200:
        raise IntranetError(f"페이지를 가져오지 못했습니다 (HTTP {status}): {target}")
    text = html_to_text(body)
    if not text.strip():
        return (f"'{target}'에서 읽을 만한 텍스트를 찾지 못했습니다. "
                "첨부 문서(.docx/.pdf)라면 파일을 내려받아 office/pdf 서버로 읽으세요.")
    return f"출처: {target}\n\n{_truncate(text, max_chars)}"


@mcp.tool()
@intranet_tool
def intranet_status() -> str:
    """사내 포털 연결 상태를 진단합니다 — 주소·인증 방식·접속 가능 여부. (🟢 읽기 전용)

    검색이 안 되거나 결과가 이상할 때 먼저 호출하세요.
    """
    lines = ["사내 포털 연결 상태:"]
    lines.append(f"  사이트 주소: {SITE_URL or '(설정 안 됨 — INTRANET_SITE_URL 또는 --site)'}")
    user, _pw = _load_credential()
    if user:
        source = "환경 변수" if os.getenv("INTRANET_USER") else "Windows 자격 증명 관리자"
        lines.append(f"  인증: 계정 '{user}' ({source})")
    else:
        lines.append("  인증: 계정 없음 — Windows 통합 인증(SSO)으로 시도")
    lines.append(f"  HTTP 경로: {'WinHTTP(COM) — NTLM/SSO 가능' if COM_AVAILABLE else f'urllib — 인증 불가 ({COM_IMPORT_ERROR})'}")
    lines.append(f"  자격 증명 저장: {'가능' if CRED_AVAILABLE else '불가(win32cred 없음)'}")
    if not SITE_URL:
        return "\n".join(lines)
    try:
        status, _body = _http_get(f"{SITE_URL}/_api/web?$select=Title")
        lines.append(f"  접속 시험: HTTP {status}" + ("  ✅" if status == 200 else "  ❌"))
        if status in (401, 403):
            lines.append("    → 인증 실패. --save-credential로 계정을 등록하세요.")
        elif status == 404:
            lines.append("    → 주소가 SharePoint 사이트가 아닐 수 있습니다(_api 없음).")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  접속 시험: 실패 ({type(e).__name__}: {e})")
    return "\n".join(lines)


# ─────────────────────────────── CLI / 서버 기동 ───────────────────────────────


def _probe(query: str) -> None:
    """서버를 띄우지 않고 단계별로 진단한다 (무엇이 막히는지 눈으로 보려고)."""
    print("=" * 62)
    print(intranet_status())
    print("=" * 62)
    if not SITE_URL:
        return
    print(f"\n[검색 시험] {query!r}")
    print(search_intranet(query, limit=3))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="사내 포털(SharePoint) 검색 MCP 서버 (읽기 전용)")
    parser.add_argument("--site", default=None, help="사내 사이트 주소 (예: https://portal.company.com/sites/team)")
    parser.add_argument("--save-credential", action="store_true",
                        help="Windows 자격 증명 관리자에 계정/암호를 저장하고 종료")
    parser.add_argument("--probe", metavar="QUERY", nargs="?", const="테스트",
                        help="연결·검색을 단계별로 진단하고 종료 (서버를 띄우지 않음)")
    parser.add_argument("--transport", choices=["stdio", "http", "sse"],
                        default=os.getenv("INTRANET_MCP_TRANSPORT", "stdio"),
                        help="stdio(기본): 로컬 클라이언트가 직접 실행. http/sse: 네트워크 접속.")
    parser.add_argument("--host", default=os.getenv("INTRANET_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("INTRANET_MCP_PORT", "8093")))
    args = parser.parse_args()

    if args.site:
        SITE_URL = args.site.rstrip("/")

    if args.save_credential:
        if COM_AVAILABLE:
            pythoncom.CoInitialize()
        try:
            u = input("사내 계정 (예: DOMAIN\\user 또는 user@company.com): ").strip()
            p = getpass.getpass("암호(화면에 표시되지 않습니다): ")
            print(_save_credential(u, p))
            if SITE_URL:
                print("\n등록 후 진단:")
                print(intranet_status())
        except IntranetError as e:
            print(f"[오류] {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.probe:
        if COM_AVAILABLE:
            pythoncom.CoInitialize()
        _probe(args.probe)
        sys.exit(0)

    if not COM_AVAILABLE:
        print(f"[주의] pywin32 없음({COM_IMPORT_ERROR}) — 인증이 필요한 사이트는 읽을 수 "
              "없습니다(urllib 저하 모드).", file=sys.stderr)
    if not SITE_URL:
        print("[주의] INTRANET_SITE_URL이 설정되지 않았습니다 — 도구가 안내만 반환합니다.",
              file=sys.stderr)

    if args.transport in ("http", "sse"):
        path = "/mcp/" if args.transport == "http" else "/sse/"
        print(f"사내 포털 MCP 서버 시작 ({args.transport}) — http://{args.host}:{args.port}{path}",
              file=sys.stderr)
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    else:
        # stdio: stdout은 MCP 프로토콜 채널 — 로그는 stderr로.
        print("사내 포털 MCP 서버 시작 (stdio)", file=sys.stderr)
        mcp.run(transport="stdio")
