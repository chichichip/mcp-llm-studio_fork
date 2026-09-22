"""standard_core.py

표준품 선정 **실행 시트(playbook)** 파서 + 선정 세션 상태기계. 실행 파일이 아니다
(MCP 도구 껍데기는 standard_server.py, 인증·서빙과 분리한 rag_core/rag_server와 같은 구조).

왜 이렇게 만드는가
------------------
사내 표준품 선정 지침서를 RAG에 통째로 넣고 도구를 잔뜩 쥐여 주면, 모델이 매 턴
"지금 어떤 도구를 써야 하나 / 어떤 스펙 문서를 봐야 하나"를 다시 추론해야 해서 헤맨다.
그래서 순서를 바꾼다.

    지침서 → (사내 AI가 1회) → 선정 실행 시트(.md) → 이 코어가 단계별로 진행

시트는 사람이 읽는 마크다운 그대로지만, 기계가 읽을 수 있는 블록(```입력 / ```판단 /
```표 / ```도구 / ```기록 / ```검증 / ```엑셀)을 곁들인다. 그 덕에

  * **분기를 모델이 감으로 판단하지 않는다.** "온도 -30~204 이내인가?"는 ```판단
    블록의 식을 수집된 값으로 계산해 결정한다(evaluate).
  * **모델이 도구를 고르지 않는다.** 각 단계가 쓸 도구·조회할 표·열어야 할 엑셀을
    시트가 미리 지정해 지시문에 박아 준다.
  * **빠진 입력으로 진행되지 않는다.** 필수 입력/기록 항목이 없으면 다음 단계로
    넘어가지 않는다(`미정`으로 명시하면 통과하되 최종 리포트에 미해결로 남는다).
  * **되짚을 수 있다.** 세션이 파일로 남아 중간에 끊겨도 이어서 하고, 끝나면 판단
    근거가 붙은 선정 결과서(.md)가 나온다.

자동화 도구가 없는 표준품도 많으므로(특히 dash no. 결정), 시트는 "이 도구로 이 스펙을
찾아라"라고 지정할 뿐 스펙 읽기 자체는 기존 서버(docs__search_docs, office__*,
pdf__read_pdf_text)에 맡긴다 — 여기서 COM/엑셀을 다시 구현하지 않는 건 의도적이다.

블록이 하나도 없는 시트도 동작한다(산문을 그대로 지시문으로 넘기고 모델의 판단을
기록) — 우아한 저하. 어느 단계가 기계 판정인지/산문인지는 lint()가 알려준다.

의존성: 표준 라이브러리만. (fastmcp는 standard_server.py에서만 import)
"""

from __future__ import annotations

import ast
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ─────────────────────────────── 설정 (env/CLI로 조정) ───────────────────────────────
# CLI가 덮어쓸 수 있으므로 다른 모듈에서는 `core.PLAYBOOK_DIR`처럼 매번 속성으로 읽을 것
# (from-import로 값을 복사하면 덮어쓴 게 안 보인다 — rag_core와 같은 규약).

# 선정 실행 시트(.md) 폴더. 기본은 저장소 루트의 playbooks/.
PLAYBOOK_DIR = os.getenv("STD_PLAYBOOKS", str(Path(__file__).resolve().parent.parent / "playbooks"))
# 진행 중인 선정 세션(JSON) 폴더. 기본은 이 스크립트 옆.
STATE_DIR = os.getenv("STD_STATE", str(Path(__file__).with_name("standard_sessions")))
# 최종 선정 결과서(.md) 폴더. 기본은 세션 폴더 아래 reports/.
REPORT_DIR = os.getenv("STD_REPORTS", "")

PLAYBOOK_PATTERNS = (".md", ".markdown")
MAX_TABLE_ROWS = 40        # lookup_table이 한 번에 보여줄 최대 행 수
UNDECIDED = ("미정", "불명", "확인필요", "tbd", "TBD")  # 기록 항목을 비워 두지 않고 명시할 때 쓰는 값


class StandardError(Exception):
    """도구가 사용자에게 그대로 돌려줄 안내 메시지를 담은 예외."""


def _nfc(text: str) -> str:
    """한글 정규화(NFC). 합성/분해 표기 차이로 매칭이 깨지는 걸 막는다."""
    return unicodedata.normalize("NFC", text or "").strip()


def _report_dir() -> Path:
    return Path(REPORT_DIR) if REPORT_DIR else Path(STATE_DIR) / "reports"


# ═══════════════════════════════ 1. 값 타입 · 단위 ═══════════════════════════════


class CIStr(str):
    """대소문자·한글 정규화를 무시하고 비교하는 문자열.

    ```판단 식에서 `작동유체 in [Air, Fuel, Oil]` 같은 비교가 'air'/'AIR'로 들어온
    입력에도 맞아야 한다. 식 평가에 쓰는 문자열은 양쪽 모두 이 타입으로 바꿔서
    `==`/`in`이 어느 방향으로 호출돼도 같은 결과가 나오게 한다.
    """

    def _key(self) -> str:
        return unicodedata.normalize("NFC", str(self)).casefold().replace(" ", "")

    def __eq__(self, other) -> bool:  # noqa: D105
        if isinstance(other, str):
            return self._key() == CIStr(other)._key()
        return NotImplemented

    def __ne__(self, other) -> bool:  # noqa: D105
        eq = self.__eq__(other)
        return NotImplemented if eq is NotImplemented else not eq

    def __hash__(self) -> int:  # noqa: D105
        return hash(self._key())


# 단위 환산표. 입력이 시트가 선언한 단위와 다르면 바꿔서 저장한다 — psi로 받은 압력이
# bar 기준 조건식에 그대로 들어가는 사고를 막는 게 목적이다.
# 압력/길이는 기준 단위에 대한 배수, 온도는 함수로 처리한다.
_PRESSURE = {           # → bar
    "bar": 1.0, "bara": 1.0, "barg": 1.0,
    "psi": 0.0689476, "psia": 0.0689476, "psig": 0.0689476,
    "mpa": 10.0, "kpa": 0.01, "pa": 1e-5,
    "kgf/cm2": 0.980665, "kgf/cm²": 0.980665, "atm": 1.01325,
}
_LENGTH = {             # → mm
    "mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4, '"': 25.4,
}
_TEMP = ("c", "°c", "섭씨", "f", "°f", "화씨", "k")


def _unit_key(u: str) -> str:
    return _nfc(u).lower().replace(" ", "")


def convert_unit(value: float, src: str, dst: str) -> tuple[float, str]:
    """src 단위의 값을 dst 단위로 바꾼다. 모르는 단위면 그대로 두고 사유를 돌려준다.

    Returns:
        (변환된 값, 안내 문자열). 안내가 빈 문자열이면 변환이 필요 없었다는 뜻.
    """
    s, d = _unit_key(src), _unit_key(dst)
    if not s or not d or s == d:
        return value, ""
    if s in _TEMP and d in _TEMP:
        c = value
        if s in ("f", "°f", "화씨"):
            c = (value - 32.0) * 5.0 / 9.0
        elif s == "k":
            c = value - 273.15
        out = c
        if d in ("f", "°f", "화씨"):
            out = c * 9.0 / 5.0 + 32.0
        elif d == "k":
            out = c + 273.15
        return round(out, 4), f"{value}{src} → {round(out, 4)}{dst} 변환"
    for table in (_PRESSURE, _LENGTH):
        if s in table and d in table:
            out = round(value * table[s] / table[d], 4)
            return out, f"{value}{src} → {out}{dst} 변환"
    return value, f"⚠ 단위 '{src}'를 '{dst}'로 환산하지 못했습니다 — 값을 그대로 씁니다."


_NUM_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(.*)$")


# ═══════════════════════════════ 2. 시트 자료구조 ═══════════════════════════════


