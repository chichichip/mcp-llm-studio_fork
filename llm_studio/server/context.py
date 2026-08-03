"""context.py

한 요청에 **무엇을 실어 보낼지** 정한다 — 이력 접기(aging)와 도구 스코프.

왜 필요한가:
    이 앱은 매 턴 대화 이력 전체를 보낸다. 그런데 도구 결과는 하나가 최대
    TOOL_RESULT_MAX(2만 자)까지 이력에 남으므로, 문서를 몇 번만 읽어도 ctx(기본 32768)가
    찬다. 넘치면 llama-server가 앞부분을 밀어내는데 거기에 system 프롬프트와 초반 지시가
    있다 — 대화가 길어질수록 조용히 멍청해지는 구조다. 도구를 많이 쓸수록 빨리 온다.

    도구가 많아지는 것도 같은 성질의 문제다. 서버를 넷만 붙여도 도구가 50개를 넘고,
    약한 로컬 모델(Gemma 등)은 그중에서 고르길 어려워한다.

두 가지 원칙:
    1. **접는 건 사본뿐** — 대화 기록(conversations/*.json)의 원문은 절대 건드리지 않는다.
       모델에 보내는 리스트만 접는다. 그래야 나중에 다시 열었을 때 내용이 남아 있고,
       접힌 자리에 "다시 호출하면 된다"고 알려 주면 모델이 스스로 복구할 수 있다.
    2. **애매하면 좁히지 않는다** — 도구 스코프를 잘못 좁히면 모델이 "할 수 없다"고
       답해 버린다. 도구가 많아 헷갈리는 것보다 나쁘다. 확신이 있을 때만 좁힌다.
"""

from __future__ import annotations

from .config import DEFAULT_TOOL_SCOPE_KEYWORDS

# 접은 도구 결과 자리에 남기는 안내. 모델이 '내용이 사라진 게 아니라 접힌 것'이고
# 다시 가져올 수 있다는 걸 알아야 한다 — 그냥 자르면 없는 내용을 지어낸다.
AGED_NOTE = "\n…[오래된 도구 결과 {total:,}자 중 앞부분만 남겼습니다. 전체가 다시 필요하면 같은 도구를 (가능하면 범위를 좁혀) 다시 호출하세요.]"


def age_messages(messages: list[dict], settings: dict) -> list[dict]:
    """모델에 보낼 사본을 만들되, 오래된 도구 결과는 앞부분만 남긴다.

    턴은 뒤에서부터 센다(user 메시지 하나 = 한 턴). 현재 턴 포함 최근
    context_tool_keep_turns 턴의 도구 결과는 원문 그대로 두고, 그보다 오래된 것만 접는다.
    지금 막 부른 도구의 결과는 항상 온전히 남는다는 뜻이다.

    원본 리스트와 그 안의 dict는 수정하지 않는다(접히는 메시지만 새 dict로 바꾼다).
    """
    if not settings.get("context_aging_enabled", True):
        return messages
    try:
        keep = max(0, int(settings.get("context_tool_keep_turns", 2)))
        limit = max(0, int(settings.get("context_tool_aged_chars", 200)))
    except (TypeError, ValueError):
        keep, limit = 2, 200

    out: list[dict] = []
    turns = 0
    for m in reversed(messages):
        if m.get("role") == "user":
            turns += 1
        elif m.get("role") == "tool" and turns >= keep:
            content = str(m.get("content") or "")
            if len(content) > limit:
                m = {**m, "content": content[:limit] + AGED_NOTE.format(total=len(content))}
        out.append(m)
    out.reverse()
    return out


def estimate_chars(messages: list[dict]) -> int:
    """이력의 대략적인 크기(문자). 토큰이 아니라 문자다 — 상대 비교용."""
    return sum(len(str(m.get("content") or "")) for m in messages)


def _scope_keywords(settings: dict) -> dict:
    custom = settings.get("tool_scope_keywords")
    if isinstance(custom, dict) and custom:
        return custom
    return DEFAULT_TOOL_SCOPE_KEYWORDS


def servers_for_tools(mcp, keywords: tuple[str, ...]) -> list[str]:
    """도구 **이름**에 주어진 낱말이 들어간 도구를 가진 서버들을 찾는다.

    첨부 파일 확장자로 서버를 고를 때 쓴다(.xlsx → 'excel'이 든 도구를 가진 서버).
    서버 이름을 하드코딩하지 않으려는 것이다 — 사용자가 'office'를 다른 이름으로
    등록했어도 도구 이름을 보고 찾아낸다.
    """
    found: list[str] = []
    try:
        servers = mcp.connected_servers()
    except Exception:  # noqa: BLE001
        return []
    for name in servers:
        try:
            tools = [t["function"]["name"].lower() for t in mcp.openai_tools(servers=[name])]
        except Exception:  # noqa: BLE001
            continue
        if any(k in t for t in tools for k in keywords):
            found.append(name)
    return found


def pick_tool_servers(mcp, query: str, settings: dict,
                      required: list[str] | None = None) -> list[str] | None:
    """이번 턴에 노출할 MCP 서버 목록. **확신이 없으면 None(=전체 노출)**을 돌려준다.

    required: 첨부 파일 등으로 '이 서버는 반드시 있어야 한다'가 이미 정해진 경우.

    좁히지 않는(None) 경우를 넉넉히 둔 게 핵심이다:
      - 기능이 꺼져 있음 / MCP 없음 / 연결된 서버가 1개 이하 (좁힐 이유가 없다)
      - 어느 서버도 안 걸림 (무슨 얘긴지 모르겠으면 다 보여준다)
      - 전부 걸림 (좁히나 마나)
    """
    if not settings.get("tool_scope_enabled", True) or mcp is None:
        return None
    try:
        servers = mcp.connected_servers()
    except Exception:  # noqa: BLE001
        return None
    if len(servers) <= 1:
        return None

    text = (query or "").lower()
    keywords = _scope_keywords(settings)
    picked = {s for s in (required or []) if s in servers}
    for name in servers:
        for kw in keywords.get(name, []):
            if kw and kw.lower() in text:
                picked.add(name)
                break

    if not picked or len(picked) == len(servers):
        return None
    # 연결 순서를 유지해 돌려준다(로그·표시가 안정적이도록)
    return [s for s in servers if s in picked]
