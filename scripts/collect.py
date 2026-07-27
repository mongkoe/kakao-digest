#!/usr/bin/env python3
"""
Kakao-Digest 수집기 (1단계)

config/targets.txt 의 방들을 vendor 포크 kakao_read.py(손쉬운 사용 화면읽기)로 읽어
daily/YYYY-MM-DD/ 에 방별 원문 JSON과 링크 목록(links.jsonl)으로 저장한다.
직전 실행과의 겹침(앵커)을 찾아 신규 메시지만 병합한다(상태: state/).

전제: 카카오톡 앱 실행 중 + 대상 방이 별도 창으로 열려 있음 + 손쉬운 사용 권한.
실행 예:
    .venv/bin/python scripts/collect.py
    .venv/bin/python scripts/collect.py --limit 300 --date 2026-07-20
    .venv/bin/python scripts/collect.py --self-test
종료 코드: 0=전체 성공 / 1=일부 실패 / 2=치명(설정·환경 문제)
"""
import argparse
import datetime
import glob
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS_FILE = ROOT / "config" / "targets.txt"
DAILY_DIR = ROOT / "daily"
STATE_DIR = ROOT / "state"
VENDOR_READER = ROOT / "scripts" / "vendor" / "kakao_read.py"

URL_RE = re.compile(r"https?://[^\s\)\]\}<>\"']+")
# URL 뒤에 붙은 한글·구두점 제거용 (카톡 본문에서 "...com입니다." 같은 형태)
URL_TRAIL_RE = re.compile(r"[가-힣.,;:!?…'\")\]}]+$")
CLOCK_RE = re.compile(r"(오전|오후)\s*(\d{1,2}:\d{2})")

TAIL_SIZE = 50          # 상태파일에 남길 직전 버퍼 꼬리 개수
ANCHOR_KS = (20, 10, 5, 3)  # 앵커 길이 후보 (긴 것부터)


def log(msg: str):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def db_mode() -> bool:
    """기본은 DB 복호화(kakaocli) 방식. KAKAO_READER=screen 이면 화면읽기로 폴백."""
    return os.environ.get("KAKAO_READER", "kakaocli") != "screen"


def find_kakao_read() -> str:
    """vendor 포크 최우선, 없으면 플러그인 캐시 폴백(최신 메시지 잘림 위험).

    기본은 DB 복호화 어댑터(kakaocli_read.py) — 재빌드한 kakaocli로 로컬 DB를
    결정적으로 읽음(창 불필요, 버퍼 한계 없음). KAKAO_READER=screen 이면
    기존 화면읽기(kakao_read.py)로 폴백한다.
    """
    if db_mode():
        adapter = VENDOR_READER.with_name("kakaocli_read.py")
        if adapter.exists():
            return str(adapter)
        log("경고: DB 어댑터(kakaocli_read.py) 없음 — 화면읽기로 폴백")
    if VENDOR_READER.exists():
        return str(VENDOR_READER)
    log("경고: vendor 포크 없음 — 플러그인 원본 사용 (최신 메시지 잘림 위험)")
    pattern = str(pathlib.Path.home() /
                  ".claude/plugins/cache/team-attention-plugins/kakaotalk/*/scripts/kakao_read.py")
    hits = sorted(glob.glob(pattern))
    if not hits:
        log("Error: kakao_read.py를 찾을 수 없습니다. scripts/vendor/ 또는 플러그인 설치 확인.")
        sys.exit(2)
    return hits[-1]  # 최신 버전


def load_targets() -> list[str]:
    if not TARGETS_FILE.exists():
        log(f"Error: {TARGETS_FILE} 없음")
        sys.exit(2)
    out = []
    for line in TARGETS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def safe_name(name: str) -> str:
    """방 이름을 파일명으로 안전하게."""
    s = re.sub(r"[^\w가-힣]+", "_", name).strip("_")
    return s[:60] or "room"


def clock(time_str: str) -> str | None:
    """time 문자열에서 '오전/오후 H:MM'만 추출(공백 1칸으로 정규화).
    날짜 접두는 신뢰하지 않는다(플러그인의 날짜 구분선 파싱 버그)."""
    if not time_str:
        return None
    m = CLOCK_RE.search(time_str)
    if not m:
        return None
    return f"{m.group(1)} {m.group(2)}"


def normalize(msg: dict) -> tuple:
    """메시지 → (sender, clock, message) 정규화 튜플."""
    return (msg.get("sender") or "", clock(msg.get("time") or ""), msg.get("message") or "")