@dataclass
class Field:
    """```입력 블록이 정의하는 수집 항목."""

    name: str
    kind: str = "text"            # text | number | choice
    unit: str = ""
    required: bool = True
    choices: list[str] = field(default_factory=list)
    default: str = ""
    note: str = ""

    def spec(self) -> str:
        """모델에게 보여줄 한 줄 설명."""
        bits = []
        if self.kind == "choice":
            bits.append("/".join(self.choices))
        elif self.kind == "number":
            bits.append(f"숫자{f'({self.unit})' if self.unit else ''}")
        else:
            bits.append("자유입력")
        bits.append("필수" if self.required else "선택")
        if self.default:
            bits.append(f"기본 {self.default}")
        if self.note:
            bits.append(self.note)
        return f"{self.name} — {', '.join(bits)}"


@dataclass
class Table:
    """단계에 실린 참조표(대체규격표, dash 표 등)."""

    name: str
    headers: list[str]
    rows: list[list[str]]

    def render(self, limit: int = MAX_TABLE_ROWS) -> str:
        out = ["| " + " | ".join(self.headers) + " |",
               "|" + "|".join(["---"] * len(self.headers)) + "|"]
        for r in self.rows[:limit]:
            out.append("| " + " | ".join(r) + " |")
        if len(self.rows) > limit:
            out.append(f"…({len(self.rows) - limit}행 생략 — 질의를 좁혀서 다시 조회하세요)")
        return "\n".join(out)

    def search(self, query: str) -> list[list[str]]:
        """행 전체를 이어 붙인 문자열에 질의 토큰이 모두 들어가는 행만 고른다."""
        toks = [CIStr(t) for t in _nfc(query).split() if t]
        if not toks:
            return list(self.rows)
        hits = []
        for r in self.rows:
            blob = CIStr(" ".join(r))
            if all(str(t).casefold() in str(blob).casefold() for t in toks):
                hits.append(r)
        return hits


@dataclass
class Branch:
    """```판단 블록 — 조건식과 참/거짓 행동."""

    expr: str = ""
    on_true: dict = field(default_factory=dict)    # {"다음": "4", "메시지": ..., "기록": {...}}
    on_false: dict = field(default_factory=dict)
    raw: list[str] = field(default_factory=list)


@dataclass
class ExcelSpec:
    """```엑셀 블록 — 사내 자동화 통합문서를 열 때 쓸 정보."""

    path: str = ""
    sheet_rules: list[tuple[str, str]] = field(default_factory=list)  # (조건식, 시트)
    sheet: str = ""            # 조건 없는 고정 시트
    key: str = ""              # 조회 키가 되는 값 이름 (예: dash_no)
    extract: list[str] = field(default_factory=list)
    tool: str = ""
    note: str = ""


@dataclass
class Step:
    no: str
    title: str
    prose: str = ""
    fields: list[Field] = field(default_factory=list)
    branch: Branch | None = None
    tables: list[Table] = field(default_factory=list)
    tools: list[tuple[str, str]] = field(default_factory=list)
    records: list[tuple[str, str, bool]] = field(default_factory=list)  # (이름, 설명, 필수)
    checks: list[str] = field(default_factory=list)
    excel: ExcelSpec | None = None
    next_default: str = ""


@dataclass
class Playbook:
    pid: str
    title: str
    meta: dict = field(default_factory=dict)
    intro: str = ""
    steps: list[Step] = field(default_factory=list)
    path: str = ""

    def step(self, no: str) -> Step | None:
        for s in self.steps:
            if s.no == str(no):
                return s
        return None

    def first_step(self) -> Step | None:
        return self.steps[0] if self.steps else None

    def next_of(self, no: str) -> str:
        """시트에 명시가 없을 때의 기본 다음 단계 = 나열 순서상 다음."""
        for i, s in enumerate(self.steps):
            if s.no == str(no):
                return self.steps[i + 1].no if i + 1 < len(self.steps) else ""
        return ""

    def all_fields(self) -> dict[str, Field]:
        out: dict[str, Field] = {}
        for s in self.steps:
            for f in s.fields:
                out.setdefault(f.name, f)
        return out

    def declared_names(self) -> set[str]:
        """시트가 이름을 선언한 값 전부 (입력 + 기록 + 분기가 기록하는 것)."""
        names = set(self.all_fields())
        for s in self.steps:
            names.update(r[0] for r in s.records)
            if s.branch:
                for act in (s.branch.on_true, s.branch.on_false):
                    names.update(act.get("기록", {}))
        return names

    def aliases(self) -> list[str]:
        raw = self.meta.get("별칭", "") or self.meta.get("alias", "")
        return [a.strip() for a in re.split(r"[,/、]", raw) if a.strip()]


# ═══════════════════════════════ 3. 시트 파서 ═══════════════════════════════

