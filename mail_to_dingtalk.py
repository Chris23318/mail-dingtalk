"""
邮件提醒钉钉推送脚本（GitHub Actions 云端版）
功能：读取腾讯企业邮箱当天收到的邮件，原文+中文翻译推送到钉钉群
配置通过环境变量注入（GitHub Secrets），不在代码中硬编码密码
"""

import imaplib
import email
import sys
import os
import json
import hashlib
import random
import urllib.request
import urllib.error
import urllib.parse
import datetime
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime

# ============================================================
# 配置：从环境变量读取（在 GitHub Secrets 中设置）
# ============================================================
IMAP_HOST      = os.environ.get("IMAP_HOST", "imap.exmail.qq.com")
IMAP_PORT      = int(os.environ.get("IMAP_PORT", "993"))
EMAIL_USER     = os.environ.get("EMAIL_USER", "")
EMAIL_PASS     = os.environ.get("EMAIL_PASS", "")
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")
BAIDU_APPID    = os.environ.get("BAIDU_APPID", "")
BAIDU_KEY      = os.environ.get("BAIDU_KEY", "")
MAX_BODY_LENGTH  = int(os.environ.get("MAX_BODY_LENGTH", "800"))
ENABLE_TRANSLATE = os.environ.get("ENABLE_TRANSLATE", "true").lower() == "true"
# ============================================================


def is_mostly_chinese(text: str) -> bool:
    """判断文本是否90%以上是中文（防止漏掉中英混合内容）"""
    if not text:
        return True
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    return chinese_chars / len(text) > 0.9


def translate_to_chinese(text: str):
    """使用百度翻译 API 翻译成中文，失败时返回 None"""
    if not text.strip():
        print("[D] translate_to_chinese: 空文本，跳过")
        return None
    # 仅当文本 90% 以上是中文时才跳过，避免漏掉中英混合内容
    if is_mostly_chinese(text):
        print("[D] translate_to_chinese: 已是中文，跳过")
        return None
    if not BAIDU_APPID or not BAIDU_KEY:
        print("[!] 百度翻译 API 未配置（APPID/KEY 为空），跳过")
        return None

    print(f"[D] 百度翻译: APPID={BAIDU_APPID[:6]}***, KEY={BAIDU_KEY[:4]}***")

    API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

    def _call_api(q: str):
        salt = str(random.randint(10000, 99999))
        sign_str = BAIDU_APPID + q + salt + BAIDU_KEY
        sign = hashlib.md5(sign_str.encode()).hexdigest()
        # 使用 POST 方式，避免 GET URL 过长导致截断或编码问题
        post_data = urllib.parse.urlencode({
            "q": q, "from": "auto", "to": "zh",
            "appid": BAIDU_APPID, "salt": salt, "sign": sign,
        }).encode("utf-8")
        print(f"[D] 请求百度翻译 body 长度={len(post_data)}")
        try:
            req = urllib.request.Request(API_URL, data=post_data)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
                print(f"[D] 百度翻译响应: {raw[:200]}")
                data = json.loads(raw)
            if "trans_result" in data:
                result = "".join(d["dst"] for d in data["trans_result"])
                print(f"[D] 翻译成功: {result[:80]}")
                return result if result.strip() else None
            error_code = data.get("error_code", "unknown")
            error_msg  = data.get("error_msg", "unknown")
            print(f"[!] 百度翻译错误码: {error_code} -- {error_msg}")
            return None
        except urllib.error.HTTPError as he:
            body = ""
            try:
                body = he.read().decode()
            except Exception:
                pass
            print(f"[!] 百度翻译 HTTP {he.code}: {he.reason} | body: {body[:200]}")
            return None
        except Exception as exc:
            print(f"[!] 百度翻译请求失败: {type(exc).__name__}: {exc}")
            return None

    if len(text) <= 2000:
        return _call_api(text)

    # 超长文本分段翻译
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 2000:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    parts = []
    for chunk in chunks:
        r = _call_api(chunk)
        parts.append(r if r else chunk)
    return "".join(parts)


