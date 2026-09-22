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


# 되돌아가는 길: JSON이 안 나올 때 **그냥 표로 옮겨 적게** 한다.
#
# ★ 왜 이게 필요한가 — 실측으로 확인된 것: 같은 쪽을 `PAGE_PROMPT`(마크다운 전사)로
#   읽으면 30행이 멀쩡히 나오는데 `TABLE_PROMPT`(JSON)로 읽으면 0행이었다. 이미지도
#   VLM도 멀쩡한데 **JSON을 요구하는 순간** 빈 결과가 돌아온 것이다. TABLE_PROMPT는
#   체결구 도면(MS/AS/NAS)에 맞춰 쓰인 프롬프트라, AS568 같은 '목록 문서'를 보면
#   모델이 "이건 그 표가 아니다"라고 판단해 버린다.
#
#   그래서 JSON을 고집하지 않고, **이미 되는 것으로 물러선다**: 표만 그대로 옮겨
#   적게 하고 그 마크다운을 우리가 행으로 바꾼다. 판단은 우리가 하고 VLM은 판독만
#   한다는 원칙(spec-reader CLAUDE.md)에도 이쪽이 더 맞다.
PLAIN_TABLE_PROMPT = """Transcribe the table on this page. Do not interpret it.

Rules:
- Output a markdown table: a header row, a separator row, then one row per data row.
- Copy every cell EXACTLY as printed. Do not round, convert units, or reorder.
- Use the full column header text as printed, including any unit words.
- If the table is split into several column groups side by side, read every group and
  output them as ONE table with one header. Missing a group is the most common mistake.
- If a cell is unreadable, write ? . Do not guess. Do not invent rows.
- Output ONLY the table. No title, no explanation, no code fences.

If this page has no table at all, output exactly: NO TABLE
"""

# 쪽을 위아래로 나눠 읽을 때, 아래 칸에는 **머리글이 없다**(머리글은 맨 위 칸에만
# 있다). 위 칸에서 얻은 컬럼 이름을 알려 줘야 같은 표로 이어 붙일 수 있다.
BAND_PROMPT_TAIL = """

NOTE: this image is the CONTINUATION of a table that started above, so the header row
may be missing or cut off. Use EXACTLY these column headers, in this order:
{columns}
Output the header row anyway, then the data rows you can see."""


def _rows_from_markdown(text: str) -> dict | None:
    """마크다운 표(`| a | b |`)를 판독 결과 모양으로 바꾼다. 표가 없으면 None.

    첫 컬럼이 dash 번호라고 본다(치수표의 관례이고 TABLE_PROMPT도 같은 전제다).
    구분선(`|---|---|`)과 헤더가 되풀이되는 줄은 버린다 — 좌우 그룹을 이어 붙이면
    가운데에 헤더가 한 번 더 끼는 경우가 있다.
    """
    # 잘린 응답은 마지막 줄이 도중에 끊겨 있다. 그 줄만 버리고 **앞의 온전한 행은
    # 건진다** — 잘렸다고 통째로 버리면 어차피 다시 읽어야 하고, 부분이라도 있으면
    # 쪽을 나눠 읽을 때 겹쳐서 채워진다.
    raw_lines = (text or "").splitlines()
    if raw_lines and raw_lines[-1].startswith("|") and not raw_lines[-1].rstrip().endswith("|"):
        raw_lines = raw_lines[:-1]
    lines = [ln.strip() for ln in raw_lines if ln.strip().startswith("|")]
    rows_raw = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if not cells or all(re.fullmatch(r"[-: ]*", c) for c in cells):
            continue                       # 구분선
        rows_raw.append(cells)
    if len(rows_raw) < 2:
        return None

    header = rows_raw[0]
    width = len(header)
    cols = [c for c in header[1:] if c]
    out = []
    for cells in rows_raw[1:]:
        if cells == header:                # 되풀이된 헤더
            continue
        cells = (cells + [""] * width)[:width]
        rec = {"dash": cells[0].lstrip("-").strip()}
        for name, val in zip(header[1:], cells[1:]):
            if name:
                rec[name] = val
        if rec["dash"]:
            out.append(rec)
    if not out:
        return None
    return {"id_column": header[0], "columns": cols, "rows": out,
            "table_title": "", "source": "markdown"}


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
# 쪽이지만 AS568 같은 치수 목록은 3쪽부터 수십 쪽까지 이어진다. 찾기 창(6쪽)으로
# 끊으면 표의 앞부분만 읽고 조용히 멈춘다 — 행이 모자란 걸 아무도 못 알아챈다.
MAX_TABLE_PAGES = settings.get_int("STD_SPEC_MAX_TABLE_PAGES", 40)
# 한 번에 쓸 수 있는 시간(초). 넘으면 **읽은 데까지 저장하고 멈춘다** — 다시 돌리면
# 캐시에서 이어간다. 사내 게이트웨이는 쪽당 수십 초라 40쪽이면 30분이 넘는데, 끝이
# 언제인지 모르는 채 기다리다 창을 닫아 버리는 게 실제로 일어났다.
TIME_BUDGET = settings.get_float("STD_SPEC_TIME_BUDGET", 1800)
# 한 쪽을 최대 몇 칸까지 나눠 읽을지. 여기까지 나눠도 응답이 잘리면 더 나누는 것은
# 답이 아니다(한도가 비정상으로 낮은 것) — 사유를 남기고 사람에게 넘긴다.
MAX_BANDS = settings.get_int("STD_SPEC_MAX_BANDS", 8)
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


