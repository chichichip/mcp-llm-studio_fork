"""standard_part_server.py

항공 표준품 **찾기** MCP 서버 — 읽기 전용(🟢). "무엇이 필요한가"를 받아
**어느 계열(대분류·중분류·도면번호)을 보면 되는지**까지 좁혀 준다.

일을 위험도로 갈라 놓은 것이 이 서버의 설계다:

    A. 어느 계열을 쓰나   — 틀리면 엉뚱한 표를 본다(사람이 알아챈다)  ← **여기**
    B. 그 계열의 어느 부품번호 — 틀리면 잘못된 부품이 조립된다(못 알아챈다)

A는 전 계열에 대해 일반화되지만, B는 계열마다 규칙이 달라(오링은 홈 치수, 클램프는
튜브 외경, 볼트는 그립…) 한꺼번에 만들 수 없다. 그래서 이 서버는 **A만** 한다.
B는 계열별 규칙이 확정되는 대로 spec-reader 쪽에 하나씩 붙인다.

무엇으로 좁히나 (셋 다 결정론적 — 모델이 계열명을 지어낼 여지를 줄인다):
    1. 요구사항의 한국어 낱말 → 중분류 후보  (`TERMS` 사전)
    2. 요구사항에 영문 계열명이 그대로 있으면 그것         (`KNOWN_MIDS` 대조)
    3. 지침서 RAG 검색 결과 본문에 등장하는 계열명         (근거 쪽과 함께)
   그리고 후보마다 **엑셀 목록에서 실제 도면번호**를 붙인다. 엑셀에 없는 계열은
   후보에서 빼지 않고 '목록에 없음'으로 표시한다 — 조용히 지우면 모델이 그 계열이
   존재하지 않는다고 잘못 결론짓는다.

⚠ **이 서버는 부품번호를 고르지 않는다.** 치수·dash 판단은 스펙 판독과 검증을 거쳐야
   하고(spec-reader의 verify.py), RAG 전사문은 그 근거로 쓸 수 없다. 응답에도 그렇게
   적어 둔다 — 모델이 요약하며 그 경고를 떨어뜨리지 않도록.

사용:
    python standard_part_server.py                      # MCP 서버 (stdio)
    python standard_part_server.py --transport http     # n8n 등, :8094
    python standard_part_server.py --find "튜브 고정용 쿠션 클램프"   # CLI 시험
    python standard_part_server.py --status             # 엑셀/지침서 연결 진단

설정(mcp_server/local_settings.py):
    STD_CATALOG_PATH  = r"C:\\...\\엔진 적용 표준품 목록.xlsx"   # 필수
    STD_SPEC_DIR      = r"C:\\...\\스펙"                        # 선택(스펙 PDF 폴더)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from fastmcp import FastMCP

import settings

# spec-reader는 폴더 이름에 하이픈이 있어 패키지로 import할 수 없다 — sys.path에 얹는다.
# (mcp_server 안의 모듈들이 같은 폴더끼리 import하는 것과 같은 이유로, 경로는 이 파일
#  기준으로 잡는다. 앱이 절대경로로 띄워 cwd가 달라도 동작해야 한다.)
_SPEC_DIR = Path(__file__).resolve().parent.parent / "spec-reader"
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

try:
    import catalog  # spec-reader/catalog.py

    CATALOG_IMPORT_ERROR = ""
except Exception as e:  # noqa: BLE001 — openpyxl 부재 등 어떤 실패든 저하로
    catalog = None  # type: ignore[assignment]
    CATALOG_IMPORT_ERROR = str(e)

try:
    import spec_table

    SPEC_IMPORT_ERROR = ""
except Exception as e:  # noqa: BLE001
    spec_table = None  # type: ignore[assignment]
    SPEC_IMPORT_ERROR = str(e)

# 지침서 검색은 RAG 코어를 그대로 재사용한다(같은 인덱스를 본다 — 따로 만들지 않는다).
try:
    import rag_core as core

    RAG_IMPORT_ERROR = ""
except Exception as e:  # noqa: BLE001
    core = None  # type: ignore[assignment]
    RAG_IMPORT_ERROR = str(e)


CATALOG_PATH = settings.get("STD_CATALOG_PATH", "")
SPEC_DIR = settings.get("STD_SPEC_DIR", "")
MAX_CANDIDATES = settings.get_int("STD_MAX_CANDIDATES", 6)
MAX_SERIES = settings.get_int("STD_MAX_SERIES", 8)   # 후보 하나당 보여줄 도면번호 수


class StdError(Exception):
    """사용자에게 그대로 보여줄 안내 메시지."""


# ─────────────────────────── 한국어 → 중분류 후보 ───────────────────────────
# 설계자가 쓰는 말과 엑셀의 영문 계열명을 잇는다. **이 사전에 없으면 못 찾는 게 아니라**
# 지침서 검색 결과에서 계열명을 줍는 경로(3번)가 따로 있다 — 여기는 빠른 길일 뿐이다.
# 새 낱말을 만나면 여기에 한 줄 추가하면 된다(코드 수정 없이 범위가 넓어진다).
TERMS: dict[str, tuple[str, ...]] = {
    "볼트": ("BOLT, HEX", "BOLT, DOUBLE HEX", "BOLT, T-HEAD"),
    "육각볼트": ("BOLT, HEX",),
    "더블헥스": ("BOLT, DOUBLE HEX", "NUT, SELF-LOCKING, DOUBLE HEX"),
    "스터드": ("STUD", "STUD, RING LOCKED, SERRATED", "STUD, KEY LOCKED"),
    "스크류": ("SCREW",), "나사": ("SCREW",),
    "너트": ("NUT, PLAIN, HEX", "NUT, SELF-LOCKING, HEX",
             "NUT, SELF-LOCKING, DOUBLE HEX", "NUT"),
    "셀프락킹": ("NUT, SELF-LOCKING, HEX", "NUT, SELF-LOCKING, DOUBLE HEX"),
    "자동풀림방지": ("NUT, SELF-LOCKING, HEX",),
    "캐슬": ("NUT, CASTELLATED",), "홈붙이": ("NUT, CASTELLATED",),
    "플로팅": ("NUT, FLOATING",), "러그": ("NUT, LUG",),
    "워셔": ("WASHER, FLAT", "WASHER, LOCK", "WASHER"),
    "와셔": ("WASHER, FLAT", "WASHER, LOCK", "WASHER"),
    "평와셔": ("WASHER, FLAT",), "스프링와셔": ("WASHER, LOCK",),
    "인서트": ("INSERT, HELICAL COIL", "INSERT, RING LOCKED", "INSERT, KEY LOCKED", "INSERT"),
    "헬리코일": ("INSERT, HELICAL COIL",),
    "핀": ("PIN, HEADLESS", "PIN, HEADED", "PIN, COTTER", "PIN, SPRING"),
    "분할핀": ("PIN, COTTER",), "스프링핀": ("PIN, SPRING",),
    "리벳": ("RIVET, SOLID", "RIVET, TUBULAR"),
    "스냅링": ("RING, RETAINING", "RING, RETAINING, SPIRAL"),
    "리테이닝": ("RING, RETAINING", "RING, RETAINING, SPIRAL"),
    "오링": ("O-RING", "O-RING, METAL"), "오-링": ("O-RING", "O-RING, METAL"),
    "실": ("SEAL",), "씰": ("SEAL",), "가스켓": ("GASKET",), "개스킷": ("GASKET",),
    "베어링": ("BEARING, SPHERICAL", "BEARING, SLEEVE", "BEARING, ROD END"),
    "구면베어링": ("BEARING, SPHERICAL",), "로드엔드": ("BEARING, ROD END",),
    "클램프": ("CLAMP, LOOP", "CLAMP, LOOP, CUSHIONED",
               "CLAMP, LOOP, MULTI TUBE", "CLAMP, SADDLE"),
    "쿠션": ("CLAMP, LOOP, CUSHIONED",), "새들": ("CLAMP, SADDLE",),
    "클립": ("CLIP",), "브라켓": ("BRACKET",), "브래킷": ("BRACKET",),
    "튜브": ("CLAMP, LOOP", "CLAMP, LOOP, CUSHIONED", "NUT, COUPLING", "FERRULE/SLEEVE"),
    "배관": ("ADAPTER", "ELBOW", "TEE", "CROSS", "NUT, COUPLING"),
    "피팅": ("ADAPTER", "ELBOW", "TEE", "CROSS", "PLUG & CAP"),
    "어댑터": ("ADAPTER",), "엘보": ("ELBOW",), "티": ("TEE",),
    "플러그": ("PLUG & CAP",), "캡": ("PLUG & CAP",),
    "페룰": ("FERRULE/SLEEVE",), "슬리브": ("FERRULE/SLEEVE", "BEARING, SLEEVE"),
    "커플링": ("COUPLING", "COUPLING, V-RETAINER", "COUPLING, V-BAND", "NUT, COUPLING"),
    "브이밴드": ("COUPLING, V-BAND", "FLANGE, V-BAND COUPLING"),
    "브이리테이너": ("COUPLING, V-RETAINER", "FLANGE, V-RETAINER"),
    "플랜지": ("FLANGE, V-RETAINER", "FLANGE, V-BAND COUPLING"),
    "호스": ("DESIGN STANDARD FOR HOSE ASSEMBLY",),
}


def _terms_hit(text: str) -> list[str]:
    """요구사항 문장에서 한국어 낱말로 중분류 후보를 뽑는다(등장 순서 유지)."""
    out: list[str] = []
    for word, mids in TERMS.items():
        if word in text:
            for m in mids:
                if m not in out:
                    out.append(m)
    return out


def _names_in(text: str) -> list[str]:
    """문장에 영문 계열명이 그대로 들어 있으면 집어낸다(지침서 본문 대조에도 쓴다).

    긴 이름부터 본다 — 'CLAMP, LOOP, CUSHIONED'가 'CLAMP, LOOP'보다 먼저 잡혀야 한다.
    """
    if catalog is None:
        return []
    key = catalog.norm(text)
    found = []
    for mid in sorted(catalog.KNOWN_MIDS, key=len, reverse=True):
        n = catalog.norm(mid)
        if n and n in key and not any(n in catalog.norm(f) for f in found):
            found.append(mid)
    return found


# ─────────────────────────── 엑셀 / 지침서 ───────────────────────────

_items: list | None = None
_items_path = ""


def _catalog_items() -> list:
    """엑셀 목록을 읽어 캐시한다. 경로가 바뀌면 다시 읽는다."""
    global _items, _items_path
    if catalog is None:
        raise StdError(
            f"표준품 목록을 읽을 수 없습니다: catalog 모듈을 불러오지 못했습니다"
            f"({CATALOG_IMPORT_ERROR}). openpyxl이 설치돼 있는지 확인하세요."
        )
    path = CATALOG_PATH
    if not path:
        raise StdError(
            "표준품 목록 엑셀 경로가 설정돼 있지 않습니다. "
            "mcp_server/local_settings.py에 STD_CATALOG_PATH를 적으세요."
        )
    if not os.path.isfile(path):
        raise StdError(f"표준품 목록 파일이 없습니다: {path}")
    if _items is None or _items_path != path:
        _items = catalog.load_catalog(path)
        _items_path = path
    return _items


def _guide_hits(query: str, top_k: int = 4) -> tuple[list[dict], str]:
    """지침서(RAG 인덱스)에서 관련 대목을 찾는다. 반환: (청크들, 검색 방식).

    RAG가 없거나 인덱스가 비어 있어도 **예외로 죽지 않는다** — 엑셀만으로도 계열
    후보는 낼 수 있으므로 근거 없이 계속한다(우아한 저하).
    """
    if core is None:
        return [], f"지침서 검색 불가({RAG_IMPORT_ERROR})"
    try:
        store = core.get_store()
        if store.stats()["chunks"] == 0:
            return [], "지침서가 인덱싱돼 있지 않음"
        pool = top_k * 3
        qv = core._embed_texts([core._embed_query_text(query)])
        mode = "하이브리드" if qv else "키워드 전용"
        fused: dict[int, float] = {}
        for hits in ((store.vector_search(qv[0], pool) if qv else []),
                     store.keyword_search(query, pool)):
            for rank, (cid, _s) in enumerate(hits):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (core.RRF_K + rank + 1)
        if not fused:
            return [], mode
        ranked = sorted(fused.items(), key=lambda t: -t[1])[:top_k]
        cmap = store.fetch_chunks([cid for cid, _ in ranked])
        return [cmap[cid] for cid, _ in ranked if cid in cmap], mode
    except Exception as e:  # noqa: BLE001 — 근거가 없어도 후보는 내야 한다
        return [], f"지침서 검색 실패({type(e).__name__}: {e})"


def _cite(c: dict) -> str:
    page = int(c.get("page") or 0)
    where = f"{page}쪽" if page else f"청크 {c['seq']}"
    head = f" › {c['heading']}" if c.get("heading") else ""
    return f"{os.path.basename(c['path'])}{head} ({where})"


# ─────────────────────────── MCP 도구 ───────────────────────────

mcp = FastMCP(
    name="std",
    instructions=(
        "항공 표준품(볼트·너트·워셔·오링·클램프·베어링·피팅 등)을 찾는 서버입니다. "
        "사용자가 '어떤 부품을 써야 하나'를 물으면 find_standard에 요구사항을 그대로 "
        "넘기세요 — 지침서와 표준품 목록을 함께 보고 어느 계열(중분류·도면번호)을 "
        "보면 되는지 좁혀 줍니다. 계열이 정해진 뒤 그 계열의 실제 부품번호 목록이 "
        "필요하면 list_parts를 부르세요. "
        "부품번호를 좁힐 때는 read_spec_table로 표와 **치수 기호 범례**를 먼저 보고, "
        "어느 기호가 사용자가 말한 치수인지 확인한 뒤 select_dash를 부르세요 — 기호의 "
        "뜻을 추측하지 마세요. "
        "⚠ 이 서버는 **부품번호를 최종 확정하지 않습니다.** dash/치수 확정은 스펙 판독과 "
        "검증을 거쳐야 하므로, 응답에 붙은 ⚠ 경고와 근거(문서명·쪽)를 반드시 그대로 "
        "사용자에게 전달하세요. 근거 없이 부품번호를 단정하지 마세요."
    ),
)


def std_tool(fn):
    """예외를 안내 문자열로 바꾼다 (도구는 항상 문자열을 돌려준다)."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except StdError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"작업에 실패했습니다: {type(e).__name__}: {e}"

    return wrapper


