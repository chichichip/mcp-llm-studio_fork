"""vision_ingest.py

Vision RAG 인제스트 — PDF를 **페이지 이미지 → VLM 전사(transcription) → 텍스트**로
바꿔 rag_core의 청킹·임베딩·검색 파이프라인에 태운다. 표·도면이 많아 텍스트 레이어만
뽑으면 내용이 사라지는 문서(표준품 선정 지침서, 스펙 도면, 부품 스펙 PDF)를 위한 경로다.

왜 텍스트 추출이 아니라 VLM인가:
    스펙 도면 PDF는 텍스트 레이어가 있어도 표가 좌우 그룹으로 쪼개져 있어 추출 순서가
    엉키고, 스캔본은 텍스트가 아예 없다. spec-reader/read_spec.py에서 확인한 대로 VLM이
    페이지 그림을 보고 표를 마크다운으로 옮기는 쪽이 훨씬 온전하다.

우아한 저하 체인 (페이지 하나 기준):
    ① VLM 전사       — VLM 서버가 응답하면 이걸 쓴다 (표/도면 포함)
    ② 텍스트 레이어   — VLM이 없거나 실패하면 PyMuPDF가 뽑은 원문
    ③ Word COM(DRM)  — PyMuPDF가 못 여는 암호화 PDF는 pdf_server의 백엔드 체인 재사용
                       (페이지 구분이 없어 문서 전체를 한 덩어리로 받는다)
    어느 것도 안 되면 예외 대신 그 페이지를 건너뛰고 사유를 남긴다.

페이지 이미지는 캐시 폴더에 남겨 청크마다 경로를 붙여 둔다. 검색이 근거 청크를 찾은 뒤
`ask_page` 도구가 **그 페이지 그림을 VLM에 다시 보여 주며** 되물을 수 있게 하기 위한 것이다
(텍스트로 한 번 접힌 정보를 원본 그림으로 되짚는 경로).

⚠ 치수표를 이 경로로 넣어 검색하는 것은 **참고용**이다. 판독값을 실제 부품 선정에 쓰려면
spec-reader의 read_spec.py + verify.py(자동 검증)를 거쳐야 한다 — RAG는 청크 경계에서
숫자가 잘릴 수 있고 판독 검증이 없다. 자세한 건 저장소 CLAUDE.md의 'Vision RAG' 절.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import settings

try:
    import fitz  # PyMuPDF — PDF 렌더링/텍스트 레이어

    FITZ_AVAILABLE = True
    FITZ_IMPORT_ERROR = ""
except ImportError as e:  # 없으면 PDF는 pdf_server 백엔드(텍스트 전용)로만 저하
    FITZ_AVAILABLE = False
    FITZ_IMPORT_ERROR = str(e)
    fitz = None  # type: ignore[assignment]

try:
    from PIL import Image

    PIL_AVAILABLE = True
    PIL_IMPORT_ERROR = ""
except ImportError as e:
    PIL_AVAILABLE = False
    PIL_IMPORT_ERROR = str(e)
    Image = None  # type: ignore[assignment]


# ─────────────────────────────── 설정 (env/CLI로 조정) ───────────────────────────────
# 다른 모듈에서는 `vision_ingest.VLM_URL`처럼 **매번 속성으로** 읽을 것 (CLI가 덮어쓴다).

# VLM 서버. OpenAI 호환 /v1/chat/completions 를 여는 곳이면 된다:
#   - 로컬 llama-server (mmproj를 붙인 Gemma 3 등)
#   - 사내 호스팅 게이트웨이(vLLM 등) — spec-reader/config.py가 쓰는 그 주소
# 기본값은 localhost. 사내 주소는 RAG_VLM_URL 로 넘긴다.
VLM_URL = settings.get("RAG_VLM_URL", "http://127.0.0.1:8003/v1")
VLM_MODEL = settings.get("RAG_VLM_MODEL", "gemma")
VLM_API_KEY = settings.get("RAG_VLM_API_KEY", "")  # 사내 게이트웨이가 요구할 때만
VLM_TIMEOUT = settings.get_float("RAG_VLM_TIMEOUT", 180)  # 한 페이지 전사 타임아웃(초)
VLM_MAX_TOKENS = settings.get_int("RAG_VLM_MAX_TOKENS", 4096)

# 렌더링. 300 DPI 전면 페이지는 사내 게이트웨이 요청 크기 제한에 걸려 413이 났다
# (spec-reader 실측). 150 DPI + 긴 변 2000px이 판독과 크기의 타협점.
VLM_DPI = settings.get_int("RAG_VLM_DPI", 150)
VLM_MAX_SIDE = settings.get_int("RAG_VLM_MAX_SIDE", 2000)

# 페이지 이미지 캐시 폴더. 비우면 이미지를 저장하지 않는다(ask_page 되묻기 불가).
PAGE_IMAGE_DIR = settings.get("RAG_PAGE_IMAGES", str(Path(__file__).with_name("rag_pages")))

# 전사 결과가 이보다 짧으면 '판독 실패'로 보고 텍스트 레이어로 저하한다.
MIN_TRANSCRIPT_CHARS = 20


PAGE_PROMPT = """이 페이지의 내용을 사람이 다시 읽을 수 있게 텍스트로 옮겨 적으세요.