# ─────────────────── 쪽 단위 캐시 (중간에 끊겨도 잃지 않게) ───────────────────
# 왜 쪽마다 저장하는가: 사내 게이트웨이는 쪽당 수십 초라 20쪽이면 10분이 넘는다.
# 판독이 다 끝난 뒤에 한 번만 저장하면 **중간에 Ctrl+C 하거나 창을 닫는 순간 그
# 시간이 통째로 날아가고**, 다시 돌려도 같은 자리에서 또 기다려야 한다 — 실제로
# 그렇게 잃었다. 쪽마다 적어 두면 어디서 끊기든 다음 실행이 이어간다.
#
# 회전각별로 따로 담는다. 각도를 바꿔 재시도할 때 앞서 읽은 것을 버리지 않기 위해서다.


def _page_cache_path(pdf: str) -> Path:
    return _cache_path(pdf).with_name(_cache_path(pdf).stem + "_pages.json")


def _load_pages(pdf: str) -> dict[int, dict[int, dict]]:
    """{회전각: {쪽번호: 판독결과}}. 원본이 바뀌었으면 버린다."""
    f = _page_cache_path(pdf)
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        st = os.stat(pdf)
        if data.get("mtime") != st.st_mtime or data.get("size") != st.st_size:
            return {}
        return {int(rot): {int(p): v for p, v in pages.items()}
                for rot, pages in (data.get("pages") or {}).items()}
    except Exception:  # noqa: BLE001 — 깨진 캐시는 없는 셈 친다
        return {}


def _save_pages(pdf: str, cache: dict[int, dict[int, dict]]) -> None:
    """쪽 캐시를 파일에 쓴다. 못 써도 판독은 계속한다(우아한 저하)."""
    try:
        st = os.stat(pdf)
        f = _page_cache_path(pdf)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(
            {"pdf": os.path.abspath(pdf), "mtime": st.st_mtime, "size": st.st_size,
             "pages": {str(rot): {str(p): v for p, v in pages.items()}
                       for rot, pages in cache.items()}},
            ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"[주의] 쪽 캐시 저장 실패({e}) — 중간에 끊기면 다시 읽어야 합니다.",
              file=sys.stderr)


def _collect(cache: dict[int, dict[int, dict]]):
    """쪽 캐시에서 실제 결과를 모은다. 반환: ([(쪽, 판독결과)], {쪽: 쓴 각도}).

    쪽마다 각도가 다를 수 있으므로 회전각을 가리지 않고 모으되, 같은 쪽이 여러
    각도로 있으면 **행이 있는 것**을 쓴다 — 0행은 '표가 없다'가 아니라 '그 각도로는
    못 읽었다'는 뜻일 뿐이다.
    """
    best: dict[int, dict] = {}
    used: dict[int, int] = {}
    for r in sorted(cache):
        for pno, got in cache[r].items():
            if (got or {}).get("rows") and pno not in best:
                best[pno], used[pno] = got, r
    return sorted(best.items()), used


def _mmss(sec: float) -> str:
    m, s = divmod(int(max(0, sec)), 60)
    return f"{m}분 {s}초" if m else f"{s}초"


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