# `## Step 2. 제목` / `## 2단계 제목` / `### 3) 제목` 등을 모두 받아 준다. 사내 AI가
# 만든 시트의 표기가 제각각이라 일부러 느슨하게 잡는다.
_STEP_RE = re.compile(
    r"^#{1,6}\s*(?:\*\*)?\s*(?:step|스텝|단계)?\s*(\d+)\s*(?:단계)?\s*[.):\-–]?\s*(.*?)\s*(?:\*\*)?\s*$",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)\s*(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _strip_bullet(line: str) -> str:
    return re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", line).strip()


def _strip_comment(value: str) -> str:
    """블록 값 뒤에 붙은 주석을 떼어 낸다.

    사내 AI가 만든 시트에는 `파일: C:\... ⚠ 실제 경로로 교체` 처럼 값 뒤에 메모가
    붙는 일이 흔하다. ⚠ 와 ※ 를 주석 시작 기호로 보고 잘라 낸다(README에 명시).
    """
    return re.split(r"[⚠※]", value, maxsplit=1)[0].strip().rstrip(",;")


def parse_playbook(text: str, pid: str, path: str = "") -> Playbook:
    """선정 실행 시트(.md)를 Playbook으로 읽는다.

    형식은 playbooks/README.md 참고. 기계 판독 블록이 없어도 파싱은 성공하고
    (산문만 있는 단계가 된다), 어디가 비었는지는 lint()가 보고한다.
    """
    lines = _nfc(text).replace("\r\n", "\n").split("\n")
    meta: dict[str, str] = {}
    i = 0

    # --- 프런트매터 (--- 로 감싼 `키: 값`) ---
    if i < len(lines) and lines[i].strip() in ("---", "+++"):
        i += 1
        while i < len(lines) and lines[i].strip() not in ("---", "+++"):
            if ":" in lines[i]:
                k, v = lines[i].split(":", 1)
                meta[k.strip().lstrip("-").strip()] = _strip_comment(v).strip("'\"")
            i += 1
        i += 1

    title = meta.get("표준품") or meta.get("title") or ""
    intro_lines: list[str] = []
    steps: list[Step] = []
    cur: Step | None = None
    body: list[str] = []

    def flush():
        if cur is not None:
            _fill_step(cur, body)

    while i < len(lines):
        line = lines[i]
        m = _STEP_RE.match(line)
        # 코드펜스 안의 '#'는 단계 제목이 아니다 — 펜스를 통째로 건너뛴다.
        fm = _FENCE_RE.match(line)
        if fm:
            # 여는 펜스: 닫힐 때까지 본문에 그대로 담는다.
            fence = line.strip()[:3]
            (body if cur is not None else intro_lines).append(line)
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                (body if cur is not None else intro_lines).append(lines[i])
                i += 1
            if i < len(lines):
                (body if cur is not None else intro_lines).append(lines[i])
                i += 1
            continue
        if m and m.group(1):
            flush()
            cur = Step(no=m.group(1), title=m.group(2).strip())
            steps.append(cur)
            body = []
            i += 1
            continue
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            i += 1
            continue
        (body if cur is not None else intro_lines).append(line)
        i += 1
    flush()

    pb = Playbook(
        pid=pid,
        title=title or pid,
        meta=meta,
        intro="\n".join(intro_lines).strip(),
        steps=steps,
        path=path,
    )
    return pb


def _fill_step(step: Step, body: list[str]) -> None:
    """단계 본문에서 블록·표를 뽑아내고 나머지를 산문으로 남긴다."""
    prose: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        line = body[i]
        fm = _FENCE_RE.match(line)
        if fm:
            info = _nfc(fm.group(1))
            fence = line.strip()[:3]
            i += 1
            inner: list[str] = []
            while i < n and not body[i].strip().startswith(fence):
                inner.append(body[i])
                i += 1
            i += 1  # 닫는 펜스
            _apply_block(step, info, inner, prose)
            continue
        # 펜스 밖의 마크다운 표
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(body[i + 1]):
            name = _table_name(prose)
            headers = _split_row(line)
            rows: list[list[str]] = []
            i += 2
            while i < n and "|" in body[i] and body[i].strip():
                cells = _split_row(body[i])
                if any(c for c in cells):
                    rows.append((cells + [""] * len(headers))[: len(headers)])
                i += 1
            step.tables.append(Table(name=name or f"표{len(step.tables) + 1}", headers=headers, rows=rows))
            continue
        prose.append(line)
        i += 1
    step.prose = "\n".join(prose).strip()
    # 산문에 '다음: 3' 같은 표기가 있으면 기본 다음 단계로 쓴다.
    if not step.next_default:
        step.next_default = _find_next(step.prose)


def _table_name(prose: list[str]) -> str:
    """표 바로 앞의 짧은 한 줄을 표 이름으로 쓴다 ('**대체규격표**' / '표: 대체규격')."""
    for line in reversed(prose):
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", s)
        s = s.strip("*_# ").strip()
        s = re.sub(r"^표\s*[:：]\s*", "", s)
        s = s.rstrip(":：").strip()
        return s if 0 < len(s) <= 60 else ""
    return ""


def _apply_block(step: Step, info: str, inner: list[str], prose: list[str]) -> None:
    """```<이름> 블록 하나를 단계에 반영한다. 모르는 이름이면 산문으로 되돌린다."""
    head = info.split()[0].lower() if info.split() else ""
    rest = info[len(head):].strip()
    lines = [ln for ln in inner if ln.strip()]

    if head in ("입력", "input", "inputs"):
        for ln in lines:
            f = _parse_field(ln)
            if f:
                step.fields.append(f)
    elif head in ("판단", "분기", "decision", "branch"):
        step.branch = _parse_branch(lines)
    elif head in ("도구", "tools", "tool"):
        for ln in lines:
            ln = _strip_bullet(ln)
            m = re.split(r"\s*(?:—|-{1,2}|:|：)\s*", ln, maxsplit=1)
            step.tools.append((m[0].strip(), m[1].strip() if len(m) > 1 else ""))
    elif head in ("기록", "record", "records", "출력"):
        for ln in lines:
            ln = _strip_bullet(ln)
            parts = [p.strip() for p in ln.split("|")]
            name = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
            required = not any("선택" in p or "optional" in p.lower() for p in parts[1:])
            if name:
                step.records.append((name, desc, required))
    elif head in ("검증", "확인", "check", "checks", "checklist"):
        step.checks.extend(_strip_bullet(ln) for ln in lines if _strip_bullet(ln))
    elif head in ("엑셀", "excel"):
        step.excel = _parse_excel(lines)
    elif head in ("표", "table"):
        tbl = _parse_table_block(lines, rest or f"표{len(step.tables) + 1}")
        if tbl:
            step.tables.append(tbl)
    else:
        # 모르는 블록(예: 실제 예제 코드)은 산문에 그대로 둔다 — 우아한 저하.
        prose.append(f"```{info}")
        prose.extend(inner)
        prose.append("```")


def _parse_table_block(lines: list[str], name: str) -> Table | None:
    rows = [ln for ln in lines if "|" in ln and not _TABLE_SEP_RE.match(ln)]
    if len(rows) < 2:
        return None
    headers = _split_row(rows[0])
    data = [(_split_row(r) + [""] * len(headers))[: len(headers)] for r in rows[1:]]
    return Table(name=name, headers=headers, rows=data)


_UNIT_IN_PAREN = re.compile(r"[(（]\s*([^)）]*)\s*[)）]")


def _looks_like_choices(part: str) -> bool:
    """`Radial/Axial`처럼 짧은 보기 나열인지 — 설명 문장과 구분한다."""
    if "/" not in part:
        return False
    toks = [t.strip() for t in part.split("/")]
    return len(toks) >= 2 and all(0 < len(t) <= 20 and " " not in t for t in toks)


def _parse_field(line: str) -> Field | None:
    """```입력 한 줄을 Field로. `이름 | 타입 | 옵션` 이 기본, 없으면 느슨하게 추정."""
    ln = _strip_bullet(line)
    if not ln:
        return None
    parts = [p.strip() for p in ln.split("|")]
    name = parts[0]
    # 이름에 붙은 괄호는 단위/선택지 힌트로 쓴다: 작동유체(Air/Fuel/Oil), 작동압력(bar)
    f = Field(name=name)
    m = _UNIT_IN_PAREN.search(name)
    if m:
        f.name = name[: m.start()].strip() or name
        hint = m.group(1).strip()
        items = [x.strip() for x in re.split(r"[/,·]", hint) if x.strip()]
        if len(items) >= 2:
            f.kind, f.choices = "choice", items
        elif hint:
            f.kind, f.unit = "number", hint
    f.name = _strip_comment(f.name).strip(":：").strip()
    if not f.name:
        return None

    for p in parts[1:]:
        low = p.lower()
        if re.match(r"^(choice|선택지|택1)\s*[:：]", low) or (":" in p and re.match(r"^(선택)\s*[:：]", p)):
            f.kind = "choice"
            f.choices = [c.strip() for c in p.split(":", 1)[1].split(",") if c.strip()]
        elif low.startswith(("number", "숫자", "int", "float", "수치")):
            f.kind = "number"
            um = _UNIT_IN_PAREN.search(p)
            if um:
                f.unit = um.group(1).strip()
        elif low.startswith(("text", "문자", "자유", "str")):
            f.kind = "text"
        elif p.strip() in ("필수", "required"):
            f.required = True
        elif p.strip() in ("선택", "optional", "선택사항", "옵션"):
            f.required = False
        elif re.match(r"^(기본|default)\s*[:：=]", low):
            f.default = p.split(":", 1)[-1].split("=", 1)[-1].strip()
        elif _looks_like_choices(p) and not f.choices and f.kind == "text":
            # `Radial/Axial`처럼 타입 선언 없이 보기만 적은 경우. 타입이 이미
            # 정해졌거나(number 등) 문장이면 건드리지 않는다 — 설명 문구를
            # 선택지로 오인하지 않기 위함.
            f.kind = "choice"
            f.choices = [c.strip() for c in p.split("/") if c.strip()]
        else:
            f.note = (f.note + " " + p).strip()
    if f.choices and f.kind != "choice":
        f.kind = "choice"
    return f


_NEXT_RE = re.compile(r"(?:다음|next|이동|→|->)\s*[:=]?\s*(?:step|스텝|단계)?\s*(\d+)", re.IGNORECASE)
_STEPWORD_RE = re.compile(r"(?:step|스텝)\s*(\d+)|(\d+)\s*단계", re.IGNORECASE)
_DONE_RE = re.compile(r"(완료|종료|끝|done|finish)")


def _find_next(text: str) -> str:
    """문장에서 '다음 단계'를 뽑는다. 완료를 뜻하면 '완료'."""
    if not text:
        return ""
    m = _NEXT_RE.search(text)
    if m:
        return m.group(1)
    m = _STEPWORD_RE.search(text)
    if m:
        return m.group(1) or m.group(2)
    if _DONE_RE.search(text) and re.search(r"(다음|next|이동|→|->)", text, re.IGNORECASE):
        return "완료"
    return ""


def _parse_action(text: str) -> dict:
    """`참 -> ...` 뒤쪽을 행동으로 읽는다.

    `키=값` 쌍은 기록으로, `Step 4` / `다음: 4` / `완료`는 이동으로, 나머지는 메시지로.
    사내 AI가 산문으로 써도 최소한 이동은 건지도록 일부러 느슨하다.
    """
    act: dict = {}
    recs: dict[str, str] = {}
    msgs: list[str] = []
    for chunk in re.split(r"[;；]", text):
        c = chunk.strip()
        if not c:
            continue
        m = re.match(r"^([\w가-힣_.\-]+)\s*=\s*(.+)$", c)
        if m and m.group(1).lower() not in ("다음", "next"):
            recs[m.group(1).strip()] = m.group(2).strip()
            continue
        m = _NEXT_RE.search(c)
        if m:
            act["다음"] = m.group(1)
            leftover = _NEXT_RE.sub("", c).strip(" ,.·-→>")
            if leftover:
                msgs.append(leftover)
            continue
        if _DONE_RE.fullmatch(c.strip()):
            act["다음"] = "완료"
            continue
        msgs.append(c)
    if "다음" not in act:
        # 명시적인 `다음=N`이 없으면 그제서야 문장 속 'Step N'을 다음 단계로 본다.
        # (메시지에 섞인 'Step 3에서 …' 같은 안내 문구를 지우지 않으려고 뒤로 미룬다.)
        nx = _find_next(text)
        if nx:
            act["다음"] = nx
    if recs:
        act["기록"] = recs
    if msgs:
        act["메시지"] = " / ".join(msgs)
    return act


def _parse_branch(lines: list[str]) -> Branch:
    b = Branch(raw=list(lines))
    for ln in lines:
        s = _strip_bullet(ln)
        m = re.match(r"^(조건|condition|if)\s*[:：]\s*(.+)$", s, re.IGNORECASE)
        if m:
            b.expr = m.group(2).strip()
            continue
        m = re.match(r"^(참|예|yes|true|만족|모두\s*yes)\s*(?:[:：]|->|→|=>)\s*(.+)$", s, re.IGNORECASE)
        if m:
            b.on_true = _parse_action(m.group(2))
            continue
        m = re.match(r"^(거짓|아니오|아니면|no|false|불만족|하나라도\s*no)\s*(?:[:：]|->|→|=>)\s*(.+)$",
                     s, re.IGNORECASE)
        if m:
            b.on_false = _parse_action(m.group(2))
            continue
    return b


def _parse_excel(lines: list[str]) -> ExcelSpec:
    x = ExcelSpec()
    for ln in lines:
        s = _strip_bullet(ln)
        m = re.match(r"^(파일|file|경로|path)\s*[:：]\s*(.+)$", s, re.IGNORECASE)
        if m:
            x.path = _strip_comment(m.group(2))
            continue
        m = re.match(r"^(시트|sheet)\s*[:：]\s*(.+)$", s, re.IGNORECASE)
        if m:
            spec = _strip_comment(m.group(2))
            # `조건 -> 시트` 규칙이 ; 로 여러 개 올 수 있다. 조건이 없으면 고정 시트.
            found = False
            for chunk in re.split(r"[;；]", spec):
                mm = re.match(r"^(.+?)\s*(?:->|→|=>)\s*(.+)$", chunk.strip())
                if mm:
                    x.sheet_rules.append((mm.group(1).strip(), _strip_comment(mm.group(2))))
                    found = True
            if not found:
                x.sheet = spec
            continue
        m = re.match(r"^(조회키|키|key)\s*[:：]\s*(.+)$", s, re.IGNORECASE)
        if m:
            x.key = _strip_comment(m.group(2))
            continue
        m = re.match(r"^(추출|출력|extract)\s*[:：]\s*(.+)$", s, re.IGNORECASE)
        if m:
            x.extract = [c.strip() for c in re.split(r"[,、]", _strip_comment(m.group(2))) if c.strip()]
            continue
        m = re.match(r"^(도구|tool)\s*[:：]\s*(.+)$", s, re.IGNORECASE)
        if m:
            x.tool = _strip_comment(m.group(2))
            continue
        x.note = (x.note + " " + s).strip()
    return x


# ═══════════════════════════════ 4. 조건식 평가 ═══════════════════════════════
#
# ```판단 블록의 식을 **수집된 값으로 계산**한다. 이 서버의 존재 이유 중 하나 —
# "온도 -30~204 이내인가?" 같은 판정을 모델의 감에 맡기지 않는다.
#
# eval()은 쓰지 않는다. ast로 파싱한 뒤 아래 화이트리스트 노드만 직접 계산한다.
# 시트는 사내에서 만들지만, 그래도 임의 코드가 실행되는 경로를 열어 두지 않는다.

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.BinOp,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Set,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Add, ast.Sub, ast.Mult, ast.Div,
)


