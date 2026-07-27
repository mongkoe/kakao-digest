#!/usr/bin/env python3
"""kakaocli(로컬 카톡 DB 복호화) 기반 리더 어댑터.

collect.py가 subprocess로 호출하며, 아래 JSON을 stdout으로 낸다:
  {"chat": <방이름>, "messages": [{"sender","time","message","is_me","msg_id"}]}
  실패 시: {"error": <메시지>, "chat": null, "messages": []}

로컬 DB에서 결정적으로 읽으므로 창을 띄워둘 필요가 없고, 버퍼 한계 없이 방에
동기화된 전 구간을 조회한다.

kakaocli 바이너리 탐색 우선순위:
  1) 환경변수 KAKAOCLI_BIN (절대경로 직접 지정)
  2) 이 저장소의 kakaocli-rebuild/**/release/kakaocli (직접 빌드한 경우)
  3) PATH의 kakaocli (brew 설치본; 최신 카톡에선 user_id를 못 찾을 수 있음)

전제: kakaocli로 auth(복호화)가 성공하는 상태(README 참조).
"""
import os
import json
import re
import argparse
import subprocess
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
# user_id 브루트포스 범위·타임아웃(재빌드본에서 인식). 계정 user_id가 크면 상향.
BRUTE_ENV = {"KAKAOCLI_BRUTE_MAX": "20000000000", "KAKAOCLI_BRUTE_TIMEOUT": "300"}
# 이 파일 기준 저장소 루트(scripts/vendor/ -> repo root)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MY_ID_CACHE = None


def find_binary():
    """kakaocli 바이너리 경로. 환경변수 > 저장소 내 재빌드본 > PATH 순."""
    env_bin = os.environ.get("KAKAOCLI_BIN")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    base = os.path.join(REPO_ROOT, "kakaocli-rebuild")
    if os.path.isdir(base):
        # 재빌드본은 .build/<arch>/release/kakaocli — .build가 숨김폴더라 os.walk로 탐색
        for root, _dirs, files in os.walk(base):
            if os.path.basename(root) == "release" and "kakaocli" in files and ".dSYM" not in root:
                cand = os.path.join(root, "kakaocli")
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return "kakaocli"  # PATH 폴백


def _env():
    e = dict(os.environ)
    e.update(BRUTE_ENV)
    return e


def my_user_id(binary):
    """로그인 user_id를 kakaocli auth 출력에서 얻는다(is_me 판정용). 실패해도 무해(0)."""
    global _MY_ID_CACHE
    if _MY_ID_CACHE is not None:
        return _MY_ID_CACHE
    try:
        r = subprocess.run([binary, "auth"], capture_output=True, text=True, timeout=600, env=_env())
        m = re.search(r"User ID:\s*(\d+)", r.stdout or "")
        _MY_ID_CACHE = int(m.group(1)) if m else 0
    except Exception:
        _MY_ID_CACHE = 0
    return _MY_ID_CACHE


def run_query(binary, sql):
    """kakaocli query 실행 -> JSON(array of arrays) 반환."""
    r = subprocess.run([binary, "query", sql], capture_output=True, text=True, timeout=300, env=_env())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "query failed").strip()[:300])
    out = r.stdout.strip()
    return json.loads(out) if out else []


def _validate_target(name):
    """방 이름 입력 검증 — 신뢰 입력이지만 방어적으로 제어문자·과길이를 거부."""
    if not name or not name.strip():
        raise ValueError("빈 방 이름")
    if len(name) > 100:
        raise ValueError("방 이름이 너무 깁니다(100자 초과)")
    if any(ord(c) < 0x20 for c in name):
        raise ValueError("방 이름에 제어문자를 넣을 수 없습니다")


def _like_escape(s):
    """SQL 문자열 리터럴 + LIKE 와일드카드 이스케이프. LIKE에 ESCAPE '\\' 와 함께 쓴다.
    순서 중요: 역슬래시 먼저 → 와일드카드(%, _) → 작은따옴표."""
    s = s.replace("\\", "\\\\")
    s = s.replace("%", "\\%").replace("_", "\\_")
    s = s.replace("'", "''")
    return s