class _Scanner:
    """쪽을 하나씩 판독하며 **쪽마다 캐시에 적는다.**

    캐시에 있는 쪽은 VLM을 아예 안 부른다 — 끊겼다 다시 돌리면 남은 쪽만 읽는다.
    `force`를 주면 그 쪽만 다시 읽는다(오독한 한 쪽만 고쳐 넣는 용도).

    시간 예산을 넘기면 `stopped`를 세우고 멈춘다. 읽은 데까지는 이미 저장돼 있어
    다시 돌리면 이어간다 — 끝이 언제인지 모르는 채 기다리다 창을 닫는 것보다 낫다.
    """

    def __init__(self, doc, total: int, pdf: str, cache: dict, budget: float,
                 fixed_rot: int = -1, plain: bool = False):
        self.doc, self.total, self.pdf, self.cache = doc, total, pdf, cache
        self.t0 = time.time()
        self.budget = budget
        self.fixed_rot = fixed_rot  # 사람이 준 각도(-1이면 쪽마다 자동 판정)
        self.stopped = False
        self.vlm_calls = 0          # 실제로 VLM을 부른 횟수(캐시 적중은 안 센다)
        self.vlm_time = 0.0
        self.failed: dict[int, int] = {}   # 0행이 난 쪽 → 그때 쓴 각도
        self.learned_rot = 0               # 이 문서에서 실제로 통한 각도
        self.prefer_plain = plain          # JSON을 건너뛰고 표 전사로 바로 간다
        self.bands = 1                     # 한 쪽을 몇 칸으로 나눠 읽을지(잘릴 때 늘어남)

    def rot_for(self, pno: int) -> int:
        """이 쪽을 몇 도로 돌려 읽을까. **쪽마다 따로 정한다.**

        ⚠ 예전에는 찾기 창 전체(1~6쪽)를 한 번에 판정했는데, 그러면 **표지·목차의
          가로쓰기가 누운 치수표를 표결에서 이겨** 0도가 나왔다. 한 쪽만 지정하면
          맞고 여러 쪽을 주면 틀리는, 원인이 안 보이는 실패였다(실제로 겪었다).
          판정은 텍스트 레이어만 보므로 쪽마다 해도 VLM 비용이 0이다.
        """
        if self.fixed_rot >= 0:
            return self.fixed_rot
        got = vision_ingest.detect_rotation(self.doc, [pno])
        # 텍스트 레이어가 없어 못 가린 쪽(0)은, 이 문서에서 **이미 통한 각도**가
        # 있으면 그걸 쓴다 — 스캔본은 쪽마다 판정이 안 되므로 한 번 배운 걸
        # 물려주지 않으면 되살린 다음 쪽부터 또 0도로 되돌아간다.
        return got or self.learned_rot

    # 쪽당 평균 소요(초). 아직 한 번도 안 불렀으면 0.
    @property
    def per_page(self) -> float:
        return self.vlm_time / self.vlm_calls if self.vlm_calls else 0.0

    def left(self) -> float:
        return self.budget - (time.time() - self.t0)

    def page(self, pno: int, rot: int, force: bool = False) -> dict:
        """이 쪽의 판독 결과({rows:[...]}). 캐시에 있으면 그걸 쓴다."""
        store = self.cache.setdefault(rot, {})
        if not force and pno in store:
            got = store[pno]
            n = len(got.get("rows") or [])
            print(f"  p{pno}{f'({rot}도)' if rot else ''} 판독: {n}행 (캐시)",
                  file=sys.stderr)
            return got

        t = time.time()
        png = vision_ingest.render_page(self.doc, pno, rotate=rot)
        tag = f"({rot}도)" if rot else ""

        # ① 검증된 JSON 경로부터. 체결구 도면은 이쪽이 가장 정확하다.
        #
        # 단 `prefer_plain` 이면 건너뛴다 — 사람이 --plain 을 줬거나, 이 문서에서
        # 이미 "JSON은 거절당하고 전사는 된다"가 확인된 경우다. 한 번 확인됐는데도
        # 쪽마다 JSON을 또 물으면 **쪽당 수십 초를 매번 버린다**(20쪽이면 10분).
        parsed: dict = {}
        got_json = None
        why = ""
        # 이미 "한 쪽이 한 번에 안 들어간다"가 확인됐으면 처음부터 나눠 읽는다 —
        # 쪽마다 잘리는 걸 한 번씩 다시 겪는 건 쪽당 수십 초를 그냥 버리는 것이다.
        if self.bands > 1 and self.prefer_plain:
            n_band = self.bands
            while n_band <= MAX_BANDS and not self.stopped:
                got, cut = self._read_bands(pno, rot, n_band, None)
                if got:
                    parsed = got
                if not cut:
                    break
                n_band *= 2                 # 이 쪽은 더 빽빽하다 — 더 잘게
                print(f"  p{pno}{tag} 아직 잘려 {n_band}칸으로 나눕니다…",
                      file=sys.stderr)
            self.bands = max(self.bands, min(n_band, MAX_BANDS))
        if not parsed.get("rows") and not self.prefer_plain:
            raw, why = vision_ingest.ask_image_detail(
                png, TABLE_PROMPT + TABLE_PROMPT_EXTRA)
            self.vlm_calls += 1
            got_json = _parse_json(raw or "")
            parsed = got_json or {}
            self._dump(pno, rot, "json", raw, why)

        # ② 안 나오면 **표만 그대로 옮겨 적게** 하고 우리가 행으로 바꾼다.
        #    같은 쪽이 마크다운 전사로는 멀쩡히 읽히는 것을 실측으로 확인했다 —
        #    JSON을 고집하다 표를 통째로 놓치는 것보다 이쪽이 낫다.
        #
        # ⚠ 단, **JSON이 제대로 와서 "행 없음"이라고 한 쪽은 다시 묻지 않는다.**
        #   그건 모델이 약속한 형식으로 답하고 "표가 없다"고 말한 것이다(표지·목차).
        #   다시 묻는 건 쪽당 수십 초를 그냥 버리는 것이다. 되묻는 경우는 **모델이
        #   형식을 안 지켰을 때**뿐 — 산문으로 거절했거나, 잘렸거나, 호출이 실패한
        #   때다. 그게 우리가 실제로 겪은 실패의 모양이다.
        if not parsed.get("rows") and (self.prefer_plain or got_json is None or why):
            if self.prefer_plain:
                pass                        # 이미 아는 길이라 조용히 간다
            elif why:
                print(f"  p{pno}{tag} JSON 경로 실패: {why}", file=sys.stderr)
            elif raw is not None:
                head = " ".join((raw or "").split())[:80]
                print(f"  p{pno}{tag} JSON 경로가 표를 안 줬습니다"
                      f'{f" — 응답 앞부분: {head}" if head else " (빈 응답)"}',
                      file=sys.stderr)
            else:
                print(f"  p{pno}{tag} 표 전사로 다시 시도합니다…", file=sys.stderr)
            raw2, why2 = vision_ingest.ask_image_detail(png, PLAIN_TABLE_PROMPT)
            self.vlm_calls += 1
            self._dump(pno, rot, "plain", raw2, why2)
            got = _rows_from_markdown(raw2 or "")

            # ③ 응답이 잘렸으면 **쪽을 위아래로 나눠** 다시 읽는다. 한도를 올릴 수
            #    없는 게이트웨이에서도 통하는 유일한 길이다. 2칸 → 4칸으로 늘린다.
            if why2 and "잘렸" in why2:
                cols = (got or {}).get("columns") or []
                n_band = max(2, self.bands)     # 1칸은 나누는 게 아니다
                while n_band <= MAX_BANDS and not self.stopped:
                    print(f"  p{pno}{tag} 응답이 잘려 {n_band}칸으로 나눠 읽습니다…",
                          file=sys.stderr)
                    band, cut = self._read_bands(pno, rot, n_band, cols)
                    if band and len(band["rows"]) > len((got or {}).get("rows") or []):
                        got = band
                    if band and not cut:
                        # 잘림 없이 다 읽었다 — 남은 쪽도 이 칸수로 시작한다.
                        self.bands = n_band
                        break
                    n_band *= 2             # 아직 잘린다 — 더 잘게
                else:
                    if not self.stopped:
                        notes_msg = (f"p{pno}는 {MAX_BANDS}칸으로 나눠도 응답이 "
                                     "잘립니다 — RAG_VLM_MAX_TOKENS를 올려야 합니다")
                        print(f"  {notes_msg}", file=sys.stderr)

            if got:
                parsed = got
                if not self.prefer_plain:
                    # 이 문서는 JSON이 안 먹고 전사가 먹는다 — 남은 쪽은 바로 전사로.
                    self.prefer_plain = True
                    print("  → 이 문서는 표 전사 경로를 씁니다(JSON은 건너뜁니다).",
                          file=sys.stderr)
            elif why2:
                print(f"  p{pno}{tag} 표 전사도 실패: {why2}", file=sys.stderr)

        took = time.time() - t
        self.vlm_time += took
        n = len(parsed.get("rows") or [])
        store[pno] = parsed
        _save_pages(self.pdf, self.cache)      # ★ 쪽마다 저장 — 끊겨도 안 잃는다
        via = "" if not n else ("" if parsed.get("source") != "markdown" else " [전사]")
        print(f"  p{pno}{tag} 판독: {n}행{via} "
              f"({took:.0f}초, 누적 {_mmss(time.time() - self.t0)})", file=sys.stderr)
        return parsed

    def _read_bands(self, pno: int, rot: int, n: int, cols):
        """쪽을 위아래 n칸으로 나눠 전사하고 합친다. 못 얻으면 None.

        왜 필요한가: 행이 많은 치수표는 전사 응답이 `max_tokens` 에서 **잘린다**.
        잘린 JSON은 파싱에 실패해 "0행"으로만 보였다. 한도를 올리면 되지만 사내
        게이트웨이가 막아 두면 올릴 수가 없다 — 그때는 **보내는 쪽을 줄이는**
        수밖에 없다. 칸은 넉넉히 겹쳐 자르고 중복은 dash로 거른다.
        """
        merged: dict[str, dict] = {}
        head: dict = {}
        cut = False                       # 어느 칸이라도 잘렸으면 더 잘게 나눠야 한다
        for i in range(n):
            if self.left() <= 0:
                self.stopped = True
                break
            png = vision_ingest.render_page(self.doc, pno, rotate=rot, band=(i, n))
            prompt = PLAIN_TABLE_PROMPT
            use_cols = list((head.get("columns") or cols) or [])
            if i and use_cols:
                prompt += BAND_PROMPT_TAIL.format(
                    columns=" | ".join([head.get("id_column") or "DASH NUMBER"] + use_cols))
            raw, why = vision_ingest.ask_image_detail(png, prompt)
            self.vlm_calls += 1
            self._dump(pno, rot, f"band{i + 1}of{n}", raw, why)
            got = _rows_from_markdown(raw or "")
            n_rows = len(got.get("rows") or []) if got else 0
            if why and "잘렸" in why:
                cut = True
            print(f"    p{pno} {i + 1}/{n}칸: {n_rows}행"
                  + (f"  [{why}]" if why else ""), file=sys.stderr)
            if not got:
                # 앞의 두 칸이 내리 비면 그 쪽엔 표가 없는 것으로 본다. 표가 끝난
                # 다음 쪽까지 칸마다 VLM을 부르면 쪽당 수십 초를 통째로 버린다.
                if i >= 1 and not merged:
                    break
                continue
            if not head:
                head = got
            for r in got["rows"]:
                merged.setdefault(str(r.get("dash", "")), r)
        merged.pop("", None)
        if not merged:
            return None, cut
        out = dict(head)
        out["rows"] = list(merged.values())
        out["source"] = "markdown"
        return out, cut

    def _dump(self, pno: int, rot: int, kind: str, raw, why: str) -> None:
        """VLM 원문을 파일로 남긴다. 사내망은 로그를 반출할 수 없어 **화면 밖에
        남는 것이 이것뿐**이고, 0행의 원인은 대개 이 원문 안에 있다. 실패해도
        판독은 계속한다(우아한 저하)."""
        try:
            d = _cache_path(self.pdf).with_name(_cache_path(self.pdf).stem + "_raw")
            d.mkdir(parents=True, exist_ok=True)
            (d / f"p{pno:03d}_{rot}_{kind}.txt").write_text(
                (f"[사유] {why}\n\n" if why else "") + (raw if raw is not None else "<None>"),
                encoding="utf-8")
        except OSError:
            pass

    def scan(self, pnos, notes: list[str], force: bool = False, rot: int = -1):
        """여러 쪽을 판독한다. 반환: [(쪽번호, 판독결과)] — 0행인 쪽은 뺀다.

        rot을 주면 그 각도로, 안 주면 `rot_for`가 쪽마다 정한다.
        0행이 나온 쪽은 `self.failed`에 (쪽, 그때 쓴 각도)로 쌓인다 — 나중에 다른
        각도로 되살릴 수 있게.
        """
        out: list[tuple[int, dict]] = []
        for pno in pnos:
            if not 1 <= pno <= self.total:
                continue
            if self.left() <= 0:
                self.stopped = True
                break
            use = rot if rot >= 0 else self.rot_for(pno)
            parsed = self.page(pno, use, force=force)
            if parsed.get("rows"):
                out.append((pno, parsed))
                self.failed.pop(pno, None)
            else:
                self.failed[pno] = use
                notes.append(f"p{pno}에서 표를 찾지 못했습니다({use}도)")
        return out