def decode_str(s):
    if s is None:
        return ""
    decoded_parts = decode_header(s)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except Exception:
                result.append(part.decode("gbk", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset, errors="replace")
                    except Exception:
                        body = payload.decode("gbk", errors="replace")
                    break
            elif ctype == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html = payload.decode(charset, errors="replace")
                    except Exception:
                        html = payload.decode("gbk", errors="replace")
                    body = re.sub(r'<[^>]+>', '', html)
                    body = re.sub(r'&nbsp;', ' ', body)
                    body = re.sub(r'&[a-z]+;', '', body)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except Exception:
                body = payload.decode("gbk", errors="replace")
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return "\n".join(lines)


def fetch_today_emails():
    today = datetime.date.today()
    date_str = today.strftime("%d-%b-%Y")
    print(f"[*] 连接邮箱 {IMAP_HOST}...")
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("INBOX")
    status, data = mail.search(None, f'SINCE "{date_str}"')
    if status != "OK":
        mail.logout()
        return []
    mail_ids = data[0].split()
    print(f"[*] 今天共找到 {len(mail_ids)} 封邮件")
    emails = []
    for mid in mail_ids:
        status, msg_data = mail.fetch(mid, "(RFC822)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        subject  = decode_str(msg.get("Subject", "(无主题)"))
        from_addr = decode_str(msg.get("From", "(未知发件人)"))
        try:
            time_str = parsedate_to_datetime(msg.get("Date", "")).strftime("%H:%M")
        except Exception:
            time_str = "未知时间"
        body = get_body(msg)
        if len(body) > MAX_BODY_LENGTH:
            body = body[:MAX_BODY_LENGTH] + "...[正文已截断]"

        subject_cn = None
        body_cn    = None
        if ENABLE_TRANSLATE:
            print(f"[*] 翻译第 {len(emails)+1} 封邮件: 主题={subject[:40]}")
            subject_cn = translate_to_chinese(subject)
            if subject_cn:
                print(f"[D] 主题翻译: {subject_cn[:50]}")
            body_cn = translate_to_chinese(body)
            if body_cn:
                print(f"[D] 正文翻译: {body_cn[:50]}")

        emails.append({
            "subject": subject, "subject_cn": subject_cn,
            "from": from_addr, "time": time_str,
            "body": body, "body_cn": body_cn,
        })
    mail.logout()
    return emails


def send_to_dingtalk(text: str):
    payload = {"msgtype": "text", "text": {"content": text}}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        DINGTALK_WEBHOOK, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("errcode") == 0:
                print("[OK] 钉钉消息发送成功")
            else:
                print(f"[!] 钉钉返回错误: {result}")
                sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[!] 发送失败: {e}")
        sys.exit(1)


def build_message(emails):
    today  = datetime.date.today().strftime("%Y年%m月%d日")
    now_h  = datetime.datetime.now().hour
    period = "上午" if now_h < 13 else "下午"
    tag    = "[已翻译]" if ENABLE_TRANSLATE else ""

    if not emails:
        return f"【提醒】{today} {period}邮件汇总\n\n今日暂无新邮件"

    lines = [f"【提醒】{today} {period}邮件汇总（共 {len(emails)} 封）{tag}\n"]
    for i, m in enumerate(emails, 1):
        lines.append("=" * 30)
        lines.append(f"[{i}] {m['time']}")
        lines.append(f"发件人：{m['from']}")
        if m.get("subject_cn"):
            lines.append(f"主  题：{m['subject']}")
            lines.append(f"   译：{m['subject_cn']}")
        else:
            lines.append(f"主  题：{m['subject']}")
        if m.get("body_cn"):
            lines.append(f"原  文：\n{m['body']}")
            lines.append("--- 中文翻译 ---")
            lines.append(m['body_cn'])
        else:
            lines.append(f"正  文：\n{m['body']}")
    lines.append("=" * 30)
    return "\n".join(lines)


def split_and_send(text: str, chunk_size: int = 4000):
    if len(text.encode("utf-8")) <= chunk_size:
        send_to_dingtalk(text)
        return
    parts  = text.split("=" * 30)
    header = parts[0]
    buffer = header
    for part in parts[1:]:
        candidate = buffer + "=" * 30 + part
        if len(candidate.encode("utf-8")) > chunk_size:
            send_to_dingtalk(buffer)
            buffer = "【提醒】（续）\n" + "=" * 30 + part
        else:
            buffer = candidate
    if buffer.strip():
        send_to_dingtalk(buffer)


def main():
    if not EMAIL_USER or not EMAIL_PASS or not DINGTALK_WEBHOOK:
        print("[!] 缺少必要环境变量：EMAIL_USER / EMAIL_PASS / DINGTALK_WEBHOOK")
        sys.exit(1)

    print(f"[*] 开始执行：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[*] 邮箱：{EMAIL_USER}  翻译：{'开' if ENABLE_TRANSLATE else '关'}")
    try:
        emails = fetch_today_emails()
    except Exception as e:
        error_msg = f"【提醒】邮件读取失败：{e}"
        print(f"[!] 错误: {e}")
        send_to_dingtalk(error_msg)
        sys.exit(1)

    message = build_message(emails)
    print(message[:300])
    split_and_send(message)


if __name__ == "__main__":
    main()
