"""check_repo.py — 반입 전 규약 검사 (표준 라이브러리만).

**커밋하기 전에 돌린다.** CLAUDE.md의 규약 중 "글로 적어 두면 지켜질 것 같지만 실제로는
조용히 깨지는 것들"을 기계가 확인한다. 규칙이 있었는데도 bat 7개가 LF로 커밋돼
`run_rag_server.bat`이 사내 PC에서 죽은 적이 있다 — 규칙보다 검사가 필요한 이유다.

    python check_repo.py           # 검사 (문제가 있으면 종료 코드 1)
    python check_repo.py --fix     # 고칠 수 있는 것(bat 줄바꿈, BOM)은 고친다

여기서 잡는 것은 전부 **사내망에 들어가서야 터지는** 종류다. 폐쇄망은 로그를 반출할 수
없어 디버깅 한 번이 반나절이라, 외부망에서 잡을 수 있는 건 여기서 잡는다.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 공개 저장소에 들어가도 되는 주소. **여기 없는 호스트가 나오면 실패한다** —
# 사내 호스트·IP가 커밋되는 걸 막는 게 목적이라, 새 주소는 사람이 보고 추가해야 한다.
ALLOWED_HOSTS = {
    "127.0.0.1", "0.0.0.0", "localhost",          # 로컬 서빙
    "example.com", "api.example.com",             # 문서용 예시
    "company.com", "portal.company.com", "pypi.company.local",  # 사내 자리표시자
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    "schemas.microsoft.com",                      # Outlook MAPI 속성 태그(네트워크 호출 아님)
    "huggingface.co", "github.com", "pypi.org",   # 문서의 내려받기 안내
}
_HOST_RE = re.compile(r"https?://([A-Za-z0-9._-]+)")
# 자리표시자는 통과시킨다: 10.x.x.x, <사내VLM>, {args.host} 같은 것.
_PLACEHOLDER = re.compile(r"[<{]|x\.x\.x|\bN\.N\.N|^\.+$")

DOTFILE_OK = {".gitignore", ".claude"}            # 이미 있는 것만. 둘 다 반입 때 빼면 되는 개발 PC용이다
MAX_BYTES = 100 * 1024 * 1024                     # GitHub 파일당 제한
TEXT_EXT = {".py", ".bat", ".md", ".txt", ".json", ".js", ".html", ".css", ".iss"}

errors: list[str] = []
warns: list[str] = []


def tracked_files() -> list[Path]:
    """git이 추적하는 파일. git이 없는 PC에서는 폴더를 직접 훑는다."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
        return [ROOT / line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", "venv", "Examples", "wheelhouse", "__pycache__", "rag_vectors",
                "rag_pages", "node_modules"}
        found = []
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in skip]
            found += [Path(base) / n for n in names]
        return found


def check_bat(paths: list[Path], fix: bool) -> None:
    """bat은 CRLF + ASCII. LF면 cmd가 줄 경계를 잘못 잘라 조용히 오동작한다."""
    for p in paths:
        if p.suffix.lower() != ".bat" or not p.is_file():
            continue
        raw = p.read_bytes()
        if b"\r\n" not in raw and b"\n" in raw:
            if fix:
                p.write_bytes(raw.replace(b"\n", b"\r\n"))
                print(f"  고침: {rel(p)} → CRLF")
            else:
                errors.append(f"{rel(p)}: 줄바꿈이 LF다 (CRLF여야 함 — cmd가 오동작한다)")
        try:
            raw.decode("ascii")
        except UnicodeDecodeError:
            errors.append(f"{rel(p)}: 비ASCII 문자가 있다 (콘솔 cp949에서 깨진다)")


def check_bom(paths: list[Path], fix: bool) -> None:
    """requirements.txt는 UTF-8 BOM 유지 — 구버전 pip가 cp949로 읽어 죽는다."""
    for p in paths:
        if p.name != "requirements.txt" or not p.is_file():
            continue
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            continue
        if fix:
            p.write_bytes(b"\xef\xbb\xbf" + raw)
            print(f"  고침: {rel(p)} → BOM 추가")
        else:
            errors.append(f"{rel(p)}: UTF-8 BOM이 없다 (구버전 pip가 UnicodeDecodeError)")


def check_dotfiles(paths: list[Path]) -> None:
    """사내 반입 검사가 dotfile을 막는다 — 새로 만들지 않는다."""
    for p in paths:
        for part in p.relative_to(ROOT).parts:
            if part.startswith(".") and part not in DOTFILE_OK:
                errors.append(f"{rel(p)}: 점으로 시작하는 이름 '{part}' (사내 반입 검사가 막는다)")
                break


def check_size(paths: list[Path]) -> None:
    for p in paths:
        if p.is_file() and p.stat().st_size > MAX_BYTES:
            mb = p.stat().st_size / 1024 / 1024
            errors.append(f"{rel(p)}: {mb:.0f}MB — GitHub 파일당 100MB 제한을 넘는다")


def check_hosts(paths: list[Path]) -> None:
    """공개 저장소다 — 사내 호스트·IP가 커밋되면 안 된다."""
    for p in paths:
        if p.suffix.lower() not in TEXT_EXT or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for host in _HOST_RE.findall(line):
                if host in ALLOWED_HOSTS or _PLACEHOLDER.search(host):
                    continue
                errors.append(
                    f"{rel(p)}:{n}: 허용 목록에 없는 주소 '{host}' — 사내 주소면 "
                    f"local_settings.py로 빼고, 예시면 check_repo.py의 ALLOWED_HOSTS에 추가")


def check_tool_stdout(paths: list[Path]) -> None:
    """stdio 트랜스포트에서 stdout은 MCP 프로토콜 채널 — 도구 안의 print는 프로토콜을 깬다.

    @mcp.tool() 이 붙은 함수 안만 본다. CLI(--probe 등)의 print는 정상이라 안 본다.
    """
    for p in paths:
        if p.suffix != ".py" or p.parent.name != "mcp_server" or not p.is_file():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any("tool" in ast.dump(d) for d in node.decorator_list):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "print"
                        and not any(k.arg == "file" for k in sub.keywords)):
                    warns.append(f"{rel(p)}:{sub.lineno}: 도구 {node.name}() 안의 "
                                 f"print가 stdout으로 간다 (file=sys.stderr 필요)")


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace(os.sep, "/")
    except ValueError:
        return str(p)


def main() -> int:
    fix = "--fix" in sys.argv
    paths = [p for p in tracked_files() if p.exists()]
    print(f"검사 대상 {len(paths)}개 파일" + (" (--fix: 고칠 수 있는 건 고친다)" if fix else ""))

    check_bat(paths, fix)
    check_bom(paths, fix)
    check_dotfiles(paths)
    check_size(paths)
    check_hosts(paths)
    check_tool_stdout(paths)

    for w in warns:
        print(f"  ! {w}")
    for e in errors:
        print(f"  X {e}")
    if errors:
        print(f"\n실패: 문제 {len(errors)}건" + (f", 경고 {len(warns)}건" if warns else ""))
        if not fix:
            print("고칠 수 있는 것(bat 줄바꿈, BOM)은 --fix 로 처리된다.")
        return 1
    print(f"\n통과" + (f" (경고 {len(warns)}건 — 확인만 할 것)" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
