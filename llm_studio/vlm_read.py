"""vlm_read.py

스캔 PDF(또는 이미지)를 사내 VLM(OpenAI 호환 API, 예: muse-glimmer)으로 읽어
표를 마크다운으로 옮기는 CLI. 표준 라이브러리 + pymupdf만 쓴다.

왜 "원본 + 타일"인가
-------------------
VLM 서버는 입력 이미지를 보통 긴 변 1~1.5천 px로 줄여서 본다. 스캔 규격서의 촘촘한 표를
페이지 통째로 넣으면 숫자가 뭉개진다. 그래서 페이지마다

    원본(전체 구조 파악용, 저해상도) + 고해상도 타일 N장(숫자 판독용)

을 **한 요청에** 넣고, 페이지 단위 요청을 병렬로 보낸다.

- 기본은 **가로 띠**로 자른다. 격자로 자르면 표의 한 행이 좌우 타일로 갈라져 "어느 숫자가
  같은 행인지"를 모델이 맞춰야 한다. 띠는 행이 한 타일 안에 온전히 들어간다.
  (그림 위주 페이지는 --split grid)
- 타일은 서로 겹친다(--overlap, 기본 12%) — 경계에 걸친 행이 잘리지 않게.

회전
----
가로로 긴 표를 90° 눕혀 넣고 쪽 머리글만 똑바로 인쇄한 스캔 페이지는, VLM이 똑바른 머리글에
속아 방향을 잘못 판단한다. 스캔본이라 텍스트 층이 없어 자동 판정도 못 하므로 --rotate로 지정한다.
값은 "읽으려면 **시계방향**으로 몇 도 돌릴지"(90/180/270) — 반시계로 누운 페이지는 90.
회전을 먼저 적용한 뒤 타일을 자른다(띠가 돌린 뒤의 표 행을 따라 잘리도록).

사용 (터미널에서 venv 활성화 후)
--------------------------------
    python vlm_read.py ARP1231.pdf --pages 2~12 --rotate 6~:90 --url http://<사내>/v1 --key <키>
    python vlm_read.py ARP1231.pdf --pages 6 --rotate 6:90 --dry-run   # 이미지만 만들어 확인
    python vlm_read.py ARP1231.pdf --pages 6~8 --runs 2                 # 두 번 읽고 불일치 비교
    python vlm_read.py scan1.png scan2.png --url ... --key ...          # 이미지는 파일마다 1쪽
    python vlm_read.py scan1.png --rotate 1:90 --url ... --key ...      # 이미지 회전은 1:각도

- 쪽번호는 **PDF 뷰어 기준**(1부터). 인쇄된 쪽번호가 아니다.
  표기: 2~12 / 2-12 / 2~5,8,10~12 / 6~ (끝까지). 생략하면 전체.
- --rotate도 같은 표기에 `:각도`를 붙인다: 6~:90 / 6~9:90,12:270.
  --pages 밖의 쪽을 가리키면 조용히 무시한다(문서 밖이면 오류).
- 키는 --key 대신 환경변수 VLM_API_KEY로도 줄 수 있다(플래그는 터미널 기록에 남는다).
  --url은 VLM_URL, --model은 VLM_MODEL로도 된다.

출력 — 실행마다 새 폴더, 기존 결과를 덮어쓰지 않는다
------------------------------------------------------
    vlm_out/<파일명>_<시각>/
        all.md       전 페이지 합본. **Nemotron에 그대로 붙여넣는 파일**
        diff.md      --runs 2일 때 두 결과가 다른 줄만
        run.json     실행 설정 (API 키는 저장하지 않음)
        prompt.txt   실제로 보낸 프롬프트
        errors.log   실패한 페이지와 사유 (실패가 있을 때만)
        pages/p006.md, p006.run2.md
        images/p006_full.png, p006_t1.png …   실제로 보낸 이미지 그대로

네트워크: --url로 지정한 사내 VLM 서버 한 곳만 호출한다(인터넷 불필요). 사내 https의
인증서 체인이 비표준이면 SSL_CERT_FILE 환경변수로 CA 번들을 지정한다.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# pymupdf 1.24.3+는 `import pymupdf`, 그 이전은 `import fitz` — 사내 미러 버전을 모르니 둘 다 받는다.
try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError:
        pymupdf = None  # type: ignore[assignment]

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
# 재시도해도 소용없는 HTTP 오류(주소·키·요청 형식 문제) — 바로 실패 처리한다.
NO_RETRY_STATUS = {400, 401, 403, 404, 405, 413, 422}

DEFAULT_PROMPT = """\
이 페이지의 내용을 옮겨 적어라. 해석하거나 요약하지 마라.

