# ai-rotator: 구독 계정 자동 순환 (클로드 + 지피티, 여러 머신 공유)

구독 계정 여러 개를 여러 머신이 같이 쓰면서, 한 계정을 주간한도까지 다 태우고
다음 계정으로 순서대로 넘어간다. **클로드(claude)와 지피티(codex) 둘 다 같은 뼈대를 쓴다.**

| | 클로드 | 지피티(Codex) |
|---|---|---|
| 명령 | `claude_pool.py`, 짧은 명령 `cc`/`cn`/`cs` | `codex_pool.py`, 짧은 명령 `gc`/`gn`/`gs` |
| 상태 파일 | `state.json` / `accounts.json` / `creds/` | `codex_state.json` / `codex_accounts.json` / `creds/codex/` |
| 자격 | `~/.claude/.credentials.json` | `~/.codex/auth.json` |
| 사용량 조회 | `api.anthropic.com/api/oauth/usage` | `chatgpt.com/backend-api/codex/usage` |
| 공유 라벨 | `A`, `B`, … | `codex:A`, `codex:B`, … (한 테이블을 나눠 씀) |

지피티 쪽만 다른 점은 아래 "지피티(Codex) 순환" 절에.

## 두 가지 핵심

**순차 소진.** 항상 최저 인덱스(A→B→C→D)부터 쓴다. A가 살아 있으면 세 머신 모두 A만 쓴다.
A가 주간한도로 오래 막히면 다 같이 B로 넘어간다. 5시간 한도로 잠깐 막힌 계정은 곧 풀려
다시 A로 돌아오므로, 자연히 오래 막히는 주간한도 기준으로 순서가 넘어간다.
분산(여러 계정을 동시에 조금씩)하지 않는다. 그래야 나머지 계정이 온전히 리셋되며 롤링된다.

**공유 상태.** 세 머신이 "어느 계정이 언제까지 막혔나"를 Supabase 한 테이블에서 같이 본다.
이게 있어야 세 머신이 같은 계정에 수렴한다. 없으면 각자 다른 계정에 앉아 흩어진다.
자격(로그인)은 공유하지 않는다. 머신마다 따로다.

| 무엇 | 범위 | 왜 |
|---|---|---|
| 로그인 자격 | 머신마다 따로 | Windows=파일, Mac=Keychain. 형식이 달라 복사 불가 |
| 구독 한도 | 계정 단위로 세 머신 공유 | 같은 계정이면 어느 머신에서 쓰든 같은 한도 창을 깎음 |
| 순환 상태(막힘) | 공유(Supabase) | 세 머신이 같은 계정을 순서대로 태우려면 필요 |

## 머신별 지형

| 머신 | claude 쓰는 것 | 순환 방법 |
|---|---|---|
| winserver (192.168.0.3) | viral-radar 전체 (작업 스케줄러) | `install_shim.ps1` 로 가로채기 |
| Mac mini | makwangsu-chat, mkt-agent, content-layer (launchd) | `install_shim.sh` 로 가로채기 |
| Windows 본체 | 대화형 claude code | `claude_pool.py --next` 수동 |

## 셋업

### 1. 공유 저장소 (한 번)

신규 Supabase 프로젝트를 만들고, SQL Editor 에 아래를 붙여넣어 실행한다.
(`python shared_state.py` 를 설정 없이 돌리면 이 SQL 을 출력해 준다.)

```sql
create table if not exists rotator_accounts (
  label text primary key, blocked_until timestamptz, reason text,
  updated_at timestamptz default now());
create table if not exists rotator_meta (
  key text primary key, value text, updated_at timestamptz default now());
alter table rotator_accounts enable row level security;
alter table rotator_meta enable row level security;
```

세 머신 각각의 `.env`(또는 셸 환경)에 넣는다. service_role 키는 채팅에 붙이지 말고 각 머신에서 직접.

```
ROTATOR_SUPABASE_URL=https://<ref>.supabase.co
ROTATOR_SUPABASE_KEY=<service_role 키>
```

`python shared_state.py` 로 연결을 확인한다.

### 2. 계정 자격 저장 (머신마다, 계정마다)

각 계정으로 claude 에서 `/login` 한 뒤 저장한다. 세 머신에서 각각 A~D 를 다 저장한다.

```
python save_account.py A     # A 계정 로그인 상태에서 → creds/A.json
```

- Mac 은 **본체 화면 세션**에서(Keychain 잠금 해제 필요, ssh 원격 불가).
- winserver·Windows 본체는 파일이라 어디서든 된다.

`accounts.json` 의 라벨 순서(A→B→C→D)가 곧 소진 순서다.

### 3. 가로채기 설치

- Mac / winserver(자동 봇): 봇 코드를 안 고치고 claude 실행 파일 자리에 래퍼를 앉힌다.
  ```
  bash install_shim.sh                      # Mac
  powershell -File install_shim.ps1         # winserver
  ```
  되돌리기: `--uninstall` / `-Uninstall`. claude 를 업데이트하면 다시 설치한다.