@mcp.tool()
@std_tool
def find_standard(requirement: str) -> str:
    """요구사항을 받아 **어느 표준품 계열을 보면 되는지** 좁혀 줍니다. (🟢 읽기)

    예: "PTFE 튜브 3/8 고정용 쿠션 클램프", "직경 .190 더블헥스 볼트와 짝 너트",
        "고온부 플랜지 실링용 오링"

    지침서(RAG)에서 관련 조항을 찾아 근거로 붙이고, 표준품 목록(엑셀)에서 그 계열의
    실제 도면번호를 함께 보여줍니다.

    ⚠ 부품번호(dash)는 정하지 않습니다 — 그건 스펙 판독과 검증을 거쳐야 합니다.

    Args:
        requirement: 필요한 부품을 자연어로. 사용자가 말한 그대로 넘기면 됩니다.
    """
    q = (requirement or "").strip()
    if not q:
        raise StdError("requirement가 비어 있습니다. 어떤 부품이 필요한지 적어 주세요.")

    hits, mode = _guide_hits(q)

    # 후보 계열 — 세 경로에서 모으고 **근거가 센 것부터** 보여준다.
    # 순서가 중요하다: 약한 모델은 맨 위 후보를 잡는다. '배관' 같은 넓은 낱말로
    # 유추한 것이 지침서가 직접 지시한 계열보다 위에 오면 엉뚱한 답으로 이어진다.
    RANK_GUIDE, RANK_NAMED, RANK_TERM = 0, 1, 2
    cands: dict[str, tuple[int, str, int]] = {}   # mid -> (근거등급, 근거문구, 등장순서)

    def add(mid: str, rank: int, why: str) -> None:
        old = cands.get(mid)
        if old is None or rank < old[0]:          # 더 센 근거로만 덮어쓴다
            cands[mid] = (rank, why, len(cands) if old is None else old[2])

    for mid in _terms_hit(q):
        add(mid, RANK_TERM, "요구사항 낱말")
    for mid in _names_in(q):
        add(mid, RANK_NAMED, "요구사항에 계열명 있음")
    for c in hits:
        for mid in _names_in(c.get("content", "")):
            add(mid, RANK_GUIDE, f"지침서 {_cite(c)}")

    out = [f"요구사항: {q}", ""]

    if hits:
        out.append(f"■ 지침서 근거 ({mode})")
        for c in hits:
            body = " ".join((c.get("content") or "").split())[:300]
            out.append(f"  - {_cite(c)}")
            out.append(f"    {body}…")
        out.append("")
    else:
        out.append(f"■ 지침서 근거: 찾지 못했습니다 ({mode})")
        out.append("   docs__list_sections로 목차를 보고 문서가 쓰는 용어로 다시 물어보세요.")
        out.append("")

    if not cands:
        out.append("■ 계열 후보: 좁히지 못했습니다.")
        out.append("   부품 종류를 한 낱말로 알려 주세요(예: 클램프, 오링, 볼트, 베어링).")
        out.append(f"   아는 계열: {', '.join(catalog.KNOWN_MIDS[:12])} …" if catalog else "")
        return "\n".join(out)

    # 엑셀에서 각 후보의 실제 도면번호를 붙인다.
    out.append("■ 계열 후보")
    try:
        items = _catalog_items()
    except StdError as e:
        out.append(f"  (표준품 목록을 읽지 못해 도면번호를 붙이지 못했습니다: {e})")
        for mid, (_r, why, _s) in sorted(cands.items(), key=lambda kv: (kv[1][0], kv[1][2]))[:MAX_CANDIDATES]:
            out.append(f"  - {mid}   [{why}]")
        return "\n".join(out)

    # 정렬: 근거가 센 것 → 엑셀에 실제로 있는 것 → 찾은 순서.
    # 목록에 없는 계열도 빼지 않는다(존재하지 않는다고 잘못 결론짓게 만든다) — 뒤로 보낼 뿐이다.
    scored = []
    for mid, (rank, why, seq) in cands.items():
        series = catalog.find_series(items, mid=mid)
        scored.append((rank, 0 if series else 1, seq, mid, why, series))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))

    for i, (_r, _has, _s, mid, why, series) in enumerate(scored):
        if i >= MAX_CANDIDATES:
            out.append(f"  … 외 {len(scored) - i}개 후보 생략 (근거가 약한 것들)")
            break
        out.append(f"  - {mid}   [{why}]")
        if not series:
            out.append("      ⚠ 표준품 목록에 이 계열이 없습니다")
        else:
            for drawing, name, dia, rows in series[:MAX_SERIES]:
                d = f", 직경 {dia}" if dia else ""
                out.append(f"      {drawing}  ({name}{d}, 부품번호 {rows}개)")
            if len(series) > MAX_SERIES:
                out.append(f"      … 외 계열 {len(series) - MAX_SERIES}개")

    out.append("")
    out.append("■ 다음 단계")
    out.append("  계열이 정해지면 list_parts로 그 계열의 부품번호를 확인하세요.")
    out.append("  ⚠ 부품번호(dash) 확정은 스펙 도면 판독과 검증을 거쳐야 합니다 — "
               "위 정보만으로 부품번호를 단정하지 마세요.")
    return "\n".join(out)


