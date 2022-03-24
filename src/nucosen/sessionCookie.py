from dataclasses import dataclass
from logging import getLogger
from typing import Optional

from pyotp import TOTP
from requests import Response, get, post
from requests.cookies import RequestsCookieJar
from requests.exceptions import ConnectionError, HTTPError
from retry import retry


class ReLoginRequested(Exception):
    pass


NetworkErrors = (ConnectionError, HTTPError, ReLoginRequested)


@dataclass
class Session(object):
    mail_tel: str
    password: str
    mfa_token: str

    user_agent: str = "NUCOSen Automatic Login"
    cookie: Optional[RequestsCookieJar] = None

    @retry(NetworkErrors, delay=1, backoff=2, logger=getLogger(__name__))
    def login(self):
        header = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        resp = post(
            "https://account.nicovideo.jp/login/redirector",
            {
                "mail_tel": self.mail_tel,
                "password": self.password
            },
            headers=header,
            allow_redirects=False
        )
        resp.raise_for_status()
        if "user_session" in resp.cookies:
            self.cookie = resp.cookies
            getLogger(__name__).info("通常ログイン成功")
            return
        if "mfa_session" in resp.cookies:
            self.__mfa_login(resp, header)
            getLogger(__name__).info("MFA成功")
            return
        raise ReLoginRequested("ログイン失敗")

    def __mfa_login(self, resp: Response, header):
        tfac = TOTP(self.mfa_token)
        mfaResp = post(
            resp.headers["Location"],
            {
                "otp": tfac.now(),
                "is_mfa_trusted_device": "false",
            },
            headers=header,
            cookies=resp.cookies,
            allow_redirects=False,
        )
        mfaResp.raise_for_status()
        resp = get(
            mfaResp.headers["Location"],
            headers={"User-Agent": self.user_agent},
            cookies=resp.cookies,
            allow_redirects=False
        )
        resp.raise_for_status()
        if "user_session" in resp.cookies:
            self.cookie = resp.cookies
            return
        raise ReLoginRequested("MFA失敗")

    def getSessionString(self) -> Optional[str]:
        # NOTE - X-niconico-sessionなどに使用
        if self.cookie is None:
            return
        if not "user_session" in self.cookie:
            return
        return self.cookie["user_session"]