def _normalize_expr(expr: str) -> str:
    """사람이 쓴 식을 파이썬 문법으로 다듬는다 (AND/그리고/≤ 등)."""
    e = _nfc(expr)
    e = e.replace("≤", "<=").replace("≥", ">=").replace("≠", "!=").replace("–", "-")
    e = re.sub(r"(?<![<>=!])=(?!=)", "==", e)                 # a = b → a == b
    e = re.sub(r"\band\b|\bAND\b|\bAnd\b|그리고|&&", " and ", e)
    e = re.sub(r"\bor\b|\bOR\b|\bOr\b|또는|\|\|", " or ", e)
    e = re.sub(r"\bnot\b|\bNOT\b|아님", " not ", e)
    e = re.sub(r"\bin\b|\bIN\b|에\s*속함|중\s*하나", " in ", e)
    return e.strip()


def expr_names(expr: str) -> list[str]:
    """식에 나오는 식별자 목록 (선언된 값인지 판별하는 데 쓴다)."""
    try:
        tree = ast.parse(_normalize_expr(expr), mode="eval")
    except SyntaxError:
        return []
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})


def _lit(v):
    """식 평가용 값 변환 — 문자열은 CIStr로 감싸 대소문자 차이를 흡수한다."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return CIStr(str(v))


def _eval_node(node, env: dict):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        return _lit(node.value)
    if isinstance(node, ast.Name):
        # 값이 있으면 그 값, 없으면 이름 자체를 문자열 리터럴로 본다.
        # (`유체 in [Air, Fuel]`처럼 따옴표 없이 쓴 보기 값을 받아 주기 위함)
        return _lit(env[node.id]) if node.id in env else CIStr(node.id)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_eval_node(e, env) for e in node.elts]
    if isinstance(node, ast.UnaryOp):
        v = _eval_node(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        return +v
    if isinstance(node, ast.BoolOp):
        vals = [_eval_node(v, env) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.BinOp):
        a, b = _eval_node(node.left, env), _eval_node(node.right, env)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        return a / b
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, env)
            if isinstance(op, ast.In):
                ok = any(right_i == left for right_i in right) if isinstance(right, list) else left in right
            elif isinstance(op, ast.NotIn):
                ok = not (any(right_i == left for right_i in right) if isinstance(right, list) else left in right)
            elif isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            else:
                # 숫자 비교 — 문자열로 들어온 값도 숫자로 바꿔 본다.
                ln, rn = _as_num(left), _as_num(right)
                if ln is None or rn is None:
                    raise StandardError(
                        f"조건식의 크기 비교에 숫자가 아닌 값이 있습니다: {left!s} / {right!s}"
                    )
                if isinstance(op, ast.Lt):
                    ok = ln < rn
                elif isinstance(op, ast.LtE):
                    ok = ln <= rn
                elif isinstance(op, ast.Gt):
                    ok = ln > rn
                else:
                    ok = ln >= rn
            if not ok:
                return False
            left = right
        return True
    raise StandardError(f"조건식에 쓸 수 없는 구문이 있습니다: {type(node).__name__}")


def _as_num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM_RE.match(str(v))
    return float(m.group(1)) if m else None


def evaluate(expr: str, values: dict, declared: set[str] | None = None) -> tuple[bool, str]:
    """조건식을 값으로 계산한다.

    Args:
        expr: ```판단 블록의 `조건:` 식.
        values: 지금까지 수집된 값 (이름 → 값).
        declared: 시트가 이름을 선언한 값들. 여기 있는데 아직 안 모인 이름이 식에
            나오면 진행을 막는다(추측 금지). 선언되지 않은 이름은 문자열 리터럴로 본다.

    Returns:
        (판정 결과, 값을 대입해 보여 주는 근거 문자열)

    Raises:
        StandardError: 값이 모자라거나 식을 계산할 수 없을 때.
    """
    e = _normalize_expr(expr)
    if not e:
        raise StandardError("조건식이 비어 있습니다.")
    try:
        tree = ast.parse(e, mode="eval")
    except SyntaxError as ex:
        raise StandardError(f"조건식을 해석하지 못했습니다: {expr} ({ex.msg})") from ex
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise StandardError(f"조건식에 쓸 수 없는 구문이 있습니다: {type(node).__name__}")

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if declared:
        missing = sorted(n for n in names if n in declared and n not in values)
        if missing:
            raise StandardError("조건을 판정할 값이 아직 없습니다: " + ", ".join(missing))
    result = bool(_eval_node(tree, values))
    return result, _describe(expr, values, names)


def _describe(expr: str, values: dict, names: set[str]) -> str:
    """식에 실제 값을 괄호로 달아 근거로 남긴다 — 사람이 리포트에서 검산할 수 있게."""
    out = expr
    for n in sorted(names, key=len, reverse=True):
        if n in values:
            out = re.sub(rf"(?<![\w가-힣_]){re.escape(n)}(?![\w가-힣_])",
                         f"{n}({values[n]})", out)
    return out


# ═══════════════════════════════ 5. 값 수집 ═══════════════════════════════


# 값 구분자: 줄바꿈·세미콜론은 언제나, 쉼표는 **바로 뒤가 또 `키=값`일 때만**.
# (`dash_근거=표 2, 단면지름 3.53mm` 같은 서술형 값이 쉼표에서 잘리는 걸 막는다)
_VALUE_SPLIT = re.compile(r"""[\n;；]|,(?=\s*["']?[\w가-힣 _.\-/]{1,40}["']?\s*[:=])""")


def parse_values(text: str) -> dict[str, str]:
    """모델이 넘긴 값 문자열을 딕셔너리로. JSON도, `키=값; 키=값`도 받는다.

    약한 모델이 JSON을 자주 깨뜨려서 두 형식을 모두 허용한다(우아한 저하).
    """
    s = _nfc(text)
    if not s:
        return {}
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in obj.items()}
        except json.JSONDecodeError:
            pass  # JSON이 깨졌으면 아래 키=값 파서로 넘어간다
    out: dict[str, str] = {}
    for chunk in re.split(_VALUE_SPLIT, s):
        c = chunk.strip().strip("{}").strip()
        if not c:
            continue
        m = re.match(r'^["\']?([\w가-힣 _.\-/]+?)["\']?\s*[:=]\s*(.*)$', c)
        if not m:
            continue
        key = m.group(1).strip()
        val = m.group(2).strip().strip('"').strip("'").rstrip(",")
        if key:
            out[key] = val
    return out


def coerce_value(f: Field | None, raw: str) -> tuple[object, str]:
    """입력 한 개를 시트가 선언한 타입/단위로 맞춘다.

    Returns:
        (값, 안내 문자열). 안내는 단위 변환처럼 사용자에게 알릴 내용.

    Raises:
        StandardError: 선택지에 없는 값 등 명백히 틀린 입력.
    """
    v = _nfc(str(raw))
    if f is None or v in UNDECIDED:
        return v, ""
    if f.kind == "number":
        m = _NUM_RE.match(v)
        if not m:
            raise StandardError(f"'{f.name}'은(는) 숫자여야 합니다 (받은 값: {raw}).")
        num = float(m.group(1))
        src = m.group(2).strip()
        note = ""
        if src and f.unit:
            num, note = convert_unit(num, src, f.unit)
        elif src and not f.unit:
            note = f"'{f.name}' 단위({src})는 시트에 선언이 없어 그대로 기록합니다."
        return (int(num) if float(num).is_integer() else num), note
    if f.kind == "choice" and f.choices:
        for c in f.choices:
            if CIStr(c) == CIStr(v):
                return c, ""
        raise StandardError(
            f"'{f.name}' 값 '{raw}'은(는) 선택지에 없습니다. 가능한 값: {', '.join(f.choices)}"
        )
    return v, ""


# ═══════════════════════════════ 6. 시트 로딩 ═══════════════════════════════


def playbook_files() -> list[Path]:
    d = Path(PLAYBOOK_DIR)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in PLAYBOOK_PATTERNS and p.name.upper() != "README.MD"
    )


def load_playbook(name: str) -> Playbook:
    """파일명(확장자 무관)·제목·별칭 중 아무거나로 시트를 찾는다."""
    q = _nfc(name)
    if not q:
        raise StandardError("표준품(시트) 이름을 지정하세요. list_playbooks로 목록을 볼 수 있습니다.")
    files = playbook_files()
    if not files:
        raise StandardError(
            f"선정 실행 시트가 없습니다. 시트(.md)를 {PLAYBOOK_DIR} 폴더에 넣으세요 "
            "(형식은 playbooks/README.md 참고)."
        )
    # 1순위: 파일명 정확 일치
    for p in files:
        if CIStr(p.stem) == CIStr(q) or CIStr(p.name) == CIStr(q):
            return _read_playbook(p)
    # 2순위: 제목/별칭 일치 → 3순위: 부분 일치
    loaded = [_read_playbook(p) for p in files]
    for pb in loaded:
        if CIStr(pb.title) == CIStr(q) or any(CIStr(a) == CIStr(q) for a in pb.aliases()):
            return pb
    key = str(CIStr(q)).casefold().replace(" ", "")
    hits = [pb for pb in loaded
            if key in str(CIStr(pb.pid)).casefold() or key in str(CIStr(pb.title)).casefold()
            or any(key in str(CIStr(a)).casefold() for a in pb.aliases())]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise StandardError(
            f"'{name}'에 해당하는 시트가 여럿입니다: " + ", ".join(h.pid for h in hits)
        )
    raise StandardError(
        f"'{name}' 시트를 찾지 못했습니다. 있는 시트: " + ", ".join(p.stem for p in files)
    )


def _read_playbook(path: Path) -> Playbook:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 사내 PC에서 메모장으로 저장하면 cp949가 섞여 들어오기도 한다 — 죽지 않게 재시도.
        text = path.read_text(encoding="cp949", errors="replace")
    return parse_playbook(text, pid=path.stem, path=str(path))


# ═══════════════════════════════ 7. 선정 세션 ═══════════════════════════════
#
# 진행 상태를 JSON 파일로 남긴다. 대화가 끊기거나 서버가 재시작돼도 이어서 진행할 수
# 있고, 끝나면 판단 근거가 그대로 붙은 선정 결과서(.md)가 나온다.

DONE = "완료"


@dataclass
class Session:
    sid: str
    playbook: str
    created: str
    updated: str
    step: str
    values: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)
    log: list = field(default_factory=list)
    request: str = ""
    report_path: str = ""

    @property
    def done(self) -> bool:
        return self.step == DONE

    def to_dict(self) -> dict:
        return {
            "sid": self.sid, "playbook": self.playbook, "created": self.created,
            "updated": self.updated, "step": self.step, "values": self.values,
            "notes": self.notes, "log": self.log, "request": self.request,
            "report_path": self.report_path,
        }

    @staticmethod
    def from_dict(d: dict) -> "Session":
        return Session(
            sid=d.get("sid", ""), playbook=d.get("playbook", ""),
            created=d.get("created", ""), updated=d.get("updated", ""),
            step=str(d.get("step", "")), values=d.get("values", {}) or {},
            notes=d.get("notes", {}) or {}, log=d.get("log", []) or [],
            request=d.get("request", ""), report_path=d.get("report_path", ""),
        )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _state_dir() -> Path:
    p = Path(STATE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_name(sid: str) -> str:
    """세션 id를 파일명으로 쓸 수 있게 다듬는다."""
    return re.sub(r"[^\w가-힣.\-]", "_", sid) or "session"


def session_path(sid: str) -> Path:
    return _state_dir() / (_safe_name(sid) + ".json")


def save_session(s: Session) -> None:
    s.updated = _now()
    session_path(s.sid).write_text(
        json.dumps(s.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_session(sid: str) -> Session:
    p = session_path(_nfc(sid))
    if not p.is_file():
        avail = [x.stem for x in _state_dir().glob("*.json")][-5:]
        hint = (" 진행 중인 세션: " + ", ".join(avail)) if avail else ""
        raise StandardError(f"세션 '{sid}'을(를) 찾지 못했습니다.{hint}")
    return Session.from_dict(json.loads(p.read_text(encoding="utf-8")))


def list_sessions() -> list[Session]:
    out = []
    for p in sorted(_state_dir().glob("*.json")):
        try:
            out.append(Session.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue  # 깨진 파일 하나 때문에 목록 전체가 죽지 않게
    return sorted(out, key=lambda s: s.updated, reverse=True)


def delete_session(sid: str) -> str:
    p = session_path(_nfc(sid))
    if not p.is_file():
        raise StandardError(f"세션 '{sid}'을(를) 찾지 못했습니다.")
    p.unlink()
    return f"세션 '{sid}'을(를) 삭제했습니다. (리포트 파일은 남아 있습니다)"


def new_session(pb: Playbook, request: str = "") -> Session:
    first = pb.first_step()
    if first is None:
        raise StandardError(
            f"시트 '{pb.pid}'에 단계가 없습니다. '## Step 1. 제목' 형태의 제목이 필요합니다 "
            "(형식은 playbooks/README.md 참고)."
        )
    base = f"{pb.pid}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    sid, n = base, 2
    while session_path(sid).is_file():
        sid, n = f"{base}-{n}", n + 1
    s = Session(sid=sid, playbook=pb.pid, created=_now(), updated=_now(),
                step=first.no, request=_nfc(request))
    _apply_defaults(pb, first, s)
    save_session(s)
    return s


def _apply_defaults(pb: Playbook, step: Step, s: Session) -> None:
    for f in step.fields:
        if f.default and f.name not in s.values:
            try:
                val, _note = coerce_value(f, f.default)
                s.values[f.name] = val
                s.notes[f.name] = "시트 기본값"
            except StandardError:
                pass  # 기본값이 잘못돼도 진행은 막지 않는다


def _match_key(key: str, known: dict) -> str | None:
    """모델이 넘긴 키를 시트가 선언한 이름에 맞춘다 (공백/대소문자 차이 흡수)."""
    if key in known:
        return key
    k = str(CIStr(key)).casefold().replace(" ", "").replace("_", "")
    for name in known:
        if str(CIStr(name)).casefold().replace(" ", "").replace("_", "") == k:
            return name
    return None


# ═══════════════════════════════ 8. 단계 지시문 렌더링 ═══════════════════════════════
#
# 모델이 "이제 뭘 해야 하지"를 고민하지 않도록, 한 단계에 필요한 것을 전부 한 화면에
# 담아 준다 — 지시·수집할 입력·확보된 값·기계 판정 조건·참고표·쓸 도구·기록 항목,
# 그리고 마지막에 **다음에 호출할 도구 한 줄**.

_BAR = "━" * 58


def _fmt_value(name: str, s: Session, pb: Playbook) -> str:
    v = s.values[name]
    f = pb.all_fields().get(name)
    unit = f" {f.unit}" if (f and f.unit) else ""
    return f"{name}={v}{unit}"


def render_step(pb: Playbook, s: Session, head: str = "") -> str:
    """현재 단계의 지시문. head는 직전 단계 처리 결과 요약."""
    if s.done:
        return (head + "\n\n" if head else "") + render_done(pb, s)
    step = pb.step(s.step)
    if step is None:
        raise StandardError(f"시트 '{pb.pid}'에 Step {s.step}이(가) 없습니다.")

    idx = [x.no for x in pb.steps].index(step.no) + 1
    out: list[str] = []
    if head:
        out += [head, ""]
    out += [
        _BAR,
        f"[표준품 선정] {pb.title}  ·  Step {step.no}/{pb.steps[-1].no} ({idx}번째/{len(pb.steps)}단계)",
        f"단계 제목: {step.title}" if step.title else "",
        f"세션: {s.sid}",
        _BAR,
        "",
    ]
    if step.prose:
        out += ["▣ 지시 (선정 지침서에서 뽑은 내용)", _indent(step.prose), ""]

    if step.fields:
        out.append("▣ 수집할 입력 — 사용자에게 물어서 채우세요 (추측 금지)")
        for f in step.fields:
            mark = "✔" if f.name in s.values else ("·" if f.required else "○")
            cur = f" → 지금 값: {s.values[f.name]}" if f.name in s.values else ""
            out.append(f"  {mark} {f.spec()}{cur}")
        out.append("")

    if s.values:
        out += ["▣ 지금까지 확보된 값",
                _indent("  ·  ".join(_fmt_value(k, s, pb) for k in s.values)), ""]

    if step.branch and step.branch.expr:
        out += [
            "▣ 기계 판정 조건 — 직접 판단하지 마세요. 값만 넘기면 서버가 계산합니다.",
            f"    {step.branch.expr}",
        ]
        if step.branch.on_true.get("메시지"):
            out.append(f"    · 참이면: {step.branch.on_true['메시지']}")
        if step.branch.on_false.get("메시지"):
            out.append(f"    · 거짓이면: {step.branch.on_false['메시지']}")
        need = [n for n in expr_names(step.branch.expr)
                if n in pb.declared_names() and n not in s.values]
        if need:
            out.append(f"    ⚠ 아직 없는 값: {', '.join(need)} — 이 값들을 먼저 채워야 판정됩니다.")
        out.append("")
    elif step.branch:
        out += ["▣ 판단 블록에 조건식이 없습니다 — 지시를 근거로 판단하고 note에 사유를 남기세요.", ""]

    if step.tables:
        out.append("▣ 참고 표 (이 시트에 실려 있음 — 밖에서 찾지 마세요)")
        for t in step.tables:
            out.append(f"  · {t.name} ({len(t.rows)}행) — "
                       f"lookup_table(playbook='{pb.pid}', table='{t.name}', query='<찾을 조건>')")
        out.append("")

    if step.tools:
        out.append("▣ 이 단계에서 쓸 도구 (시트가 지정 — 다른 도구를 찾아 헤매지 마세요)")
        for name, desc in step.tools:
            out.append(f"  · {name}{f' — {desc}' if desc else ''}")
        out.append("")

    if step.excel:
        out += [_render_excel(step.excel, s), ""]

    if step.records:
        out.append("▣ 이 단계에서 기록해야 할 항목 (values로 함께 넘기세요)")
        for name, desc, req in step.records:
            mark = "✔" if name in s.values else ("·" if req else "○")
            out.append(f"  {mark} {name}{f' — {desc}' if desc else ''}"
                       f"{'' if req else ' (선택)'}")
        out.append("  ※ 확정할 수 없으면 값을 '미정'으로 넘기세요 — 결과서에 미해결로 남습니다.")
        out.append("")

    if step.checks:
        out.append("▣ 검증 체크리스트 — 하나씩 확인하고 결과를 note에 적으세요")
        for i, c in enumerate(step.checks, 1):
            out.append(f"  {i}) {c}")
        out.append("")

    out += ["▶ 다음 행동", f"    {_submit_hint(step, s)}"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _submit_hint(step: Step, s: Session) -> str:
    want = [f.name for f in step.fields if f.name not in s.values]
    want += [r[0] for r in step.records if r[0] not in s.values]
    sample = "; ".join(f"{n}=<값>" for n in want[:4]) if want else ""
    note = ", note='검증 결과/판단 사유'" if (step.checks or not want) else ""
    return f"submit_step(session_id='{s.sid}', values='{sample}'{note})"


def _render_excel(x: ExcelSpec, s: Session) -> str:
    """```엑셀 블록을 '지금 이 세션 값 기준'의 구체적 지시로 바꾼다."""
    out = ["▣ 사내 자동화 엑셀"]
    if x.path:
        out.append(f"  파일: {x.path}")
    sheet = x.sheet
    why = ""
    for cond, sh in x.sheet_rules:
        try:
            ok, detail = evaluate(cond, s.values, None)
        except StandardError:
            ok, detail = False, ""
        if ok:
            sheet, why = sh, f" (조건 {detail or cond} 충족)"
            break
    if sheet:
        out.append(f"  시트: {sheet}{why}")
    elif x.sheet_rules:
        out.append("  시트: " + " / ".join(f"{c} → {sh}" for c, sh in x.sheet_rules)
                   + "  ⚠ 값이 없어 시트를 정하지 못했습니다 — 조건에 쓰인 값을 먼저 채우세요.")
    if x.key:
        kv = s.values.get(x.key)
        out.append(f"  조회 키: {x.key}" + (f" = {kv}" if kv is not None else " (아직 값 없음)"))
    if x.extract:
        out.append(f"  추출할 값: {', '.join(x.extract)}")
    tool = x.tool or "office__read_excel_range"
    args = f"path=r'{x.path}'" if x.path else "path=<파일 경로>"
    if sheet:
        args += f", sheet='{sheet}'"
    out.append(f"  → {tool}({args}) 로 위 값을 읽어 values에 담아 제출하세요.")
    if x.note:
        out.append(f"  비고: {x.note}")
    return "\n".join(out)


def _indent(text: str, pad: str = "    ") -> str:
    return "\n".join(pad + ln if ln.strip() else "" for ln in text.split("\n"))


def render_done(pb: Playbook, s: Session) -> str:
    loc = f"\n결과서: {s.report_path}" if s.report_path else ""
    return (f"✅ [{pb.title}] 선정이 끝났습니다. (세션 {s.sid}){loc}\n"
            f"아래 결과서를 사용자에게 그대로 보여 주세요.\n\n{report_markdown(pb, s)}")


# ═══════════════════════════════ 9. 단계 제출 (상태 전이) ═══════════════════════════════


def submit(pb: Playbook, s: Session, values_text: str = "", note: str = "") -> str:
    """한 단계를 마감하고 다음 단계로 넘어간다.

    필수 입력/기록이 비어 있으면 **넘어가지 않고** 무엇이 없는지 알려 준 뒤 같은
    단계에 머문다. ```판단 블록이 있으면 조건식을 수집된 값으로 계산해 분기를 정한다
    (모델의 판단이 아니라 서버의 계산이다).
    """
    if s.done:
        return render_done(pb, s)
    step = pb.step(s.step)
    if step is None:
        raise StandardError(f"시트 '{pb.pid}'에 Step {s.step}이(가) 없습니다.")

    before = dict(s.values)
    known = pb.all_fields()
    declared = pb.declared_names()
    incoming = parse_values(values_text)
    msgs: list[str] = []
    rejected: list[str] = []

    for raw_key, raw_val in incoming.items():
        if raw_val == "":
            continue
        name = _match_key(raw_key, known) or _match_key(raw_key, {n: None for n in declared}) or raw_key
        f = known.get(name)
        try:
            val, note_txt = coerce_value(f, raw_val)
        except StandardError as e:
            rejected.append(str(e))
            continue
        s.values[name] = val
        if note_txt:
            s.notes[name] = note_txt
            msgs.append(f"ℹ {note_txt}")
        if f is None and name not in declared:
            s.notes[name] = "시트에 선언되지 않은 값 (참고용으로 기록)"
    _apply_defaults(pb, step, s)

    if rejected:
        # 잘못된 값은 저장하지 않고 같은 단계에 머문다.
        save_session(s)
        return render_step(pb, s, head="⛔ 값을 받지 못했습니다:\n  " + "\n  ".join(rejected))

    # --- 필수 항목 점검 (여기서 막는 게 이 서버의 핵심) ---
    miss_in = [f.name for f in step.fields if f.required and f.name not in s.values]
    miss_rec = [r[0] for r in step.records if r[2] and r[0] not in s.values]
    if miss_in or miss_rec:
        save_session(s)
        parts = []
        if miss_in:
            parts.append("필수 입력이 없습니다: " + ", ".join(miss_in))
        if miss_rec:
            parts.append("기록할 항목이 없습니다: " + ", ".join(miss_rec)
                         + "  (확정 불가면 '미정'으로 명시)")
        return render_step(pb, s, head="⛔ 다음 단계로 넘어가지 못했습니다.\n  " + "\n  ".join(parts))
    if step.checks and not _nfc(note):
        save_session(s)
        return render_step(
            pb, s,
            head="⛔ 검증 단계입니다 — 체크리스트 확인 결과를 note에 적어 다시 제출하세요.",
        )

    # --- 분기 ---
    entry: dict = {
        "시각": _now(), "단계": step.no, "제목": step.title,
        "메모": _nfc(note), "이전값": before,
    }
    act: dict = {}
    if step.branch and step.branch.expr:
        try:
            result, detail = evaluate(step.branch.expr, s.values, declared)
        except StandardError as e:
            save_session(s)
            return render_step(pb, s, head=f"⛔ 조건을 판정하지 못했습니다: {e}")
        act = step.branch.on_true if result else step.branch.on_false
        entry["조건"] = step.branch.expr
        entry["판정"] = "참" if result else "거짓"
        entry["근거"] = detail
        msgs.append(f"⚖ 판정: {detail} → {'참' if result else '거짓'}")
        if act.get("메시지"):
            msgs.append(f"→ {act['메시지']}")
    for k, v in (act.get("기록") or {}).items():
        f = known.get(_match_key(k, known) or k)
        try:
            s.values[_match_key(k, known) or k] = coerce_value(f, v)[0]
        except StandardError:
            s.values[k] = v
        msgs.append(f"✔ 시트 규칙에 따라 기록: {k}={v}")
    if act.get("기록"):
        entry["기록"] = act["기록"]
    if step.checks:
        entry["검증"] = list(step.checks)

    # --- 다음 단계 ---
    nxt = act.get("다음") or step.next_default or pb.next_of(step.no)
    if nxt and nxt != DONE and pb.step(nxt) is None:
        msgs.append(f"⚠ 시트가 가리키는 Step {nxt}이(가) 없어 여기서 마칩니다 — 시트를 점검하세요.")
        nxt = DONE
    entry["다음"] = nxt or DONE
    s.log.append(entry)
    s.step = nxt or DONE

    if s.done:
        write_report(pb, s)   # s.report_path를 채운다 (실패해도 진행은 막지 않음)
        save_session(s)
        head = "\n".join(msgs)
        return (head + "\n\n" if head else "") + render_done(pb, s)
    save_session(s)
    head = f"✅ Step {step.no} 완료." + ("\n" + "\n".join(msgs) if msgs else "")
    return render_step(pb, s, head=head)


def back(pb: Playbook, s: Session) -> str:
    """직전 단계로 되돌린다 (값도 그 시점으로 복원)."""
    if not s.log:
        raise StandardError("되돌릴 단계가 없습니다 — 아직 아무 단계도 제출하지 않았습니다.")
    entry = s.log.pop()
    s.values = dict(entry.get("이전값") or {})
    s.step = str(entry.get("단계"))
    s.report_path = ""
    save_session(s)
    return render_step(pb, s, head=f"↩ Step {s.step}로 되돌렸습니다 (그 시점의 값으로 복원).")


# ═══════════════════════════════ 10. 선정 결과서 ═══════════════════════════════


def report_markdown(pb: Playbook, s: Session) -> str:
    """단계별 판단 근거가 붙은 선정 결과서. 도면 검토에 그대로 첨부할 수 있게 쓴다."""
    known = pb.all_fields()
    inputs = [n for n in s.values if n in known]
    results = [n for n in s.values if n not in known]
    undecided = [n for n, v in s.values.items() if str(v) in UNDECIDED]

    out = [
        f"# 표준품 선정 결과서 — {pb.title}",
        "",
        f"- 세션: `{s.sid}`",
        f"- 실행 시트: `{pb.pid}`" + (f" ({pb.meta.get('지침서')})" if pb.meta.get("지침서") else ""),
        f"- 시작: {s.created} / 마지막 갱신: {s.updated}",
        f"- 진행: {'완료' if s.done else f'진행 중 (현재 Step {s.step})'}",
    ]
    if s.request:
        out.append(f"- 요청: {s.request}")
    out += ["", "## 1. 입력 조건", ""]
    if inputs:
        out += ["| 항목 | 값 | 비고 |", "|---|---|---|"]
        for n in inputs:
            f = known.get(n)
            unit = f" {f.unit}" if (f and f.unit) else ""
            out.append(f"| {n} | {s.values[n]}{unit} | {s.notes.get(n, '')} |")
    else:
        out.append("_아직 없음_")

    out += ["", "## 2. 단계별 진행과 판단 근거", ""]
    if not s.log:
        out.append("_아직 없음_")
    for e in s.log:
        out.append(f"### Step {e.get('단계')}. {e.get('제목', '')}")
        if e.get("조건"):
            out.append(f"- 조건: `{e['조건']}`")
            out.append(f"- 판정: **{e.get('판정')}** — 근거 `{e.get('근거', '')}`")
        if e.get("기록"):
            out.append("- 시트 규칙에 따른 기록: "
                       + ", ".join(f"{k}={v}" for k, v in e["기록"].items()))
        if e.get("검증"):
            out.append("- 검증 항목 (확인 결과는 아래 메모 참조):")
            for c in e["검증"]:
                out.append(f"  - {c}")
        if e.get("메모"):
            out.append(f"- 메모: {e['메모']}")
        nxt = e.get("다음")
        out.append("- 다음: 선정 종료" if nxt == DONE else f"- 다음: Step {nxt}")
        out.append("")

    out += ["## 3. 선정 결과", ""]
    if results:
        out += ["| 항목 | 값 |", "|---|---|"]
        for n in results:
            out.append(f"| {n} | {s.values[n]} |")
    else:
        out.append("_아직 없음_")

    out += ["", "## 4. 미해결 항목", ""]
    if undecided:
        for n in undecided:
            out.append(f"- ⚠ **{n}** — 확정되지 않았습니다. 담당자 확인 필요.")
    else:
        out.append("없음")
    out += ["", "---", f"_생성: {_now()} · 실행 시트 기반 자동 작성 — 최종 책임은 설계 담당자에게 있습니다._"]
    return "\n".join(out)


def write_report(pb: Playbook, s: Session) -> str:
    """결과서를 파일로 남긴다. 실패해도 선정 진행을 막지 않는다(우아한 저하)."""
    try:
        d = _report_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / (_safe_name(s.sid) + ".md")
        p.write_text(report_markdown(pb, s), encoding="utf-8")
        s.report_path = str(p)
        return str(p)
    except OSError as e:
        s.report_path = ""
        return f"(결과서 파일 저장 실패: {e} — 아래 본문을 직접 저장하세요)"


# ═══════════════════════════════ 11. 시트 점검(lint) ═══════════════════════════════


def lint(pb: Playbook) -> str:
    """시트가 기계 판독 가능한지 점검한다.

    사내 AI가 만든 시트를 그대로 넣었을 때 **어느 단계가 자동 판정이고 어느 단계가
    모델의 자유 판단으로 남는지**를 보여 주는 게 목적이다. 경고가 있어도 시트는 동작한다.
    """
    out = [f"■ 시트 점검: {pb.pid} — {pb.title}"]
    if pb.path:
        out.append(f"  파일: {pb.path}")
    if pb.meta:
        out.append("  메타: " + ", ".join(f"{k}={v}" for k, v in pb.meta.items()))
    out.append(f"  단계 수: {len(pb.steps)}")
    if not pb.steps:
        out.append("  ⛔ 단계가 없습니다 — '## Step 1. 제목' 형태의 제목이 필요합니다.")
        return "\n".join(out)

    declared = pb.declared_names()
    nums = [s.no for s in pb.steps]
    warns_total = 0
    for st in pb.steps:
        bits = []
        if st.fields:
            bits.append(f"입력 {len(st.fields)}")
        if st.branch and st.branch.expr:
            bits.append("자동판정")
        elif st.branch:
            bits.append("판단(식 없음)")
        if st.tables:
            bits.append(f"표 {len(st.tables)}")
        if st.tools:
            bits.append(f"도구 {len(st.tools)}")
        if st.records:
            bits.append(f"기록 {len(st.records)}")
        if st.checks:
            bits.append(f"검증 {len(st.checks)}")
        if st.excel:
            bits.append("엑셀")
        out.append(f"  · Step {st.no}. {st.title or '(제목 없음)'} — "
                   + (", ".join(bits) if bits else "산문만"))

        w: list[str] = []
        if not bits:
            w.append("기계 판독 블록이 없습니다 — 모델이 산문만 보고 판단하게 됩니다.")
        if st.branch and st.branch.expr:
            if not ast_ok(st.branch.expr):
                w.append(f"조건식을 해석하지 못했습니다: {st.branch.expr}")
            else:
                lits = _literal_names(st.branch.expr)
                unknown = [n for n in expr_names(st.branch.expr)
                           if n not in declared and n not in lits]
                if unknown:
                    w.append("조건식의 " + ", ".join(unknown)
                             + " 은(는) 시트가 선언한 값이 아니라 '문자열'로 취급됩니다 "
                               "(선택지 값이면 정상, 입력 항목이면 ```입력에 추가하세요).")
            if not st.branch.on_true or not st.branch.on_false:
                w.append("참/거짓 중 한쪽 행동이 없습니다 (`참 -> ...`, `거짓 -> ...`).")
        for f in st.fields:
            if f.kind == "choice" and not f.choices:
                w.append(f"입력 '{f.name}'이 선택형인데 선택지가 비었습니다.")
        for tgt in _branch_targets(st):
            if tgt and tgt != DONE and tgt not in nums:
                w.append(f"가리키는 Step {tgt}이(가) 시트에 없습니다.")
        if st.excel and not st.excel.path:
            w.append("엑셀 블록에 `파일:` 경로가 없습니다.")
        names = [t.name for t in st.tables]
        if len(names) != len(set(names)):
            w.append("같은 이름의 표가 둘 이상입니다 — lookup_table이 헷갈립니다.")
        for line in w:
            out.append(f"      ⚠ {line}")
        warns_total += len(w)

    out.append(f"  경고 {warns_total}건." if warns_total else "  경고 없음.")
    return "\n".join(out)


def _literal_names(expr: str) -> set[str]:
    """`유체 in [Air, Fuel]`의 Air/Fuel처럼 목록 안에 따옴표 없이 쓴 보기 값들.

    이것들은 값 이름이 아니라 문자열 리터럴로 쓰인 것이므로 lint가 경고하지 않는다.
    """
    try:
        tree = ast.parse(_normalize_expr(expr), mode="eval")
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            out.update(e.id for e in node.elts if isinstance(e, ast.Name))
    return out


def ast_ok(expr: str) -> bool:
    try:
        ast.parse(_normalize_expr(expr), mode="eval")
        return True
    except SyntaxError:
        return False


def _branch_targets(st: Step) -> list[str]:
    tgts = [st.next_default]
    if st.branch:
        tgts += [st.branch.on_true.get("다음", ""), st.branch.on_false.get("다음", "")]
    return [t for t in tgts if t]