- Windows 본체(대화형): 설치 안 함. 한도 걸리면 `python claude_pool.py --next`.

## 대화형 짧은 명령 (사람이 쓰는 창)

가로채기(shim)는 봇의 단발 호출(-p)만 다루고 사람 창은 일부러 통과시킨다.
그래서 대화형 순환은 아래 래퍼가 맡는다. 두 머신에서 이름이 같다.

| 머신 | 실행 파일 | 등록 위치 |
|---|---|---|
| Windows 본체 | `ccauto.ps1` | PowerShell 프로필 |
| Mac | `ccauto.sh` | `~/.zshrc` (2026-07-31 추가) |

```
cc    클로드 켜기. 한도로 끝나면 다음 계정으로 갈아끼우고 직전 대화를 이어받아 재시작
cn    지금 계정을 건너뛰고 다음 계정으로 넘긴 뒤 켜기
cs    계정별 남은 사용량 보기
```

Mac 은 Orca 원격 터미널을 켜면 이 셸이 뜬다. 맥에서 `cc` 는 원래 C 컴파일러(`/usr/bin/cc`)라
등록 전에는 조용히 엉뚱한 게 돌았다. `~/.zshrc` 는 이 저장소 밖이라 백업이 안 되므로
설치용 세 줄을 `ccauto.sh` 헤더 주석에 적어뒀다.

## 매일 사용

```
python claude_pool.py --status     현재 계정과 각 계정 상태(공유 반영)
python claude_pool.py --next       한도 걸렸을 때 다음 계정으로 (대화형용)
python claude_pool.py --use B      B 계정으로 바로 지정
```

## 동작 요약

봇이 claude 를 부르다 한도 거절을 받으면, 그 계정을 해제 시각까지 막고(공유에 기록),
최저 인덱스의 살아 있는 계정으로 자격을 갈아끼운 뒤 같은 요청을 다시 보낸다.

- 한도 거절 → 계정 막고 다음(순서대로). 세 머신이 같은 막힘을 봐서 같은 계정에 수렴.
- 로그인 거절: **처음부터**면 이 환경 로그인이 안 된 것이라 안 바꾸고 멈춤. **갈아끼운 뒤**면
  그 저장본이 상한 것이라 30분 쉬게 하고 다음 계정으로.
- 원인 모를 실패는 계정 안 바꾸고 한 번만 더 시도.
- 쓸 계정이 없으면 `AllAccountsExhausted` + 텔레그램 알림(소진 구간당 세 머신 통틀어 1건).
- `check=True`, `stdout=...` 같은 인자도 `subprocess.run` 과 똑같이 받는다.
- 공유 저장소가 죽어도 봇을 멈추지 않는다. 각 머신이 로컬 상태로 버틴다.

## 지피티(Codex) 순환

클로드와 규칙은 같다. 거절을 받은 뒤에만 계정을 바꾸고, 남은량은 알림으로만 알린다
(2026-07-28 사용자 결정 — 사용률을 보고 미리 넘기지 않는다).

### 계정마다 codex 폴더가 따로다 (이게 핵심)

**한 폴더 안에서 다른 계정으로 로그인하면 이전 계정 자격이 서버에서 폐기된다**(2026-07-28
실측, 두 번 당함). 그래서 클로드처럼 자격 파일을 백업해 두고 갈아끼우는 방식이 지피티에서는
성립하지 않는다. 폴더를 나누면 서로 다른 설치로 취급돼 두 계정이 공존한다.

```
codex_accounts.json
[ { "label": "A", "codex_home": "~/.codex" },
  { "label": "B", "codex_home": "~/.codex-b" } ]
```

### 셋업

```
python codex_login.py A   # 폴더 준비 + CODEX_HOME 지정까지 알아서 하고 브라우저를 띄운다
python codex_login.py B
python codex_home.py      # 계정별 폴더와 로그인 여부 확인
```

`codex login` 을 직접 치지 말 것. 환경변수를 빠뜨리면 기본 폴더에 로그인되고
그 순간 원래 그 폴더의 계정이 죽는다(셸마다 문법이 달라 실제로 두 번 그랬다).
로그인이 2개 이상이어야 순환이 켜진다.

### 매일 사용

```
codex                           평소대로 쓰면 된다. 계정 전환은 알아서 일어난다
gs                              계정별 사용률 (주간/세션 남은 %, 초기화 시각)
gn                              지금 계정을 한도로 보고 다음 계정으로 (수동)
python codex_pool.py --use B    B 계정으로 바로 지정
python codex_pool.py --home     지금 써야 할 계정의 폴더 (래퍼가 쓴다. 필요하면 여기서 갈아탄다)
python codex_alert.py --preview 알림 문구 미리보기
```

**`codex` 자체가 감싸져 있다**(PowerShell 프로필의 `codex` 함수 → `gcauto.ps1`). 그래서 명령을
따로 외울 필요가 없다. 켤 때마다 이 순서로 돈다.