규칙:
- 표는 마크다운 표로 옮깁니다. 셀 값은 인쇄된 그대로 적습니다 (반올림·단위 추가·재정렬 금지).
- 표가 좌우로 여러 그룹으로 나뉘어 있으면 모든 그룹을 읽어 하나의 표로 이어 적습니다.
  한 그룹만 읽고 끝내는 것이 가장 흔한 실패입니다 — 페이지 전체 폭을 확인하세요.
- 도면·그림은 `[그림]` 으로 시작하는 문단에 무엇이 그려져 있는지 적고, 치수 지시선과
  주기(callout) 문구는 인쇄된 그대로 옮깁니다.
- 제목, 머리말/꼬리말, 문서번호, 쪽번호도 그대로 포함합니다.
- 원문 언어를 그대로 둡니다. 번역하지 마세요.
- 읽을 수 없는 글자는 지어내지 말고 `?` 로 둡니다.
- 요약·해설·감상을 덧붙이지 말고 페이지에 있는 것만 옮깁니다.

맨 첫 줄에 이 페이지의 제목이나 절 번호를 `# ` 로 시작해 적으세요 (없으면 생략).
"""


class VisionError(Exception):
    """호출부가 사용자에게 그대로 돌려줄 안내 메시지를 담은 예외."""


# ─────────────────────────────── VLM 클라이언트 ───────────────────────────────


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if VLM_API_KEY:
        h["Authorization"] = f"Bearer {VLM_API_KEY}"
    return h


def _base_url() -> str:
    """VLM_URL을 `.../v1` 형태의 베이스로 정규화한다.

    spec-reader의 config.py는 `http://<사내주소>/v1/chat/completions` 처럼 **엔드포인트
    전체 주소**를 쓴다. 그 값을 그대로 --vlm-url에 붙여넣는 실수가 잦은데, 그러면
    `.../chat/completions/models`를 부르게 돼 연결 실패로 보인다. 끝에 붙은 엔드포인트
    경로를 떼어 양쪽 표기를 모두 받아 준다.
    """
    u = VLM_URL.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/embeddings", "/models"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u


def _ping(url: str, payload: dict | None) -> tuple[bool, str]:
    """엔드포인트 하나를 찔러 (성공여부, 사유)를 돌려준다. 사유는 사람이 읽을 한 줄."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers=_headers(), method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return (resp.status == 200), f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        hint = {401: " (인증 필요 — RAG_VLM_API_KEY)", 403: " (권한 거부)",
                404: " (이 경로 없음)", 413: " (요청이 너무 큼 — --dpi를 낮출 것)"}.get(e.code, "")
        return False, f"HTTP {e.code}{hint}"
    except urllib.error.URLError as e:
        return False, f"연결 불가: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def vlm_check() -> tuple[bool, str]:
    """VLM 서버가 쓸 수 있는지 확인하고 **사유까지** 돌려준다.

    사내망에서는 로그 파일을 빼낼 수 없어 화면에 뜬 한 줄로 원인을 알아야 한다.
    그래서 실패를 조용히 삼키지 않고 어디서 어떻게 막혔는지 남긴다.

    /models 를 먼저 보고, 없으면(게이트웨이가 안 여는 경우가 있다) **실제 chat 요청**
    으로 확정한다 — /models 가 없다고 전사까지 못 하는 건 아니기 때문이다.
    """
    base = _base_url()
    ok, why = _ping(base + "/models", None)
    if ok:
        return True, f"{base} 연결됨"
    # 게이트웨이가 /models를 안 열 수 있다. 아주 작은 chat 요청으로 확정한다.
    ok2, why2 = _ping(
        base + "/chat/completions",
        {"model": VLM_MODEL, "messages": [{"role": "user", "content": "ping"}],
         "max_tokens": 1, "temperature": 0},
    )
    if ok2:
        return True, f"{base} 연결됨 (/models는 없지만 chat은 응답)"

    reason = f"{base} 연결 실패 — /models: {why} / chat: {why2}"
    proxies = urllib.request.getproxies()
    if proxies:
        reason += (f"\n    ⚠ 프록시 설정이 잡혀 있습니다({', '.join(sorted(proxies))}). "
                   "사내 주소가 프록시로 나가면 막힙니다 — NO_PROXY에 이 호스트를 넣어 보세요.")
    if VLM_URL.rstrip("/").endswith(("chat/completions", "completions")):
        reason += "\n    ⚠ --vlm-url 에는 `/v1` 까지만 주세요(`/chat/completions` 는 코드가 붙입니다)."
    return False, reason


