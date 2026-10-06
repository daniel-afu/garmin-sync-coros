"""
高驰 → 佳明 同步（按开始时间去重版）

背景：
    这个脚本原来是「靠仓库里的 db/coros.db 记录哪些已同步」来去重的，
    但 db/ 在 .gitignore 里，GitHub runner 每次都是干净环境 ——
    db 根本不会被带过去，去重等于失效：
    每次运行都会把高驰的全部活动重新下载一遍、再全量尝试上传给佳明（靠佳明
    返回 DUPLICATE_ACTIVITY 兜底），一次跑 20 多分钟，纯属白干。

现在的做法：
    - 不再依赖任何 db：先拉「佳明已有活动的开始时间」当作已同步集合，
      高驰有、佳明没有的才下载 + 上传。两边都用 UTC 秒比对，容差 ±120 秒。
    - 加时间窗口 SYNC_DAYS（默认 30 天），别把陈年旧账一次性灌进佳明。
    - 加 MAX_UPLOAD 安全阀（默认 5 条）。
    - 有失败时以非 0 退出码结束，避免 Actions 假绿勾。

环境变量：GARMIN_* / COROS_*（同正向同步），另加
    SYNC_DAYS    只同步最近 N 天（0 = 不限，默认 30）
    MAX_UPLOAD   单次最多上传多少条（默认 5）
    DRY_RUN      设为 1 时只比对、不上传
"""

import io
import os
import sys
import time
import zipfile
import datetime as dt
import bisect

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))  # scripts/coros/
config_path = os.path.dirname(CURRENT_DIR)                # scripts/
sys.path.append(config_path)
sys.path.append(CURRENT_DIR)

from coros_client import CorosClient
from config import COROS_FIT_DIR
from garmin.garmin_client import GarminClient


SYNC_CONFIG = {
    'GARMIN_AUTH_DOMAIN': '',
    'GARMIN_EMAIL': '',
    'GARMIN_PASSWORD': '',
    'GARMIN_NEWEST_NUM': 0,
    "COROS_EMAIL": '',
    "COROS_PASSWORD": '',
}

## 只同步最近 N 天内的活动；0 表示不限
SYNC_DAYS = int(os.getenv("SYNC_DAYS") or 30)
## 单次最多上传多少条（安全阀）
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD") or 5)
## 只比对不上传
DRY_RUN = str(os.getenv("DRY_RUN") or "").lower() in ("1", "true", "yes")

## 允许的时间误差（秒）
MATCH_TOLERANCE = 120


def load_config():
    """优先读 GitHub Actions 的环境变量（来自 Secrets）。"""
    for k in SYNC_CONFIG:
        if os.getenv(k):
            SYNC_CONFIG[k] = os.getenv(k)
    if not SYNC_CONFIG["GARMIN_AUTH_DOMAIN"]:
        SYNC_CONFIG["GARMIN_AUTH_DOMAIN"] = "CN"
        print("[配置] GARMIN_AUTH_DOMAIN 未设置，按国区 CN 处理")


