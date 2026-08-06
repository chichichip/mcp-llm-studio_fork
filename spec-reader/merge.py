# -*- coding: utf-8 -*-
"""여러 페이지에서 읽은 표를 하나로 병합.

두 가지 경우를 모두 처리한다.

1) 행 분할  - 같은 표가 페이지를 넘어 이어짐 (dash 는 다르고 컬럼은 같음)
              p1: dash 02~15 [L,K]   p2: dash 16~30 [L,K]
              -> 행을 이어붙임

2) 컬럼 분할 - 같은 부품을 다른 치수 표가 나눠 다룸 (dash 는 같고 컬럼은 다름)
              p2: TABLE 1A dash 02~48 [A,B,C,D,E]
              p3: TABLE 1B dash 02~48 [F,G,H,J,L,M,W]
              -> 같은 dash 끼리 컬럼을 합침
"""

from collections import OrderedDict


def merge_pages(results):
    """results: [(page_no, parsed_dict), ...]
    반환: (merged_dict, notes)
    """
    merged = OrderedDict()      # dash -> {컬럼: 값}
    origin = {}                 # (dash, 컬럼) -> page_no  충돌 추적용
    col_order = []              # 컬럼 등장 순서 유지
    id_cols = set()
    titles = []
    notes = []

    for page_no, res in results:
        if not res:
            continue
        rows = res.get("rows") or []
        if not rows:
            continue

        t = res.get("table_title")
        if t:
            titles.append(f"p{page_no}: {t}")
        ic = res.get("id_column")
        if ic:
            id_cols.add(ic)

        for r in rows:
            dash = r.get("dash")
            if dash is None:
                continue
            dash = str(dash).strip().lstrip("-")
            slot = merged.setdefault(dash, OrderedDict())

            for k, v in r.items():
                if k == "dash":
                    continue
                if k not in col_order:
                    col_order.append(k)

                if k in slot and slot[k] != v:
                    # 같은 칸을 두 페이지가 다르게 읽음
                    if slot[k] is None:
                        slot[k] = v          # null 이었으면 채움
                    elif v is not None:
                        notes.append(
                            f"충돌 dash {dash} '{k}': "
                            f"p{origin.get((dash, k), '?')}={slot[k]} vs p{page_no}={v}"
                        )
                else:
                    slot[k] = v
                    origin[(dash, k)] = page_no

    # dash 정렬 - 숫자면 숫자순, 아니면 문자순
    def key(d):
        return (0, int(d)) if d.isdigit() else (1, d)

    out_rows = []
    for dash in sorted(merged, key=key):
        row = OrderedDict([("dash", dash)])
        for c in col_order:
            row[c] = merged[dash].get(c)
        out_rows.append(row)

    # 컬럼이 비어 있는 dash 알림 (컬럼 분할인데 한쪽만 읽힌 경우)
    if len(col_order) > 1:
        for row in out_rows:
            missing = [c for c in col_order if row.get(c) is None]
            if missing and len(missing) < len(col_order):
                notes.append(f"dash {row['dash']}: {', '.join(missing)} 값 없음")

    if len(id_cols) > 1:
        notes.append(f"식별 컬럼명 불일치: {', '.join(sorted(id_cols))}")

    return {
        "table_title": " | ".join(titles) if titles else None,
        "id_column": sorted(id_cols)[0] if id_cols else None,
        "columns": col_order,
        "rows": out_rows,
    }, notes
