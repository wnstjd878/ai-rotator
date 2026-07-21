# fable-rotator — 구독 계정 자동 순환

클로드 구독 계정 여러 개를 번갈아 쓴다. 쓰는 길이 두 가지다.

1. **봇 자동 순환 (핵심)** — 봇이 claude 를 부르다 한도 거절을 받으면, 그 계정을 해제 시각까지
   쉬게 표시하고 다음 계정 자격으로 갈아끼운 뒤 **같은 요청을 자동으로 다시 보낸다**. 사람이 안 봐도 된다.
2. **사람이 쓰는 대화형** — 내 claude 창이 한도에 걸리면 `--next` 한 번으로 다음 계정으로 갈아끼운다.
   (대화형 창은 자동 감지가 안 된다. 이미 떠 있는 창은 껐다 켜야 새 계정이 붙는다.)

옛 방식(크롬으로 사용량 화면을 미리 읽어 고르기, `rotate.py`)도 남겨뒀다. 지금은 보조 수단이다.
잔여량을 미리 정확히 알 방법이 없어 오차가 크고, 거절이 오면 그때 넘기는 쪽이 확실하기 때문.

## 플랫폼 차이 (중요)

| | Windows | Mac |
|---|---|---|
| 자격 저장 | `~/.claude/.credentials.json` 파일 | Keychain (`Claude Code-credentials` / `kwon`) |
| 교체 방식 | 파일 바꿔치기 → 완전 자동 | `security` 로 Keychain 덮어쓰기 |
| 실행 위치 | 어디서나 | **화면(Aqua) 세션에서만**. launchd 봇은 해당되지만 ssh 원격은 안 됨 |

Mac 봇은 launchd LaunchAgent 로 화면 세션에서 도니 자동 교체가 된다.
ssh 로 직접 돌리는 스크립트는 Keychain 을 못 고쳐 교체가 실패한다.

## 최초 셋업 (계정마다 1회, 각 머신에서 각각)

라벨(A, B, C, D)이 세 곳을 잇는 공통 키다: 크롬 프로필 ↔ `creds/<라벨>.json` ↔ `accounts.json`.

| 라벨 | 크롬 프로필 디렉토리 |
|---|---|
| A | `Default` (원래 쓰던 프로필) |
| B | `Profile 1` |
| C | `Profile 2` |
| D | `Profile 3` |

1. **웹 로그인**: 크롬에서 계정마다 프로필을 만들고 각 프로필로 `claude.ai` 에 로그인.
   프로필 디렉토리명은 `chrome://version` 의 "프로필 경로" 끝부분으로 확인.
2. **CLI 자격 저장**: 각 계정으로 claude 에서 `/login` 한 뒤 그 자격을 라벨로 백업한다.
   ```
   python save_account.py A     # A 계정으로 로그인된 상태에서 → creds/A.json
   ```
   B, C, D 도 각 계정으로 다시 로그인한 뒤 반복. **Mac 은 본체 터미널에서** 실행(Keychain 읽기).
3. **지금 꽂혀 있는 계정을 알려준다** (순환기가 어느 계정으로 시작할지 알아야 한다):
   ```
   python claude_pool.py --use A
   ```

## 매일 사용

```
python claude_pool.py --status     지금 계정과 각 계정 상태 보기
python claude_pool.py --next       한도 걸렸을 때 다음 계정으로 넘기기
python claude_pool.py --use B      B 계정으로 바로 지정
python claude_pool.py -- -p "안녕"  순환 보호를 받으며 claude 한 번 실행
```

`--next` / `--use` 로 바꾼 뒤에는 **열려있던 claude 를 껐다 다시 실행**해야 적용된다.

## 봇에 끼우는 법

봇 코드에서 claude 를 부르는 `subprocess.run(...)` 을 이 모듈의 `run(...)` 으로 바꾸면 끝이다.
인자와 반환값이 `subprocess.run` 과 같아서 나머지 코드는 안 고쳐도 된다.

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/projects/fable-rotator"))
from claude_pool import run as pool_run

proc = pool_run(args, capture_output=True, text=True, timeout=240)
```

- 한도 거절이면 계정을 바꿔 같은 요청을 다시 보낸다.
- 로그인 문제면 그 계정을 30분 쉬게 하고 다음 계정으로 넘긴다.
- 원인 모를 실패는 계정을 안 바꾸고 한 번만 더 시도한 뒤 결과를 그대로 돌려준다.
- 쓸 계정이 하나도 없으면 `AllAccountsExhausted`(RuntimeError 의 한 종류)를 올리고 텔레그램으로 알린다.

여러 봇이 동시에 한도에 걸려도 교체는 잠금으로 한 번만 일어난다.
다른 봇이 이미 계정을 바꿨으면 또 바꾸지 않고 바로 재시도한다.

## 파일

핵심(자동 순환)
- `claude_pool.py` — 실행 래퍼 + 명령줄 도구 (메인)
- `limit_detect.py` — 한도 거절 판정과 해제 시각 읽기
- `pool_state.py` — 현재 계정·막힌 계정 상태와 잠금
- `notify.py` — 전 계정 소진 같은 일을 텔레그램으로 알림
- `test_rotation.py` — 순환 로직 시험 (가짜 호출로 9가지 상황 재현)
- `state.json` — 현재 상태 (자동 생성, git 제외)
- `rotator.log` — 교체 기록

자격
- `save_account.py` / `switch_account.py` — 자격 저장·교체 (Win=파일, Mac=Keychain 자동 분기)
- `accounts.json` — 계정↔프로필 매핑 / `creds/` — 자격 백업 (둘 다 git 제외)

보조(옛 방식, 사용량 화면 읽기)
- `rotate.py` — 크롬을 계정마다 재시작하며 사용량을 읽어 여유 계정을 고름
- `reader.py` — 한 계정 사용량만 읽어 판정
- `launch_chrome_debug.ps1` / `.sh` — 크롬을 디버그 포트 9222 로 재시작

## 주의

- `creds/` 는 자격정보다. 화면 출력·git 커밋 금지(`.gitignore` 처리됨).
- `creds/` 는 각 머신에서 각자 저장한다(Win 자격 파일 ≠ Mac Keychain 값). 머신 간 복사하지 말 것.
- Mac 의 Keychain 교체는 자격 값이 로컬 프로세스에 잠깐 실린다. 원격(ssh)이 아니라 본체/launchd 에서만.
- 계정을 바꿔도 이미 떠 있는 claude 세션에는 반영 안 된다. 다음 실행부터 적용된다.
- `rotate.py` 는 계정마다 크롬을 강제 종료하고 재시작한다. 그동안 크롬으로 다른 작업 하지 말 것.