[이미지 구성]
- 첫 이미지는 페이지 전체다. 표의 구조(머리글, 열 위치, 어느 값이 어느 행인지)를 파악하는 데 쓴다.
- 그다음 이미지들은 같은 페이지를 확대한 타일이다. 타일끼리 서로 겹친다.
  숫자와 글자는 타일에서 읽는다. 전체 이미지와 타일이 다르게 보이면 타일을 따른다.
  겹친 부분에 있는 행은 한 번만 적는다.
- 쪽 머리글·바닥글은 옆으로 누워 보일 수 있다. 본문 방향을 기준으로 읽는다.

[출력 규칙]
1. 맨 위에 한 줄: `문서: <문서 번호/제목> | 페이지: <인쇄된 쪽번호>` (머리글·바닥글에서 읽는다. 없으면 `페이지: 없음`)
2. 표는 마크다운 표로 옮긴다.
   - 모든 행과 열을 빠짐없이 옮긴다. 병합된 셀은 해당하는 모든 칸에 같은 값을 채운다.
   - 숫자는 보이는 그대로 적는다. 자릿수, 소수점, ± 기호, 단위를 유지한다. 반올림·단위 환산을 하지 않는다.
   - 읽기 어려운 칸은 추측하지 말고 `[판독불가]`로 적는다.
   - 표 번호, 표 제목, 머리글(단위 포함), 각주를 그대로 포함한다.
3. 페이지에 그림(단면도 등)이 있으면 그림의 뜻을 해석하지 말고 아래만 적는다.
   - `그림 <번호>: <그림 제목>`
   - `그림 속 기호:` 그림에 적힌 문자·기호를 빠짐없이 쉼표로 나열한다.
   - 그림에 적힌 주석 문구는 그대로 옮긴다.
