## 同步原理（2026-10-05 起已改为「直传 FIT」）

高驰在 2026-10-03 关闭了取 OSS 临时凭证的免鉴权通道，老的
「佳明下载 FIT → 上传阿里云 OSS → 通知高驰去 OSS 拉取导入」链路整体失效。
现在改成把 FIT 文件直接 POST 给 `https://teamcnapi.coros.com/activity/fit/import`，
完全绕开 OSS / STS。

同时修掉了两个坑：

- 旧代码在出错时 `except: exit()`（退出码 0），GitHub Actions 会显示绿勾，属于「假成功」。
  现在有失败会以非 0 退出码结束，绿勾就是真的绿。
- 不再依赖仓库里的 `db/*.db` 去重（Actions 每次都是干净环境，db 也不会被提交）。
  改为拉取高驰已有活动的开始时间做比对，佳明里有、高驰里没有的才上传。

`SYNC_DAYS` 控制只同步最近多少天内的活动（默认 30，填 `0` 表示不限）。

## 致谢
- 本脚本佳明模块代码来自@[yihong0618](https://github.com/yihong0618) 的 [running_page](https://github.com/yihong0618/running_page) 个人跑步主页项目,在此非常感谢@[yihong0618](https://github.com/yihong0618)大佬的无私奉献！！！

## DeepWiki源码解析
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/XiaoSiHwang/garmin-sync-coros)

## 注意
由于高驰平台只允许单设备登录，同步期间如果打开网页会影响到数据同步导致同步失败，同步期间切记不要打开网页。

## Local activity backups

`sync-coros-garmin.ps1` can download activities from one account without
uploading anything to the other service. Choose the source account explicitly:

```powershell
# Set only the credentials for the selected source.
$env:GARMIN_EMAIL = "you@example.com"
$env:GARMIN_PASSWORD = "your-password"
$env:GARMIN_AUTH_DOMAIN = "COM" # Optional; use CN for China.
.\sync-coros-garmin.ps1 -Mode backup -Source garmin

$env:COROS_EMAIL = "you@example.com"
$env:COROS_PASSWORD = "your-password"
.\sync-coros-garmin.ps1 -Mode backup -Source coros
```

By default, files are stored in `backups\garmin` or `backups\coros` and are
named `YYYYMMDDTHHMMSS_<activity-id>.<extension>` using the activity start
time. Existing activity files are skipped, so rerunning the command downloads
only files not already present. Use `-Overwrite` to download every activity again, or
`-OutputDirectory <path>` to use another backup location. The `backups`
directory is ignored by Git; keep it on durable local or cloud storage.

Running `.\sync-coros-garmin.ps1` with no arguments preserves its original
COROS to Garmin sync behavior.

## 参数配置
|       参数名       |                备注                |        案例        |
| :----------------: | :--------------------------------: | :----------------: |
|    GARMIN_EMAIL    |          佳明登录帐号邮箱          |                    |
|  GARMIN_PASSWORD   |            佳明登录密码            |                    |
| GARMIN_AUTH_DOMAIN | 佳明区域（国际区填:COM 国区填:CN） |    (COM or CN)     |
| GARMIN_NEWEST_NUM  |            最新记录条数            | (默认0，可写大于0) |
|     SYNC_DAYS      | 只同步最近多少天内的活动(0=不限)   |     (默认30)       |
|    COROS_EMAIL     |           高驰 登录邮箱            |                    |
|   COROS_PASSWORD   |             高驰 密码              |                    |

## Github配置步骤
### 1.参数配置
打开**Setting**
![打开Setting](doc/3451692931372_.pic.jpg)
找到**Secrets and variables**点击**New repository secret**按钮
![Secrets and variables](/doc/3461692931472_.pic.jpg)
打开**New repository secret**后将上述的参数填入，下图以佳明帐号为例,**Name**填写参数名,**Secret**填写你的信息，重复以上步骤填入五个参数即可
![填入参数](doc/3471692931624_.pic.jpg)

### 2.配置WorkFlow权限
打开**Setting**找到**Actions**点击**General**按钮,按照下图勾选并save
![配置WorkFlow权限](doc/3481692931856_.pic.jpg)

### 3. wrokflow配置
打开**github/workflows/garmin-sync-coros.yml**文件,将**GITHUB_NAME**更改为你的Github用户名、**GITHUB_EMAIL**更改为你的Github登录邮箱，更改步骤如下:
![更改步骤](doc/3491692932110_.pic.jpg)
更改完成后点击右上角**Commit changes...**提交即可
![Commit](doc/3501692932345_.pic.jpg)

## 重新fork项目步骤
点击页面上**Sync Frok**然后点击**Dicard commit**即可
![fork sync](doc/image.png)
## 删除db步骤
按照图片顺序执行即可
![alt text](doc/image5.png)
![alt text](doc/image-1.png)
![alt text](doc/image-2.png)
![alt text](doc/image-3.png)
![alt text](doc/image-4.png)
删除完后等脚本自己执行即可