@mcp.tool()
@std_tool
def list_parts(mid: str, major: str = "", diameter: float = 0) -> str:
    """계열(중분류)에 속한 **실제 부품번호**를 표준품 목록에서 조회합니다. (🟢 읽기)

    find_standard로 계열을 좁힌 뒤 쓰세요. 엑셀에 적힌 값을 그대로 보여줍니다
    (판독이 아니라 조회라 값이 정확합니다).

    Args:
        mid: 중분류. 예 "BOLT, DOUBLE HEX", "CLAMP, LOOP, CUSHIONED".
        major: 대분류. 같은 중분류 이름이 여러 대분류에 있을 때만 필요합니다
               (예: 'NUT'은 NUT 밑에도 FITTING 밑에도 있습니다).
        diameter: 직경으로 좁히려면 지정 (예: 0.190). 0이면 전부.
    """
    if not (mid or "").strip():
        raise StdError("mid(중분류)가 비어 있습니다. find_standard로 먼저 좁히세요.")
    items = _catalog_items()
    dia = diameter if diameter else None
    series = catalog.find_series(items, major=major or None, mid=mid, diameter=dia)
    if not series:
        known = catalog.norm(mid) in {catalog.norm(m) for m in catalog.KNOWN_MIDS}
        raise StdError(
            f"'{mid}'"
            + (f" (직경 {diameter})" if dia else "")
            + " 에 해당하는 계열이 표준품 목록에 없습니다."
            + ("" if known else " 중분류 이름을 확인하세요 — 목록에 없는 이름입니다.")
        )

    out = [f"{mid}" + (f" / {major}" if major else "") + (f" / 직경 {diameter}" if dia else ""),
           f"계열 {len(series)}개", ""]
    for drawing, name, d, rows in series:
        out.append(f"  {drawing}  {name}" + (f"  (직경 {d})" if d else "") + f"  — 부품번호 {rows}개")
        if SPEC_DIR:
            p = catalog.get_spec_path(name, SPEC_DIR) or catalog.get_spec_path(drawing, SPEC_DIR)
            out.append(f"      스펙: {p}" if p else "      스펙: 폴더에서 찾지 못함")
    out.append("")
    out.append("⚠ 어느 부품번호(dash)를 쓸지는 스펙 도면의 치수표를 판독·검증해야 정해집니다.")
    return "\n".join(out)


