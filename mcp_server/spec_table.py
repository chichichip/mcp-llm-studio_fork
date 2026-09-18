"""spec_table.py — 표준품 스펙 도면의 치수표를 읽어 조건으로 거르는 층.

`standard_part_server`가 부품번호(dash)를 확정할 때 쓴다. spec-reader의 도메인 지식
(판독 프롬프트·다중 페이지 병합·검증 규칙)과 mcp_server의 VLM 배관(vision_ingest)을
잇는 어댑터다.

⚠ **`read_spec.py`를 import하지 않는다.** 그 파일은 `config.py`가 없으면 import 시점에
   `sys.exit()`으로 프로세스를 죽인다 — MCP 서버에서 부르면 서버가 통째로 내려간다.
   순수 로직인 `prompts`/`merge`/`verify`만 가져오고, VLM 호출은 이미 설정이 끝난
   vision_ingest(=local_settings.py의 RAG_VLM_*)를 쓴다. 그래서 사내 주소를 두 군데
   적을 필요도 없다.

왜 RAG가 아니라 직접 판독인가:
    치수표를 RAG에 넣으면 청크 경계에서 행이 잘리고 어느 dash의 값인지 흐려진다.
    부품번호를 정하는 자리에서는 **표 전체를 한 번에** 읽고, 검증을 거친 값만 써야 한다.
    도면은 검색할 필요도 없다 — 엑셀의 도면번호/품명이 곧 파일 이름이다.

선정의 두 갈래 (이 모듈은 앞의 것만 한다):
    직접 조건  "나사 .190-32, 머리 직경 0.35 이하"  → 표의 컬럼으로 거르기.
               **계열마다 규칙이 필요 없다** — 어느 표든 컬럼 값 비교는 같다.
    간접 조건  "체결두께 12mm" → 그립 → dash        → 계열별 규칙이 필요하다.
               지침서 조항이 확정되면 `selection.py`에 붙인다.

판독 결과는 캐시한다. VLM은 쪽당 수 초가 걸려서 같은 도면을 두 번 읽으면 안 된다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import settings
import vision_ingest

# spec-reader의 순수 로직만 가져온다(위 경고 참고). 폴더명에 하이픈이 있어 패키지
# import가 안 되므로 이 파일 기준 절대경로를 sys.path에 얹는다.
_SR = Path(__file__).resolve().parent.parent / "spec-reader"
if str(_SR) not in sys.path:
    sys.path.insert(0, str(_SR))

try:
    from prompts import TABLE_PROMPT
    from merge import merge_pages
    import verify as _verify

    SR_IMPORT_ERROR = ""
except Exception as e:  # noqa: BLE001
    TABLE_PROMPT = ""
    merge_pages = None  # type: ignore[assignment]
    _verify = None  # type: ignore[assignment]
    SR_IMPORT_ERROR = str(e)

# spec-reader의 TABLE_PROMPT는 **체결구 도면(MS/AS/NAS)에 맞춰 검증된** 프롬프트라
# 그대로 둔다(fixtures 회귀 시험이 걸려 있다). 다만 이 저장소는 그 외의 표도 읽는다 —
# AS568 오링 치수 목록처럼 도면이 아니라 **목록 문서**이고, 컬럼이 기호(H·K)가 아니라
# 낱말(`I.D. MILLIMETERS`)이며 단위가 인치만이 아니다. 그런 표에 원 프롬프트만 주면
# "해당하는 표가 없다"로 **0행**이 돌아온다 — 오류가 아니라 빈 결과라 원인이 안 보인다.
# 그래서 검증된 본문은 건드리지 않고 **호출 직전에 짧은 보충만** 덧붙인다.
TABLE_PROMPT_EXTRA = """

ALSO IMPORTANT - this document may NOT be a fastener drawing:
- It may be a size/dimension STANDARD LIST (for example an O-ring size standard).
  Then the table runs over many pages and the page has no drawing figure at all.
  Read it anyway - a page that is nothing but table rows is still the table.
- Column headers may be WORDS, not single letters (for example "I.D. INCHES",
  "I.D. MILLIMETERS", "W INCHES", "VOLUME"). Use the FULL header text as the key,
  exactly as printed, INCLUDING the unit word. Do NOT shorten it to a letter and do
  NOT merge two unit columns into one.
- Units are not always inches. Keep every value exactly as printed. Never convert.
- If the page is printed sideways, read it in whatever orientation makes the text
  upright, and still report the rows.