def resolve_chat_id(binary, name):
    """방 이름 일부 -> chatId. 오픈채팅(NTOpenLink.linkName)에서 부분일치."""
    _validate_target(name)
    esc = _like_escape(name)
    rows = run_query(binary, (
        "SELECT r.chatId, o.linkName FROM NTChatRoom r "
        "JOIN NTOpenLink o ON o.linkId = r.linkId "
        f"WHERE o.linkName LIKE '%{esc}%' ESCAPE '\\' ORDER BY r.chatId"
    ))
    if not rows:
        return None, None
    return int(rows[0][0]), str(rows[0][1])


def fmt_time(epoch_sec):
    """epoch(초) -> '오전/오후 H:MM' (KST)."""
    dt = datetime.fromtimestamp(int(epoch_sec), KST)
    h = dt.hour
    ampm = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    return f"{ampm} {h12}:{dt.minute:02d}"


def _day_bounds(date_str):
    """'YYYY-MM-DD'(KST) → (start_epoch, end_epoch) 초, 반열림 [start, end)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def read_messages(binary, chat_id, limit, since_id=None, on_date=None, my_id=0):
    """메시지를 과거->최신(ASC) 순으로 반환. 빈 본문 제외.

    우선순위: since_id(커서 이후 신규, 매일 운영) > on_date(특정일만, 첫 수집
    날짜혼재 방지) > 최근 limit개(백필). logId는 방 내 단조증가 로그 순번.
    """
    inner = (
        "COALESCE(NULLIF(u.nickName,''), NULLIF(u.friendNickName,''), NULLIF(u.displayName,''), '알수없음') AS sender, "
        "m.message AS message, m.sentAt AS sentAt, m.authorId AS authorId, m.logId AS logId "
        "FROM NTChatMessage m LEFT JOIN NTUser u ON u.userId = m.authorId "
        f"WHERE m.chatId = {int(chat_id)} AND m.message IS NOT NULL AND m.message != '' "
    )
    if since_id is not None:
        sql = (
            "SELECT sender, message, sentAt, authorId, logId FROM ("
            f"  SELECT {inner} AND m.logId > {int(since_id)} "
            f"  ORDER BY m.logId ASC LIMIT {int(limit)}"
            ") ORDER BY logId ASC"
        )
    elif on_date is not None:
        start, end = _day_bounds(on_date)
        sql = (
            "SELECT sender, message, sentAt, authorId, logId FROM ("
            f"  SELECT {inner} AND m.sentAt >= {start} AND m.sentAt < {end} "
            f"  ORDER BY m.sentAt ASC LIMIT {int(limit)}"
            ") ORDER BY sentAt ASC"
        )
    else:
        sql = (
            "SELECT sender, message, sentAt, authorId, logId FROM ("
            f"  SELECT {inner} "
            f"  ORDER BY m.sentAt DESC LIMIT {int(limit)}"
            ") ORDER BY sentAt ASC"
        )
    rows = run_query(binary, sql)
    messages = []
    for row in rows:
        sender, message, sent_at, author_id, log_id = row[0], row[1], row[2], row[3], row[4]
        is_me = bool(my_id) and int(author_id) == my_id
        messages.append({
            "sender": "나" if is_me else str(sender),
            "time": fmt_time(sent_at),
            "message": str(message),
            "is_me": is_me,
            "msg_id": int(log_id),
        })
    return messages


def read_chat(name, limit, since_id=None, on_date=None):
    binary = find_binary()
    chat_id, link_name = resolve_chat_id(binary, name)
    if chat_id is None:
        return {"error": f"방 '{name}' 을(를) NTOpenLink.linkName에서 못 찾음", "chat": None, "messages": []}
    my_id = my_user_id(binary)
    messages = read_messages(binary, chat_id, limit, since_id, on_date, my_id)
    return {"chat": link_name or name, "messages": messages}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="방 이름 일부(부분일치)")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--since-id", type=int, default=None, dest="since_id",
                    help="이 logId 초과분만(정확 dedup). 미지정 시 on-date 또는 최근 limit개")
    ap.add_argument("--on-date", type=str, default=None, dest="on_date",
                    help="이 날짜(KST YYYY-MM-DD)만. since-id 없을 때 적용(첫 수집 날짜혼재 방지)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        result = read_chat(args.target, args.limit, args.since_id, args.on_date)
    except Exception as e:  # noqa: BLE001 - CLI 경계에서 에러를 JSON으로 반환
        result = {"error": str(e)[:300], "chat": None, "messages": []}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