def _resolve_spec(drawing: str, name: str, spec_dir: str) -> str:
    """도면번호/품명으로 스펙 PDF 경로를 찾는다. 폴더는 인자 > 설정 순."""
    if spec_table is None:
        raise StdError(f"스펙 판독 모듈을 불러오지 못했습니다: {SPEC_IMPORT_ERROR}")
    folder = spec_dir or SPEC_DIR
    if not folder:
        raise StdError(
            "스펙 폴더가 지정되지 않았습니다. spec_dir 인자로 넘기거나 "
            "mcp_server/local_settings.py에 STD_SPEC_DIR을 적으세요."
        )
    path = spec_table.find_spec_file(folder, drawing, name)
    if not path:
        raise StdError(
            f"'{drawing or name}' 스펙 파일을 {folder} 에서 찾지 못했습니다. "
            "도면번호나 품명이 파일 이름과 맞는지 확인하세요."
        )
    return path


@mcp.tool()
@std_tool
def read_spec_table(drawing: str, spec_dir: str = "", name: str = "",
                    pages: str = "", refresh: bool = False) -> str:
    """스펙 도면의 **치수표 전체**를 판독해 보여줍니다 (VLM). (🟢 읽기)

    표를 통째로 읽고 자동 검증까지 돌립니다. 어떤 컬럼이 있는지 모를 때 먼저 이걸
    부르고, 그다음 select_dash로 조건을 걸면 됩니다.

    같은 도면을 두 번 읽지 않도록 결과를 캐시합니다(판독은 쪽당 수 초 걸립니다).

    Args:
        drawing: 도면번호 (예 "AS9556"). 파일 이름을 찾는 데 씁니다.
        spec_dir: 스펙 PDF 폴더. 비우면 설정값(STD_SPEC_DIR)을 씁니다.
        name: 품명. 파일이 품명으로 저장돼 있을 때 함께 넘기면 찾을 확률이 올라갑니다.
        pages: 표가 있는 쪽 (예 "2,3"). 비우면 앞쪽부터 훑습니다.
        refresh: True면 캐시를 무시하고 다시 판독합니다.
    """
    pdf = _resolve_spec(drawing, name, spec_dir)
    cached = spec_table.load_cached(pdf) is not None and not refresh
    data = spec_table.read_table(pdf, pages=pages, refresh=refresh)
    txt, passed = spec_table.verify_table(data)

    out = [f"{os.path.basename(pdf)}" + ("  (캐시된 판독 결과)" if cached else "  (새로 판독)")]
    if data.get("table_title"):
        out.append(f"표: {data['table_title']}")
    out.append(f"쪽: {', '.join(map(str, data.get('pages_used', [])))} / "
               f"컬럼: {', '.join(data.get('columns', []))} / {len(data.get('rows', []))}행")
    out.append("")
    out.append("■ 치수 기호 (도면 그림에서 판독)")
    legend = spec_table.read_legend(pdf, refresh=refresh)
    out.append(spec_table.format_legend(legend, data.get("columns")))
    out.append("")
    out.append(spec_table.format_rows(data.get("rows", []), data.get("columns")))
    out.append("")
    out.append(f"[검증] {txt}")
    if not passed:
        out.append("⚠ 검증을 통과하지 못했습니다 — 값을 쓰기 전에 사람이 도면을 확인하세요.")
    for n in (data.get("notes") or [])[:5]:
        out.append(f"  알림: {n}")
    out.append("")
    out.append("⚠ 위 치수 기호 설명은 **VLM이 도면 그림을 읽은 것**이라 검증되지 않았습니다. "
               "조건을 걸기 전에 어느 기호가 원하는 치수인지 사용자에게 확인하세요 — "
               "기호의 뜻을 추측하지 마세요.")
    out.append('조건으로 좁히려면: select_dash(drawing="{}", conditions={{"기호":"값"}}) '
               "— 기호는 위 표의 컬럼 이름을 그대로 쓰세요."
               .format(drawing or os.path.basename(pdf)))
    return "\n".join(out)


