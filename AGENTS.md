# AGENTS.md — AI 에이전트용 셋업·운영 지침

이 문서는 **AI 코딩 에이전트(Claude Code 등)**가 이 저장소를 사용자의 맥에 셋업하고 운영할 때 따르는 지침입니다. 사람용 개요는 [README.md](README.md)를 보세요.

## 목표
사용자 카카오톡 오픈채팅방을 읽어 노션에 일자별로 아카이브하는 파이프라인을 셋업·운영한다. 카톡 로컬 DB를 본인 기기에서 복호화해 읽는다.

## 사람(사용자)이 직접 해야 하는 것 — 에이전트가 대신 못 함
1. **전체 디스크 접근(FDA)**: 시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근에 터미널 추가(GUI 작업). 에이전트는 안내만 한다.
2. **대상 방 열기**: 카톡은 "방을 열 때만" 서버 대화를 로컬 DB에 동기화한다. 수집할 방을 한 번씩 열어야 최근 대화가 로컬에 들어온다.
3. **노션 준비**: integration 토큰 발급, DB 생성, `.env`에 기록.
4. 복호화·DB 접근 명령이 에이전트 실행 환경에서 자동 차단되면, 사용자가 직접 셸에서 실행하게 요청한다(우회하지 말 것).

## 셋업 절차 (에이전트 수행)
### 1. 사전 점검
- `swift --version`, `brew`, `python3`(3.10+) 존재 확인. 없으면 설치 안내. `brew install sqlcipher`.

### 2. kakaocli 빌드(+patch)
```
git clone https://github.com/silver-flight-group/kakaocli.git kakaocli-rebuild/kakaocli
cd kakaocli-rebuild/kakaocli
git apply ../../patches/kakaocli-userid-bruteforce.patch
swift build -c release
```
patch는 user_id 브루트포스를 전 코어 병렬 + 범위/타임아웃 환경변수화(`KAKAOCLI_BRUTE_MAX`, `KAKAOCLI_BRUTE_TIMEOUT`)로 바꾼다. patch가 안 맞으면(kakaocli 최신 변경) `Sources/KakaoCore/Database/DeviceInfo.swift`의 `recoverUserIdFromSHA512`를 같은 취지로 직접 수정한다. 상세는 [docs/kakaocli-rebuild.md](docs/kakaocli-rebuild.md).

### 3. 복호화 검증 (사용자가 실행)
대상 방을 카톡에서 연 뒤:
```
KAKAOCLI_BRUTE_TIMEOUT=1800 .build/release/kakaocli auth --verbose
```
- "User ID: N" + "Database opened successfully" → 성공.
- "No candidate user id..." → user_id가 범위 밖. `KAKAOCLI_BRUTE_MAX`를 키워 재시도. 그래도 실패하면 이 방식 불가(README 한계 참조) — 사용자에게 보고하고 중단한다.

### 4. 설정
- `cp config/targets.txt.example config/targets.txt` 후 방 이름(그 방에만 있는 순수 한글 조각) 채우기. 방 이름을 모르면 `.build/release/kakaocli query "SELECT linkId, linkName FROM NTOpenLink"`로 찾는다(chats의 display_name은 "(unknown)"일 수 있음).
- `cp config/rooms.json.example config/rooms.json` — 방별 마스킹(mask true/false). 미등록 방은 기본 mask=true.
- `.env`에 NOTION_TOKEN, NOTION_DB_ID, NOTION_DATA_SOURCE_ID 등.

### 5. 첫 수집·기록
```
python scripts/collect.py --limit 1000     # 기본 DB 방식. 첫 수집은 그날치만(--on-date 자동)
python scripts/notion_push.py
```
검증: `daily/<오늘>/` 파일의 시각이 오늘 범위인지, 노션에 페이지가 생성됐는지 확인.

## 일일 운영 (에이전트)
- `python scripts/collect.py` → `python scripts/notion_push.py`.
- 놓친 날: `python scripts/collect.py --backfill --date YYYY-MM-DD` (그날치만 정확 수집).
- dedup은 자동(msgId 커서). `scripts/collect.py --self-test`로 로직 회귀 확인 가능.

## 안전 규칙 (반드시 지킬 것)
- **본인 기기의 본인 대화만** 읽는다. 남의 데이터·기기에 쓰지 않는다.
- 대화 원본(`daily/`)·비밀(`.env`)·상태(`state/`)를 **외부로 유출하거나 공개 저장소에 커밋하지 않는다**(`.gitignore`로 제외됨 — 커밋 전 `git status` 확인).
- `kakaocli auth --verbose`가 출력하는 **복호화 키를 로그·화면공유·스크린샷에 남기지 않는다**.
- 노션 등 외부로 보낼 때 마스킹 정책(rooms.json)을 지킨다.
- 복호화·DB 접근 명령이 차단되면 우회하지 말고 사용자에게 직접 실행을 요청한다.