4. 표나 그림 밖의 본문 문장은 그대로 옮긴다.
"""


def log(msg: str) -> None:
    """진행 상황은 stderr로 — stdout에는 마지막 요약만 낸다."""
    print(msg, file=sys.stderr, flush=True)


# ─────────────────────────────── 쪽 범위 / 회전 지정 ───────────────────────────────


def _parse_range(part: str, n: int, src: str) -> tuple[int, int]:
    """'2~12' / '2-12' / '6~' / '~5' / '7' → (시작, 끝). 문서 밖이면 오류."""
    part = part.strip()
    m = re.fullmatch(r"(\d*)\s*[~\-]\s*(\d*)", part)
    if m:
        lo = int(m.group(1)) if m.group(1) else 1
        hi = int(m.group(2)) if m.group(2) else n
    elif part.isdigit():
        lo = hi = int(part)
    else:
        raise ValueError(f"{src}: 쪽 범위 '{part}'를 읽을 수 없습니다 (예: 2~12, 2~5,8,10~12, 6~).")
    if lo < 1 or lo > n or hi > n:
        raise ValueError(f"{src}: 쪽 범위 '{part}'가 문서 밖입니다 — 이 문서는 {n}쪽입니다.")
    if lo > hi:
        raise ValueError(f"{src}: 쪽 범위 '{part}'의 시작이 끝보다 큽니다.")
    return lo, hi


def parse_pages(spec: str | None, n: int, src: str) -> list[int]:
    """--pages 값을 쪽번호 목록으로. 생략하면 전체. 중복은 한 번만."""
    if not spec or not spec.strip():
        return list(range(1, n + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        if part.strip():
            lo, hi = _parse_range(part, n, src)
            pages.update(range(lo, hi + 1))
    return sorted(pages)


def parse_rotate(spec: str | None, n: int, src: str) -> dict[int, int]:
    """--rotate 값을 {쪽: 시계방향 각도}로. 예: '6~:90,12:270'."""
    rot: dict[int, int] = {}
    if not spec or not spec.strip():
        return rot
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        rng, sep, deg_s = item.rpartition(":")
        if not sep or not rng.strip():
            raise ValueError(
                f"{src}: 회전 지정 '{item}'에 각도가 없습니다 — 구간마다 ':각도'를 붙이세요 (예: 6~:90,12:270)."
            )
        try:
            deg = int(deg_s) % 360
        except ValueError:
            raise ValueError(f"{src}: 회전 각도 '{deg_s}'는 숫자여야 합니다 (90/180/270).") from None
        if deg not in (0, 90, 180, 270):
            raise ValueError(f"{src}: 회전 각도는 90/180/270만 됩니다 (받은 값: {deg_s}).")
        lo, hi = _parse_range(rng, n, src)
        for p in range(lo, hi + 1):
            rot[p] = deg
    return rot


# ─────────────────────────────── 렌더링 (회전 → 타일) ───────────────────────────────


def _span(i: int, n: int, overlap: float) -> tuple[float, float]:
    """n등분 중 i번째 구간을 앞뒤로 overlap(구간 길이 대비 비율)만큼 넓힌다."""
    step = 1.0 / n
    return max(0.0, i * step - overlap * step), min(1.0, (i + 1) * step + overlap * step)


def tile_boxes(split: str, tiles: int, overlap: float) -> list[tuple[tuple[float, float, float, float], str]]:
    """회전 후 페이지 기준의 타일 영역(비율 좌표)과 모델에게 보여줄 설명."""
    boxes = []
    if tiles <= 1:
        return boxes
    if split == "rows":
        for i in range(tiles):
            y0, y1 = _span(i, tiles, overlap)
            boxes.append(((0.0, y0, 1.0, y1),
                          f"타일 {i + 1}/{tiles} — 페이지 세로 {y0:.0%}~{y1:.0%} 구간 (위아래 타일과 겹침)"))
    else:
        total = tiles * tiles
        for r in range(tiles):
            for c in range(tiles):
                y0, y1 = _span(r, tiles, overlap)
                x0, x1 = _span(c, tiles, overlap)
                boxes.append(((x0, y0, x1, y1),
                              f"타일 {r * tiles + c + 1}/{total} (행{r + 1}·열{c + 1}) — "
                              f"가로 {x0:.0%}~{x1:.0%}, 세로 {y0:.0%}~{y1:.0%} (이웃 타일과 겹침)"))
    return boxes


def render_page(doc, pno: int, deg: int, native_zoom: float | None, args, img_dir: Path) -> list[tuple[str, Path]]:
    """한 쪽을 회전한 뒤 '원본 + 타일' PNG로 저장하고 (설명, 경로) 목록을 돌려준다.

    회전은 렌더링 행렬에 넣고, 타일 영역은 **회전 후 좌표**로 정한 뒤 역행렬로 원래 페이지
    좌표의 clip으로 바꾼다 — 그래야 띠가 돌린 뒤의 표 행을 따라 잘린다.
    prerotate(+90)이 시계방향 90°다(실측 확인).
    """
    page = doc[pno - 1]
    rot = pymupdf.Matrix(deg)
    rrect = page.rect * rot  # 회전 후 페이지 영역 (90° 배수라 경계 상자가 정확하다)
    cs = pymupdf.csRGB if args.color else pymupdf.csGRAY
    # PDF는 dpi로, 이미지는 원본 해상도 그대로(업샘플링해도 정보가 늘지 않는다).
    zoom = native_zoom if native_zoom else args.dpi / 72.0
    boxes = tile_boxes(args.split, args.tiles, args.overlap)
    out: list[tuple[str, Path]] = []

    # 원본: 타일이 있으면 구조 파악용으로 긴 변 --full-px까지 줄이고, 타일이 없으면 고해상도 그대로.
    long_side = max(rrect.width, rrect.height)
    fz = min(zoom, args.full_px / long_side) if boxes else zoom
    pix = page.get_pixmap(matrix=pymupdf.Matrix(fz, fz).prerotate(deg), colorspace=cs, alpha=False)
    path = img_dir / f"p{pno:03d}_full.png"
    pix.save(str(path))
    out.append(("페이지 전체" if boxes else "페이지 전체 (고해상도)", path))

    for i, ((x0, y0, x1, y1), label) in enumerate(boxes, 1):
        box = pymupdf.Rect(
            rrect.x0 + x0 * rrect.width, rrect.y0 + y0 * rrect.height,
            rrect.x0 + x1 * rrect.width, rrect.y0 + y1 * rrect.height,
        )
        clip = (box * ~rot).normalize()
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom).prerotate(deg),
                              clip=clip, colorspace=cs, alpha=False)
        path = img_dir / f"p{pno:03d}_t{i}.png"
        pix.save(str(path))
        out.append((label, path))
    return out


# ─────────────────────────────── VLM 호출 ───────────────────────────────


def _endpoint(url: str) -> str:
    """'http://host/v1' / '.../v1/' / '.../chat/completions' 어느 쪽을 줘도 받는다."""
    u = url.rstrip("/")
    return u if u.endswith("/chat/completions") else u + "/chat/completions"


def call_vlm(images: list[tuple[str, Path]], prompt: str, args) -> tuple[str, str | None]:
    """원본+타일을 한 요청에 넣어 보낸다. 이미지마다 앞에 설명 문구를 붙인다.

    Returns:
        (모델 출력, finish_reason)
    """
    content: list[dict] = [{"type": "text", "text": prompt}]
    for label, path in images:
        content.append({"type": "text", "text": f"[{label}]"})
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": content}],
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    req = urllib.request.Request(
        _endpoint(args.url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {args.key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    choice = data["choices"][0]
    text = choice["message"]["content"]
    if isinstance(text, list):  # 일부 서버는 content를 조각 목록으로 돌려준다
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return (text or "").strip(), choice.get("finish_reason")


def call_with_retry(images, prompt, args, tag: str) -> tuple[str, str | None]:
    """실패하면 --retries번 더 시도한다. 주소·키 문제(4xx)는 재시도하지 않는다."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return call_vlm(images, prompt, args)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            err = f"HTTP {e.code}: {detail}"
            if e.code in NO_RETRY_STATUS or attempt > args.retries:
                raise RuntimeError(err) from None
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
            err = f"{type(e).__name__}: {e}"
            if attempt > args.retries:
                raise RuntimeError(err) from None
        log(f"  {tag} 재시도 ({attempt}/{args.retries}) — {err}")
        time.sleep(2)


