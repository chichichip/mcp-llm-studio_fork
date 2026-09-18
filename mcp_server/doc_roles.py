"""doc_roles.py — 인덱싱된 문서가 어떤 **역할**의 문서인지 판정한다.

왜 필요한가:
    같은 인덱스에 성격이 완전히 다른 문서가 섞여 있는데, 지금까지는 전부 "문서" 한
    덩어리로 다뤘다. 실제로는 네 종류고 **쓰는 법이 다르다**:

        라우팅(사내 지침서)  "이 요구사항이면 저 규격/도면을 봐라"  ← 표
        설계기준(ARP 등)     계산식·설계 규칙                       ← 인용해서 쓴다
        치수표(AS568 등)     dash별 실제 숫자                       ← 🔴 RAG로 읽으면 안 된다
        스펙                 그 부품의 규격 본문

    구분이 없으면 두 가지가 실제로 깨진다.
    ① standard_part_server가 근거를 **전부 "지침서"라고 표기**한다 — ARP1231에서 나온
       국제 규격 조항이 사내 규정처럼 보인다. 항공 부품에서 그 구분이 지워지면 안 된다.
    ② 치수표에 붙어야 할 경고가 `STD_SPEC_DIR` **폴더 안에 있을 때만** 붙었다. AS568
       같은 공개 표준 치수표가 일반 폴더에 있으면 아무 경고 없이 숫자가 흘러나온다.

판정 방법 — 파일명 힌트 + **전사문 내용**:
    문서가 많아 사람이 일일이 못 적으므로 자동으로 정한다. 파일명만 보면 규칙이
    금방 어긋나므로(`ARP1231.pdf`와 `ARP1231_O-RING GENERAL DESIGN.pdf`는 이름이 거의
    같지만 역할이 다르다) 전사문의 성격도 함께 본다. **자동 판정이 틀릴 때를 위해
    local_settings.py의 `RAG_DOC_ROLES`로 덮어쓸 수 있다(사람이 적은 게 언제나 이긴다).**

⚠ **도구가 돌려주는 문자열에는 마크다운을 쓰지 않는다.** llm_studio는 도구 결과를
   `pre.textContent`로 넣어 그대로 보여 준다(치수표가 줄바꿈으로 흐트러지면 안 되므로
   일부러 렌더링하지 않는다) — `**강조**`를 쓰면 별표가 그대로 화면에 찍힌다. 모델
   답변은 `md()`를 타므로 거기서는 렌더링된다. 도구 설명(docstring)과 서버
   `instructions`는 모델이 읽는 것이라 마크다운을 써도 된다.

⚠ 이 판정은 경고를 붙이고 도구를 안내하는 용도다. 판정이 틀려도 검색은 그대로
   동작한다(우아한 저하) — 다만 치수표를 놓치면 경고가 안 붙으므로, 애매하면
   치수표 쪽으로 기울도록 해 두었다(놓치는 것보다 한 번 더 경고하는 게 낫다).
"""

from __future__ import annotations

import fnmatch
import os
import re

import settings

# 역할 코드 → 사람이 읽을 이름. 빈 문자열은 '모름'.
ROLE_NAMES = {
    "routing": "라우팅(지침서)",
    "design": "설계기준",
    "table": "치수표",
    "spec": "스펙",
    "": "역할 미상",
}

# 역할별로 응답에 덧붙일 안내. 코드가 직접 박아 넣는다 — 프롬프트로 부탁하면 모델이
# 요약하며 떨어뜨린다(standard_part_server와 같은 방침).
ROLE_NOTES = {
    "table": (
        "⚠ 치수표입니다. 여기 보이는 숫자를 부품 선정 근거로 쓰지 마세요 — 청크 "
        "경계에서 행이 잘렸을 수 있고, 어느 행의 값인지 흐려졌을 수 있으며, 판독 "
        "검증을 거치지 않았습니다. 치수로 부품번호를 정해야 하면 read_spec_table / "
        "select_dash(std 서버)로 표를 통째로 판독해 쓰세요."
    ),
    "design": (
        "ℹ 설계기준입니다(사내 규정이 아니라 규격 문서). 여기 식을 쓸 때는 "
        "인용한 식 · 대입한 숫자 · 계산 결과를 모두 보여 주세요 — 사람이 검산할 수 "
        "있어야 합니다. 식에서 나온 치수로 부품번호를 고르는 마지막 단계는 "
        "select_dash(std 서버)가 검증된 표로 합니다."
    ),
    "routing": (
        "ℹ 사내 지침서입니다. 보통 '어느 규격·도면을 보라'를 가리키는 문서이니, "
        "가리키는 문서를 이어서 찾아보세요."
    ),
}

# ─────────────────────────── 파일명 힌트 ───────────────────────────
# 대소문자 무시. 순서가 우선순위다(먼저 걸리는 것이 이긴다) — `ARP1231_O-RING GENERAL
# DESIGN`처럼 규격번호와 성격어가 같이 있으면 **성격어가 이겨야** 한다.
_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("design", r"general\s*design|design\s*practice|설계\s*기준|설계\s*지침|practice"),
    ("table", r"\bsize\b|dimension|\btable\b|치수|사이즈|규격\s*표"),
    ("routing", r"지침서|선정\s*지침|guideline|선정\s*기준"),
    # 설명어 없이 규격번호만인 파일명(ARP1231.pdf, MS9555.pdf)은 스펙 본문으로 본다.
    ("spec", r"^(as|ms|nas|an|arp|ams|mil)[-_ ]?\d+[a-z]?$"),
)