@mcp.tool()
@std_tool
def select_dash(drawing: str, conditions: dict, spec_dir: str = "", name: str = "",
                pages: str = "") -> str:
    """치수표에서 **조건에 맞는 부품번호(dash)**를 고릅니다. (🟢 읽기)

    조건은 표의 컬럼 이름(도면의 치수 기호) → 값입니다.

    ⚠ **어느 기호가 무슨 치수인지 추측하지 마세요.** 'H'가 머리 높이인지 머리 직경인지는
    도면 그림에만 있습니다. read_spec_table을 먼저 불러 **치수 기호 범례**를 확인하고,
    거기 적힌 기호를 그대로 쓰세요. 확정되지 않은 이름을 주면 이 도구는 거르지 않고
    거절합니다(조건이 무시된 채 '전부 통과'가 되면 잘못된 부품을 고르게 되므로).

    조건 표기:
        "0.350"    같은 값
        "<=0.350"  이하 (셀이 범위면 최대값 기준 — 안전한 쪽)
        ">=0.20"   이상
        "~0.35"    셀의 범위가 이 값을 포함 (그립이 체결두께를 받는지 등)
        ".190-32"  나사 규격 (10-32 처럼 다르게 적혀 있어도 같은 나사면 맞음)
        "A286"     문자열 부분 일치

    예: conditions={"THREAD": ".190-32", "H": "<=0.350"}

    ⚠ 맞는 게 없으면 **없다고 답합니다.** 가까운 행을 참고로 보여주지만 그건 선정이
    아닙니다 — 그대로 부품번호로 쓰지 마세요.

    Args:
        drawing: 도면번호.
        conditions: {컬럼명: 조건} 사전.
        spec_dir: 스펙 PDF 폴더 (비우면 설정값).
        name: 품명 (파일 찾기 보조).
        pages: 표가 있는 쪽.
    """
    if not isinstance(conditions, dict) or not conditions:
        raise StdError('conditions가 비었습니다. 예: {"THREAD": ".190-32", "L": "<=1.5"}')
    pdf = _resolve_spec(drawing, name, spec_dir)
    data = spec_table.read_table(pdf, pages=pages)
    rows = data.get("rows", [])
    cols = data.get("columns", [])
    legend = spec_table.read_legend(pdf)

    # ★ 조건의 키가 표의 어느 컬럼인지 확정되지 않으면 **거절한다.**
    #   조건을 무시한 채 거르면 '전부 통과'가 되어 잘못된 부품을 고르게 된다.
    #   여기서 모델이 'H니까 Height겠지'라고 찍는 걸 막는 것이 이 도구의 핵심이다.
    resolved, problems = spec_table.resolve_columns(conditions, cols, legend)
    if problems:
        out = [f"{os.path.basename(pdf)} — 조건을 적용하지 못했습니다.", ""]
        for pmsg in problems:
            out.append(f"  ⚠ {pmsg}")
        out.append("")
        out.append("■ 치수 기호 (도면 그림에서 판독)")
        out.append(spec_table.format_legend(legend, cols))
        out.append("")
        out.append("어느 기호가 원하는 치수인지 확인한 뒤 그 **기호 이름 그대로** 다시 "
                   "부르세요. 추측해서 고르면 안 됩니다.")
        return "\n".join(out)

    hits, notes = spec_table.filter_rows(rows, resolved)
    vtxt, passed = spec_table.verify_table(data)

    cond = ", ".join(f"{k}={v}" for k, v in resolved.items())
    out = [f"{os.path.basename(pdf)} — 조건: {cond}", ""]
    if hits:
        out.append(f"■ 선정 {len(hits)}건")
        out.append(spec_table.format_rows(hits, data.get("columns")))
    else:
        out.append("■ 선정 0건 — 조건에 맞는 부품번호가 없습니다.")
        # 숫자 조건이 걸린 첫 컬럼 기준으로 가까운 행을 참고로 보여준다(선정 아님).
        for k, v in resolved.items():
            m = re.match(r"^[<>~=]*\s*([\d.]+)$", str(v).strip())
            if m and k in {c for r in rows for c in r}:
                near = spec_table.near_rows(rows, k, float(m.group(1)))
                if near:
                    out.append(f"  가까운 후보(선정 아님, {k} 기준):")
                    out.append(spec_table.format_rows(near, data.get("columns")))
                break
    for n in notes:
        out.append(f"  ⚠ {n}")
    out.append("")
    out.append(f"[검증] {vtxt}")
    if not passed:
        out.append("⚠ 판독 검증을 통과하지 못했습니다 — 도면을 직접 확인하세요.")
    out.append("근거: " + os.path.basename(pdf)
               + f" (쪽 {', '.join(map(str, data.get('pages_used', [])))}, VLM 판독)")
    return "\n".join(out)


