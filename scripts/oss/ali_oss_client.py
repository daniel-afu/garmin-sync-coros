import urllib3
import json
import os
import hashlib
import certifi
import oss2

from oss2 import SizedFileAdapter, determine_part_size
from oss2.models import PartInfo
from utils.coros_oss_credients_utils import decode

# ============================================================================
# 2026-10-03 起，高驰关闭了「免鉴权」的 STS 通道：
#   旧地址 https://faq.coros.com/openapi/oss/sts  现在直接返回 404（HTML），
#   导致 json.loads(response.data) 抛 "Expecting value: line 1 column 1 (char 0)"，
#   而 garmin_sync_coros.py 的 except 分支会把它吞掉并 exit()（退出码 0）→ Actions 假绿。
#
# 新的取凭证通道（按优先级回退，任一命中即用）：
#   1) 训练中心 BFF 代理：https://trainingcn.coros.com/api/proxy/oss/sts
#      鉴权：Cookie: CPL-coros-token=<accessToken>（网页版同款）
#   2) 官方 v2 接口：https://faq.coros.com/openapi/v2/oss/sts
#      鉴权：请求头 accesstoken: <accessToken>
#   3) 旧地址（已失效，仅作兜底）
#
# accessToken 优先取调用方传入的值；若没有传入，就用环境变量 COROS_EMAIL /
# COROS_PASSWORD 现场登录一次高驰账号拿 token。
# 也就是说：只需替换本文件，garmin_sync_coros.py 不需要改。
# ============================================================================


class AliOssClient:
    def __init__(self, access_token=None, bucket="coros-oss", service="aliyun",
                 app_id="1660188068672619112", sign="9AD4AA35AAFEE6BB1E847A76848D58DF", v=2):
        self.access_token = access_token
        self.bucket = bucket
        self.service = service
        self.app_id = app_id
        self.sign = sign
        self.security_token = None
        self.access_key_id = None
        self.access_key_secret = None
        self.req = urllib3.PoolManager(cert_reqs='CERT_REQUIRED', ca_certs=certifi.where())
        self.client = None
        self.v = v
        self.initClient()

    def _getAccessToken(self):
        if self.access_token:
            return self.access_token
        ## 回退：用环境变量里的高驰账号现场登录
        email = os.getenv("COROS_EMAIL")
        password = os.getenv("COROS_PASSWORD")
        if not email or not password:
            print("[AliOssClient] 未拿到 accessToken：调用方未传入，且环境变量 COROS_EMAIL/COROS_PASSWORD 为空")
            return None
        try:
            login_data = {
                "account": email,
                "pwd": hashlib.md5(password.encode()).hexdigest(),
                "accountType": 2,
            }
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.39 Safari/537.36",
                "referer": "https://teamcnapi.coros.com/",
                "origin": "https://teamcnapi.coros.com/",
            }
            response = self.req.request('POST', "https://teamcnapi.coros.com/account/login",
                                        body=json.dumps(login_data), headers=headers)
            login_response = json.loads(response.data)
            if login_response.get("result") != "0000":
                print("[AliOssClient] 高驰登录异常：%s" % (login_response.get("message"),))
                return None
            token = login_response["data"]["accessToken"]
            print("[AliOssClient] 已用 COROS_EMAIL 现场登录获取 accessToken")
            return token
        except Exception as err:
            print("[AliOssClient] 高驰登录失败：%s" % (err,))
            return None

    def fetchStsCredentials(self):
        token = self._getAccessToken()

        candidates = []
        if token:
            candidates.append((
                "web-proxy",
                "https://trainingcn.coros.com/api/proxy/oss/sts?bucket=%s&service=%s&v=2" % (self.bucket, self.service),
                {"Cookie": "CPL-coros-token=%s" % token},
            ))
            candidates.append((
                "v2",
                "https://faq.coros.com/openapi/v2/oss/sts?bucket=%s&service=%s&v=2" % (self.bucket, self.service),
                {"accesstoken": token},
            ))
        ## 旧通道（已失效，仅作兜底）
        candidates.append((
            "legacy",
            "https://faq.coros.com/openapi/oss/sts?bucket=%s&service=%s&app_id=%s&sign=%s&v=%s"
            % (self.bucket, self.service, self.app_id, self.sign, self.v),
            {},
        ))

        last_error = None
        for channel, url, headers in candidates:
            try:
                response = self.req.request('GET', url, headers=headers)
                body = json.loads(response.data)
                credentials = body.get("data", {}).get("credentials")
                if response.status == 200 and credentials:
                    print("[AliOssClient] STS 凭证获取成功，通道=%s" % (channel,))
                    return credentials
                last_error = "通道=%s HTTP=%s body=%s" % (channel, response.status, str(body)[:200])
            except Exception as err:
                last_error = "通道=%s 异常=%s" % (channel, err)
        raise StsTokenError("获取阿里云OSS STS Token异常: %s" % (last_error,))

    def initClient(self):
        credentials = self.fetchStsCredentials()
        credients_json = decode(credentials)

        SecurityToken = credients_json["SecurityToken"]
        AccessKeyId = credients_json["AccessKeyId"]
        AccessKeySecret = credients_json["AccessKeySecret"]
        self.security_token = SecurityToken
        self.access_key_id = AccessKeyId
        self.access_key_secret = AccessKeySecret

        auth = oss2.StsAuth(self.access_key_id, self.access_key_secret, self.security_token)
        self.client = oss2.Bucket(auth, "https://oss-cn-beijing.aliyuncs.com", self.bucket)

    def multipart_upload(self, filePath, fileName):
        key = f"fit_zip/{fileName}"
        print(key)
        init_multipart_upload_result = self.client.init_multipart_upload(key)
        if init_multipart_upload_result.status != 200:
            raise AliOssError("初始化阿里云分片上传异常")
        upload_id = init_multipart_upload_result.upload_id
        total_size = os.path.getsize(filePath)
        # determine_part_size方法用于确定分片大小。
        part_size = determine_part_size(total_size, preferred_size=1024 * 1024)
        parts = []

        # 逐个上传分片。
        with open(filePath, 'rb') as fileobj:
            part_number = 1
            offset = 0
            while offset < total_size:
                num_to_upload = min(part_size, total_size - offset)
                # 调用SizedFileAdapter(fileobj, size)方法会生成一个新的文件对象，重新计算起始追加位置。
                result = self.client.upload_part(key, upload_id, part_number,
                                                 SizedFileAdapter(fileobj, num_to_upload))
                parts.append(PartInfo(part_number, result.etag))

                offset += num_to_upload
                part_number += 1

        # 完成分片上传。
        # 如需在完成分片上传时设置相关Headers，请参考如下示例代码。
        headers = dict()
        # 设置文件访问权限ACL。此处设置为OBJECT_ACL_PRIVATE，表示私有权限。
        # headers["x-oss-object-acl"] = oss2.OBJECT_ACL_PRIVATE
        r = self.client.complete_multipart_upload(key, upload_id, parts, headers=headers)
        return key


class StsTokenError(Exception):

    def __init__(self, status):
        """Initialize."""
        super(StsTokenError, self).__init__(status)
        self.status = status


class AliOssError(Exception):
    def __init__(self, status):
        """Initialize."""
        super(AliOssError, self).__init__(status)
        self.status = status
