"""worklog.py — 이 대화에서 무엇을 읽고 무엇을 고쳤는지 기록한다 (중기 기억).

기억을 세 층으로 나누면 이 파일은 가운데다:
    단기  이번 턴의 도구 결과      → 메시지 이력 (context.py가 오래된 건 접는다)
    중기  **이 대화에서 다룬 문서** → 여기
    장기  대화를 넘는 사용자 사실   → memory.py

왜 필요한가 (약한 로컬 모델에서 특히):
    1. **같은 걸 또 읽는다.** 세 턴 전에 읽은 시트를 기억 못 해 다시 read를 부른다.
       도구 결과가 접혔으면 더 그렇다. 무엇을 이미 봤는지 목록으로 주면 줄어든다.
    2. **저장하지 않은 걸 저장했다고 답한다.** office_server의 Excel 쓰기는 메모리만
       바꾸고 디스크에는 save_workbook(🔴)이 따로 있는데, 모델은 "수정했습니다"를
       "저장했습니다"로 뭉뚱그린다. 미저장 상태를 매 턴 명시하면 이 환각이 잡힌다.

회상(주입)은 모델의 도구 호출에 맡기지 않고 하네스가 매 턴 결정적으로 system에 넣는다 —
memory.py의 회상과 같은 방침이다.

기록은 대화 JSON의 `worklog` 키에 남는다(서버 쪽 파일 — 저장 위치 원칙 그대로).
"""

from __future__ import annotations

import json
import os
import time

# 도구 이름(서버 접두사를 뗀 짧은 이름)에 이게 들어가면 '문서를 다루는 도구'로 본다.
# path 인자가 없어도(=활성 문서) 기록하기 위한 판단 근거다.
_DOC_HINTS = ("excel", "word", "powerpoint", "ppt", "pdf", "workbook", "document", "slide")
# 문서를 '다루는' 게 아니라 훑어보는 도구 — 대장에 남길 대상이 아니다.
_SKIP_PREFIX = ("list_",)
_SKIP_SUFFIX = ("_status",)

# 무엇을 한 도구인지 (앞에 오는 것이 우선 — save가 write보다 먼저다)
_SAVE_HINTS = ("save", "export")
_WRITE_HINTS = ("write", "set_", "apply", "create", "delete", "move", "insert", "update")

# 대장에 세부를 적을 때 볼 인자들 (순서 유지)
_DETAIL_KEYS = ("sheet", "cell_range", "cell", "start_cell", "pages", "slides",
                "table_index", "kind")

READ, WRITE, SAVE = "읽음", "수정", "저장"


def _short(name: str) -> str:
    return name.split("__", 1)[-1]


def _action(short: str) -> str:
    if any(h in short for h in _SAVE_HINTS):
        return SAVE
    if any(h in short for h in _WRITE_HINTS):
        return WRITE
    return READ


def _detail(args: dict) -> str:
    """도구 인자에서 '어디를' 만졌는지 요약한다. 없으면 빈 문자열."""
    sheet = str(args.get("sheet") or "").strip()
    span = ""
    for key in ("cell_range", "cell", "start_cell"):
        if args.get(key):
            span = str(args[key]).strip()
            break
    if sheet and span:
        return f"{sheet}!{span}"
    bits = []
    for key in _DETAIL_KEYS:
        v = args.get(key)
        if v not in (None, "", 0):
            bits.append(f"{key}={v}")
    return ", ".join(bits)[:80]


def _target(args: dict) -> tuple[str, str]:
    """(전체 경로, 표시용 이름). path가 없거나 비면 '활성 문서'로 본다."""
    raw = args.get("path") or args.get("out_path") or args.get("file") or ""
    raw = str(raw).strip()
    if not raw:
        return "(활성 문서)", "(활성 문서)"
    return raw, os.path.basename(raw.replace("\\", "/")) or raw