@mcp.tool()
@std_tool
def find_mating_part(mid: str, thread: str = "", spec_dir: str = "") -> str:
    """볼트에 **짝이 되는 너트 계열**을 찾고, 나사가 맞는 부품번호까지 좁힙니다. (🟢 읽기)

    중분류로 짝을 정합니다(단순 문자열 치환이 아니라 매핑표) — 후보가 둘이면
    자동으로 고르지 않고 되묻습니다. 셀프락킹 여부는 설계 요구사항이라 부품 목록에서
    유도할 수 없기 때문입니다.

    Args:
        mid: 볼트 중분류 (예 "BOLT, DOUBLE HEX").
        thread: 나사 규격 (예 ".190-32"). 주면 그 나사에 맞는 dash까지 좁힙니다.
        spec_dir: 스펙 PDF 폴더 (비우면 설정값).
    """
    if catalog is None:
        raise StdError(f"catalog 모듈을 불러오지 못했습니다: {CATALOG_IMPORT_ERROR}")
    cands, hint = catalog.nut_categories_for(mid)
    out = [f"{mid} 의 짝 너트", ""]
    if not cands:
        out.append(f"■ {hint}")
        out.append("  추정하지 않습니다 — 설계 기준을 확인하세요.")
        return "\n".join(out)
    if len(cands) > 1:
        out.append(f"■ 후보가 {len(cands)}개입니다 — 사용자에게 확인이 필요합니다.")
        for c in cands:
            out.append(f"  - {c}")
        out.append(f"  {hint}")
        return "\n".join(out)

    nut_mid = cands[0]
    out.append(f"■ 짝 중분류: {nut_mid}")
    try:
        items = _catalog_items()
        series = catalog.find_series(items, mid=nut_mid)
    except StdError as e:
        out.append(f"  (표준품 목록을 읽지 못해 도면번호를 붙이지 못했습니다: {e})")
        return "\n".join(out)
    if not series:
        out.append("  ⚠ 표준품 목록에 이 계열이 없습니다.")
        return "\n".join(out)
    for drawing, name, dia, rows in series[:MAX_SERIES]:
        out.append(f"  {drawing}  {name}" + (f" (직경 {dia})" if dia else "")
                   + f" — 부품번호 {rows}개")

    if not thread.strip():
        out.append("")
        out.append("나사 규격(thread)을 주면 맞는 부품번호까지 좁힙니다.")
        return "\n".join(out)

    out.append("")
    out.append(f"■ 나사 {thread} 에 맞는 부품번호")
    folder = spec_dir or SPEC_DIR
    if not folder or spec_table is None:
        out.append("  스펙 폴더가 없어 판독하지 못했습니다 — spec_dir을 지정하세요.")
        return "\n".join(out)
    found_any = False
    for drawing, name, _dia, _rows in series[:MAX_SERIES]:
        try:
            pdf = spec_table.find_spec_file(folder, drawing, name)
            if not pdf:
                out.append(f"  {drawing}: 스펙 파일을 찾지 못했습니다")
                continue
            data = spec_table.read_table(pdf)
            col = next((c for c in data.get("columns", [])
                        if "THREAD" in str(c).upper() or str(c).upper() == "THD"), "")
            if not col:
                out.append(f"  {drawing}: 나사 컬럼을 찾지 못했습니다 "
                           f"(컬럼: {', '.join(data.get('columns', []))})")
                continue
            hits, _n = spec_table.filter_rows(data.get("rows", []), {col: thread})
            if hits:
                found_any = True
                out.append(f"  {drawing} ({col}):")
                out.append(spec_table.format_rows(hits, [col]))
            else:
                out.append(f"  {drawing}: 맞는 부품번호 없음")
        except (StdError, Exception) as e:  # noqa: BLE001 — 한 계열 실패가 전체를 막지 않게
            out.append(f"  {drawing}: 판독 실패 ({type(e).__name__}: {e})")
    if not found_any:
        out.append("")
        out.append("  ★ 맞는 너트를 찾지 못했습니다. 이건 실제로 없을 수 있습니다 — "
                   "지어내지 말고 설계 기준을 확인하세요.")
    return "\n".join(out)


