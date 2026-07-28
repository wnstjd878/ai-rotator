"""
== 운영 맥락 ==
용도: 지피티(Codex) 계정별 설정 폴더를 다룬다. 계정 하나당 폴더 하나다.
      라벨 → 폴더 경로 해석, 폴더 준비(설정 복사), 로그인 여부 확인.
왜 폴더를 나누나(2026-07-28 실측으로 확정): 한 폴더 안에서 다른 계정으로 로그인하면
      **이전 계정의 자격이 서버에서 폐기된다**
      ("Your access token could not be refreshed because you have since logged out or
        signed in to another account"). 그래서 자격 파일을 백업해 두고 갈아끼우는 방식은
      지피티에서 성립하지 않는다(두 번 실측: 2주 전 사본, 당일 사본 모두 폐기됨).
      폴더를 나누면 서로 다른 설치로 취급돼 두 계정이 공존한다.
      같은 원리로 Mac 로그인은 Windows 에서 여러 번 갈아끼워도 살아 있었다.
계정 추가: python codex_login.py <라벨>   (그 라벨의 폴더로 codex login 을 띄운다)
전환 방식: 파일을 복사하지 않는다. 어느 폴더를 쓸지(CODEX_HOME)만 바꾼다.
          그래서 자격이 낡거나 덮어써질 일이 없다 — 각 폴더의 codex 가 자기 자격을 갱신한다.
마지막 점검: 2026-07-28
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ACCOUNTS_FILE = BASE / "codex_accounts.json"
DEFAULT_HOME = Path.home() / ".codex"
# 새 폴더에 복사해 둘 설정. 없으면 그냥 건너뛴다(자격 파일은 절대 복사하지 않는다).
COPY_FILES = ("config.toml", "hooks.json")


def accounts() -> list[dict]:
    """codex_accounts.json 의 계정 목록. 파일이 없거나 깨졌으면 빈 목록."""
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [a for a in data if isinstance(a, dict) and str(a.get("label", "")).strip()]


def labels() -> list[str]:
    return [str(a["label"]).strip() for a in accounts()]


def home_of(label: str) -> Path:
    """그 계정의 설정 폴더. 목록에 없으면 기본 폴더를 준다."""
    for a in accounts():
        if str(a.get("label", "")).strip() == label:
            raw = str(a.get("codex_home") or "").strip()
            if raw:
                return Path(raw).expanduser()
    return DEFAULT_HOME


def auth_path(label: str) -> Path:
    return home_of(label) / "auth.json"


def is_logged_in(label: str) -> bool:
    """그 폴더에 로그인이 들어 있는지. 자격 내용은 안 본다(파일 크기만)."""
    p = auth_path(label)
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def logged_in_labels() -> list[str]:
    """로그인이 살아 있는 계정 라벨. 계정 목록 순서를 지킨다."""
    return [l for l in labels() if is_logged_in(l)]


def prepare(label: str) -> Path:
    """로그인 전에 폴더를 만들고 설정을 복사해 둔다. 폴더 경로를 돌려준다.

    폴더가 없으면 codex 가 경고를 내고 별칭 설정도 못 한다. 설정(config.toml/hooks.json)을
    같이 넣어야 모델·승인 방식·끼어드는 스크립트가 계정마다 같게 동작한다.
    자격 파일(auth.json)은 절대 복사하지 않는다 — 복사한 자격은 어차피 폐기된다.
    """
    home = home_of(label)
    home.mkdir(parents=True, exist_ok=True)
    for name in COPY_FILES:
        src, dst = DEFAULT_HOME / name, home / name
        if src.exists() and not dst.exists() and src != dst:
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    return home


def env_for(label: str, base_env: dict | None = None) -> dict:
    """그 계정으로 codex 를 실행할 때 쓸 환경. CODEX_HOME 만 바꾼다."""
    import os

    env = dict(base_env if base_env is not None else os.environ)
    env["CODEX_HOME"] = str(home_of(label))
    return env


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for l in labels():
        mark = "로그인 있음" if is_logged_in(l) else "로그인 없음 (codex_login.py 로 로그인)"
        print(f"{l}: {home_of(l)}  — {mark}")