def observe(conv: dict, tool_name: str, raw_args: str, settings: dict) -> None:
    """실행된 도구 호출 하나를 대장에 반영한다. 기록할 게 없으면 조용히 넘어간다.

    **실제로 실행된 호출만** 넘길 것 — 승인 거절/시간초과된 호출을 기록하면 하지도 않은
    수정을 했다고 대장이 거짓말하게 된다.
    """
    if not settings.get("worklog_enabled", True):
        return
    short = _short(tool_name)
    if short.startswith(_SKIP_PREFIX) or short.endswith(_SKIP_SUFFIX):
        return
    try:
        args = json.loads(raw_args or "{}")
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    if "path" not in args and not any(h in short for h in _DOC_HINTS):
        return  # 문서를 다루는 도구가 아니다

    target, label = _target(args)
    action = _action(short)
    turn = sum(1 for m in conv.get("messages", []) if m.get("role") == "user")
    log = conv.setdefault("worklog", [])

    if action == SAVE:
        # 이 문서의 미저장 표시를 푼다. '(활성 문서)'로 수정하고 경로로 저장하는 경우가
        # 있어(모델이 path를 생략했다가 나중에 지정) 대상이 하나뿐이면 그것도 같이 푼다.
        pending = [e for e in log if e["action"] == WRITE and not e.get("saved")]
        for e in pending:
            if e["target"] == target or len({p["target"] for p in pending}) == 1:
                e["saved"] = True

    entry = {"target": target, "label": label, "action": action,
             "detail": _detail(args), "turn": turn, "at": time.time()}
    if action == WRITE:
        entry["saved"] = False
    # 같은 대상·동작·범위는 새로 쌓지 않고 최신 것으로 갱신한다(같은 셀을 여러 번 고쳐도
    # 대장이 길어지지 않게).
    key = (entry["target"], entry["action"], entry["detail"])
    for i, e in enumerate(log):
        if (e["target"], e["action"], e["detail"]) == key:
            # 이전 항목의 saved를 **물려받지 않는다** — 저장한 뒤 같은 셀을 또 고치면
            # 그건 다시 미저장이다. 물려받으면 '저장했다'는 환각을 막으려던 표시가
            # 바로 그 상황에서 사라진다.
            log.pop(i)
            break
    log.append(entry)

    try:
        cap = max(1, int(settings.get("worklog_max_items", 12)))
    except (TypeError, ValueError):
        cap = 12
    if len(log) > cap:
        # 오래된 것부터 버리되, 미저장 수정은 남긴다 — 사용자가 저장 여부를 알아야 한다.
        keep = [e for e in log if e["action"] == WRITE and not e.get("saved")]
        rest = [e for e in log if e not in keep]
        conv["worklog"] = (rest[-(cap - len(keep)):] if cap > len(keep) else []) + keep


def make_observer(conv: dict, settings: dict):
    """agent.run_chat에 넘길 관찰자. 비활성이면 None."""
    if not settings.get("worklog_enabled", True):
        return None

    def _observer(tool_name: str, raw_args: str) -> None:
        observe(conv, tool_name, raw_args, settings)

    return _observer


def render(conv: dict, settings: dict) -> str:
    """system 프롬프트에 붙일 [작업 중인 문서] 블록. 기록이 없으면 빈 문자열."""
    if not settings.get("worklog_enabled", True):
        return ""
    log = conv.get("worklog") or []
    if not log:
        return ""
    now = sum(1 for m in conv.get("messages", []) if m.get("role") == "user")
    lines = []
    unsaved = False
    for e in log:
        ago = now - int(e.get("turn", now))
        when = "방금" if ago <= 0 else f"{ago}턴 전"
        detail = f" {e['detail']}" if e.get("detail") else ""
        mark = ""
        if e["action"] == WRITE and not e.get("saved"):
            mark = "  ⚠ 아직 저장되지 않음"
            unsaved = True
        lines.append(f"- {e['label']}  {e['action']}{detail}  ({when}){mark}")

    out = [
        "[작업 중인 문서] 이 대화에서 이미 다룬 문서다. 아래를 참고해 **같은 내용을 다시 "
        "읽지 마라**. 값이 바뀌었을 수 있으면 필요한 부분만 좁혀서 다시 확인한다.",
        *lines,
    ]
    if unsaved:
        out.append(
            "⚠ 저장되지 않은 변경이 있다. 메모리에만 반영된 상태이므로 '저장했다'고 "
            "말하지 마라. 사용자가 저장을 원하면 save_workbook을 confirm=true로 호출한다."
        )
    return "\n".join(out)