@mcp.tool()
@std_tool
def std_status() -> str:
    """표준품 목록·지침서 연결 상태를 확인합니다. (🟢 읽기)

    후보가 안 나오거나 도면번호가 안 붙을 때 먼저 호출하세요.
    """
    lines = [settings.status_line()]
    lines.append(f"표준품 목록: {CATALOG_PATH or '(STD_CATALOG_PATH 미설정)'}")
    if catalog is None:
        lines.append(f"  catalog 모듈 불러오기 실패: {CATALOG_IMPORT_ERROR}")
    else:
        try:
            items = _catalog_items()
            lines.append(f"  {catalog.summarize(items)}")
            notes = catalog.validate_catalog(items)
            for n in notes[:5]:
                lines.append(f"  ⚠ {n}")
        except StdError as e:
            lines.append(f"  {e}")
    lines.append(f"스펙 폴더: {SPEC_DIR or '(STD_SPEC_DIR 미설정 — 스펙 경로를 안 보여줍니다)'}")
    if core is None:
        lines.append(f"지침서 검색: 불가 ({RAG_IMPORT_ERROR})")
    else:
        try:
            s = core.get_store().stats()
            lines.append(f"지침서 인덱스: 파일 {s['files']}개, 청크 {s['chunks']}개 "
                         f"(벡터 {s['with_vector']}개)")
        except Exception as e:  # noqa: BLE001
            lines.append(f"지침서 인덱스: 확인 실패 ({type(e).__name__}: {e})")
    lines.append(f"한국어 낱말 사전: {len(TERMS)}개")
    return "\n".join(lines)