1. 켜기 전: 지금 계정이 막혔거나 한도면 다음 계정으로 바꿔서 켠다
2. 그 계정의 폴더(`CODEX_HOME`)를 걸고 codex 를 실행
3. 끝난 뒤: 한도에 걸렸으면 다음 계정으로 바꿔 다시 켠다

`login` / `mcp` / `--version` 같은 관리·조회 명령은 순환을 안 태우고 그대로 넘긴다.
`codex logout` 은 막는다 — 그 폴더의 자격을 지우기 때문이다.

`gc` 라는 짧은 명령은 두지 않았다. PowerShell 기본 별칭 `gc`(=`Get-Content`)가 함수보다
먼저 잡혀서 엉뚱하게 동작한다. `codex` 를 그냥 쓰면 된다.

정기 알림: Windows 본체 작업 스케줄러 `ai-rotator-codex-alert` 가 10분마다
`codex_alert.py` 를 돌린다. 주간/세션이 50·70·90% 를 넘을 때 그 구간에 한 번씩 알린다.

### 지피티에서 특히 조심할 것 (실측)

1. **한 폴더에 계정 하나.** 같은 폴더에서 다른 계정으로 로그인하면 이전 계정 자격이
   서버에서 폐기된다 — `Your access token could not be refreshed because you have since
   logged out or signed in to another account`. 자격 파일을 복사해 갈아끼우는 방식도
   같은 이유로 안 된다(복사본은 이미 폐기된 자격이다).
2. **`codex logout` 금지.** 그 폴더의 자격을 지운다. 계정 추가는 `codex_login.py` 로만.
3. **다른 기기끼리는 공존한다.** Mac 로그인은 Windows 에서 계정을 여러 번 갈아끼워도
   살아 있었다. 폐기는 "같은 설치 안에서 갈아치울 때" 일어난다 — 폴더 분리가 통하는 이유다.
4. **한도 판정은 서버에 한 번 더 물어 확증한다.** codex 는 모델 답변을 표준출력에 그대로
   흘려서 문구만 믿으면 답변 내용에 넘어간다. 사용량 조회에 여유가 있으면 계정을 안 막는다.
5. **Mac 에서는 사용량 조회가 403(HTML)** 으로 막힌다(2026-07-28 확인). 조회가 안 되면
   확증 단계는 건너뛰고 문구 판정을 따른다 — 순환 자체는 돈다.

## 파일

### 지피티(Codex) 쪽
- `codex_pool.py`: 실행 래퍼 + 명령줄 도구(`--status/--next/--use/--limited/--home`)
- `codex_home.py`: 계정↔폴더 해석, 폴더 준비, 로그인 여부
- `codex_login.py`: 그 계정 폴더로 로그인(폴더·환경변수 자동)
- `codex_limit.py`: 한도 판정(무시 문구 + 서버 확증)
- `codex_usage.py`: 계정별 사용률 조회(주간/세션은 창 길이로 가름)
- `codex_alert.py`: 사용률 단계 알림(50/70/90%)
- `codex_shim.py` / `install_codex_shim.sh`: `codex exec`·`codex review` 가로채기
- `gcauto.ps1`: 본체 대화형 래퍼(`gc`)
- `test_codex_rotation.py`: 가짜 호출로 15가지 상황 재현(폴더 전환·자격 무손상 포함)

### 클로드 쪽
- `claude_pool.py`: 실행 래퍼 + 명령줄 도구
- `limit_detect.py`: 한도 거절 판정과 해제 시각 읽기
- `pool_state.py`: 순차 소진 pick + 로컬/공유 막힘 병합
- `shared_state.py`: 세 머신이 같이 보는 한도 상태(Supabase REST)
- `claude_shim.py`: claude 실행 파일 자리에 앉는 가로채기(Mac·Windows 겸용)
- `install_shim.sh` / `install_shim.ps1`: 가로채기 설치·해제(Mac / Windows)
- `save_account.py` / `switch_account.py`: 자격 저장·교체(Win 파일 / Mac Keychain)
- `notify.py`: 전 계정 소진 텔레그램 알림
- `test_rotation.py`: 가짜 호출로 20가지 상황 재현(순차·공유·세 머신 수렴 포함)
- `accounts.json` / `creds/`: 계정 매핑 / 자격 백업(둘 다 git 제외)

## 주의

- `creds/`·`state.json`·`.env` 는 자격정보/로컬 상태. git 제외.
- Mac Keychain 교체는 자격을 표준입력으로 넘긴다(명령 인자에 안 실림). 본체/launchd 화면 세션에서만.
- 계정을 바꿔도 이미 떠 있는 claude 세션에는 반영 안 된다. 다음 실행부터.
- `rotate.py`(옛 방식, 크롬으로 사용량 화면 읽기)는 보조로 남겨둠. 지금 순환은 거절 기반이다.