# ─────────────────────────────── 결과 쓰기 ───────────────────────────────


def page_header(stem: str, pno: int, deg: int, src_name: str, args, run: int, when: str) -> str:
    """페이지 파일 첫머리. 첫 줄은 키트의 [추출본] 형식, 둘째 줄은 추적용 HTML 주석."""
    rot = f"시계방향 {deg}° 회전" if deg else "회전 없음"
    if args.tiles <= 1:
        split = "타일 없음"
    elif args.split == "rows":
        split = f"가로띠 {args.tiles}개, 겹침 {args.overlap:.0%}"
    else:
        split = f"격자 {args.tiles}×{args.tiles}, 겹침 {args.overlap:.0%}"
    return (f"[추출본: {stem} p.{pno}]\n"
            f"<!-- 원본 {src_name} | PDF {pno}쪽 | {rot} | {split} | {args.model} | {when} | {run}회차 -->\n")


def _body(md: str) -> list[str]:
    """비교용 본문 — 머리 두 줄(추출본 표시, 주석)을 뺀다."""
    return md.split("\n")[2:]


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def write_diff(job: dict) -> int:
    """--runs 2의 두 결과에서 다른 줄만 diff.md로. 불일치가 있는 쪽 수를 돌려준다."""
    out = [f"# 두 번 읽기 불일치 — {job['stem']}", "",
           "두 결과가 다른 줄만 모았다. `images/pNNN_*.png`를 열어 이 줄만 원본과 대조한다.", ""]
    bad = 0
    for pno in job["pages"]:
        a = job["results"].get((pno, 1))
        b = job["results"].get((pno, 2))
        if a is None or b is None:
            out += [f"## p.{pno} — 비교 불가 (한쪽 읽기 실패)", ""]
            bad += 1
            continue
        la, lb = _body(a), _body(b)
        sm = difflib.SequenceMatcher(None, [_norm(x) for x in la], [_norm(x) for x in lb], autojunk=False)
        chunks = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            chunks += [f"- (1회차) {x}" for x in la[i1:i2]]
            chunks += [f"+ (2회차) {x}" for x in lb[j1:j2]]
            chunks.append("")
        if chunks:
            bad += 1
            n = sum(1 for op, *_ in sm.get_opcodes() if op != "equal")
            out += [f"## p.{pno} — 불일치 {n}곳", "", "```diff", *chunks[:-1], "```", ""]
        else:
            out += [f"## p.{pno} — 일치", ""]
    (job["dir"] / "diff.md").write_text("\n".join(out), encoding="utf-8")
    return bad