# ─────────────────────────────── CLI / 서버 기동 ───────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="항공 표준품 찾기 MCP 서버 (계열 좁히기 — 부품번호 확정은 별도)"
    )
    parser.add_argument("--find", metavar="요구사항", help="계열 찾기를 시험하고 종료")
    parser.add_argument("--parts", metavar="중분류", help="계열의 부품번호 조회 후 종료")
    parser.add_argument("--status", action="store_true", help="연결 상태만 출력하고 종료")
    parser.add_argument("--catalog", default=None, help=f"표준품 목록 엑셀 (기본 {CATALOG_PATH})")
    parser.add_argument("--spec-dir", default=None, help="스펙 PDF 폴더")
    parser.add_argument(
        "--transport", choices=["stdio", "http", "sse"],
        default=os.getenv("STD_MCP_TRANSPORT", "stdio"),
        help="stdio(기본): 로컬 클라이언트가 직접 실행. http/sse: n8n 등 네트워크 접속.",
    )
    parser.add_argument("--host", default=os.getenv("STD_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("STD_MCP_PORT", "8094")))
    args = parser.parse_args()

    if args.catalog:
        CATALOG_PATH = os.path.abspath(os.path.expanduser(args.catalog))
    if args.spec_dir:
        SPEC_DIR = os.path.abspath(os.path.expanduser(args.spec_dir))

    if args.status:
        print(std_status())
        sys.exit(0)
    if args.find:
        print(find_standard(args.find))
        sys.exit(0)
    if args.parts:
        print(list_parts(args.parts))
        sys.exit(0)

    if args.transport in ("http", "sse"):
        path = "/mcp/" if args.transport == "http" else "/sse/"
        print(f"표준품 MCP 서버 시작 ({args.transport}) — http://{args.host}:{args.port}{path}",
              file=sys.stderr)
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    else:
        # stdio: stdout은 MCP 프로토콜 채널 — 로그는 stderr로.
        print("표준품 MCP 서버 시작 (stdio)", file=sys.stderr)
        mcp.run(transport="stdio")
