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
# 표를 찾을 때 훑을 최대 쪽 수(치수표는 보통 앞쪽에 있다).
MAX_SCAN_PAGES = settings.get_int("STD_SPEC_MAX_PAGES", 6)
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


def read_table(pdf: str, pages: str = "", refresh: bool = False) -> dict:
    """스펙 PDF의 치수표를 판독한다. 반환: {rows, columns, id_column, pages, notes, ...}

    pages: "2,3" 처럼 쪽을 지정. 비우면 앞쪽부터 MAX_SCAN_PAGES쪽까지 훑어 표가
    나오는 쪽만 쓴다(치수표는 대개 앞에 있다).
    refresh=True면 캐시를 무시하고 다시 읽는다.
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
        if pages.strip():
            targets = [int(x) for x in re.split(r"[,\s]+", pages.strip()) if x.isdigit()]
        else:
            targets = list(range(1, min(total, MAX_SCAN_PAGES) + 1))
        results, notes = [], []
        for pno in targets:
            if not 1 <= pno <= total:
                notes.append(f"p{pno}는 이 문서에 없습니다(전체 {total}쪽)")
                continue
            png = vision_ingest.render_page(doc, pno)
            raw = vision_ingest.ask_image(png, TABLE_PROMPT)
            parsed = _parse_json(raw or "")
            n = len(parsed.get("rows", [])) if parsed else 0
            print(f"  p{pno} 판독: {n}행", file=sys.stderr)
            if n:
                results.append((pno, parsed))
            else:
                notes.append(f"p{pno}에서 표를 찾지 못했습니다")
    finally:
        doc.close()

    if not results:
        raise SpecError(
            f"'{os.path.basename(pdf)}'에서 치수표를 찾지 못했습니다. "
            "pages 인자로 쪽을 직접 지정해 보세요(예: pages='2,3'). "
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
        "rows": res.get("rows", []),
        "columns": res.get("columns", []),
        "id_column": res.get("id_column", ""),
        "table_title": res.get("table_title", ""),
        "notes": notes + list(mnotes or []),
    }
    _save_cache(pdf, data)
    return data


def read_legend(pdf: str, page: int = 1, refresh: bool = False) -> dict:
    """도면 그림에서 **치수 기호가 무엇을 재는지** 읽는다. 반환: {문자: {en, ko}}.

    표 판독과 별개로 계열당 한 번이면 충분해 캐시에 함께 저장한다.
    읽지 못하면 빈 사전을 돌려준다 — **없는 게 틀린 것보다 낫다.**
    """
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
        "0.350"    같은 값 (허용오차 ±tol)
        "<=0.350"  이하   — 셀이 범위면 **최대값**으로 본다(최악값 기준, 안전한 쪽)
        ">=0.20"   이상   — 셀이 범위면 최소값으로 본다
        "~0.35"    셀의 범위가 이 값을 **포함**한다 (그립이 체결두께를 받는지 등)
        ".190-32"  나사 규격 — 표기가 달라도 같은 나사면 맞다(10-32 == .1900-32)
        "A286"     문자열 — 공백·대소문자 무시 부분 일치
    """
    if cell is None:
        return False
    c = str(cond).strip()
    lo, hi = _cell_range(cell)

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