def vlm_available() -> bool:
    """VLM 서버가 응답하는지 (사유 없이). 저하 판정용."""
    return vlm_check()[0]


def ask_image(png_bytes: bytes, prompt: str) -> str | None:
    """이미지 한 장을 VLM에 보내 텍스트 응답을 받는다. 실패하면 None (저하 신호).

    temperature=0 — 전사는 창작이 아니다. 같은 페이지를 두 번 읽으면 같아야 한다.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = json.dumps(
        {
            "model": VLM_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": VLM_MAX_TOKENS,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _base_url() + "/chat/completions", data=body,
        headers=_headers(), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=VLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 413(요청 과대)은 DPI를 낮추라는 신호 — 사유를 남겨 두면 진단이 빠르다.
        print(f"[주의] VLM 응답 오류 {e.code} — DPI({VLM_DPI})를 낮춰 보세요.", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        print(f"[주의] VLM 호출 실패({type(e).__name__}) — 텍스트 레이어로 저하합니다.",
              file=sys.stderr)
        return None
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return None


# ─────────────────────────────── 페이지 렌더링 ───────────────────────────────


def _cache_dir_for(path: str) -> Path:
    """파일별 이미지 캐시 폴더. 같은 이름 다른 폴더가 섞이지 않게 경로 해시를 붙인다.

    Word를 임시 PDF로 내보내 읽는 경로에서는 **원본 .docx 경로**를 키로 넘긴다
    (image_key) — 임시 PDF 이름으로 캐시하면 매번 새 폴더가 생겨 되짚기가 끊긴다.
    """
    stem = re.sub(r"[^\w.-]+", "_", Path(path).stem)[:60] or "doc"
    h = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
    return Path(PAGE_IMAGE_DIR) / f"{stem}_{h}"


def render_page(doc, page_no: int, dpi: int = 0, max_side: int = 0) -> bytes:
    """열린 fitz 문서의 페이지 하나를 PNG 바이트로 렌더링한다 (1-based).

    Pillow가 있으면 긴 변을 max_side로 줄인다 — 요청 크기 제한(413) 대응.
    없으면 렌더 해상도만으로 조절한다(우아한 저하).
    """
    dpi = dpi or VLM_DPI
    max_side = max_side or VLM_MAX_SIDE
    pix = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    png = pix.tobytes("png")
    if PIL_AVAILABLE and max_side > 0:
        img = Image.open(io.BytesIO(png))
        if max(img.size) > max_side:
            s = max_side / max(img.size)
            img = img.convert("RGB").resize(
                (max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS
            )
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
    return png


def _save_page_image(path: str, page_no: int, png: bytes) -> str:
    """페이지 이미지를 캐시에 쓰고 절대경로를 돌려준다. 못 쓰면 빈 문자열(저하)."""
    if not PAGE_IMAGE_DIR:
        return ""
    try:
        d = _cache_dir_for(path)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"p{page_no:04d}.png"
        f.write_bytes(png)
        return str(f.resolve())
    except OSError as e:
        print(f"[주의] 페이지 이미지 저장 실패({e}) — 경로 없이 인덱싱합니다.", file=sys.stderr)
        return ""


def page_image_path(path: str, page_no: int) -> str:
    """이미 캐시된 페이지 이미지 경로. 없으면 빈 문자열."""
    if not PAGE_IMAGE_DIR:
        return ""
    f = _cache_dir_for(path) / f"p{page_no:04d}.png"
    return str(f.resolve()) if f.exists() else ""


def ensure_page_image(path: str, page_no: int) -> str:
    """페이지 이미지를 확보한다(캐시에 없으면 즉석 렌더링). 실패하면 빈 문자열.

    인덱싱 때 이미지를 못 남겼거나 캐시를 지운 뒤에도 ask_page가 동작하게 하는 경로다.
    """
    cached = page_image_path(path, page_no)
    if cached:
        return cached
    if not FITZ_AVAILABLE or not os.path.isfile(path):
        return ""
    try:
        doc = fitz.open(path)
    except Exception:  # noqa: BLE001 — 암호화/손상 PDF
        return ""
    try:
        if not 1 <= page_no <= len(doc):
            return ""
        png = render_page(doc, page_no)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        doc.close()
    return _save_page_image(path, page_no, png)


# ─────────────────────────────── PDF 페이지 추출 ───────────────────────────────


def _pdf_fallback_text(path: str) -> tuple[str, str]:
    """PyMuPDF가 못 여는 PDF(사내 DRM 등)를 pdf_server의 백엔드 체인으로 읽는다.

    반환: (본문, 사용한 백엔드). 실패하면 ("", 사유).
    페이지 구분이 없어 문서 전체가 한 덩어리로 온다 — 페이지 번호는 0으로 둔다.
    """
    try:
        import pdf_server  # 같은 폴더. fastmcp/pywin32가 없어도 import는 된다.
    except Exception as e:  # noqa: BLE001
        return "", f"pdf_server를 불러오지 못했습니다({e})"
    try:
        text, backend, reasons = pdf_server._extract(path, "")
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"
    if text:
        return text, backend
    return "", "; ".join(reasons[:3]) or "추출 실패"


def extract_pdf_pages(
    path: str, use_vlm: bool | None = None, dpi: int = 0, max_pages: int = 0,
    image_key: str = "", first_page: int = 1,
) -> tuple[list[dict], list[str]]:
    """PDF를 페이지 단위로 읽어 [{page, text, image, source}, ...] 와 알림 목록을 돌려준다.

    use_vlm=None이면 서버 응답 여부를 확인해 자동으로 정한다. False면 텍스트 레이어만
    쓴다(VLM 없이 인덱스 뼈대를 먼저 만들고, 나중에 --reindex로 전사를 붙이는 운용).

    image_key: 페이지 이미지를 캐시할 때 쓸 키(기본은 path). Word를 임시 PDF로 내보내
    읽을 때 **원본 .docx 경로**를 넘겨 캐시가 원본에 붙게 한다.

    first_page/max_pages: 읽을 구간(1-based, 앞 쪽부터가 기본). 진단할 때 특정 쪽만
    보려고 있다 — VLM 전사는 쪽마다 돈이 드니 필요 없는 쪽을 읽지 않는다.
    """
    key = image_key or path
    notes: list[str] = []
    if use_vlm is None:
        use_vlm = vlm_available()

    if not FITZ_AVAILABLE:
        text, backend = _pdf_fallback_text(path)
        if not text:
            notes.append(f"PyMuPDF 없음({FITZ_IMPORT_ERROR}) + 대체 추출 실패: {backend}")
            return [], notes
        notes.append(f"PyMuPDF 없음 — {backend} 백엔드로 페이지 구분 없이 읽었습니다.")
        return [{"page": 0, "text": text, "image": "", "source": backend}], notes

    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001 — 암호화/손상
        text, backend = _pdf_fallback_text(path)
        if not text:
            notes.append(f"열기 실패({type(e).__name__}: {e}) + 대체 추출 실패: {backend}")
            return [], notes
        notes.append(f"PyMuPDF로 열 수 없어(암호화 추정) {backend} 백엔드로 읽었습니다.")
        return [{"page": 0, "text": text, "image": "", "source": backend}], notes

    pages: list[dict] = []
    try:
        if doc.needs_pass:
            doc.close()
            text, backend = _pdf_fallback_text(path)
            if not text:
                notes.append(f"암호로 보호된 PDF이고 대체 추출도 실패했습니다: {backend}")
                return [], notes
            notes.append(f"암호로 보호된 PDF — {backend} 백엔드로 읽었습니다.")
            return [{"page": 0, "text": text, "image": "", "source": backend}], notes

        total = len(doc)
        start = max(1, first_page)
        if start > total:
            notes.append(f"{first_page}쪽은 이 문서에 없습니다(전체 {total}쪽).")
            return [], notes
        last = min(total, start + max_pages - 1) if max_pages else total
        if (start, last) != (1, total):
            notes.append(f"전체 {total}쪽 중 {start}~{last}쪽만 읽었습니다.")
        for pno in range(start, last + 1):
            layer = ""
            try:
                layer = (doc[pno - 1].get_text() or "").strip()
            except Exception:  # noqa: BLE001 — 한 페이지 실패가 전체를 멈추지 않게
                pass

            text, source, image = layer, "text", ""
            if use_vlm:
                try:
                    png = render_page(doc, pno, dpi)
                except Exception as e:  # noqa: BLE001
                    png = b""
                    notes.append(f"p{pno} 렌더링 실패({type(e).__name__})")
                if png:
                    image = _save_page_image(key, pno, png)
                    got = ask_image(png, PAGE_PROMPT)
                    if got and len(got.strip()) >= MIN_TRANSCRIPT_CHARS:
                        text, source = got.strip(), "vlm"
                    else:
                        notes.append(f"p{pno} VLM 전사 실패 — 텍스트 레이어로 저하")

            if not text:
                notes.append(f"p{pno} 본문 없음 — 건너뜀")
                continue
            pages.append({"page": pno, "text": text, "image": image, "source": source})
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    return pages, notes


def status_text() -> str:
    """VLM/렌더링 의존성 상태 한 덩어리 — rag_status와 인덱서가 함께 쓴다."""
    ok, why = vlm_check()
    lines = [
        settings.status_line(),
        f"VLM 서버(모델 {VLM_MODEL}): {'연결됨' if ok else '연결 안 됨 — PDF는 텍스트 레이어로 저하'}",
        f"  {why}  [주소 출처: {settings.source_of('RAG_VLM_URL')}]",
        f"PDF 렌더링(PyMuPDF): {'가능' if FITZ_AVAILABLE else '불가 — ' + FITZ_IMPORT_ERROR}",
        f"이미지 축소(Pillow): {'가능' if PIL_AVAILABLE else '불가 — ' + PIL_IMPORT_ERROR + ' (DPI로만 조절)'}",
        f"페이지 이미지 캐시: {PAGE_IMAGE_DIR or '사용 안 함 — ask_page 되묻기 불가'}",
    ]
    return "\n".join(lines)


# ─────────────────────────────── 진단 CLI ───────────────────────────────
# 인덱싱 전에 "이 PDF가 이 PC에서 실제로 읽히는가"를 서버 없이 확인한다
# (pdf_server --probe, intranet_server --probe와 같은 규약).


def _probe(path: str, page: int, count: int) -> int:
    """지정한 쪽만 판독해 결과를 보여 준다. 앞쪽을 훑지 않는다 — 전사는 쪽당 비용이다."""
    print(status_text(), file=sys.stderr)
    print("", file=sys.stderr)
    if not os.path.isfile(path):
        print(f"[오류] 파일이 없습니다: {path}", file=sys.stderr)
        return 1
    pages, notes = extract_pdf_pages(path, first_page=page, max_pages=max(1, count))
    for n in notes:
        print(f"  알림: {n}", file=sys.stderr)
    if not pages:
        print("[실패] 본문을 얻지 못했습니다.", file=sys.stderr)
        return 1
    for pg in pages:
        print(f"\n--- p{pg['page']} ({pg['source']}) "
              f"{len(pg['text'])}자 / 이미지 {pg['image'] or '없음'} ---", file=sys.stderr)
        print(pg["text"][:3000])
        if len(pg["text"]) > 3000:
            print("…(생략)")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="PDF 페이지 전사 진단 (MCP 서버 없이 백엔드만 확인)"
    )
    ap.add_argument("--probe", metavar="PDF", required=True, help="진단할 PDF 경로")
    ap.add_argument("--page", type=int, default=1,
                    help="판독할 쪽 번호 (기본 1). 이 쪽만 읽는다 — 앞쪽은 건드리지 않는다.")
    ap.add_argument("--count", type=int, default=1,
                    help="이 쪽부터 몇 쪽을 읽을지 (기본 1)")
    ap.add_argument("--vlm-url", default=None, help=f"VLM 서버 /v1 (기본 {VLM_URL})")
    ap.add_argument("--vlm-model", default=None, help=f"VLM 모델 (기본 {VLM_MODEL})")
    ap.add_argument("--dpi", type=int, default=None, help=f"렌더링 DPI (기본 {VLM_DPI})")
    a = ap.parse_args()
    if a.vlm_url:
        VLM_URL = a.vlm_url
    if a.vlm_model:
        VLM_MODEL = a.vlm_model
    if a.dpi:
        VLM_DPI = a.dpi
    sys.exit(_probe(a.probe, a.page, a.count))
