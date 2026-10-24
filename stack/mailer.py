#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简发信：QQ 掉线时用它通知主人。

为什么单独做一个：QQ 掉线时出站通道也断了，QQ 消息发不出去；而 QQ 号自带
QQ 邮箱，是唯一还通的渠道。

配置放 /opt/dsh/mail.env（600），键名：
    SMTP_HOST=smtp.qq.com
    SMTP_PORT=465
    SMTP_USER=3600398389@qq.com      # 发信邮箱（机器人自己的 QQ 邮箱）
    SMTP_PASS=xxxxxxxxxxxxxxxx       # QQ 邮箱「授权码」，不是 QQ 密码
    MAIL_TO=1709392682@qq.com        # 收件人（主人的 QQ 邮箱）

没配置时 send() 直接返回 (False, 原因)，不抛异常——看护脚本不能因为没邮箱就挂。
"""
import os
import smtplib
import ssl
import time
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ENVF = "/opt/dsh/mail.env"


def _conf():
    d = {}
    try:
        with open(ENVF, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return d


def configured():
    c = _conf()
    return all(c.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))


def send(subject, body, attachments=None, timeout=25):
    """返回 (成功?, 说明)。attachments 是本地文件路径列表。"""
    c = _conf()
    if not configured():
        return False, "邮件未配置（/opt/dsh/mail.env 缺 SMTP_HOST/SMTP_USER/SMTP_PASS/MAIL_TO）"
    host = c["SMTP_HOST"]
    try:
        port = int(c.get("SMTP_PORT") or 465)
    except Exception:
        port = 465

    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = c["SMTP_USER"]
    msg["To"] = c["MAIL_TO"]
    msg["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S +0800")
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for p in (attachments or []):
        try:
            with open(p, "rb") as f:
                part = MIMEApplication(f.read())
            part.add_header("Content-Disposition", "attachment",
                            filename=("utf-8", "", os.path.basename(p)))
            msg.attach(part)
        except Exception:
            continue          # 附件读不到就只发正文

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx) as s:
                s.login(c["SMTP_USER"], c["SMTP_PASS"])
                s.sendmail(c["SMTP_USER"], [c["MAIL_TO"]], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.starttls(context=ctx)
                s.login(c["SMTP_USER"], c["SMTP_PASS"])
                s.sendmail(c["SMTP_USER"], [c["MAIL_TO"]], msg.as_string())
        return True, "已发送到 %s" % c["MAIL_TO"]
    except Exception as e:
        return False, "发送失败: %s" % str(e)[:160]


if __name__ == "__main__":
    import sys
    if not configured():
        print("未配置：" + _conf().__str__()[:0] + "缺 mail.env 里的必要项")
        sys.exit(1)
    ok, msg = send("【测试】鲸鲸发信测试", "如果你看到这封邮件，说明掉线告警通道打通了。")
    print(("成功: " if ok else "失败: ") + msg)
    sys.exit(0 if ok else 1)