def read_table(pdf: str, pages: str = "", refresh: bool = False,
               rotate: int = -1, plain: bool = False) -> dict:
    """스펙 PDF의 치수표를 판독한다. 반환: {rows, columns, id_column, pages, notes, ...}

    pages: "2,3" 또는 "4-12" 처럼 쪽을 지정. 비우면 앞에서부터 MAX_SCAN_PAGES쪽까지
        훑어 표가 나오는 쪽을 찾고, **찾은 뒤에는 표가 끊길 때까지 이어서** 읽는다
        (치수 목록은 수십 쪽까지 이어진다).
    refresh=True면 캐시를 무시하고 다시 읽는다. **pages와 같이 주면 그 쪽만** 다시
        읽고 나머지 쪽은 캐시를 쓴다 — 한 쪽을 오독했을 때 그것만 고쳐 넣는 길이다.
    rotate: 시계방향 회전각. -1(기본)이면 자동 — 글자 방향으로 먼저 보고, 그래도
        한 쪽도 못 읽으면 90도·270도로 다시 시도한다.

    쪽 단위 캐시를 쓰므로 중간에 끊겨도(Ctrl+C·시간 예산 초과) 읽은 쪽은 남는다.
    다시 부르면 남은 쪽만 읽는다.
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
    notes: list[str] = []
    cache = _load_pages(pdf)
    try:
        total = len(doc)
        explicit = bool(pages.strip())
        targets = (_parse_pages(pages, total) if explicit
                   else list(range(1, min(total, MAX_SCAN_PAGES) + 1)))
        for pno in targets:
            if not 1 <= pno <= total:
                notes.append(f"p{pno}는 이 문서에 없습니다(전체 {total}쪽)")

        # 회전은 **쪽마다** 정한다(_Scanner.rot_for). 찾기 창 전체를 한 번에
        # 판정하면 표지의 가로쓰기가 누운 치수표를 이겨 버린다 — 아래 참고.
        scan = _Scanner(doc, total, pdf, cache, TIME_BUDGET,
                        fixed_rot=int(rotate) % 360 if rotate >= 0 else -1,
                        plain=plain)
        if plain:
            print("[진행] 표 전사 경로로 읽습니다(JSON 판독은 건너뜁니다).",
                  file=sys.stderr)

        done = len(cache.get(0, {})) + sum(len(v) for k, v in cache.items() if k)
        if done:
            print(f"[진행] 이미 읽어 둔 {done}쪽은 캐시에서 씁니다"
                  " (다시 읽으려면 --refresh).", file=sys.stderr)
        print("[진행] 멈추려면 Ctrl+C — 읽은 쪽은 저장돼 다음 실행이 이어갑니다.",
              file=sys.stderr)

        try:
            # ① 찾기 — 앞쪽을 훑어 표가 어디서 시작하는지 본다.
            results = scan.scan(targets, notes, force=refresh)

            # ② 못 읽은 쪽 되살리기 — **한 문서에 한 번만.**
            #
            # ⚠ 왜 이렇게 바꿨나: 예전에는 "한 쪽도 못 읽었을 때만" 다른 각도를
            #   시도했다. 그런데 누운 문서에서 **어쩌다 한 쪽이 0도로 읽히면**
            #   그 한 쪽이 재시도를 통째로 막아, 나머지 표 쪽이 전부 0행인 채로
            #   끝났다. 한 쪽만 지정하면 되는데 여러 쪽을 주면 안 되던 원인이다.
            #
            # 표가 나온 쪽 **바로 옆**의 못 읽은 쪽을 골라 다른 각도를 시험한다.
            # 이어지는 표일 가능성이 높아 표적이 정확하고, 통하면 그 각도로 못 읽은
            # 쪽을 한꺼번에 되살린다. 실패해도 비용은 최대 세 번이다.
            #
            # 단, **글자 방향을 이미 아는 쪽은 건드리지 않는다.** 텍스트 레이어가
            # 방향을 말해 주는 쪽이 0행이라면 그건 회전 문제가 아니라 그냥 표가 없는
            # 쪽이다(표지·목차). 거기에 각도를 더듬으면 쪽당 수십 초를 그냥 버린다.
            blind = [p for p in sorted(scan.failed)
                     if not vision_ingest.text_dir_known(doc, p)]
            if blind and rotate < 0 and not scan.stopped:
                ok = {p for p, _ in results}
                probe = next((p for p in blind if (p - 1) in ok or (p + 1) in ok), None)
                probe = probe or max(blind)
                tried = scan.failed[probe]
                for alt in (90, 270, 180):
                    if alt == tried or scan.stopped:
                        continue
                    print(f"[진행] p{probe}를 {alt}도로 돌려 시험합니다 "
                          f"({tried}도로는 표를 못 찾았습니다).", file=sys.stderr)
                    if not scan.scan([probe], [], rot=alt):
                        continue
                    notes.append(f"페이지가 누워 있어 {alt}도 돌려 판독했습니다"
                                 f"(p{probe}는 {tried}도로 안 읽혔습니다).")
                    scan.learned_rot = alt          # 이후 쪽도 이 각도로
                    rest = [p for p in blind if p != probe and p in scan.failed]
                    if rest:
                        print(f"[진행] 같은 각도로 못 읽은 쪽을 다시 읽습니다: "
                              f"{', '.join('p' + str(p) for p in rest)}", file=sys.stderr)
                        scan.scan(rest, notes, rot=alt)
                    # 되살린 쪽이 results에 들어가야 이어읽기가 **표의 끝**에서
                    # 시작한다. 안 그러면 원래 읽혔던 한 쪽 다음부터 읽어, 바로
                    # 다음 쪽이 0행이면 거기서 멈춰 버린다.
                    results, _u = _collect(cache)
                    break

            # ③ 이어읽기 — 표가 끊기는 쪽까지. 쪽을 직접 지정받았으면 하지 않는다.
            if results and not explicit and not scan.stopped:
                pno = max(p for p, _ in results)
                limit = min(total, pno + MAX_TABLE_PAGES)
                if pno < limit:
                    left = limit - pno
                    est = (f" — 남은 {left}쪽이면 약 {_mmss(left * scan.per_page)}"
                           if scan.per_page else "")
                    print(f"[진행] p{min(p for p, _ in results)}부터 표가 이어집니다. "
                          f"끊길 때까지 계속 읽습니다(최대 p{limit}){est}.",
                          file=sys.stderr)
                while pno < limit:
                    pno += 1
                    more = scan.scan([pno], [])
                    if scan.stopped or not more:
                        break
                    results.extend(more)
                if pno >= limit and limit < total:
                    notes.append(
                        f"p{limit}까지만 읽었습니다"
                        f"(STD_SPEC_MAX_TABLE_PAGES={MAX_TABLE_PAGES}). "
                        "표가 더 길면 pages 인자로 범위를 지정하세요."
                    )
        except KeyboardInterrupt:
            # 읽은 쪽은 이미 파일에 있다. 여기서 끝내지 말고 **가진 것으로 결과를
            # 만들어** 돌려준다 — 10분 기다린 사람에게 아무것도 안 주면 안 된다.
            print("\n[중단] Ctrl+C — 지금까지 읽은 쪽으로 정리합니다.", file=sys.stderr)
            notes.append("Ctrl+C로 중단했습니다. 다시 돌리면 남은 쪽을 이어 읽습니다.")
            scan.stopped = True

        if scan.stopped:
            notes.append(
                f"끝까지 읽지 못했습니다(시간 예산 {_mmss(TIME_BUDGET)} 또는 중단). "
                "같은 명령을 다시 돌리면 캐시에서 이어갑니다."
            )
        # 결과는 **캐시 전체**에서 모은다 — 이번에 읽은 쪽과 지난 실행에서 읽어 둔
        # 쪽이 함께 들어가야 이어 돌리기가 실제로 이어진다. 쪽마다 각도가 다를 수
        # 있으므로 회전각을 가리지 않고 모으되, 같은 쪽이 여러 각도로 있으면
        # **행이 있는 것**을 쓴다(0행은 '그 각도로는 못 읽었다'는 뜻일 뿐이다).
        results, used = _collect(cache)
        # 대표 회전각 = 읽힌 쪽들에서 가장 많이 쓰인 각도(응답·캐시 표시용).
        rot = max(set(used.values()), key=list(used.values()).count) if used else 0
        turned = sorted(p for p, r in used.items() if r != rot)
        if turned:
            notes.append("다른 각도로 읽은 쪽: "
                         + ", ".join(f"p{p}({used[p]}도)" for p in turned[:8]))
    finally:
        _save_pages(pdf, cache)
        doc.close()

    if not results:
        raise SpecError(
            f"'{os.path.basename(pdf)}'에서 치수표를 찾지 못했습니다. "
            f"훑어본 쪽: {targets} (전체 {total}쪽). "
            "표가 그 뒤에 있으면 pages 인자로 지정하세요(예: pages='3-12'). "
            "CLI로는 python spec_table.py <도면> --dir <폴더> --pages 3-12 --rotate 90 "
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
        "rotate": rot,
        "complete": not scan.stopped,
        "rows": res.get("rows", []),
        "columns": res.get("columns", []),
        "id_column": res.get("id_column", ""),
        "table_title": res.get("table_title", ""),
        "notes": notes + list(mnotes or []),
    }
    # 끝까지 못 읽은 결과는 **최종 캐시에 넣지 않는다** — 넣으면 다음 호출이 "이미
    # 다 읽었다"고 보고 모자란 표를 그대로 쓴다. 쪽 캐시는 이미 저장돼 있어 이어
    # 돌리는 데는 지장이 없다.
    if not scan.stopped:
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
#   python spec_table.py AS568 --dir C:\rag\vision --pages 3-12 --rotate 90
#
# 쪽 단위로 캐시하므로 **중간에 끊겨도(Ctrl+C·시간 초과) 읽은 쪽은 남는다** — 같은
# 명령을 다시 돌리면 남은 쪽만 읽는다. 한 쪽만 오독했으면 그 쪽만 다시 읽어 고친다:
#
#   python spec_table.py AS568 --dir C:\rag\vision --pages 3 --refresh
#
# 컬럼 이름을 눈으로 확인하는 용도이기도 하다 — select_dash 조건을 그 이름 그대로
# 적어야 하기 때문이다.


def _cli() -> int:
    global TIME_BUDGET
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
    ap.add_argument("--plain", action="store_true",
                    help="JSON 판독을 건너뛰고 표를 그대로 옮겨 적게 한다. "
                         "도면이 아니라 치수 목록 문서(AS568 등)에서 확실한 길")
    ap.add_argument("--rows", type=int, default=10, help="보여줄 행 수 (기본 10)")
    ap.add_argument("--budget", type=float, default=0,
                    help=f"이번 실행에 쓸 최대 시간(초, 기본 {TIME_BUDGET:.0f}). "
                         "넘으면 읽은 데까지 저장하고 멈춘다 — 다시 돌리면 이어간다")
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

    if a.budget:
        TIME_BUDGET = a.budget

    t0 = time.time()
    data = read_table(pdf, pages=a.pages, refresh=a.refresh, rotate=a.rotate,
                      plain=a.plain)
    cols = data.get("columns") or []
    rows = data.get("rows") or []
    rot = data.get("rotate") or 0
    print(f"판독 {time.time() - t0:.1f}초, 쪽 {data.get('pages_used')}, 행 {len(rows)}개"
          + (f", 회전 {rot}도" if rot else ""))
    for n in (data.get("notes") or [])[:8]:
        print(f"  · {n}")
    if not data.get("complete", True):
        print()
        print("[주의] 끝까지 읽지 못했습니다. 같은 명령을 다시 돌리세요 — 읽은 쪽은")
        print("  캐시에 있어 남은 쪽만 읽습니다. 아래 표도 그만큼 모자랍니다.")
        print(f"  (쪽 캐시: {_page_cache_path(pdf)})")
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
