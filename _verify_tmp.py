"""일회성: 저장된 자격이 실제로 서버에 먹히는지 확인한다(파일 교체 없음). 확인 후 삭제.

자격 파일을 바꿔 끼우지 않고, 저장본에 든 출입증으로 사용량을 직접 물어본다.
방금 새로 로그인한 계정이면 출입증이 살아 있으므로 이 조회가 성공해야 정상이다.
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import usage_check  # noqa: E402

ACC = json.loads((BASE / "accounts.json").read_text(encoding="utf-8"))
MAIL = {x["label"]: x.get("email", "?") for x in ACC}

for label in sorted(MAIL):
    f = BASE / "creds" / f"{label}.json"
    if not f.exists():
        print(f"{label} ({MAIL[label]}): 저장본 없음")
        continue
    saved = time.strftime("%m/%d %H:%M", time.localtime(f.stat().st_mtime))
    u = usage_check.get_usage(f)
    if not u:
        print(f"{label} ({MAIL[label]}): 조회 실패  저장시각={saved}")
        continue
    print(f"{label} ({MAIL[label]}): 살아있음  주간 {u.get('weekly_pct')}% 씀  저장시각={saved}")