def garmin_epoch(activity):
    """取佳明活动的开始时刻（UTC 秒）。"""
    begin_ts = activity.get("beginTimestamp")
    if begin_ts:
        return int(begin_ts) // 1000
    start_gmt = activity.get("startTimeGMT")
    if start_gmt:
        return int(dt.datetime.strptime(start_gmt, "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=dt.timezone.utc).timestamp())
    return None


def already_on_garmin(ts, garmin_times):
    idx = bisect.bisect_left(garmin_times, ts - MATCH_TOLERANCE)
    return idx < len(garmin_times) and abs(garmin_times[idx] - ts) <= MATCH_TOLERANCE


def extract_fit(raw):
    """高驰下载回来的可能是 .zip（内含 .fit），也可能是裸 .fit。"""
    buf = io.BytesIO(raw)
    if raw[:2] == b"PK" or zipfile.is_zipfile(buf):
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fits:
                raise ValueError("zip 内没有 .fit 文件")
            return zf.read(fits[0]), os.path.basename(fits[0])
    return raw, "coros.fit"


def describe(activity):
    name = activity.get("name") or activity.get("sportName") or ""
    distance = activity.get("distance") or 0
    try:
        distance = float(distance)
    except Exception:
        distance = 0.0
    ## 高驰的 distance 单位是米，超过 1000 就换算成公里
    if distance > 1000:
        distance = distance / 1000.0
    return "%s %.2f km" % (name, distance)


def main():
    load_config()

    coros_client = CorosClient(SYNC_CONFIG["COROS_EMAIL"], SYNC_CONFIG["COROS_PASSWORD"])
    coros_client.login()
    print("[高驰] 登录成功 userId=%s regionId=%s" % (coros_client.userId, coros_client.regionId))

    garmin_client = GarminClient(
        SYNC_CONFIG["GARMIN_EMAIL"],
        SYNC_CONFIG["GARMIN_PASSWORD"],
        SYNC_CONFIG["GARMIN_AUTH_DOMAIN"],
        SYNC_CONFIG["GARMIN_NEWEST_NUM"] or 0,
    )

    garmin_activities = garmin_client.getAllActivities() or []
    garmin_times = sorted(
        t for t in (garmin_epoch(a) for a in garmin_activities) if t is not None
    )
    print("[佳明] 已有活动 %d 条（用于去重）" % len(garmin_times))

    coros_activities = coros_client.getAllActivities() or []
    print("[高驰] 取到活动 %d 条" % len(coros_activities))
    if not coros_activities:
        print("[结果] 高驰侧没有取到活动，结束")
        sys.exit(0)

    deadline = int(time.time()) - SYNC_DAYS * 86400 if SYNC_DAYS > 0 else None
    if deadline:
        print("[范围] 只处理最近 %d 天内的活动（SYNC_DAYS=%d，改成 0 表示不限）" % (SYNC_DAYS, SYNC_DAYS))
    else:
        print("[范围] 不限时间，全部历史活动都参与比对")

    todo = []
    for activity in coros_activities:
        label_id = activity.get("labelId")
        sport_type = activity.get("sportType")
        ts = activity.get("startTime")
        if not label_id or ts is None:
            continue
        ts = int(ts)
        if deadline and ts < deadline:
            continue
        if already_on_garmin(ts, garmin_times):
            continue
        todo.append((ts, label_id, sport_type, activity))

    todo.sort(key=lambda item: item[0])
    print("[对比] 高驰有、佳明没有：%d 条" % len(todo))
    beijing = dt.timezone(dt.timedelta(hours=8))
    for ts, label_id, sport_type, activity in todo:
        print("   - %s  %s  %s" % (
            label_id,
            dt.datetime.fromtimestamp(ts, beijing).strftime("%Y-%m-%d %H:%M:%S"),
            describe(activity)))

    if not todo:
        print("[结果] 没有缺口，两边已经一致")
        sys.exit(0)

    if DRY_RUN:
        print("[dry-run] 只比对不上传，结束")
        sys.exit(0)

    if not os.path.exists(COROS_FIT_DIR):
        os.makedirs(COROS_FIT_DIR, exist_ok=True)

    ok_count = 0
    fail_count = 0
    for ts, label_id, sport_type, activity in todo[:MAX_UPLOAD]:
        try:
            response = coros_client.downloadActivitie(label_id, sport_type)
            fit_bytes, fit_name = extract_fit(response.data)
            fit_path = os.path.join(COROS_FIT_DIR, "%s-%s" % (label_id, fit_name))
            with open(fit_path, "wb") as fh:
                fh.write(fit_bytes)
        except Exception as err:
            print("[下载失败] %s：%s" % (label_id, err))
            fail_count += 1
            continue

        status = garmin_client.upload_activity(fit_path)
        if status in ("SUCCESS", "DUPLICATE_ACTIVITY"):
            ok_count += 1
            print("[上传成功] %s %s → %s" % (label_id, fit_name, status))
        else:
            fail_count += 1
            print("[上传失败] %s %s → %s" % (label_id, fit_name, status))
        time.sleep(1.5)

    skipped = max(0, len(todo) - MAX_UPLOAD)
    if skipped:
        print("[提示] 本次上限 %d 条，还有 %d 条留到下次运行" % (MAX_UPLOAD, skipped))
    print("[结果] 成功 %d 条，失败 %d 条" % (ok_count, fail_count))
    if fail_count:
        ## 非 0 退出码 → GitHub Actions 显示红叉，不再假绿
        sys.exit(1)


if __name__ == "__main__":
    main()