# ─────────────────────────── 내용 힌트 ───────────────────────────
_DECIMAL = re.compile(r"\d+\.\d+")
_DASH_ROW = re.compile(r"^\s*\|?\s*-?\d{2,4}\s*[|\t]")      # -214 | 0.301 | ...
_DESIGN_WORDS = re.compile(
    r"압축률|스퀴즈|squeeze|compression|stretch|gland|식\s*\(|공식|"
    r"shall\s+be\s+calculated|percent\s+of|=\s*\(", re.I)
_ROUTING_WORDS = re.compile(
    r"규격\s*번호|도면\s*번호|품\s*명|대분류|중분류|적용\s*규격|해당\s*규격", re.I)
_SPEC_WORDS = re.compile(
    r"\bscope\b|\brequirements\b|applicable\s+documents|적용\s*범위|요구\s*사항", re.I)

_cache: dict[str, tuple[str, str]] = {}


def _name_role(path: str) -> tuple[str, str]:
    """파일명만으로 본 역할. 못 정하면 ('', '')."""
    stem = os.path.splitext(os.path.basename(path))[0]
    flat = re.sub(r"[_\-]+", " ", stem).strip()
    for role, pat in _NAME_HINTS:
        if re.search(pat, flat, re.I):
            return role, f"파일명 '{stem}'"
    return "", ""


def _text_role(text: str) -> tuple[str, str]:
    """전사문 내용으로 본 역할. 못 정하면 ('', '')."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return "", ""
    decimals = len(_DECIMAL.findall(text))
    dash_rows = sum(1 for ln in lines if _DASH_ROW.match(ln))
    per_line = decimals / max(1, len(lines))

    # 치수표: 소수가 줄마다 쏟아진다. 라우팅 표는 규격번호(문자)라 소수가 거의 없다.
    if (per_line >= 3.0 and decimals >= 20) or (dash_rows >= 5 and decimals >= 20):
        return "table", f"내용(소수 {decimals}개/{len(lines)}줄, dash 행 {dash_rows}개)"
    if len(_DESIGN_WORDS.findall(text)) >= 2:
        return "design", "내용(계산식·압축률 용어)"
    if len(_ROUTING_WORDS.findall(text)) >= 2:
        return "routing", "내용(규격번호·도면번호 표 머리글)"
    if len(_SPEC_WORDS.findall(text)) >= 2:
        return "spec", "내용(SCOPE/REQUIREMENTS 절)"
    return "", ""


def _under_spec_dir(key: str) -> bool:
    """STD_SPEC_DIR 아래인가. 경로 정규화 실패는 '아니다'로 본다(검색을 막지 않는다)."""
    spec_dir = settings.get("STD_SPEC_DIR", "")
    if not spec_dir:
        return False
    try:
        r = os.path.normcase(os.path.abspath(spec_dir))
    except Exception:  # noqa: BLE001
        return False
    return key == r or key.startswith(r + os.sep)


def role_of(path: str, text: str = "") -> tuple[str, str]:
    """이 문서의 역할과 그렇게 본 근거. 반환: (역할코드, 사유).

    우선순위: 사람이 적은 것 > 내용/파일명 > 스펙 폴더(마지막 수단).
    파일명이 못 정하면 내용이 정하고, **내용이 치수표라고 하면 파일명보다 우선**한다
    (경고를 놓치는 쪽이 한 번 더 붙는 쪽보다 나쁘다).
    """
    key = os.path.normcase(os.path.abspath(path))
    if key in _cache and not text:
        return _cache[key]

    # ① 사람이 적은 것이 언제나 이긴다 (RAG_DOC_ROLES = {"AS568*": "table", ...})
    rules = settings.get_obj("RAG_DOC_ROLES")
    if isinstance(rules, dict):
        base = os.path.basename(path)
        for pat, role in rules.items():
            if fnmatch.fnmatch(base.lower(), str(pat).lower()) and role in ROLE_NAMES:
                out = (role, f"local_settings.py의 RAG_DOC_ROLES['{pat}']")
                _cache[key] = out
                return out

    name_role, name_why = _name_role(path)
    text_role, text_why = _text_role(text) if text else ("", "")

    # ② 내용이 '치수표'라고 하면 파일명을 이긴다 — 놓치면 경고가 아예 안 붙는다.
    if text_role == "table":
        out = ("table", text_why)
    elif name_role:
        out = (name_role, name_why)
    elif text_role:
        out = (text_role, text_why)
    elif _under_spec_dir(key):
        # ③ 마지막 수단. **자동 판정보다 뒤에 둔다** — STD_SPEC_DIR을 지침서·설계기준이
        #    섞인 폴더(예: 인덱싱용 rag 폴더)로 잡는 운용이 실제로 있고, 그때 앞에 두면
        #    그 폴더의 모든 문서가 '치수표'가 돼 경고가 사방에 붙는다. 경고가 흔해지면
        #    무시당하므로, 도면만 든 폴더에서 '못 가린 파일'을 건지는 용도로만 쓴다.
        out = ("table", "STD_SPEC_DIR 아래인데 달리 못 가림")
    else:
        out = ("", "")
    _cache[key] = out
    return out


def role_name(role: str) -> str:
    return ROLE_NAMES.get(role, ROLE_NAMES[""])


def note_for(roles) -> str:
    """이번 응답에 섞인 역할들에 대한 안내를 모은다(중복 없이, 위험한 것부터)."""
    out = [ROLE_NOTES[r] for r in ("table", "design", "routing")
           if r in set(roles) and r in ROLE_NOTES]
    return "\n".join(out)


def clear_cache() -> None:
    """설정을 바꾼 뒤 다시 판정하게 한다(시험용)."""
    _cache.clear()
