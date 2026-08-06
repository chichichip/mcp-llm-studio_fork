# -*- coding: utf-8 -*-
"""프롬프트 분리. 판독이 안 맞으면 여기만 고친다."""

TABLE_PROMPT = """You are reading a dimension table from an aerospace fastener standard drawing (MS / AS / NAS).

Find the dimension table. Its title varies by standard: "TABLE I - DASH NUMBERS AND DIMENSIONS", "TABLE I - PART NUMBERS AND DIMENSIONS", "TABLE 1A - DIMENSIONS A-E", or similar.

CRITICAL: this table is usually SPLIT INTO SEVERAL COLUMN GROUPS placed side by side on the same page. The left group may hold dash -02..-15, the middle -16..-30, the right -31..-45. Each group repeats the same headers. Read EVERY group and merge them into ONE list. Missing a group is the most common failure - check the full width of the table.

For each row extract:
- "dash": the row identifier from the FIRST column, as a string, WITHOUT any leading minus sign. That column may be headed "DASH NUMBER", "PART NUMBER", "BASIC NO.", "SIZE CODE", or similar. Always use the key "dash" no matter what the column is called. Keep the value exactly as printed (e.g. "02", "08", "1032").
- one key per dimension column, using the exact column header letter as the key (e.g. "L", "K", "T")

Rules:
- Values are inches. Keep them EXACTLY as printed. Do not round, pad, or reformat.
- A range like ".062-.082" must stay one string ".062-.082". Do not split it.
- A thread spec like ".1640-36" must stay one string ".1640-36".
- SKIP the weight column entirely.
- If any character is unclear, return null for that value. Do NOT guess.
- Do NOT invent rows. Report only rows you can actually see.
- Dash numbers may be out of order near the end (later additions). Report them in the order printed.

Return ONLY a JSON object. No markdown fences, no commentary:

{
  "table_title": "<title as printed>",
  "id_column": "<what the first column is actually called>",
  "columns": ["<header letters in order>"],
  "rows": [
    {"dash": "02", "L": ".250", "K": ".062-.082"}
  ]
}
"""

# 표가 좌우로 넓어 한 번에 안 잡힐 때: crop 으로 그룹 하나씩 읽히고 병합
GROUP_PROMPT = TABLE_PROMPT.replace(
    "CRITICAL: this table is usually SPLIT INTO SEVERAL COLUMN GROUPS placed side by side on the same page. The left group may hold dash -02..-15, the middle -16..-30, the right -31..-45. Each group repeats the same headers. Read EVERY group and merge them into ONE list. Missing a group is the most common failure - check the full width of the table.",
    "This image contains ONE column group of a larger table. Read every row you see in it."
)


# ---------------------------------------------------------------------------
# 도면 주기(callout)에서 계열 속성 추출.
# 치수표가 아니라 도면 영역의 지시선/주기와 REQUIREMENTS 문단을 읽는다.
# 나사규격은 계열당 1개이므로 표 판독과 별개로 1회만 하면 된다.
# ---------------------------------------------------------------------------

META_PROMPT = """You are reading an aerospace fastener standard drawing (MS / AS / NAS).

Do NOT read the dimension table. Instead read the DRAWING CALLOUTS (leader lines
and notes pointing at the part) and the REQUIREMENTS / NOTES text block.

Extract these fields:

1. "standard_no"  - the standard number in the title block (e.g. "MS9555", "AS4395")
2. "title"        - the title as printed in the title block
3. "thread"       - the thread callout pointing at the threaded end,
                    e.g. ".164-36 UNJF-3A", ".190-32UNJF-3A", "10-32UNJF-3B".
                    Copy it EXACTLY as printed including class (3A / 3B).
                    If the thread differs per dash number (i.e. it appears as a
                    TABLE COLUMN rather than a single callout), return null and
                    set "thread_varies_by_dash" to true.
4. "thread_pd"    - pitch diameter range if printed (e.g. ".1439-.1460"), else null
5. "material"     - the material spec from REQUIREMENTS, e.g. "AMS 5731"
6. "material_desc"- the material description, e.g. "CORROSION AND HEAT RESISTANT STEEL"
7. "coating"      - surface coating spec if stated (e.g. "AMS 2410 silver plate"), else null
8. "temperature"  - any temperature rating printed in the title or notes
                    (e.g. "1200F"), else null
9. "thread_spec"  - the thread standard referenced (e.g. "MIL-S-8879", "AS8879"), else null

Rules:
- Copy values EXACTLY as printed. Do not normalize, expand, or reformat.
- If a field is not present or not legible, return null. Do NOT guess.
- The thread callout is usually near the threaded end of the part, with a leader line.

Return ONLY a JSON object, no markdown fences, no commentary:

{
  "standard_no": "MS9555",
  "title": "SCREW, MACHINE-DOUBLE HEXAGON, EXTENDED WASHER HEAD, ...",
  "thread": ".164-36 UNJF-3A",
  "thread_varies_by_dash": false,
  "thread_pd": ".1439-.1460",
  "material": "AMS 5731",
  "material_desc": "CORROSION AND HEAT RESISTANT STEEL",
  "coating": null,
  "temperature": null,
  "thread_spec": "MIL-S-8879"
}
"""
