"""
佳明 → 高驰 同步（直传 FIT 版）

背景（2026-10-03）：
    高驰关闭了取 OSS 临时凭证的「免鉴权」通道，老链路
    「佳明下载 FIT → 传阿里云 OSS → 通知高驰去 OSS 拉取导入」整体失效。
    而且旧代码在出错时 `except: exit()`（退出码 0），GitHub Actions 会显示绿勾，
    属于「假成功」。

现在的做法：
    直接 POST https://teamcnapi.coros.com/activity/fit/import，
    用 multipart 把 FIT 文件本体递上去，完全绕开 OSS / STS。

去重：
    不再依赖仓库里的 db/*.db（GitHub runner 每次都是干净环境、db 也不会被提交），
    改成「先拉高驰已有活动的开始时间，佳明里有、高驰里没有的才传」。
    两边都用 UTC 时间戳比对，允许 ±120 秒误差。
"""

import io
import os
import sys
import time
import zipfile
import datetime as dt

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))  # 当前目录
config_path = os.path.dirname(CURRENT_DIR)  # 上一级（scripts/），用 dirname 兼容 Windows
sys.path.append(config_path)

from garmin.garmin_client import GarminClient
from coros.coros_client import CorosClient


SYNC_CONFIG = {
    'GARMIN_AUTH_DOMAIN': '',
    'GARMIN_EMAIL': '',
    'GARMIN_PASSWORD': '',
    'GARMIN_NEWEST_NUM': 0,
    'SYNC_DAYS': 30,          ## 只同步最近 N 天内的活动；改成 0 表示不限
    "COROS_EMAIL": '',
    "COROS_PASSWORD": '',
}

## 允许的时间误差（秒），避免两边记录存在秒级偏差时被当成两条
MATCH_TOLERANCE = 120


def load_config():
    """优先读 GitHub Actions 的环境变量（来自 Secrets）。"""
    for k in SYNC_CONFIG:
        if os.getenv(k):
            SYNC_CONFIG[k] = os.getenv(k)
    ## 佳明国区账号必须走 garmin.cn，没配就按国区来（本仓库自用）
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


def coros_start_times(coros_client):
    """返回高驰端已有活动的开始时刻（UTC 秒）有序列表。"""
    activities = coros_client.getAllActivities() or []
    times = sorted(int(a["startTime"]) for a in activities if a.get("startTime"))
    return times


def already_on_coros(ts, coros_times):
    import bisect
    idx = bisect.bisect_left(coros_times, ts - MATCH_TOLERANCE)
    return idx < len(coros_times) and abs(coros_times[idx] - ts) <= MATCH_TOLERANCE


def extract_fit(raw):
    """佳明下载回来的可能是 .zip（内含 .fit），也可能是裸 .fit。"""
    buf = io.BytesIO(raw)
    if raw[:2] == b"PK" or zipfile.is_zipfile(buf):
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fits:
                raise ValueError("zip 内没有 .fit 文件")
            return zf.read(fits[0]), os.path.basename(fits[0])
    return raw, "activity.fit"


def describe(activity):
    sport = (activity.get("activityType") or {}).get("typeKey")
    distance = (activity.get("distance") or 0) / 1000.0
    return "%s %.2f km" % (sport, distance)


def main():
    load_config()

    garmin_client = GarminClient(
        SYNC_CONFIG["GARMIN_EMAIL"],
        SYNC_CONFIG["GARMIN_PASSWORD"],
        SYNC_CONFIG["GARMIN_AUTH_DOMAIN"],
        SYNC_CONFIG["GARMIN_NEWEST_NUM"] or 0,
    )

    coros_client = CorosClient(SYNC_CONFIG["COROS_EMAIL"], SYNC_CONFIG["COROS_PASSWORD"])
    coros_client.login()
    print("[高驰] 登录成功 userId=%s regionId=%s" % (coros_client.userId, coros_client.regionId))

    coros_times = coros_start_times(coros_client)
    print("[高驰] 已有活动 %d 条（用于去重）" % len(coros_times))

    all_activities = garmin_client.getAllActivities() or []
    print("[佳明] 取到活动 %d 条" % len(all_activities))
    if not all_activities:
        print("[结果] 佳明侧没有取到活动，结束")
        sys.exit(0)

    ## GARMIN_NEWEST_NUM > 0 时只处理最新 N 条（佳明接口按时间倒序返回）
    newest_num = int(SYNC_CONFIG["GARMIN_NEWEST_NUM"] or 0)
    if newest_num > 0:
        all_activities = all_activities[:newest_num]
        print("[佳明] 按 GARMIN_NEWEST_NUM=%d 截取最新 %d 条" % (newest_num, len(all_activities)))

    ## 时间窗口：默认只看最近 30 天，避免把很久以前的杂项记录一次性灌进高驰
    sync_days = int(SYNC_CONFIG["SYNC_DAYS"] or 0)
    deadline = int(time.time()) - sync_days * 86400 if sync_days > 0 else None
    if deadline:
        print("[范围] 只同步最近 %d 天内的活动（SYNC_DAYS=%d，改成 0 表示不限）" % (sync_days, sync_days))
    else:
        print("[范围] 不限时间，全部历史活动都参与比对")

    todo = []
    for activity in all_activities:
        activity_id = activity.get("activityId")
        ts = garmin_epoch(activity)
        if not activity_id or ts is None:
            continue
        if deadline and ts < deadline:
            continue
        if already_on_coros(ts, coros_times):
            continue
        todo.append((ts, activity_id, activity))

    todo.sort(key=lambda item: item[0])
    print("[对比] 佳明有、高驰没有：%d 条" % len(todo))
    beijing = dt.timezone(dt.timedelta(hours=8))
    for ts, activity_id, activity in todo:
        print("   - %s  %s  %s" % (
            activity_id,
            dt.datetime.fromtimestamp(ts, beijing).strftime("%Y-%m-%d %H:%M:%S"),
            describe(activity)))

    if not todo:
        print("[结果] 没有缺口，两边已经一致")
        sys.exit(0)

    ok_count = 0
    fail_count = 0
    for ts, activity_id, activity in todo:
        try:
            raw = garmin_client.downloadFitActivity(activity_id)
            fit_bytes, fit_name = extract_fit(raw)
        except Exception as err:
            print("[下载失败] %s：%s" % (activity_id, err))
            fail_count += 1
            continue

        ok, message = coros_client.importFit(fit_bytes, fit_name)
        if ok:
            ok_count += 1
            print("[上传成功] %s %s → %s" % (activity_id, fit_name, message))
        else:
            fail_count += 1
            print("[上传失败] %s %s → %s" % (activity_id, fit_name, message))
        time.sleep(1.5)

    print("[结果] 成功 %d 条，失败 %d 条" % (ok_count, fail_count))
    if fail_count:
        ## 非 0 退出码 → GitHub Actions 显示红叉，不再假绿
        sys.exit(1)


if __name__ == "__main__":
    main()