"""


# 도면 그림의 치수 기호 범례를 읽는 프롬프트.
#
# ★ 이게 왜 필요한가: 표 헤더에는 알파벳(H, K, L, T…)만 있고 **그게 무슨 치수인지는
#   도면 그림의 지시선에만** 있다. 모델이 'H니까 Height겠지'라고 추측하면 엉뚱한
#   컬럼으로 걸러 잘못된 부품이 나오고 아무도 못 알아챈다 — 되돌릴 수 없는 실패다.
#   그래서 **추측 대신 그림을 읽는다.** 이건 판단이 아니라 판독이라 VLM이 할 일이다.
#   모르는 기호는 지어내지 말고 빼라고 명시한다(빠진 건 사람이 채우면 되지만,
#   틀린 건 아무도 모른다).
LEGEND_PROMPT = """You are reading an aerospace fastener standard drawing (MS / AS / NAS).

Do NOT read the dimension table values. Instead look at the DRAWING FIGURE and find the
dimension letters (A, B, C, D, H, K, L, T, W, ...) that label dimensions with leader lines
or dimension lines. For each letter, describe WHAT PART of the geometry it measures.

Be specific about the feature and the direction:
- "head diameter (across flats)" vs "head diameter (across corners)" vs "head height"
- "overall length under head" vs "overall length including head"
- "grip length" vs "thread length"

Rules:
- Report ONLY letters you can actually see labelling a dimension in the figure.
- If you cannot tell what a letter measures, OMIT it. Do NOT guess.
- Also give a short Korean description for each, so a Korean engineer can match it.

Return ONLY a JSON object, no markdown fences, no commentary:

{
  "legend": {
    "L": {"en": "overall length under the head", "ko": "머리 밑 전체 길이"},
    "H": {"en": "head height", "ko": "머리 높이"},
    "K": {"en": "grip length (unthreaded shank)", "ko": "그립 길이(나사부 제외)"}
  }
}
"""

# 판독 캐시. 도면 하나당 JSON 하나.
CACHE_DIR = settings.get("STD_SPEC_CACHE", str(Path(__file__).with_name("spec_cache")))
# 표를 **찾기 위해** 훑을 최대 쪽 수(치수표는 보통 앞쪽에 있다).
MAX_SCAN_PAGES = settings.get_int("STD_SPEC_MAX_PAGES", 6)
# 표를 찾은 뒤 **이어서** 읽을 최대 쪽 수. 왜 따로 두는가 — 체결구 도면은 표가 한두
# 쪽이지만 AS568 같은 치수 목록은 4쪽부터 수십 쪽까지 이어진다. 찾기 창(6쪽)으로
# 끊으면 표의 앞부분만 읽고 조용히 멈춘다 — 행이 모자란 걸 아무도 못 알아챈다.
MAX_TABLE_PAGES = settings.get_int("STD_SPEC_MAX_TABLE_PAGES", 40)
SPEC_EXTS = (".pdf", ".PDF")


class SpecError(Exception):
    """호출부가 사용자에게 그대로 돌려줄 안내 메시지."""


# ─────────────────────────────── 도면 파일 찾기 ───────────────────────────────


def _norm_name(s: str) -> str:
    """파일명 비교용 키 — 공백·쉼표·대소문자 흔들림을 흡수한다."""
    return re.sub(r"[\s,_-]+", "", (s or "")).lower()


def find_spec_file(spec_dir: str, *names: str) -> str:
    """도면번호/품명 중 **아무거나 하나라도** 맞는 PDF를 찾는다. 없으면 빈 문자열.

    사내 파일은 도면번호와 품명이 둘 다 적혀 있는 경우가 많아, 어느 쪽으로 이름이
    붙었는지 모른다. 정확 일치 → 부분 일치 순으로 둘 다 시도한다.
    """
    root = os.path.abspath(os.path.expanduser(spec_dir or ""))
    if not os.path.isdir(root):
        raise SpecError(f"스펙 폴더가 없습니다: {root}")
    cands = [n for n in names if (n or "").strip()]
    if not cands:
        raise SpecError("도면번호나 품명 중 하나는 필요합니다.")

    files = []
    for dirpath, _dirs, fnames in os.walk(root):
        for f in fnames:
            if os.path.splitext(f)[1] in SPEC_EXTS and not f.startswith("~"):
                files.append(os.path.join(dirpath, f))

    keys = [(f, _norm_name(Path(f).stem)) for f in files]
    for name in cands:                                   # 정확 일치 먼저
        n = _norm_name(name)
        for f, k in keys:
            if k == n:
                return f
    for name in cands:                                   # 그다음 부분 일치
        n = _norm_name(name)
        if len(n) < 4:                                   # 너무 짧으면 오탐이 난다
            continue
        for f, k in keys:
            if n in k:
                return f
    return ""


# ─────────────────────────────── 판독 + 캐시 ───────────────────────────────


def _cache_path(pdf: str) -> Path:
    import hashlib

    h = hashlib.sha1(os.path.abspath(pdf).encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^\w.-]+", "_", Path(pdf).stem)[:50] or "spec"
    return Path(CACHE_DIR) / f"{stem}_{h}.json"


def load_cached(pdf: str) -> dict | None:
    """캐시된 판독 결과. 원본이 바뀌었으면(수정시각·크기) 무시한다."""
    p = _cache_path(pdf)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        st = os.stat(pdf)
        if data.get("mtime") == st.st_mtime and data.get("size") == st.st_size:
            return data
    except Exception:  # noqa: BLE001 — 깨진 캐시는 없는 셈 친다
        pass
    return None


def _save_cache(pdf: str, data: dict) -> None:
    try:
        p = _cache_path(pdf)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"[주의] 판독 캐시 저장 실패({e}) — 다음에 다시 읽습니다.", file=sys.stderr)


def _parse_json(text: str) -> dict | None:
    t = re.sub(r"^```(?:json)?\s*", "", (text or "").strip())
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def _parse_pages(spec: str, total: int) -> list[int]:
    """쪽 지정을 번호 목록으로. "4", "2,3", "4-12", "2 4-6" 을 모두 받는다.

    구간(`4-12`)을 받는 이유: 치수 목록은 "4쪽부터 끝까지"가 흔한데, 그걸 쉼표로
    일일이 적게 하면 사람이 중간을 빠뜨린다.
    """
    out: list[int] = []
    for tok in re.split(r"[,\s]+", (spec or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.extend(range(a, min(b, total) + 1))
        elif tok.isdigit():
            out.append(int(tok))
    seen: set[int] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _scan_pages(doc, targets: list[int], total: int, rot: int,
                notes: list[str]) -> list[tuple[int, dict]]:
    """주어진 쪽들을 한 각도로 판독한다. 반환: [(쪽번호, 판독결과)] (0행인 쪽은 뺀다)."""
    prompt = TABLE_PROMPT + TABLE_PROMPT_EXTRA
    tag = f"({rot}도)" if rot else ""
    out: list[tuple[int, dict]] = []
    for pno in targets:
        if not 1 <= pno <= total:
            continue
        png = vision_ingest.render_page(doc, pno, rotate=rot)
        raw = vision_ingest.ask_image(png, prompt)
        parsed = _parse_json(raw or "")
        n = len(parsed.get("rows", [])) if parsed else 0
        print(f"  p{pno}{tag} 판독: {n}행", file=sys.stderr)
        if n:
            out.append((pno, parsed))
        elif not rot:
            notes.append(f"p{pno}에서 표를 찾지 못했습니다")
    return out


def read_table(pdf: str, pages: str = "", refresh: bool = False,
               rotate: int = -1) -> dict:
    """스펙 PDF의 치수표를 판독한다. 반환: {rows, columns, id_column, pages, notes, ...}

    pages: "2,3" 또는 "4-12" 처럼 쪽을 지정. 비우면 앞에서부터 MAX_SCAN_PAGES쪽까지
        훑어 표가 나오는 쪽을 찾고, **찾은 뒤에는 표가 끊길 때까지 이어서** 읽는다
        (치수 목록은 수십 쪽까지 이어진다).
    refresh=True면 캐시를 무시하고 다시 읽는다.
    rotate: 시계방향 회전각. -1(기본)이면 자동 — 글자 방향으로 먼저 보고, 그래도
        한 쪽도 못 읽으면 90도·270도로 다시 시도한다.
    """
    if SR_IMPORT_ERROR:
        raise SpecError(f"판독 모듈을 불러오지 못했습니다: {SR_IMPORT_ERROR}")
    if not os.path.isfile(pdf):
        raise SpecError(f"스펙 파일이 없습니다: {pdf}")
    if not refresh:
        got = load_cached(pdf)
        if got:
            return got
    if not vision_ingest.FITZ_AVAILABLE:
        raise SpecError(
            f"PDF를 렌더링할 수 없습니다(PyMuPDF 없음: {vision_ingest.FITZ_IMPORT_ERROR}). "
            "venv에 PyMuPDF를 설치하세요."
        )
    ok, why = vision_ingest.vlm_check()
    if not ok:
        raise SpecError(f"VLM 서버에 연결하지 못했습니다 — {why}")

    doc = vision_ingest.fitz.open(pdf)
    try:
        total = len(doc)
        explicit = bool(pages.strip())
        if explicit:
            targets = _parse_pages(pages, total)
        else:
            targets = list(range(1, min(total, MAX_SCAN_PAGES) + 1))
        notes: list[str] = []
        for pno in targets:
            if not 1 <= pno <= total:
                notes.append(f"p{pno}는 이 문서에 없습니다(전체 {total}쪽)")

        # 회전 계획 — 사람이 준 각도 > 글자 방향(공짜) > 0도.
        if rotate >= 0:
            plan = [int(rotate) % 360]
        else:
            auto = vision_ingest.detect_rotation(doc, targets)
            if auto:
                print(f"[진행] 글자 방향으로 보아 {auto}도 돌려 읽습니다.", file=sys.stderr)
            plan = [auto]

        results = _scan_pages(doc, targets, total, plan[0], notes)
        used_rot = plan[0]

        # 한 쪽도 못 읽었고 각도를 사람이 지정하지 않았다면, 누운 페이지를 의심한다.
        # **전멸했을 때만** 재시도한다 — 일부라도 읽혔으면 문서는 바로 선 것이고,
        # 그때 각도를 더 시도하면 쪽마다 수십 초를 헛되이 더 쓴다.
        if not results and rotate < 0:
            for rot in (90, 270, 180):
                if rot == plan[0]:
                    continue
                print(f"[진행] 표를 못 찾아 {rot}도로 돌려 다시 시도합니다.", file=sys.stderr)
                results = _scan_pages(doc, targets, total, rot, notes)
                if results:
                    used_rot = rot
                    notes.append(f"페이지가 누워 있어 {rot}도 돌려 판독했습니다.")
                    break

        # 표를 찾았고 쪽을 지정받지 않았다면, **끊길 때까지 이어서** 읽는다.
        if results and not explicit:
            pno = max(p for p, _ in results)
            limit = min(total, pno + MAX_TABLE_PAGES)
            while pno < limit:
                pno += 1
                more = _scan_pages(doc, [pno], total, used_rot, [])
                if not more:
                    break
                results.extend(more)
            if pno >= limit and limit < total:
                notes.append(
                    f"p{limit}까지만 읽었습니다(STD_SPEC_MAX_TABLE_PAGES={MAX_TABLE_PAGES}). "
                    "표가 더 길면 pages 인자로 범위를 지정하세요."
                )
    finally:
        doc.close()

    if not results:
        raise SpecError(
            f"'{os.path.basename(pdf)}'에서 치수표를 찾지 못했습니다. "
            f"훑어본 쪽: {targets} (전체 {total}쪽). "
            "표가 그 뒤에 있으면 pages 인자로 지정하세요(예: pages='4-12'). "
            "CLI로는 python spec_table.py <도면> --dir <폴더> --pages 4-12 --rotate 90 "
            "처럼 쪽과 각도를 직접 줄 수 있습니다. "
            + ("; ".join(notes[:3]) if notes else "")
        )

    res, mnotes = merge_pages(results)
    st = os.stat(pdf)
    data = {
        "pdf": os.path.abspath(pdf),
        "mtime": st.st_mtime,
        "size": st.st_size,
        "read_at": time.time(),
        "pages_used": [p for p, _ in results],
        "rotate": used_rot,
        "rows": res.get("rows", []),
        "columns": res.get("columns", []),
        "id_column": res.get("id_column", ""),
        "table_title": res.get("table_title", ""),
        "notes": notes + list(mnotes or []),
    }
    _save_cache(pdf, data)
    return data


def needs_legend(columns) -> bool:
    """이 표에 치수 기호 범례가 필요한가.

    범례는 컬럼이 `H`·`K`·`L` 같은 **알파벳 기호**일 때만 의미가 있다. AS568처럼
    컬럼이 `I.D. MILLIMETERS` 같은 낱말이면 뜻이 이미 적혀 있으므로 도면 그림을
    다시 판독할 이유가 없다 — VLM 호출 한 번은 사내 게이트웨이에서 수십 초라
    그냥 낭비이고, 읽어 봐야 엉뚱한 범례가 붙을 위험만 생긴다.
    """
    for c in (columns or []):
        t = re.sub(r"[^A-Za-z0-9]", "", str(c))
        if 1 <= len(t) <= 2 and t.isalpha():
            return True
    return False


def read_legend(pdf: str, page: int = 1, refresh: bool = False, columns=None) -> dict:
    """도면 그림에서 **치수 기호가 무엇을 재는지** 읽는다. 반환: {문자: {en, ko}}.

    표 판독과 별개로 계열당 한 번이면 충분해 캐시에 함께 저장한다.
    읽지 못하면 빈 사전을 돌려준다 — **없는 게 틀린 것보다 낫다.**

    columns를 주면 **기호 컬럼이 없는 표에서는 VLM을 아예 부르지 않는다**(needs_legend).
    """
    if columns is not None and not needs_legend(columns):
        return {}
    cached = load_cached(pdf) or {}
    if not refresh and cached.get("legend") is not None:
        return cached.get("legend") or {}
    if not vision_ingest.FITZ_AVAILABLE:
        return {}
    ok, _why = vision_ingest.vlm_check()
    if not ok:
        return {}
    try:
        doc = vision_ingest.fitz.open(pdf)
    except Exception:  # noqa: BLE001
        return {}
    try:
        if not 1 <= page <= len(doc):
            return {}
        png = vision_ingest.render_page(doc, page)
    finally:
        doc.close()
    parsed = _parse_json(vision_ingest.ask_image(png, LEGEND_PROMPT) or "") or {}
    legend = parsed.get("legend") or {}
    # 값 모양을 통일한다(모델이 문자열만 줄 때도 있다).
    out: dict[str, dict] = {}
    for k, v in legend.items():
        if isinstance(v, dict):
            out[str(k)] = {"en": str(v.get("en", "")), "ko": str(v.get("ko", ""))}
        elif v:
            out[str(k)] = {"en": str(v), "ko": ""}
    if cached:
        cached["legend"] = out
        _save_cache(pdf, cached)
    return out


def format_legend(legend: dict, columns: list[str] | None = None) -> str:
    """치수 기호 범례를 사람이 읽을 형태로. 표에 있는 컬럼만 보여준다."""
    if not legend:
        return ("(치수 기호 범례를 읽지 못했습니다 — 각 알파벳이 무슨 치수인지 "
                "도면 그림을 직접 확인하세요. 추측해서 조건을 걸면 안 됩니다.)")
    keys = [c for c in (columns or legend.keys()) if c in legend] or list(legend)
    lines = []
    for k in keys:
        v = legend.get(k) or {}
        ko, en = v.get("ko", ""), v.get("en", "")
        lines.append(f"  {k:<6} {ko}" + (f"  ({en})" if en else ""))
    missing = [c for c in (columns or []) if c not in legend]
    if missing:
        lines.append(f"  ⚠ 그림에서 못 읽은 기호: {', '.join(missing)} — 도면을 직접 확인하세요")
    return "\n".join(lines)


def resolve_columns(conditions: dict, columns: list[str],
                    legend: dict) -> tuple[dict, list[str]]:
    """조건의 키를 **표에 실제로 있는 컬럼 이름**으로 바꾼다.

    반환: (해석된 조건, 문제 목록). 문제가 하나라도 있으면 호출부는 **거절해야 한다** —
    조건이 안 걸린 채로 거르면 '전부 통과'가 되어 잘못된 부품을 고르게 된다.

    알파벳을 그대로 주면 그대로 쓴다. 한국어/영어 설명을 주면 범례에서 찾는데,
    **여럿에 걸리면 고르지 않고 되묻는다** — 여기서 찍으면 되돌릴 수 없다.
    """
    cols = list(columns or [])
    colset = {c.upper(): c for c in cols}
    resolved: dict = {}
    problems: list[str] = []
    for key, cond in conditions.items():
        k = str(key).strip()
        if k in cols:
            resolved[k] = cond
            continue
        if k.upper() in colset:
            resolved[colset[k.upper()]] = cond
            continue
        # 설명으로 찾기 — 범례의 한국어/영어 문구에 부분 일치
        needle = re.sub(r"\s+", "", k).lower()
        hits = []
        for letter, v in (legend or {}).items():
            if letter not in cols:
                continue
            text = re.sub(r"\s+", "", f"{v.get('ko','')}{v.get('en','')}").lower()
            if needle and needle in text:
                hits.append(letter)
        if len(hits) == 1:
            resolved[hits[0]] = cond
        elif len(hits) > 1:
            problems.append(
                f"'{k}' 이(가) 여러 기호에 해당합니다: {', '.join(hits)}. "
                "어느 것인지 지정해 주세요."
            )
        else:
            problems.append(
                f"'{k}' 이(가) 표의 어느 컬럼인지 확인되지 않았습니다. "
                f"표의 컬럼: {', '.join(cols)}"
            )
    return resolved, problems


def verify_table(data: dict, expect_lk: float | None = None) -> tuple[str, bool]:
    """판독 결과를 검증한다. 반환: (사람이 읽을 요약, 통과 여부).

    검증 규칙은 데이터에서 찾은 불변식이다(L−K_max 계열 상수, L이 1/16" 격자,
    dash 중복·누락). 검증을 못 하는 표라도 **못 했다고 말할 뿐** 막지는 않는다 —
    대신 결과에 그 사실을 남겨 사람이 판단하게 한다.
    """
    if _verify is None:
        return "검증 모듈 없음 — 판독값이 검증되지 않았습니다", False
    try:
        txt, passed = _verify.report(data.get("rows", []), expect_lk, None,
                                     label=(data.get("table_title") or "")[:30])
        return txt, passed
    except Exception as e:  # noqa: BLE001 — 검증 실패가 판독 결과를 못 쓰게 만들면 안 된다
        return f"검증을 수행하지 못했습니다({type(e).__name__}: {e})", False


# ─────────────────────────────── 조건으로 거르기 ───────────────────────────────
# 표의 컬럼 값을 비교하는 일반 규칙 — **계열마다 다르지 않다.** 어느 표든 "이 컬럼이
# 이 값인 행"은 같은 방식으로 찾는다. 그래서 볼트든 클램프든 오링이든 이 코드가 쓰인다.

_NUMS = re.compile(r"\d*\.\d+|\d+")


def _cell_range(v) -> tuple[float | None, float | None]:
    """셀 값에서 (최소, 최대). '.062-.082' → (0.062, 0.082), '.250' → (0.250, 0.250)."""
    if v is None:
        return None, None
    nums = [float(x) for x in _NUMS.findall(str(v))]
    if not nums:
        return None, None
    return min(nums), max(nums)


def _looks_thread(v) -> bool:
    """'.1900-32', '10-32 UNJF-3A' 처럼 나사 표기인가."""
    return bool(re.match(r"^\s*#?\d*\.?\d+\s*[-/]\s*\d+", str(v or "")))


def match(cell, cond: str, tol: float = 0.0005) -> bool:
    """셀 값 하나가 조건에 맞는가.

    조건 표기:
        "0.350"      같은 값 (허용오차 ±tol)
        "2.8..3.4"   **이 구간 안에 드는 값** — 치수표에서 후보를 추리는 주력 표기
        "<=0.350"    이하   — 셀이 범위면 **최대값**으로 본다(최악값 기준, 안전한 쪽)
        ">=0.20"     이상   — 셀이 범위면 최소값으로 본다
        "~0.35"      셀의 범위가 이 값을 **포함**한다 (그립이 체결두께를 받는지 등)
        ".190-32"    나사 규격 — 표기가 달라도 같은 나사면 맞다(10-32 == .1900-32)
        "A286"       문자열 — 공백·대소문자 무시 부분 일치

    `a..b`와 `~x`를 헷갈리지 말 것 — **방향이 반대다.**
        `~x`     : 셀이 범위이고 그 안에 x가 들어오나  (조건이 점, 셀이 범위)
        `a..b`   : 셀 값이 a~b 구간에 드나            (조건이 범위, 셀이 점)
    치수표(AS568 등)는 값이 이산적이라 "정확히 3.1mm"인 행이 대개 없다. 점으로 물으면
    늘 0건이 나와 쓸모가 없으므로, **구간으로 물어 후보 몇 개를 받는 것**이 정상 사용법이다.
    셀 자체가 범위면(공차 표기) 구간과 **겹치기만 해도** 통과시킨다 — 공차 안에 답이
    있을 수 있는 행을 미리 지우면 안 되기 때문이다(거르는 쪽이 아니라 남기는 쪽으로 틀린다).
    """
    if cell is None:
        return False
    c = str(cond).strip()
    lo, hi = _cell_range(cell)

    # 구간 조건 `a..b` — 다른 연산자보다 먼저 본다(`..`가 숫자 파싱에 걸리지 않게).
    m = re.match(r"^\s*(-?[\d.]+)\s*\.\.\s*(-?[\d.]+)\s*$", c)
    if m:
        try:
            a, b = float(m.group(1)), float(m.group(2))
        except ValueError:
            return False
        if a > b:
            a, b = b, a
        if lo is None:
            return False
        # 셀이 범위면 구간과 겹치기만 해도 통과 (공차 안에 답이 있을 수 있다).
        return lo <= b + tol and hi >= a - tol

    m = re.match(r"^(<=|>=|<|>|~)\s*(.+)$", c)
    if m:
        op, val = m.group(1), m.group(2).strip()
        try:
            x = float(val)
        except ValueError:
            return False
        if lo is None:
            return False
        if op == "~":                      # 범위가 값을 포함
            return lo - tol <= x <= hi + tol
        if op == "<=":
            return hi <= x + tol           # 최악값(최대)이 기준을 넘지 않아야 한다
        if op == "<":
            return hi < x - tol
        if op == ">=":
            return lo >= x - tol
        return lo > x + tol                # ">"

    # 나사 규격 — 표기가 달라도 같은 나사면 통과시킨다.
    if _looks_thread(c) and _looks_thread(cell):
        try:
            import selection  # spec-reader (sys.path에 이미 올려 뒀다)

            return selection.threads_match(cell, c)
        except Exception:  # noqa: BLE001 — 못 부르면 아래 일반 비교로
            pass

    try:                                    # 숫자면 값 비교
        x = float(c)
        return lo is not None and abs(lo - x) <= tol and abs(hi - x) <= tol
    except ValueError:
        pass

    a = re.sub(r"\s+", "", str(cell)).upper()
    b = re.sub(r"\s+", "", c).upper()
    return b in a


def filter_rows(rows: list[dict], conditions: dict) -> tuple[list[dict], list[str]]:
    """조건을 **모두** 만족하는 행. 반환: (맞는 행들, 알림).

    조건에 없는 컬럼 이름을 주면 조용히 무시하지 않고 알린다 — 조건이 안 걸린 채
    "전부 통과"로 보이면 잘못된 부품을 고르게 된다.
    """
    notes: list[str] = []
    if not rows:
        return [], ["표가 비어 있습니다."]
    cols = {k for r in rows for k in r}
    for name in conditions:
        if name not in cols:
            near = ", ".join(sorted(cols))
            notes.append(f"'{name}' 컬럼이 표에 없습니다 — 이 조건은 적용되지 않았습니다. "
                         f"표의 컬럼: {near}")
    active = {k: v for k, v in conditions.items() if k in cols}
    if not active:
        return list(rows), notes + ["적용된 조건이 없어 전체 행을 돌려줍니다."]
    hits = [r for r in rows if all(match(r.get(k), v) for k, v in active.items())]
    return hits, notes


def near_rows(rows: list[dict], column: str, target: float, n: int = 3) -> list[dict]:
    """조건에 맞는 게 없을 때 참고로 보여줄 '가까운' 행들. **선정이 아니다.**"""
    scored = []
    for r in rows:
        lo, hi = _cell_range(r.get(column))
        if lo is None:
            continue
        scored.append((min(abs(lo - target), abs(hi - target)), r))
    scored.sort(key=lambda t: t[0])
    return [r for _d, r in scored[:n]]


def format_rows(rows: list[dict], columns: list[str] | None = None, limit: int = 40) -> str:
    """행들을 사람이 눈으로 볼 표로. 사내망에서는 화면을 보고 구두로 옮겨야 한다."""
    if not rows:
        return "(없음)"
    cols = list(columns) if columns else []
    if not cols:
        seen: list[str] = []
        for r in rows:
            for k in r:
                if k != "dash" and k not in seen:
                    seen.append(k)
        cols = seen
    head = ["dash"] + cols
    widths = [max(len(h), 8) for h in head]
    for r in rows[:limit]:
        cells = [str(r.get("dash", ""))] + [str(r.get(c, "")) for c in cols]
        widths = [max(w, len(c)) for w, c in zip(widths, cells)]
    out = [" ".join(h.ljust(w) for h, w in zip(head, widths))]
    out.append("-" * (sum(widths) + len(widths)))
    for r in rows[:limit]:
        cells = [str(r.get("dash", ""))] + [str(r.get(c, "")) for c in cols]
        out.append(" ".join(c.ljust(w) for c, w in zip(cells, widths)))
    if len(rows) > limit:
        out.append(f"… 외 {len(rows) - limit}행")
    return "\n".join(out)


# ─────────────────────────── 진단 CLI ───────────────────────────
# 앱의 MCP 도구 호출에는 제한 시간이 있는데(llm_studio 기본 120초), 표가 여러 쪽이면
# VLM 판독이 그걸 넘긴다. 그러면 도구는 타임아웃으로 죽고 캐시도 안 남아 **몇 번을
# 시도해도 같은 자리에서 실패한다.** 여기서 미리 한 번 읽어 캐시에 넣어 두면, 이후
# 앱에서의 호출은 캐시를 읽어 즉시 끝난다.
#
#   python spec_table.py AS568 --dir C:\rag\vision
#   python spec_table.py AS568 --dir C:\rag\vision --pages 4-12 --rotate 90 --refresh
#
# 컬럼 이름을 눈으로 확인하는 용도이기도 하다 — select_dash 조건을 그 이름 그대로
# 적어야 하기 때문이다.


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="스펙 치수표를 미리 판독해 캐시에 넣는다(앱의 호출 제한 밖에서).")
    ap.add_argument("drawing", help="도면번호 또는 품명 (파일 이름의 일부)")
    ap.add_argument("--dir", default="", help="스펙 PDF 폴더 (비우면 STD_SPEC_DIR)")
    ap.add_argument("--pages", default="",
                    help='쪽 지정 "2,3" 또는 "4-12" (비우면 앞에서부터 훑고 이어 읽음)')
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 다시 판독")
    ap.add_argument("--rotate", type=int, default=-1,
                    help="시계방향 회전각 0/90/180/270 (기본 자동). 페이지가 누워 있을 때")
    ap.add_argument("--rows", type=int, default=10, help="보여줄 행 수 (기본 10)")
    a = ap.parse_args()

    folder = a.dir or settings.get("STD_SPEC_DIR", "")
    if not folder:
        print("스펙 폴더를 --dir 로 주거나 local_settings.py에 STD_SPEC_DIR을 적으세요.",
              file=sys.stderr)
        return 2
    pdf = find_spec_file(folder, a.drawing)
    if not pdf:
        print(f"'{a.drawing}' 스펙 파일을 {folder} 에서 찾지 못했습니다.", file=sys.stderr)
        return 2
    print(f"파일: {pdf}")
    print("판독 중… (쪽마다 VLM 호출이라 수십 초 걸릴 수 있습니다)", file=sys.stderr)

    t0 = time.time()
    data = read_table(pdf, pages=a.pages, refresh=a.refresh, rotate=a.rotate)
    cols = data.get("columns") or []
    rows = data.get("rows") or []
    rot = data.get("rotate") or 0
    print(f"판독 {time.time() - t0:.1f}초, 쪽 {data.get('pages_used')}, 행 {len(rows)}개"
          + (f", 회전 {rot}도" if rot else ""))
    for n in (data.get("notes") or [])[:5]:
        print(f"  · {n}")
    print()
    print("컬럼 (select_dash 조건에 이 이름을 그대로 쓰세요):")
    for c in cols:
        print(f"  - {c}")
    print()
    if needs_legend(cols):
        legend = read_legend(pdf, refresh=a.refresh, columns=cols)
        print(format_legend(legend, cols))
        print()
    else:
        print("치수 기호 범례: 필요 없음 (컬럼이 낱말이라 뜻이 이미 적혀 있음)")
        print()
    vtxt, ok = verify_table(data)
    print(f"[검증] {vtxt}")
    if not ok:
        print("⚠ 검증을 통과하지 못했습니다 — 도면을 직접 확인하세요.")
    print()
    print(format_rows(rows[: max(1, a.rows)], cols))
    print()
    print(f"캐시: {_cache_path(pdf)}")
    print("이제 앱에서 read_spec_table / select_dash 를 부르면 이 캐시를 씁니다.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
