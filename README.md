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

### 셋업

```
codex login              # 계정 1로 로그인 (codex logout 은 절대 쓰지 말 것)
python codex_save.py A   # → creds/codex/A.json
codex login              # 계정 2로 로그인 (logout 없이 바로 login)
python codex_save.py B   # → creds/codex/B.json
```

`codex_save.py` 는 저장한 자격으로 서버에 한 번 물어 계정 주소와 요금제를 보여준다.
그게 안 뜨면 저장이 안 된 것이다. 저장본이 2개 이상이어야 순환이 켜진다.

### 매일 사용

```
gs                              계정별 사용률 (주간/세션 남은 %, 초기화 시각)
gc                              codex 를 감싸 실행 (한도로 끝나면 자동 전환 후 재시작)
gn                              지금 계정을 한도로 보고 다음 계정으로
python codex_pool.py --use B    B 계정으로 바로 지정
python codex_pool.py --sync     지금 자격을 사본에 반영(대화형으로 쓴 뒤)
python codex_alert.py --preview 알림 문구 미리보기
```

정기 알림: Windows 본체 작업 스케줄러 `ai-rotator-codex-alert` 가 10분마다
`codex_alert.py` 를 돌린다. 주간/세션이 50·70·90% 를 넘을 때 그 구간에 한 번씩 알린다.

### 지피티에서 특히 조심할 것 (실측)

1. **`codex logout` 금지.** 저장된 자격을 지운다. 계정을 바꿔 로그인할 땐 `codex login` 만.
2. **같은 계정으로 다시 로그인하면 그 전에 저장한 사본이 죽는다.** 옛 사본을 꽂으면
   `refresh token was revoked` 로 거절당한다(2026-07-28 실측). 로그인했으면 바로
   `codex_save.py` 로 다시 저장한다.
3. **자격 사본은 저절로 낡는다.** codex 는 돌면서 `auth.json` 을 갱신한다. 그래서 순환기가
   ① 교체 직전 ② 실행이 끝날 때마다 사본을 최신으로 맞춘다(`sync_current_cred`).
   계정 번호가 다르면 되백업하지 않는다(남의 자리를 덮어쓰지 않게).
4. **한도 판정은 서버에 한 번 더 물어 확증한다.** codex 는 모델 답변을 표준출력에 그대로
   흘려서 문구만 믿으면 답변 내용에 넘어간다. 사용량 조회에 여유가 있으면 계정을 안 막는다.
5. **Mac 에서는 사용량 조회가 403(HTML)** 으로 막힌다(2026-07-28 확인). 조회가 안 되면
   확증 단계는 건너뛰고 문구 판정을 따른다 — 순환 자체는 돈다.

## 파일

### 지피티(Codex) 쪽
- `codex_pool.py`: 실행 래퍼 + 명령줄 도구(`--status/--next/--use/--limited/--sync`)
- `codex_limit.py`: 한도 판정(무시 문구 + 서버 확증)
- `codex_usage.py`: 계정별 사용률 조회(주간/세션은 창 길이로 가름)
- `codex_switch.py` / `codex_save.py`: 자격 되백업·교체 / 저장
- `codex_alert.py`: 사용률 단계 알림(50/70/90%)
- `codex_shim.py` / `install_codex_shim.sh`: `codex exec`·`codex review` 가로채기
- `gcauto.ps1`: 본체 대화형 래퍼(`gc`)
- `test_codex_rotation.py`: 가짜 호출로 15가지 상황 재현

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