def _search_from_back(seq: list, anchor: list) -> int | None:
    """seq에서 anchor(연속 부분수열)를 뒤에서부터 찾아 '앵커 끝 인덱스' 반환."""
    k = len(anchor)
    if k == 0:
        return None
    for start in range(len(seq) - k, -1, -1):
        if seq[start:start + k] == anchor:
            return start + k - 1
    return None


def find_anchor_end(tail: list, today: list) -> int | None:
    """직전 실행 tail과 오늘 버퍼의 겹침 앵커를 찾는다.
    반환: 오늘 버퍼에서 '이미 수집된 마지막 메시지' 인덱스. 실패 시 None."""
    ks = [k for k in ANCHOR_KS if k <= len(tail)]
    if not ks and tail:
        ks = [len(tail)]
    # 1차: (sender, clock, message) 그대로
    for k in ks:
        end = _search_from_back(today, tail[-k:])
        if end is not None:
            return end
    # 2차: sender 제외 (clock, message) — 발신자 오귀속 완화
    today_cm = [(c, m) for (_, c, m) in today]
    for k in ks:
        anchor_cm = [(c, m) for (_, c, m) in tail[-k:]]
        end = _search_from_back(today_cm, anchor_cm)
        if end is not None:
            return end
    return None


def load_state(name: str) -> dict | None:
    path = STATE_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(name: str, date_str: str, tail: list, last_msg_id: int | None = None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last = {
        "date": date_str,
        "collected_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "tail": [list(t) for t in tail],
    }
    if last_msg_id is not None:
        last["last_msg_id"] = int(last_msg_id)
    (STATE_DIR / f"{name}.json").write_text(
        json.dumps({"last_run": last}, ensure_ascii=False, indent=2), encoding="utf-8")


def read_room(kakao_read: str, target: str, limit: int,
              since_id: int | None = None, on_date: str | None = None) -> dict:
    """리더를 호출해 방 하나를 읽는다.
    since_id/on_date는 DB 방식(kakaocli_read.py)에서만 전달 — 화면읽기 리더는 미지원."""
    cmd = [sys.executable, kakao_read, target, "--limit", str(limit), "--json"]
    if since_id is not None:
        cmd += ["--since-id", str(since_id)]
    if on_date is not None:
        cmd += ["--on-date", str(on_date)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return {"error": "읽기 240초 초과(timeout)", "chat": None, "messages": []}
    if r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip()[:200] or "read failed"
        return {"error": err, "chat": None, "messages": []}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": f"JSON 파싱 실패: {r.stdout[:200]}", "chat": None, "messages": []}


def extract_links(room_name: str, messages: list[dict]) -> list[dict]:
    """메시지에서 URL을 뽑아 맥락과 함께 반환."""
    links = []
    for m in messages:
        text = m.get("message", "") or ""
        for url in URL_RE.findall(text):
            url = URL_TRAIL_RE.sub("", url)  # 후행 한글·구두점 제거
            if not url:
                continue
            domain = re.sub(r"^https?://", "", url).split("/")[0]
            links.append({
                "room": room_name,
                "sender": m.get("sender"),
                "time": m.get("time"),
                "url": url,
                "domain": domain,
                # 링크 앞뒤 맥락(첫 줄) — 분류 힌트용
                "context": text.strip().splitlines()[0][:120] if text.strip() else "",
            })
    return links


def write_room_json(path: pathlib.Path, payload: dict):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def process_room(kakao_read: str, target: str, limit: int,
                 out_dir: pathlib.Path, date_str: str,
                 backfill: bool = False) -> tuple[dict, list[dict]]:
    """방 하나 수집: 읽기 → 중복제거 → 당일 파일 merge → 상태 저장.

    DB 모드(기본): logId(msg_id) 기반 정확 dedup — state의 last_msg_id 이후만
    어댑터에 요청하므로 중복·날짜혼재가 없다. backfill=True면 커서를 무시하고
    date_str 하루치만 수집(놓친 날 채우기).
    화면읽기 모드: 기존 앵커 겹침 dedup.
    """
    name = safe_name(target)
    dbm = db_mode()
    state = load_state(name)
    first_run = state is None
    now_str = datetime.datetime.now().isoformat(timespec="seconds")
    # 파일명은 target 기준(안정 식별자) — 창 제목은 실패 시 None이라 이름이 갈라질 수 있음
    room_path = out_dir / f"{name}.json"
    warnings = []

    def existing_messages() -> list:
        if room_path.exists():
            try:
                return json.loads(room_path.read_text(encoding="utf-8")).get("messages") or []
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def error_entry(msg: str) -> tuple[dict, list]:
        if room_path.exists():
            log(f"  → 읽기 실패({msg}) — 기존 당일 파일 보존")
        else:
            write_room_json(room_path, {
                "target": target, "chat": target, "collected_at": now_str,
                "message_count": 0, "first_run": False, "new_count": 0,
                "error": msg, "messages": []})
        return ({"target": target, "chat": target, "messages": 0, "new_messages": 0,
                 "links": 0, "error": msg, "warnings": warnings}, [])

    # DB 모드: 직전 last_msg_id 이후만 요청(정확·완전). 화면읽기는 since_id 없음.
    # last_msg_id가 실제로 있을 때만 커서 사용 — 없으면(첫 DB 수집/화면읽기 state)
    # since_id=None으로 둔다(0으로 두면 가장 오래된 것부터 오는 버그).
    since_id = None
    on_date = None
    if dbm and backfill:
        # 백필: 커서 무시하고 지정 날짜(date_str) 하루치만 수집
        on_date = date_str
    elif dbm and not first_run:
        lid = state.get("last_run", {}).get("last_msg_id")
        if lid is not None:
            since_id = int(lid)
        if since_id is None:  # 화면읽기 state 등 커서 없음 → 그날치
            on_date = date_str
    elif dbm:
        # 첫 수집: 저장 날짜만 수집(여러 날 혼재 방지)
        on_date = date_str

    data = read_room(kakao_read, target, limit, since_id, on_date)
    chat = data.get("chat") or target
    msgs = data.get("messages", [])
    err = data.get("error")
    log(f"  창 제목: {chat}")

    if err:
        return error_entry(err)

    # 신규 0건: DB 모드(커서 조회)에서는 정상, 화면읽기에서는 이상(가드)
    if not msgs:
        if dbm and not first_run:
            existing = existing_messages()
            log("  → 신규 0개 (새 메시지 없음)")
            return ({"target": target, "chat": chat, "messages": len(existing),
                     "new_messages": 0, "links": 0, "error": None, "warnings": warnings}, [])
        return error_entry("raw 0건")

    # 신규분 분리
    today_tuples = None
    new_max_id = 0
    if dbm:
        # 어댑터가 since_id로 이미 신규만 반환(first_run이면 최근 limit개)
        new_msgs = msgs
        new_max_id = max((int(m.get("msg_id", 0)) for m in msgs), default=0)
    else:
        today_tuples = [normalize(m) for m in msgs]
        # 진단: 시계 파싱 실패 비율
        if len(msgs) > 10:
            none_ratio = sum(1 for t in today_tuples if t[1] is None) / len(today_tuples)
            if none_ratio > 0.3:
                warnings.append("시간 파싱 이상(24시간제 설정?)")
                log(f"  경고: clock=None 비율 {none_ratio:.0%} — 시간 파싱 이상(24시간제 설정?)")
        if first_run:
            new_msgs = msgs
        else:
            tail = [tuple(t) for t in state.get("last_run", {}).get("tail", [])]
            end = find_anchor_end(tail, today_tuples)
            if end is None:
                new_msgs = msgs
                warnings.append("앵커 매칭 실패 — 중복 유입 가능")
                log("  경고: 앵커 매칭 실패 — 중복 유입 가능")
            else:
                new_msgs = msgs[end + 1:]

    # 당일 파일 merge (기존 파일에 messages가 비어 있으면 신규 생성과 동일 취급)
    merged = existing_messages() + new_msgs
    write_room_json(room_path, {
        "target": target, "chat": chat, "collected_at": now_str,
        "message_count": len(merged), "first_run": first_run,
        "new_count": len(new_msgs), "error": None, "messages": merged})

    # 상태 저장
    if dbm:
        prev_id = 0 if first_run else int(state.get("last_run", {}).get("last_msg_id", 0) or 0)
        save_state(name, date_str, [], max(prev_id, new_max_id))
    else:
        save_state(name, date_str, today_tuples[-TAIL_SIZE:])

    links = extract_links(chat, new_msgs)
    log(f"  → 신규 {len(new_msgs)}개 (파일 누계 {len(merged)}개), 링크 {len(links)}개"
        + (" [첫 실행]" if first_run else ""))
    return ({"target": target, "chat": chat, "messages": len(merged),
             "new_messages": len(new_msgs), "links": len(links),
             "error": None, "warnings": warnings}, links)


def run_self_test():
    """앵커 매칭 단위 테스트 (모의 데이터, 카톡 조작 없음)."""
    # 케이스 1: 정상 겹침 — 직전 tail 뒷부분이 오늘 버퍼 앞에 겹침
    tail = [("철수", f"오전 9:{i:02d}", f"m{i}") for i in range(10)]
    today = tail[4:] + [("영희", "오전 10:00", "새1"), ("영희", "오전 10:01", "새2")]
    end = find_anchor_end(tail, today)
    assert end == 5, f"케이스1 실패: end={end}"
    assert today[end + 1:] == [("영희", "오전 10:00", "새1"), ("영희", "오전 10:01", "새2")]
    log("self-test 1/4 통과: 정상 겹침")

    # 케이스 2: 겹침 없음 → None (전체 신규 취급)
    tail2 = [("A", f"오전 9:0{i}", f"x{i}") for i in range(5)]
    today2 = [("B", f"오후 1:0{i}", f"y{i}") for i in range(5)]
    assert find_anchor_end(tail2, today2) is None, "케이스2 실패"
    log("self-test 2/4 통과: 겹침 없음")

    # 케이스 3: sender 오귀속 → (clock, message) 폴백으로 매칭
    tail3 = [("철수", f"오전 9:0{i}", f"m{i}") for i in range(5)]
    today3 = ([("방제목", f"오전 9:0{i}", f"m{i}") for i in range(5)]
              + [("영희", "오전 10:00", "새")])
    end3 = find_anchor_end(tail3, today3)
    assert end3 == 4, f"케이스3 실패: end={end3}"
    log("self-test 3/4 통과: sender 오귀속 폴백")

    # 케이스 4: 같은 문구·같은 시각 재게시가 드롭되지 않음
    tail4 = [("철수", f"오전 9:0{i}", f"m{i}") for i in range(5)]
    repost = ("철수", "오전 9:04", "m4")  # tail4 마지막과 동일
    today4 = list(tail4) + [("영희", "오전 9:05", "중간"), repost]
    end4 = find_anchor_end(tail4, today4)
    assert end4 == 4, f"케이스4 실패: end={end4}"
    new4 = today4[end4 + 1:]
    assert len(new4) == 2 and repost in new4, f"케이스4 실패: new={new4}"
    log("self-test 4/4 통과: 재게시 미드롭")

    log("self-test 전체 통과 (4/4)")


def main():
    ap = argparse.ArgumentParser(description="Kakao-Digest 수집기")
    ap.add_argument("--limit", type=int, default=500, help="방당 최대 메시지 수 (기본 500)")
    ap.add_argument("--date", type=str, default=None, help="저장 날짜 override (YYYY-MM-DD)")
    ap.add_argument("--self-test", action="store_true", dest="self_test",
                    help="앵커 매칭 단위 테스트만 실행")
    ap.add_argument("--backfill", action="store_true",
                    help="DB 모드: 커서 무시하고 --date 하루치만 수집(놓친 날 채우기)")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return

    date_str = args.date or datetime.date.today().isoformat()
    out_dir = DAILY_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    kakao_read = find_kakao_read()
    targets = load_targets()
    if not targets:
        log("대상 방이 없습니다. config/targets.txt에 방 이름 일부를 넣으세요.")
        sys.exit(2)

    all_links = []
    summary = []
    for target in targets:
        log(f"[수집] '{target}' 읽는 중...")
        entry, links = process_room(kakao_read, target, args.limit, out_dir, date_str,
                                    backfill=args.backfill)
        summary.append(entry)
        all_links.extend(links)

    # 링크 통합 저장 — append 모드 (당일 재실행 시 덮어쓰지 않고 병합)
    if all_links:
        with (out_dir / "links.jsonl").open("a", encoding="utf-8") as f:
            for lk in all_links:
                f.write(json.dumps(lk, ensure_ascii=False) + "\n")

    (out_dir / "_summary.json").write_text(json.dumps(
        {"date": date_str,
         "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
         "rooms": summary,
         "total_messages": sum(s["messages"] for s in summary),
         "total_new_messages": sum(s["new_messages"] for s in summary),
         "total_links": len(all_links)},
        ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [s for s in summary if s["error"]]
    log(f"완료: {out_dir}")
    log(f"  방 {len(summary)}개 (실패 {len(failed)}개), "
        f"신규 메시지 {sum(s['new_messages'] for s in summary)}개, 링크 {len(all_links)}개")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