# ─────────────────────────────── 메인 ───────────────────────────────


def _new_dir(base: Path, stem: str, stamp: str) -> Path:
    d = base / f"{stem}_{stamp}"
    n = 2
    while d.exists():
        d = base / f"{stem}_{stamp}-{n}"
        n += 1
    (d / "pages").mkdir(parents=True)
    (d / "images").mkdir()
    return d


def main() -> int:
    ap = argparse.ArgumentParser(
        description="스캔 PDF/이미지를 사내 VLM으로 읽어 표를 마크다운으로 옮긴다 (원본+타일, 병렬).",
    )
    ap.add_argument("files", nargs="+", help="PDF 또는 이미지 파일 (여러 개 가능)")
    ap.add_argument("--pages", help="읽을 쪽 (PDF 뷰어 기준). 예: 2~12 / 2~5,8,10~12 / 6~. 생략하면 전체")
    ap.add_argument("--rotate", help="시계방향 회전. 예: 6~:90 / 6~9:90,12:270")
    ap.add_argument("--url", default=os.getenv("VLM_URL"), help="VLM 서버 주소 (예: http://host:port/v1)")
    ap.add_argument("--key", default=os.getenv("VLM_API_KEY"), help="API 키 (환경변수 VLM_API_KEY로도 가능)")
    ap.add_argument("--model", default=os.getenv("VLM_MODEL", "muse-glimmer"), help="모델 이름 (기본 muse-glimmer)")
    ap.add_argument("--split", choices=["rows", "grid"], default="rows",
                    help="rows: 가로 띠(표에 권장, 기본) / grid: 격자 N×N(그림에)")
    ap.add_argument("--tiles", type=int, default=3,
                    help="rows면 띠 개수, grid면 한 변 개수 (기본 3). 1이면 타일 없이 고해상도 원본만")
    ap.add_argument("--overlap", type=float, default=0.12, help="타일 겹침 비율 (기본 0.12)")
    ap.add_argument("--dpi", type=int, default=300, help="PDF 렌더링 해상도 (기본 300)")
    ap.add_argument("--full-px", type=int, default=1600, help="원본 이미지의 긴 변 픽셀 (기본 1600)")
    ap.add_argument("--color", action="store_true", help="컬러로 보낸다 (기본은 흑백 — 용량이 작다)")
    ap.add_argument("--workers", type=int, default=4, help="동시 요청 수 (기본 4)")
    ap.add_argument("--runs", type=int, default=1, choices=[1, 2], help="2면 두 번 읽고 diff.md 생성")
    ap.add_argument("--retries", type=int, default=1, help="실패 시 재시도 횟수 (기본 1)")
    ap.add_argument("--timeout", type=int, default=300, help="요청 하나의 제한 시간(초, 기본 300)")
    ap.add_argument("--max-tokens", type=int, default=8192, help="응답 최대 토큰 (기본 8192)")
    ap.add_argument("--temperature", type=float, default=0.0, help="기본 0 — 전사 작업이라 무작위성을 끈다")
    ap.add_argument("--prompt-file", help="기본 프롬프트 대신 쓸 텍스트 파일")
    ap.add_argument("--out", default="vlm_out", help="출력 상위 폴더 (기본 ./vlm_out)")
    ap.add_argument("--dry-run", action="store_true", help="VLM을 부르지 않고 회전·타일 이미지만 저장")
    args = ap.parse_args()

    if pymupdf is None:
        log("pymupdf가 필요합니다: pip install pymupdf")
        return 2
    if args.tiles < 1 or not (0.0 <= args.overlap < 0.5) or args.workers < 1:
        log("--tiles는 1 이상, --overlap은 0~0.5 미만, --workers는 1 이상이어야 합니다.")
        return 2
    if not args.dry_run and (not args.url or not args.key):
        log("--url과 --key가 필요합니다 (또는 환경변수 VLM_URL / VLM_API_KEY). 이미지만 보려면 --dry-run.")
        return 2

    prompt = DEFAULT_PROMPT
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")

    # ── 1) 전부 열어서 검증 — 요청을 하나라도 보내기 전에 잘못된 지정을 막는다 ──
    jobs: list[dict] = []
    for f in args.files:
        p = Path(f)
        if not p.is_file():
            log(f"파일이 없습니다: {f}")
            return 2
        try:
            doc = pymupdf.open(str(p))
        except Exception as e:  # noqa: BLE001 — 깨진 파일은 사유를 보여주고 멈춘다
            log(f"{p.name}: 열 수 없습니다 — {e}")
            return 2
        is_image = p.suffix.lower() in IMAGE_EXTS
        n = doc.page_count
        try:
            # 이미지 파일은 1쪽짜리 — --pages는 PDF에만 적용한다. 회전은 `--rotate 1:90`처럼 준다.
            pages = [1] if is_image else parse_pages(args.pages, n, p.name)
            rot = parse_rotate(args.rotate, n, p.name)
        except ValueError as e:
            log(str(e))
            return 2
        native_zoom = None
        if is_image:
            # 이미지는 원본 픽셀 그대로 렌더링되게 배율을 맞춘다.
            native_zoom = pymupdf.Pixmap(str(p)).width / doc[0].rect.width
        jobs.append({"path": p, "stem": p.stem, "doc": doc, "pages": pages,
                     "rot": {k: v for k, v in rot.items() if k in pages},
                     "native_zoom": native_zoom, "results": {}, "errors": {}, "cut": set()})

    # ── 2) 렌더링 (pymupdf 문서 객체는 스레드 공유가 안전하지 않아 메인 스레드에서 전부 끝낸다) ──
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    base = Path(args.out)
    for job in jobs:
        job["dir"] = _new_dir(base, job["stem"], stamp)
        job["images"] = {}
        for pno in job["pages"]:
            deg = job["rot"].get(pno, 0)
            job["images"][pno] = render_page(job["doc"], pno, deg, job["native_zoom"], args,
                                             job["dir"] / "images")
        log(f"{job['path'].name}: {len(job['pages'])}쪽 렌더링 완료 "
            f"(회전 {len(job['rot'])}쪽) → {job['dir']}")
        (job["dir"] / "prompt.txt").write_text(prompt, encoding="utf-8")
        (job["dir"] / "run.json").write_text(json.dumps({
            "file": str(job["path"].resolve()), "pages": job["pages"],
            "rotate_clockwise": {str(k): v for k, v in job["rot"].items()},
            "split": args.split, "tiles": args.tiles, "overlap": args.overlap,
            "dpi": args.dpi, "full_px": args.full_px, "color": args.color,
            "model": args.model, "url": args.url, "runs": args.runs,
            "max_tokens": args.max_tokens, "temperature": args.temperature,
            "prompt_sha1": hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12],
            "prompt_file": args.prompt_file, "dry_run": args.dry_run, "started": when,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.dry_run:
        for job in jobs:
            # 픽셀 크기를 보여 준다 — VLM 서버가 긴 변을 줄이는 방식이면, 폭이 너무 넓은 띠는
            # 줄어든 뒤 원본과 해상도가 비슷해져 타일의 이득이 사라진다. 판단 근거로 쓴다.
            first = job["pages"][0]
            sizes = []
            for label, path in job["images"][first]:
                px = pymupdf.Pixmap(str(path))
                sizes.append(f"{path.stem.split('_')[-1]} {px.width}×{px.height}")
            print(f"[dry-run] 이미지 확인: {job['dir'] / 'images'}")
            print(f"          p.{first} 픽셀 크기: " + ", ".join(sizes))
        return 0

    # ── 3) 페이지×회차 요청을 병렬로 ──
    tasks = [(job, pno, run) for job in jobs for pno in job["pages"] for run in range(1, args.runs + 1)]
    log(f"요청 {len(tasks)}건 전송 (동시 {args.workers})")

    def work(job, pno, run):
        tag = f"[{job['stem']} p.{pno}" + (f" {run}회차]" if args.runs > 1 else "]")
        t0 = time.time()
        text, finish = call_with_retry(job["images"][pno], prompt, args, tag)
        return tag, text, finish, time.time() - t0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, job, pno, run): (job, pno, run) for job, pno, run in tasks}
        for fut in as_completed(futs):
            job, pno, run = futs[fut]
            try:
                tag, text, finish, dt = fut.result()
            except Exception as e:  # noqa: BLE001 — 한 쪽이 실패해도 나머지는 계속한다
                job["errors"][(pno, run)] = str(e)
                log(f"  ✗ [{job['stem']} p.{pno} {run}회차] 실패 — {e}")
                continue
            md = page_header(job["stem"], pno, job["rot"].get(pno, 0), job["path"].name, args, run, when)
            md += "\n" + text + "\n"
            if finish == "length":
                # 조용히 잘리면 표 뒷부분이 빠진 줄 모른다 — 눈에 보이게 남긴다.
                md += "\n[⚠ 출력 잘림 — 이 페이지의 뒷부분이 빠졌을 수 있음. --max-tokens를 늘려 다시 읽을 것]\n"
                job["cut"].add((pno, run))
            job["results"][(pno, run)] = md
            name = f"p{pno:03d}.md" if run == 1 else f"p{pno:03d}.run{run}.md"
            (job["dir"] / "pages" / name).write_text(md, encoding="utf-8")
            log(f"  ✓ {tag} {dt:.0f}초" + (" ⚠ 출력 잘림" if finish == "length" else ""))

    # ── 4) 합본 / 불일치 / 오류 기록 ──
    failed_total = 0
    print()
    for job in jobs:
        parts = []
        for pno in job["pages"]:
            md = job["results"].get((pno, 1))
            if md is None:
                reason = job["errors"].get((pno, 1), "알 수 없음")
                # 실패한 쪽을 빼지 않고 자리표시를 남긴다 — 표가 빠진 줄 모르고 넘어가지 않게.
                md = f"[추출본: {job['stem']} p.{pno}]\n[추출 실패: p.{pno} — {reason}]\n"
            parts.append(md.rstrip())
        (job["dir"] / "all.md").write_text("\n\n---\n\n".join(parts) + "\n", encoding="utf-8")
        if job["errors"]:
            lines = [f"p.{p} {r}회차: {e}" for (p, r), e in sorted(job["errors"].items())]
            (job["dir"] / "errors.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        failed_total += len(job["errors"])

        ok = sum(1 for p in job["pages"] if (p, 1) in job["results"])
        print(f"{job['path'].name}: {ok}/{len(job['pages'])}쪽 성공 → {job['dir'] / 'all.md'}")
        if job["errors"]:
            print(f"  실패 {len(job['errors'])}건 — {job['dir'] / 'errors.log'}")
        if job["cut"]:
            print(f"  ⚠ 출력 잘림 {len(job['cut'])}건 — 해당 쪽만 --max-tokens를 늘려 다시 읽을 것")
        if args.runs == 2:
            bad = write_diff(job)
            print(f"  두 번 읽기: 불일치/비교불가 {bad}쪽 — {job['dir'] / 'diff.md'}")
    return 1 if failed_total else 0


if __name__ == "__main__":
    sys.exit(main())
